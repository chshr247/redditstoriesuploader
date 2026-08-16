"""Pipeline: Reddit story -> vertical mp4 with a narrated title card.

    python main.py        one video
    python main.py 5      five

A long post can become two or three videos instead of one, and that is a TikTok
arrangement only - YouTube is never offered a part, it always gets whole
stories. The whole split is written in a single LLM call and parked in seen.db;
one part is rendered per run, in the run that publishes it, because on a CI
runner out/ does not survive to the next one.

Which story is next is normally source.py's decision. A channel with a plan
file (config.PLAN_FILE, e.g. plan_ru.md) takes that decision away from it: the
stories go out in the order written there, one at a time, and the pool is not
touched until the plan runs out.
"""
import json
import logging
import sys
from pathlib import Path

import publish
import render
import review
import script
import source
import voice
from config import (CHANNEL, LOUD_AT, MIN_SEC, OUT_DIR, TIKTOK_ENABLED,
                    TIKTOK_PER_DAY, chan_file, chan_key)

log = logging.getLogger("main")

MAX_SLOWDOWN = 0.15   # beyond this the voice starts sounding drugged


def _render(title: str, body: str, gender: str, key: str, sub: str,
            fish_voice: str = "", meta: dict | None = None) -> Path:
    """Narrate one script and burn it into out/<key>[_<channel>].mp4.

    `key` names the STORY; the channel is what turns it into a file name. Both
    channels render the same story from the same post id, so without that they
    would write over each other in out/ and count as one row in `uploaded`.
    """
    name = chan_file(key)
    mp3, words, title_end, title_words = voice.speak_parts(
        title, body, name, gender=gender, fish_voice=fish_voice)

    # Word counts only approximate duration - the voice paced 167-214 wpm across
    # runs. Cheaper to re-synthesize slower than to ask the model for more words.
    total = voice.duration(mp3)
    if total < MIN_SEC:
        slow = min((MIN_SEC + 2) / total - 1, MAX_SLOWDOWN)
        log.info("%.1fs is under the %ds floor, re-voicing at -%.0f%%",
                 total, MIN_SEC, slow * 100)
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
                        key=key, title_words=title_words,
                        part=(meta or {}).get("part", 0)
                        if (meta or {}).get("total", 0) > 1 else 0)
    # publishing runs separately and later, so the text has to survive on disk.
    # The channel goes in with it - the part marker's language and which
    # channel's queue the file belongs to are both read back from here.
    (out.with_suffix(".meta.json")).write_text(json.dumps(
        {"title": script.plain(title), "body": script.plain(body), "sub": sub,
         "channel": CHANNEL, **(meta or {})}, ensure_ascii=False), "utf-8")
    return out


def write_and_park(post: dict, n: int = 1) -> None:
    """Write the story and hand its title to the user. Renders nothing.

    The post is marked used here rather than at the render, for the reason
    make_split() always marked it early: the text is safer in sqlite than the
    story is loose in the pool, where the next run would write it a second time
    while the first is still waiting for an answer.
    """
    gender, written = script.write_script(post, parts=n)
    if n > 1 and len(written) < 2:
        log.info("model returned one part, %s will publish as a single video",
                 post["id"])
    review.park(post, gender, written)
    source.mark_used(post["id"], post["score"], post["sub"])


def make_reviewed(r: dict) -> Path:
    """Render the parked story under the title that came back off the issue.

    The chosen title replaces the model's on EVERY part. A split story has one
    title by construction - it is narrated and lit at the head of each part -
    and all of the parts were written against it in a single call.
    """
    post = {"id": r["post_id"], "sub": r["sub"], "score": r["score"]}
    # Already final: review.py has folded the chosen title and any narration
    # rewritten by hand into it, and a rewrite may have merged three parts into
    # two - so the count comes from here and not from what the model wrote.
    written = r["written"]
    if len(written) > 1:
        source.queue_parts(post, written, r["gender"],
                           voice.pick_voice(r["gender"]), issue=r["issue"])
        out = make_part(source.next_part())
    else:
        # The score travels with the file: youtube.py decides by band and the
        # post is out of reach by then - seen.db keeps it per story, not per
        # rendered mp4. The issue travels the same way and for the same reason:
        # publish.py puts the caption back into the issue the title came from.
        out = _render(written[0][0], written[0][1], r["gender"], post["id"],
                      post["sub"],
                      meta={"score": post["score"], "issue": r["issue"]})
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
    return max(0, TIKTOK_PER_DAY - publish.sent_today())


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

    # A story whose title is with the user owns the next slot, on the same
    # grounds a queued part does: it is written, it is paid for, and it is one
    # answer away from being a video. Nothing new is written while one waits -
    # a second unanswered story is a second one nobody asked for, and at two
    # runs an hour that is how a day's tokens go into a queue of questions.
    if len(done) < count and (r := review.ready()):
        log.info("%s: title settled, rendering", r["post_id"])
        try:
            done.append(make_reviewed(r))
        except Exception:
            log.exception("failed on %s after its title came back", r["post_id"])
            failed += 1

    if review.waiting():
        log.info("a title is still with the user - nothing new written this run")
    # Asked BEFORE the LLM call and never after: a story written with no issue
    # to ask on is a story nobody can answer, and the next run would pay for it
    # again. Better to skip the slot than to buy the same script every tick.
    elif len(done) < count and (why := review.ok()):
        log.error("cannot reach GitHub to ask for a title (%s) - writing nothing", why)
    # A plan is an ORDER, so while one is running it is the only source: the
    # pool below cannot be consulted even as a fallback, because a story taken
    # from it publishes ahead of the next planned one and the order is gone.
    # A run with nothing to do is the cheaper failure - the plan is finite and
    # the pool is waiting at the end of it.
    elif len(done) < count and (left := source.plan_left()):
        log.info("plan: %d stories still to publish", left)
        if part:
            # Its remaining parts come first by definition, and this run has
            # already decided not to render one (publish.due() said no). A new
            # story here would land between two halves of the one on air.
            log.info("%s is mid-flight - nothing new until its parts are out",
                     part["post_id"])
        elif not (p := source.next_planned()):
            log.error("plan: %d stories left but none could be read this run",
                      left)
        else:
            n = p["parts"]
            # multipart_today() is deliberately NOT consulted here. It exists to
            # stop the pool from splitting story after story; the plan already
            # spaces its multi-parters one to a day, and re-asking would only
            # stall the order whenever a run drifts across midnight.
            if n > 1 and (room := _room()) < n:
                log.warning("plan: %s is %d parts and only %d send(s) are left "
                            "today - starting it tomorrow rather than letting "
                            "it straddle the night", p["id"], n, room)
            else:
                log.info("plan: r/%s [%d] %d part(s): %s", p["sub"], p["score"],
                         n, p["title"][:60])
                try:
                    write_and_park(p, n)
                except script.Unsuitable as e:
                    # burned, exactly as in the pool below: the plan moves on to
                    # the next story rather than offering this one again for ever
                    source.mark_used(p["id"], p["score"], p["sub"])
                    log.info("skipped: %s", e)
                    skipped += 1
                except Exception:
                    log.exception("failed on %s", p["id"])
                    failed += 1
    elif len(done) < count:
        # One band, one call. The loud story used to be read first out of a
        # band of its own, on a slot of one a day; a year of three subs held
        # twelve posts above that floor, so the slot was promising daily what
        # the subs produce monthly. Loudness is a term in contested() now, and
        # the loud story competes for the top of one list like everything else.
        # most posts get rejected as unsuitable, so pull a pool rather than exactly count
        posts = source.fetch(count * 4)
        if not posts and not done:
            log.error("no fresh stories - lower MIN_SCORE or add subreddits")
            return 1

        for p in posts:
            # One story is written per run, not `count` of them: it is parked
            # for a title rather than rendered, so nothing here can add to
            # `done` and the loop would otherwise write the whole pool out to
            # issues in one go. It only walks on when a story turns out unusable.
            # Asked fresh each time rather than tracked in a flag: a split that
            # queues its parts and then dies on the render has still used up the
            # day's one story, and a SKIP before any of that has not.
            may_split = not part and not source.multipart_today()
            n = max(1, min(script.part_count(p), _room())) if may_split else 1
            log.info("r/%s [%d%s] contested %.2f, %d parts: %s", p["sub"],
                     p["score"], " LOUD" if p["score"] >= LOUD_AT else "",
                     p.get("rank", 0), n, p["title"][:60])
            try:
                write_and_park(p, n)
                break
            except script.Unsuitable as e:
                # burn it so the same dud is not reconsidered next run
                source.mark_used(p["id"], p["score"], p["sub"])
                log.info("skipped: %s", e)
                skipped += 1
            except Exception:
                # one bad story must not kill the batch; it stays unmarked for a retry
                log.exception("failed on %s, moving on", p["id"])
                failed += 1

    for f in done:
        print(f)
    log.info("%d ready, %d skipped, %d failed", len(done), skipped, failed)
    # A run that only wrote a story and asked for its title has produced no
    # video and must say so with a non-zero exit: the workflow reads that as
    # "nothing to upload" and stops before youtube.py, which is exactly right.
    # It is not the pool running dry, so it does not get that warning.
    if len(done) < count and not review.waiting():
        log.warning("wanted %d, got %d - pool ran out", count, len(done))
    return 0 if done else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if "--selftest" in sys.argv:
        # The part is rendered in the run that can send it, and in no other:
        # nothing else clears it, and a run spent on a part publishes nothing
        # to YouTube. Stubs, because the real thing costs an LLM call and a TTS
        # call - the point is which branch is taken, not what it renders.
        rendered = []
        TIKTOK_ENABLED = True
        source.next_part = lambda: {"post_id": "x", "n": 2, "total": 2}
        source.fetch = lambda *a: []
        make_part = lambda p: rendered.append(p) or Path("stub.mp4")  # noqa: E731
        publish.due = lambda: "only 0.4h since the last draft"
        # nothing is out for review in any of the part cases below, and asking
        # GitHub about it would put a subprocess and a network call in a test
        # whose whole point is which branch runs
        review.ready, review.waiting, review.ok = lambda: None, lambda: False, lambda: ""
        assert main(1) == 1 and not rendered, "a part must wait for TikTok's clock"
        assert main(1, force=True) == 0 and rendered, "--force renders it anyway"
        rendered.clear()
        publish.due = lambda: ""
        assert main(1) == 0 and len(rendered) == 1, "and so does an open gap"

        # A title with the user stops the run from writing a second story: at
        # two runs an hour that is the difference between one question and a
        # day's worth of them, all paid for and none of them answered.
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
        review.waiting = lambda: True
        assert main(1) == 1 and not written, "a pending title must block the pool"
        review.waiting = lambda: False
        assert main(1) == 1 and written == ["y"], "and let it through once answered"
        # ...and the answer, when it comes, renders without touching the pool
        written.clear()
        review.ready = lambda: {"post_id": "y", "sub": "s", "score": 1,
                                "gender": "male", "title": "T", "written": [["m", "b"]]}
        globals()["make_reviewed"] = lambda r: rendered.append(r) or Path("stub.mp4")
        assert main(1) == 0 and len(rendered) == 1 and not written, (rendered, written)
        print("part gating and title review ok")
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sys.exit(main(int(args[0]) if args else 1, force="--force" in sys.argv))
