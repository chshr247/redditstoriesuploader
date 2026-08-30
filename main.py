"""Pipeline: Reddit story -> vertical mp4 with a narrated title card.

    python main.py        one video
    python main.py 5      five

A long post can become two or three videos instead of one, and that is a TikTok
arrangement only - YouTube is never offered a part, it always gets whole
stories. The whole split is written in a single LLM call and parked in seen.db;
one part is rendered per run, in the run that publishes it, because on a CI
runner out/ does not survive to the next one.

    python main.py --park    write the day's stories, render nothing
    python main.py --takes   read the settled ones aloud and ask which take

Nothing is rendered until a human has answered for it: a story is written and
parked on a GitHub issue (review.py), and a whole day's worth are parked at
once so the questions arrive together. Answering does not publish anything on
its own - the gap between sends and the daily allowance are unchanged, and one
video still goes out per run.

Which story is next is normally source.py's decision. A channel with a plan
file (config.PLAN_FILE, e.g. plan_ru.md) takes that decision away from it: the
stories go out in the order written there, one at a time, and the pool is not
touched until the plan runs out.
"""
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

import publish
import render
import review
import script
import source
import voice
from config import (CHANNEL, LOUD_AT, MIN_SEC, OUT_DIR, REVIEW_BATCH,
                    REVIEW_TAKES, STOP_REASON, STOPPED, TIKTOK_ENABLED,
                    TIKTOK_PER_DAY, chan_file, chan_key)

log = logging.getLogger("main")

MAX_SLOWDOWN = 0.15   # beyond this the voice starts sounding drugged


def _render(title: str, body: str, gender: str, key: str, sub: str,
            fish_voice: str = "", meta: dict | None = None,
            body_mp3=None) -> Path:
    """Narrate one script and burn it into out/<key>[_<channel>].mp4.

    `key` names the STORY; the channel is what turns it into a file name. Both
    channels render the same story from the same post id, so without that they
    would write over each other in out/ and count as one row in `uploaded`.
    """
    name = chan_file(key)
    # Pinned HERE and not inside speak_parts: an unpinned story lets that
    # function draw its own narrator, and the re-voice below is a second call -
    # so a story that lands under the floor comes back read by someone else, at
    # that voice's own pace instead of the -15% asked for. Measured 2026-08-18:
    # 173 wpm re-voiced to 124, a 29% drop from a 15% request.
    fish_voice = fish_voice or voice.pick_voice(gender, sub)
    mp3, words, title_end, title_words = voice.speak_parts(
        title, body, name, gender=gender, fish_voice=fish_voice,
        body_mp3=body_mp3)

    # Word counts only approximate duration - the voice paced 167-214 wpm across
    # runs. Cheaper to re-synthesize slower than to ask the model for more words.
    total = voice.duration(mp3)
    if total < MIN_SEC:
        slow = min((MIN_SEC + 2) / total - 1, MAX_SLOWDOWN)
        log.info("%.1fs is under the %ds floor, re-voicing at -%.0f%%",
                 total, MIN_SEC, slow * 100)
        # NOT body_mp3: the floor is missed by the audio being too short, and
        # the only way to lengthen it is to have the engine read it again. The
        # take the user picked is lost here, and saying so beats a video that
        # is quietly eight seconds under the minimum.
        if body_mp3:
            log.warning("the chosen take is %.1fs, under the %ds floor - "
                        "re-voicing, so it is not the take that ships",
                        total, MIN_SEC)
        mp3, words, title_end, title_words = voice.speak_parts(
            title, body, name, rate=f"-{slow * 100:.0f}%",
            speed=round(1 - slow, 2), gender=gender, fish_voice=fish_voice)
        total = voice.duration(mp3)
        if total < MIN_SEC:
            log.warning("still %.1fs after slowing down - story was too short", total)

    # keyed on the story, not the file: that is what keeps the two channels'
    # versions of one story off the same background clip.
    # The part number goes in for the card's "Часть N" line, and is read out of
    # the same meta that publish.py captions from - one source, so the card and
    # the caption can never disagree about which part this is.
    out = render.render(mp3, words, name, title=title, title_end=title_end,
                        key=key, title_words=title_words, sub=sub, gender=gender,
                        part=(meta or {}).get("part", 0)
                        if (meta or {}).get("total", 0) > 1 else 0)
    # publishing runs separately and later, so the text has to survive on disk.
    # The channel goes in with it - the part marker's language and which
    # channel's queue the file belongs to are both read back from here. So does
    # the measured length: youtube.py decides on the #Shorts tag by it, and by
    # then the mp4 is all it has.
    (out.with_suffix(".meta.json")).write_text(json.dumps(
        {"title": script.plain(title), "body": script.plain(body), "sub": sub,
         "channel": CHANNEL, "sec": round(total), **(meta or {})},
        ensure_ascii=False), "utf-8")
    return out


def write_and_park(post: dict, n: int = 1) -> None:
    """Write the story and hand its title to the user. Renders nothing.

    The post is marked used here rather than at the render, for the reason
    make_split() always marked it early: the text is safer in sqlite than the
    story is loose in the pool, where the next run would write it a second time
    while the first is still waiting for an answer.
    """
    gender, written, note = script.write_script(post, parts=n)
    if n > 1 and len(written) < 2:
        log.info("model returned one part, %s will publish as a single video",
                 post["id"])
    review.park(post, gender, written, note)
    source.mark_used(post["id"], post["score"], post["sub"])


def offer_takes(r: dict) -> None:
    """Read the approved story aloud N times and ask which reading goes out.

    Runs in the render slot rather than in review.yml: this is where the TTS
    key and the minutes already are, so the stage costs no new workflow. The
    story does not render on this tick - it renders on the one after the answer,
    or six hours later on the first take.
    """
    name = chan_file(r["post_id"])
    story, _ = script.split_cta(r["written"][0][1])
    # Drawn ONCE, here, and written onto the row by offer_takes(). pick_voice()
    # picks at random out of the channel's pool, and the take that wins is the
    # body of the video only - make_reviewed() synthesizes the title and the
    # closing question at the render, off the same row. Drawing there as well
    # is one video in two voices: latent while a pool holds one narrator, live
    # the moment it holds two, and the English pool already holds two.
    fish_voice = voice.pick_voice(r["gender"], r["sub"])
    mp3s = voice.takes(story, name, REVIEW_TAKES, fish_voice)
    review.offer_takes(r, mp3s, fish_voice)


def _chosen(r: dict) -> "Path | None":
    """The picked take, downloaded. None if the stage is off or it is missing.

    A take that cannot be fetched is not worth failing the render over - the
    story is approved either way, and a fresh reading of it is what every video
    got before this existed. So this warns and hands back None, which sends
    speak_parts() down its ordinary path.
    """
    if r.get("take", -1) < 0:
        return None
    dest = OUT_DIR / f"{chan_file(r['post_id'])}_chosen.mp3"
    try:
        urllib.request.urlretrieve(review.take_url(r), dest)
    except (urllib.error.URLError, OSError) as e:
        log.warning("could not fetch take %d (%s) - narrating it fresh",
                    r["take"] + 1, e)
        return None
    log.info("%s: take %d fetched, %.1f sec",
             r["post_id"], r["take"] + 1, voice.duration(dest))
    return dest


def make_reviewed(r: dict) -> Path:
    """Render the parked story under the title that came back off the issue.

    The chosen title replaces the model's on EVERY part. A split story has one
    title by construction - it is narrated and lit at the head of each part -
    and all of the parts were written against it in a single call.

    The narrator comes off the row where the readings put it, and is drawn here
    only for a story that was never read aloud - a split one, or a channel with
    the stage turned off. pick_voice() draws at random, and the take the user
    chose is the BODY of the video: the title and the closing question are
    synthesized below. Drawing again here reads them in a second voice, and the
    chosen take then sits in the middle of a video by somebody else.
    """
    post = {"id": r["post_id"], "sub": r["sub"], "score": r["score"]}
    fish_voice = r.get("voice") or voice.pick_voice(r["gender"], r["sub"])
    # Already final: review.py has folded the chosen title and any narration
    # rewritten by hand into it, and a rewrite may have merged three parts into
    # two - so the count comes from here and not from what the model wrote.
    written = r["written"]
    if len(written) > 1:
        source.queue_parts(post, written, r["gender"], fish_voice,
                           issue=r["issue"])
        out = make_part(source.next_part())
    else:
        # The score travels with the file: youtube.py decides by band and the
        # post is out of reach by then - seen.db keeps it per story, not per
        # rendered mp4. The issue travels the same way and for the same reason:
        # publish.py puts the caption back into the issue the title came from.
        out = _render(written[0][0], written[0][1], r["gender"], post["id"],
                      post["sub"], fish_voice=fish_voice, body_mp3=_chosen(r),
                      meta={"score": post["score"], "issue": r["issue"]})
        review.drop_takes(post["id"], REVIEW_TAKES)
    # Last, so a render that dies leaves the answer on the row for a retry.
    review.rendered(r["post_id"])
    return out


def make_part(p: dict) -> Path:
    """Render one already-written part of a split story."""
    key = f"{p['post_id']}_p{p['n']}"
    out = OUT_DIR / f"{chan_file(key)}.mp4"
    if out.exists():
        # only reachable when out/ outlived the failure, i.e. locally. Re-voicing
        # a part that is already on disk costs a TTS call for nothing.
        log.info("%s already rendered, reusing it", out.name)
        return out
    return _render(p["title"], p["body"], p["gender"], key, p["sub"],
                   fish_voice=p["voice"],
                   meta={"post_id": p["post_id"], "part": p["n"],
                         "total": p["total"], "issue": p["issue"]})


def _park_one(part: dict | None, may_split: bool) -> tuple[bool, int, int]:
    """Write ONE story and hand its title to the user. (parked, skipped, failed).

    Where the story comes from is unchanged and still in order: the plan file
    while a plan is running, then the day's hand-picked reserve, then the
    horror slot, then the pool. What changed is the caller - this used to be
    inline in main() and ran once a run; top_up() calls it until the day's
    batch of questions is full.
    """
    # A plan is an ORDER, so while one is running it is the only source: the
    # pool below cannot be consulted even as a fallback, because a story taken
    # from it publishes ahead of the next planned one and the order is gone.
    # A run with nothing to do is the cheaper failure - the plan is finite and
    # the pool is waiting at the end of it.
    if left := source.plan_left():
        log.info("plan: %d stories still to publish", left)
        if part:
            # Its remaining parts come first by definition, and this run has
            # already decided not to render one (publish.due() said no). A new
            # story here would land between two halves of the one on air.
            log.info("%s is mid-flight - nothing new until its parts are out",
                     part["post_id"])
            return False, 0, 0
        if not (p := source.next_planned()):
            log.error("plan: %d stories left but none could be read this run",
                      left)
            return False, 0, 0
        n = p["parts"]
        # multipart_today() is deliberately NOT consulted here. It exists to
        # stop the pool from splitting story after story; the plan already
        # spaces its multi-parters one to a day, and re-asking would only
        # stall the order whenever a run drifts across midnight.
        if n > 1 and (room := _room()) < n:
            log.warning("plan: %s is %d parts and only %d send(s) are left "
                        "today - starting it tomorrow rather than letting "
                        "it straddle the night", p["id"], n, room)
            return False, 0, 0
        log.info("plan: r/%s [%d] %d part(s): %s", p["sub"], p["score"], n,
                 p["title"][:60])
        try:
            write_and_park(p, n)
            return True, 0, 0
        except script.Unsuitable as e:
            # burned, exactly as in the pool below: the plan moves on to the
            # next story rather than offering this one again for ever
            source.mark_used(p["id"], p["score"], p["sub"])
            log.info("skipped: %s", e)
            return False, 1, 0
        except Exception:
            log.exception("failed on %s", p["id"])
            return False, 0, 1

    # One band, one call. The loud story used to be read first out of a
    # band of its own, on a slot of one a day; a year of three subs held
    # twelve posts above that floor, so the slot was promising daily what
    # the subs produce monthly. Loudness is a term in contested() now, and
    # the loud story competes for the top of one list like everything else.
    # The day's reserved slot comes first: one hand-picked story a day (see
    # config.DAILY_FILE), and when it has had its slot - or the list is spent
    # - next_daily() returns None and the pool fills this slot as before.
    # most posts get rejected as unsuitable, so pull a pool rather than one
    if daily := source.next_daily():
        log.info("daily reserve: %s", daily["id"])
        posts = [daily]
    # ...and after it the horror slot, which is the same bargain again: one
    # story a day off a pool of its own, and the rest of the day untouched.
    # Second rather than first because the reserve is hand-picked and finite,
    # while this one draws from subs that keep producing.
    elif horror := source.next_horror():
        log.info("horror slot: r/%s %s and %d more to fall back on",
                 horror[0]["sub"], horror[0]["id"], len(horror) - 1)
        posts = horror
    else:
        posts = source.fetch(4)
    if not posts:
        log.error("no fresh stories - lower MIN_SCORE or add subreddits")
        return False, 0, 0

    skipped = failed = 0
    for p in posts:
        n = max(1, min(script.part_count(p), _room())) if may_split else 1
        log.info("r/%s [%d%s] contested %.2f, %d parts: %s", p["sub"],
                 p["score"], " LOUD" if p["score"] >= LOUD_AT else "",
                 p.get("rank", 0), n, p["title"][:60])
        try:
            write_and_park(p, n)
            return True, skipped, failed
        except script.Unsuitable as e:
            # burn it so the same dud is not reconsidered next run
            source.mark_used(p["id"], p["score"], p["sub"])
            log.info("skipped: %s", e)
            skipped += 1
        except Exception:
            # one bad story must not kill the batch; it stays unmarked for a retry
            log.exception("failed on %s, moving on", p["id"])
            failed += 1
    return False, skipped, failed


def top_up(part: dict | None) -> tuple[int, int, int]:
    """Fill the day's batch of parked stories. (written, skipped, failed).

    The whole point of the batch: a day's questions arrive together and are
    answered in one sitting, instead of one landing minutes before each video
    was due and holding the pipeline until someone replied. Nothing downstream
    changes - one video a run, the gap between sends intact. REVIEW_BATCH
    bounds the batch in VIDEOS, so a split story is one question and two or
    three of them: four questions with a three-parter among them would be six
    sends against an allowance of four.

    A story turned down deletes its row on the spot, so the slot it held is
    free here and the replacement is written immediately - by the review
    workflow within the minute of the `-`, or by the next tick at the latest.

    _batch_room() is the ONLY thing that decides how many are written, and it
    answers exactly one question: how many videos this day still owes. A run
    that had also rendered used to be allowed one story and no more, on the
    grounds that writing is the slow half and a run does not have fifteen
    minutes of LLM left of its hour. What that bought was a day's questions
    arriving in ones and twos across the afternoon - 08:16, 11:41 and 12:48 on
    2026-08-26 - which is the batch not existing. The hour is not really the
    constraint either: the workflow's concurrency group does not cancel a run
    in progress, so a long morning delays the next tick rather than losing it,
    and there are thirty-two ticks in a day for four videos.
    """
    wrote = skipped = failed = 0
    # One story per video the day still owes, at most: parking one always takes
    # at least one video off _batch_room(), so this bound is never what ends the
    # loop - it is here so a room that somehow fails to shrink cannot spin.
    for _ in range(REVIEW_BATCH):
        if not _batch_room():
            break
        # One split story in flight at a time, and a parked batch counts:
        # source.multipart_today() reads the `parts` table, which is not
        # written until the RENDER, so on its own it would let every story in
        # a morning batch be sized for splitting.
        may_split = (not part and not source.multipart_today()
                     and not review.split_parked())
        ok, s, f = _park_one(part, may_split)
        skipped, failed = skipped + s, failed + f
        if not ok:
            break
        wrote += 1
    return wrote, skipped, failed


def _batch_room() -> int:
    """Videos the batch may still be asked for today.

    REVIEW_BATCH is the DAY's questions, so what has already gone out today
    comes off it. Without that, a channel that has sent two of its four and
    then emptied its batch would be asked for four more - two of which cannot
    go out until tomorrow, and by tomorrow morning the batch is written fresh
    anyway. It also makes the count self-clearing: the last send of the day
    takes the ceiling to zero, nothing is written overnight, and the first run
    after the midnight reset asks for the whole day at once.

    Four things come off it and the last two are the ones that are easy to
    miss. A story that has started publishing is NOT in `review` any more - its
    row goes the moment part 1 renders - so without source.parts_left() the day
    after a split story began would look two videos emptier than it is.

    And a video rendered by THIS run is in none of the three: its row is gone,
    it is not a part, and it does not reach sent_today() until publish.py sends
    it minutes later. The morning run is exactly that run - it renders the
    story left over from yesterday and then tops the batch up - so without
    publish.pending() the day is asked for a full four on top of the video
    already in out/, owes five, and slides the last one into tomorrow. That is
    the batch parked 08:23 with a story still promised 18:07 that never went
    out (2026-08-29). review._claim() counts the same list for the same reason.

    Counting those parts is deliberately stricter than TikTok is: a part is
    exempt from the daily count once it exists, so the platform would take
    them on top of the four. The same call _room() already makes, and for the
    same reason - it caps how much of a day one story may lay claim to, and a
    run spent on a part publishes nothing to YouTube, which is never offered
    one.

    NOTE: sent_today() is TikTok's count. On a channel with TikTok off it
    is always zero and YouTube's slower allowance governs, so the batch there
    is one or two questions too generous - set REVIEW_BATCH for that channel.
    """
    return max(0, REVIEW_BATCH - publish.sent_today() - len(publish.pending())
               - review.queued() - source.parts_left())


def _room() -> int:
    """TikTok sends still available to this run.

    Splitting is TikTok's and only TikTok's - YouTube never sees a part, see
    youtube._split() - so this is the only allowance the decision answers to.

    A split story must not straddle the night: the parts are spaced by hours,
    and a part 2 landing the next morning is a different video to everyone who
    saw part 1. So the whole story has to fit in what is left of today.

    Zero when TikTok is not due right now, paused included. Splitting there
    would queue a part that only publish.upload_next() can clear, so the
    story's middle would sit in the queue behind a platform that is not
    running - blocking every later video for as long as that lasts.

    A part is exempt from TikTok's daily count once it exists, so this ceiling
    is stricter than what the parts will actually be allowed. Deliberately: it
    caps how much of the day one story may lay claim to, which is the question
    being asked here.

    What the batch has already claimed comes off it. Stories are parked a day
    at a time now, and every one of them is a send this day still owes - so
    sizing a split against the raw allowance would let a three-parter be
    written on top of three questions already asked, six videos against four.

    A video this run has already rendered comes off it for the same reason -
    see _batch_room(), which is where that one was actually costing a slot.
    Only reachable here with REVIEW_BATCH set above TIKTOK_PER_DAY: at the
    default the two are equal, _batch_room() subtracts strictly more, and the
    min below always lands on it. Cheap enough to be right in both.

    The tau backend used to be refused here outright, on the grounds that only
    a send clears a part and on tau the sender is a desk whose seen.db never
    travels - so CI would re-render part 1 for ever. That stopped being true
    when the handoff step arrived: publish.handoff() calls _clear_part() on the
    runner, and CI commits seen.db, so a part is closed by the machine that
    RENDERED it rather than by the one that sends it. The desk clearing it a
    second time locally is a no-op nobody reads.

    So splitting works on a locally-posted channel, with one condition that
    lives outside this file: the poster has to run often enough to honour
    PART_GAP_HOURS. An hour between a cliffhanger and its answer is the point
    of splitting; a poster on a three-hour timer turns the second half into a
    stranger's video. See the timer in VPS.md.
    """
    if publish.due():
        return 0
    return min(max(0, TIKTOK_PER_DAY - publish.sent_today()
                   - len(publish.pending()) - review.queued()),
               _batch_room())


def main(count: int = 1, force: bool = False) -> int:
    done, skipped, failed = [], 0, 0

    # A story already split owns the next slot: its middle must not queue behind
    # a fresh video, and in CI the mp4 dies with the runner, so the part is
    # rendered now and cleared only once publish.py has actually sent it.
    part = source.next_part()
    # Splitting is TikTok's alone and only a TikTok send clears a part, so a
    # queue standing in front of a channel whose TikTok is off never drains.
    # _room() already refuses to CREATE a split there; this is the other half -
    # a story split before the pause, or before the channel moved off TikTok
    # entirely. Left alone it is rendered again on every run and published on
    # none of them, and it holds the slot, so the channel makes nothing else.
    # Measured on the English channel, 2026-08-04: four runs, four identical
    # renders of the same part 1, no upload and no new story since.
    if part and not TIKTOK_ENABLED:
        log.warning("%s is split across %d parts and TikTok is paused for this "
                    "channel (%s) - nothing can publish them, dropping the rest "
                    "of the story", part["post_id"], part["total"],
                    chan_key("TIKTOK_ENABLED"))
        source.drop_parts(part["post_id"])
        part = None
    # ...and rendered only in a run that can actually SEND it. Only a TikTok
    # send clears a part, so a part built against a closed gap is built again
    # next run and sent on none of them - four identical renders of the same
    # part 1 on 2026-08-04. It also costs the run its whole video: a part is
    # never offered to YouTube, so a run spent rendering one publishes nothing
    # there, and while a split story waits out its gap YouTube stops entirely.
    # `part` stays set either way below - a story mid-flight still blocks a
    # second split, whether or not its next part is rendered this run.
    if part and not force and (reason := publish.due()):
        log.info("%s part %d of %d waits (%s) - an ordinary story this run",
                 part["post_id"], part["n"], part["total"], reason)
    elif part:
        log.info("continuing %s: part %d of %d", part["post_id"], part["n"],
                 part["total"])
        try:
            done.append(make_part(part))
        except Exception:
            log.exception("failed on part %d of %s", part["n"], part["post_id"])
            source.fail_part(part["post_id"], part["n"])
            failed += 1

    # A story whose title is settled owns the next slot, on the same grounds a
    # queued part does: it is written, it is paid for, and there is nothing
    # left to decide about it. Several may be settled at once now - a batch is
    # answered in one sitting - and ready() hands back the one that has been
    # waiting longest, which is the order the user was quoted times for.
    if len(done) < count and (r := review.ready()):
        try:
            if r.get("needs_takes"):
                # Not a render: this tick spends its slot on the takes instead,
                # and the video follows on the tick after the answer.
                log.info("%s: title settled, offering %d takes",
                         r["post_id"], REVIEW_TAKES)
                offer_takes(r)
            else:
                log.info("%s: title settled, rendering", r["post_id"])
                done.append(make_reviewed(r))
        except Exception:
            log.exception("failed on %s after its title came back", r["post_id"])
            failed += 1

    # Asked BEFORE the LLM call and never after: a story written with no issue
    # to ask on is a story nobody can answer, and the next run would pay for it
    # again. Better to skip the slot than to buy the same script every tick.
    if why := review.ok():
        log.error("cannot reach GitHub to ask for a title (%s) - writing nothing", why)
    else:
        # Topping up happens whether or not this run rendered, and to the FULL
        # size of what the day still owes either way. Rendering spends a parked
        # story, so a run that made a video is exactly the run with a slot to
        # refill - and the first run after the midnight reset finds the whole
        # day open and asks for the whole day, in one sitting, before any of it
        # has published. See top_up().
        wrote, skipped, more = top_up(part)
        failed += more
        if wrote:
            log.info("%d new stor%s with the user: %d issue(s), %d video(s), "
                     "%d slot(s) left today", wrote,
                     "y" if wrote == 1 else "ies", review.parked(),
                     review.queued(), _batch_room())

    for f in done:
        print(f)
    log.info("%d ready, %d skipped, %d failed", len(done), skipped, failed)
    # A run that only wrote a story and asked for its title has produced no
    # video and must say so with a non-zero exit: the workflow reads that as
    # "nothing to upload" and stops before youtube.py, which is exactly right.
    # It is not the pool running dry, so it does not get that warning.
    if len(done) < count and not review.parked():
        log.warning("wanted %d, got %d - pool ran out", count, len(done))
    return 0 if done else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if "--selftest" in sys.argv:
        # One story, one narrator: the re-voice under the floor is a SECOND
        # call, and unpinned it comes back in somebody else's voice.
        heard, dur = [], iter([30.0, 70.0])
        voice.pick_voice = lambda g="male", s="": "picked"
        voice.speak_parts = lambda *a, **k: (
            heard.append(k["fish_voice"]) or (Path("m.mp3"), [], 0.0, []))
        voice.duration = lambda p: next(dur)
        render.render = lambda *a, **k: OUT_DIR / "_selftest_voice.mp4"
        _render("t", "b", "male", "_selftest_voice", "s")
        assert heard == ["picked", "picked"], heard

        # ...and the narrator the READINGS were made in is the one the render
        # uses. The take that wins is the body and nothing else, so a fresh
        # draw here reads the title and the closing question in a second voice
        # and leaves the chosen take in the middle of somebody else's video.
        heard.clear()
        dur = iter([70.0])
        globals()["_chosen"] = lambda r: None
        review.drop_takes = lambda *a: None
        review.rendered = lambda *a: None
        _row = {"post_id": "_selftest_voice", "sub": "s", "score": 1, "issue": 1,
                "gender": "male", "title": "T", "voice": "the one they heard",
                "written": [("T", "b")]}
        make_reviewed(_row)
        assert heard == ["the one they heard"], heard
        # ...and a story that was never read aloud draws one, as it always did
        heard.clear()
        dur = iter([70.0])
        make_reviewed({**_row, "voice": ""})
        assert heard == ["picked"], heard

        # The part is rendered in the run that can send it, and in no other:
        # nothing else clears it, and a run spent on a part publishes nothing
        # to YouTube. Stubs, because the real thing costs an LLM call and a TTS
        # call - the point is which branch is taken, not what it renders.
        rendered = []
        TIKTOK_ENABLED = True
        source.next_part = lambda: {"post_id": "x", "n": 2, "total": 2}
        source.fetch = lambda *a: []
        # the reserve and the horror slot are sources of their own and would read
        # daily_<chan>.md and the live archive; stub them off so these cases
        # exercise the pool path alone
        source.next_daily = lambda: None
        source.next_horror = lambda: []
        make_part = lambda p: rendered.append(p) or Path("stub.mp4")  # noqa: E731
        publish.due = lambda: "only 0.4h since the last draft"
        # nothing is out for review in any of the part cases below, and asking
        # GitHub about it would put a subprocess and a network call in a test
        # whose whole point is which branch runs
        review.ready, review.ok = lambda: None, lambda: ""
        review.parked, review.split_parked = lambda: 0, lambda: False
        review.queued = lambda: 0
        assert main(1) == 1 and not rendered, "a part must wait for TikTok's clock"
        assert main(1, force=True) == 0 and rendered, "--force renders it anyway"
        rendered.clear()
        publish.due = lambda: ""
        assert main(1) == 0 and len(rendered) == 1, "and so does an open gap"

        # A FULL batch stops the run from writing another story. The ceiling
        # is the point: at two runs an hour without one, a day's tokens go
        # into a queue of questions nobody has got to yet.
        rendered.clear()
        source.next_part = lambda: None
        written = []
        # "text" included on purpose: part_count() reads it, and whether it is
        # reached depends on source.multipart_today(), which reads the live
        # seen.db. A stub without it passes or raises depending on what the
        # channel published today, which is not something a test may depend on.
        source.fetch = lambda *a: [{"id": "y", "sub": "s", "score": 1,
                                    "title": "t", "text": "x" * 500}]
        globals()["write_and_park"] = lambda p, n=1: written.append(p["id"])
        publish.sent_today = lambda: 0
        # read off the live out/ otherwise, so the batch tests below would pass
        # or fail on whatever mp4 happens to be lying around
        publish.pending = lambda: []
        source.parts_left = lambda: 0
        review.queued = lambda: REVIEW_BATCH
        assert main(1) == 1 and not written, "a full batch must block the pool"
        # ...and so does a day already spent, with the batch empty
        review.queued = lambda: 0
        publish.sent_today = lambda: REVIEW_BATCH
        assert main(1) == 1 and not written, "a spent day must block it too"
        # ...and so do parts still in flight, which are in NEITHER of those:
        # the review row went when part 1 rendered, and the parts have not
        # been sent. The day after a split story begins is the case.
        publish.sent_today = lambda: 0
        source.parts_left = lambda: REVIEW_BATCH
        assert main(1) == 1 and not written, "a queued split must block it too"
        source.parts_left = lambda: 0
        # ...and so does a video rendered this run and not yet sent, which is in
        # neither of those either: its review row went at the render and
        # sent_today() does not see it until publish.py runs.
        publish.pending = lambda: [Path("stub.mp4")] * REVIEW_BATCH
        assert main(1) == 1 and not written, "an unsent render must block it too"
        publish.pending = lambda: []
        # ...and a batch with room fills to REVIEW_BATCH in one run, which is
        # the whole feature: a day's questions asked together, not one an hour.
        vids = [0]
        review.queued = lambda: vids[0]
        # the ceiling is the DAY's, so the count of what already went out is
        # part of it - pinned here rather than read off the live seen.db
        publish.sent_today = lambda: 0
        globals()["write_and_park"] = lambda p, n=1: (
            written.append(p["id"]), vids.__setitem__(0, vids[0] + n))
        assert main(1) == 1 and len(written) == REVIEW_BATCH, written
        # ...and a three-parter fills it with FEWER questions, because it is
        # three sends: the loop stops on videos, not on issues.
        written.clear()
        vids[0] = 0
        globals()["write_and_park"] = lambda p, n=1: (
            written.append(p["id"]), vids.__setitem__(0, vids[0] + 3))
        assert main(1) == 1 and len(written) * 3 >= REVIEW_BATCH, written
        assert len(written) < REVIEW_BATCH, "a split must cost more than one"

        # The other half of the ceiling, and the one that keeps the total from
        # overshooting it: the loop above may start a story while ONE slot is
        # free, so what stops that story from being a three-parter is _room()
        # coming down as the batch fills. Six sends against an allowance of
        # four is exactly what that arithmetic is there to refuse.
        _real_sent, _real_queued = publish.sent_today, review.queued
        try:
            publish.due, publish.sent_today = (lambda: ""), (lambda: 0)
            review.queued = lambda: 0
            assert _room() == min(TIKTOK_PER_DAY, REVIEW_BATCH), _room()
            publish.pending = lambda: [Path("stub.mp4")]
            assert _room() == min(TIKTOK_PER_DAY, REVIEW_BATCH) - 1, _room()
            publish.pending = lambda: []
            review.queued = lambda: REVIEW_BATCH - 1
            assert _room() == 1, "one slot left means no split fits"
            review.queued = lambda: REVIEW_BATCH
            assert _room() == 0, "a full batch leaves nothing to split into"
            # and what already went out today counts against it the same way
            review.queued, publish.sent_today = (lambda: 0), (lambda: REVIEW_BATCH)
            assert _room() == 0, "a spent day leaves nothing to split into"
            publish.sent_today, source.parts_left = (lambda: 0), (lambda: REVIEW_BATCH)
            assert _room() == 0, "nor does one already split"
            source.parts_left = lambda: 0
        finally:
            review.queued = lambda: vids[0]
            publish.sent_today = lambda: 0
        # ...and the answer, when it comes, renders without touching the pool
        written.clear()
        review.ready = lambda: {"post_id": "y", "sub": "s", "score": 1,
                                "gender": "male", "title": "T", "written": [["m", "b"]]}
        globals()["make_reviewed"] = lambda r: rendered.append(r) or Path("stub.mp4")
        vids[0] = REVIEW_BATCH
        assert main(1) == 0 and len(rendered) == 1 and not written, (rendered, written)
        # ...and a run that DID render fills the batch to the day's size just
        # the same. It used to be allowed one story and no more, on the grounds
        # that writing is the slow half - and that is how a day's questions
        # came to arrive at 08:16, 11:41 and 12:48 (2026-08-26) rather than all
        # of them at 08:16, which is the only thing the batch is for.
        written.clear()
        rendered.clear()
        vids[0] = 0
        globals()["write_and_park"] = lambda p, n=1: (
            written.append(p["id"]), vids.__setitem__(0, vids[0] + n))
        assert main(1) == 0 and len(rendered) == 1, rendered
        assert len(written) == REVIEW_BATCH, written
        print("part gating and title review ok")
        sys.exit(0)

    # A stopped channel makes nothing at all - checked here rather than inside
    # main(), so --park is covered by the same line: parking pays the LLM for a
    # story and opens an issue for a human to answer, and both are wasted on a
    # channel that cannot publish. Exit 1 is what CI already reads as "no video
    # this run", so the workflow needs no change.
    if STOPPED:
        log.warning("%s - writing and rendering nothing", STOP_REASON)
        sys.exit(1)

    # Read every settled story aloud and ask which reading goes out. Renders
    # nothing and publishes nothing - like --park, this is the half of main.py
    # that needs no slot, so it can neither spend the day's allowance nor move
    # the gap between sends.
    #
    # The review workflow runs it the moment a title is accepted. It used to
    # happen in the render slot instead, which is where the TTS key and the
    # minutes already were, and that put up to TIKTOK_MIN_GAP_HOURS between
    # answering one question and being asked the next: #141 was accepted at
    # 09:57 and read aloud at 13:41. The render slot keeps it as the fallback -
    # a row that gets there still owing takes is offered them exactly as before,
    # so a failure here costs a wait and never the stage.
    if "--takes" in sys.argv:
        if why := review.ok():
            log.error("cannot reach GitHub to offer the readings (%s)", why)
            sys.exit(1)
        owed = review.owed_takes()
        for r in owed:
            # One story's failure is not the others': the takes are three TTS
            # calls and an upload, and the story that follows this one in the
            # batch has nothing to do with any of them.
            try:
                offer_takes(r)
            except Exception:
                log.exception("could not offer readings for %s", r["post_id"])
        log.info("%d stor%s read aloud", len(owed), "y" if len(owed) == 1 else "ies")
        sys.exit(0)

    # Park stories and render nothing. The review workflow runs this the
    # moment a `-` arrives, so the replacement story is written within the
    # minute instead of at the next tick - which is what "the next one comes
    # straight away" has to mean to be worth anything.
    if "--park" in sys.argv:
        if why := review.ok():
            log.error("cannot reach GitHub to ask for a title (%s)", why)
            sys.exit(1)
        wrote, _, _ = top_up(source.next_part())
        log.info("%d written, %d open with the user", wrote, review.parked())
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sys.exit(main(int(args[0]) if args else 1, force="--force" in sys.argv))
