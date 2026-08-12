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
import script
import source
import voice
from config import (CHANNEL, MIN_SEC, OUT_DIR, TIKTOK_ENABLED, TIKTOK_PER_DAY,
                    VIRAL_MIN_SCORE, YT_VIRAL_ONLY, chan_file, chan_key)

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


def make_video(post: dict) -> Path:
    """One post, one video. Marks the post used only once the mp4 exists."""
    gender, written = script.write_script(post)
    title, body = written[0]
    # The score travels with the file: youtube.py decides by band and the post
    # is out of reach by then - seen.db keeps it per story, not per rendered mp4.
    out = _render(title, body, gender, post["id"], post["sub"],
                  meta={"score": post["score"]})
    source.mark_used(post["id"], post["score"], post["sub"])
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
                   meta={"post_id": p["post_id"], "part": p["n"], "total": p["total"]})


def make_split(post: dict, n: int) -> Path:
    """Write the post as n videos, store them all, render the first.

    The post is marked used here rather than when the last part ships: every
    part's text is already in sqlite by then, so the story is safer stored than
    it would be left loose in the pool for a second run to pick up again.
    """
    gender, written = script.write_script(post, parts=n)
    if len(written) < 2:
        log.info("model returned one part, publishing %s as a single video", post["id"])
        title, body = written[0]
        out = _render(title, body, gender, post["id"], post["sub"],
                      meta={"score": post["score"]})
        source.mark_used(post["id"], post["score"], post["sub"])
        return out

    source.queue_parts(post, written, gender, voice.pick_voice(gender))
    source.mark_used(post["id"], post["score"], post["sub"])
    return make_part(source.next_part())


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
    """
    if publish.due():
        return 0
    return max(0, TIKTOK_PER_DAY - publish.sent_today())


def _takers(score: int) -> bool:
    """Would either platform in this run actually take a story from this band?

    A video nobody takes is not free. It is written, narrated, rendered, refused
    by both platforms and dies with the runner - and the story it spent is
    marked used on the way out, so it never comes back. The en channel spent ten
    stories that way on 2026-08-12: TikTok off for the channel
    (TIKTOK_ENABLED_EN=0), YouTube on YT_VIRAL_ONLY, and every candidate in the
    ordinary band. Its YouTube allowance never got used either, so due() stayed
    true and the next run did it again.

    Only the two refusals that hold for a whole run are asked here. TikTok's
    clock is one of them: due() answers for the run, not for the file, and
    publish.py checks the file again on its own before it sends anything.
    """
    return not publish.due() or not YT_VIRAL_ONLY or score >= VIRAL_MIN_SCORE


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

    # A plan is an ORDER, so while one is running it is the only source: the
    # pool below cannot be consulted even as a fallback, because a story taken
    # from it publishes ahead of the next planned one and the order is gone.
    # A run with nothing to do is the cheaper failure - the plan is finite and
    # the pool is waiting at the end of it.
    if len(done) < count and (left := source.plan_left()):
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
            # The plan waits rather than spends: a planned story rendered into a
            # run neither platform would publish from is burned exactly like a
            # pool one, and the order loses it for good.
            if not _takers(p["score"]):
                log.info("plan: %s waits - nothing this run would take it "
                         "(TikTok closed, YouTube takes %d+ only)",
                         p["id"], VIRAL_MIN_SCORE)
            # multipart_today() is deliberately NOT consulted here. It exists to
            # stop the pool from splitting story after story; the plan already
            # spaces its multi-parters one to a day, and re-asking would only
            # stall the order whenever a run drifts across midnight.
            elif n > 1 and (room := _room()) < n:
                log.warning("plan: %s is %d parts and only %d send(s) are left "
                            "today - starting it tomorrow rather than letting "
                            "it straddle the night", p["id"], n, room)
            else:
                log.info("plan: r/%s [%d] %d part(s): %s", p["sub"], p["score"],
                         n, p["title"][:60])
                try:
                    done.append(make_split(p, n) if n > 1 else make_video(p))
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
        # The day's loud story is read first, before the ordinary band is even
        # asked. It is the one video a day that has to happen, and a run that
        # spends its slot on an ordinary story does not get the slot back - the
        # next run is hours away and the day has a fixed number of them.
        # Ahead of the pool rather than instead of it: a viral post can still
        # come back SKIP, and then the run falls through to an ordinary story
        # exactly as it did before, with the slot still open for a later run.
        posts = source.fetch_viral(count * 2) if source.viral_due() else []
        if posts:
            log.info("viral slot open: %d candidate(s) from %d upvotes up",
                     len(posts), VIRAL_MIN_SCORE)
        # The ordinary band is only worth asking for if something would take it,
        # see _takers() - and not asking also saves the archive call.
        if _takers(0):
            # most posts get rejected as unsuitable, so pull a pool rather than exactly count
            posts += source.fetch(count * 4)
        elif not posts and not done:
            log.info("only the viral band can go out this run - TikTok is "
                     "closed and YouTube takes %d+ only - and it is empty",
                     VIRAL_MIN_SCORE)
            return 1
        if not posts and not done:
            log.error("no fresh stories - lower MIN_SCORE or add subreddits")
            return 1

        for p in posts:
            if len(done) >= count:
                break
            # Asked fresh each time rather than tracked in a flag: a split that
            # queues its parts and then dies on the render has still used up the
            # day's one story, and a SKIP before any of that has not.
            may_split = not part and not source.multipart_today()
            n = max(1, min(script.part_count(p), _room())) if may_split else 1
            log.info("r/%s [%d%s] contested %.2f, %d parts: %s", p["sub"],
                     p["score"],
                     " VIRAL" if p["score"] >= VIRAL_MIN_SCORE else "",
                     p.get("rank", 0), n, p["title"][:60])
            try:
                done.append(make_split(p, n) if n > 1 else make_video(p))
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
    if len(done) < count:
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
        source.fetch = source.fetch_viral = lambda *a: []
        source.viral_due = lambda: False
        make_part = lambda p: rendered.append(p) or Path("stub.mp4")  # noqa: E731
        publish.due = lambda: "only 0.4h since the last draft"
        assert main(1) == 1 and not rendered, "a part must wait for TikTok's clock"
        assert main(1, force=True) == 0 and rendered, "--force renders it anyway"
        rendered.clear()
        publish.due = lambda: ""
        assert main(1) == 0 and len(rendered) == 1, "and so does an open gap"
        print("part gating ok")

        # A band nobody in this run would take must not be rendered, and the
        # pool must not even be asked for one - ten stories on en, 2026-08-12.
        asked = []
        source.next_part = lambda: None
        source.plan_left = lambda: 0
        source.viral_due = lambda: True
        source.fetch_viral = lambda *a: []
        source.fetch = lambda *a: asked.append("pool") or []
        publish.due = lambda: "TikTok is paused for this channel"
        globals()["YT_VIRAL_ONLY"] = True
        assert main(1) == 1 and not asked, "nothing takes the ordinary band here"
        globals()["YT_VIRAL_ONLY"] = False
        assert main(1) == 1 and asked, "YouTube takes any band, so ask the pool"
        print("taker gating ok")
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    sys.exit(main(int(args[0]) if args else 1, force="--force" in sys.argv))
