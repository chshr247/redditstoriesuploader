"""Pipeline: Reddit story -> vertical mp4 with a narrated title card.

    python main.py        one video
    python main.py 5      five

A long post can become two or three videos instead of one. The whole split is
written in a single LLM call and parked in seen.db; one part is rendered per
run, in the run that publishes it, because on a CI runner out/ does not survive
to the next one.
"""
import json
import logging
import sys
from pathlib import Path

import render
import script
import source
import voice
import youtube
from config import CHANNEL, MIN_SEC, OUT_DIR, VIRAL_MIN_SCORE, chan_file

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
    mp3, words, title_end = voice.speak_parts(title, body, name, gender=gender,
                                              fish_voice=fish_voice)

    # Word counts only approximate duration - the voice paced 167-214 wpm across
    # runs. Cheaper to re-synthesize slower than to ask the model for more words.
    total = voice.duration(mp3)
    if total < MIN_SEC:
        slow = min((MIN_SEC + 2) / total - 1, MAX_SLOWDOWN)
        log.info("%.1fs is under the %ds floor, re-voicing at -%.0f%%",
                 total, MIN_SEC, slow * 100)
        mp3, words, title_end = voice.speak_parts(
            title, body, name, rate=f"-{slow * 100:.0f}%",
            speed=round(1 - slow, 2), gender=gender, fish_voice=fish_voice)
        total = voice.duration(mp3)
        if total < MIN_SEC:
            log.warning("still %.1fs after slowing down - story was too short", total)

    # keyed on the story, not the file: that is what keeps the two channels'
    # versions of one story off the same background clip
    out = render.render(mp3, words, name, title=title, title_end=title_end, key=key)
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
    # kind rides along to publishing time: it decides which hashtag pool the
    # description draws from, and by then the post itself is long gone.
    out = _render(title, body, gender, post["id"], post["sub"],
                  meta={"kind": post.get("kind", "story")})
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
                      meta={"kind": post.get("kind", "story")})
        source.mark_used(post["id"], post["score"], post["sub"])
        return out

    source.queue_parts(post, written, gender, voice.pick_voice(gender))
    source.mark_used(post["id"], post["score"], post["sub"])
    return make_part(source.next_part())


def _room() -> int:
    """YouTube uploads still available to this run.

    A split story must not straddle the night: the parts are spaced by hours,
    and a part 2 landing the next morning is a different video to everyone who
    saw part 1. So the whole story has to fit in what is left of today.

    Zero when YouTube is not due right now, which is what a TikTok-only run is.
    Splitting there would queue a part that only youtube.upload_next() can
    clear, so the story's middle would sit in the queue until YouTube's turn
    comes round - blocking every later video behind it for hours.
    """
    if youtube.due():
        return 0
    s = youtube.status()
    return max(0, s["allowed"] - s["today"])


def main(count: int = 1) -> int:
    done, skipped, failed = [], 0, 0

    # A story already split owns the next slot: its middle must not queue behind
    # a fresh video, and in CI the mp4 dies with the runner, so the part is
    # rendered now and cleared only once youtube.py has actually uploaded it.
    part = source.next_part()
    if part:
        log.info("continuing %s: part %d of %d", part["post_id"], part["n"],
                 part["total"])
        try:
            done.append(make_part(part))
        except Exception:
            log.exception("failed on part %d of %s", part["n"], part["post_id"])
            source.fail_part(part["post_id"], part["n"])
            failed += 1

    if len(done) < count:
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
        # most posts get rejected as unsuitable, so pull a pool rather than exactly count
        posts += source.fetch(count * 4)
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
            log.info("r/%s [%d%s] %s %d parts: %s", p["sub"], p["score"],
                     " VIRAL" if p["score"] >= VIRAL_MIN_SCORE else "",
                     p.get("kind", "story"), n, p["title"][:60])
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
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
