"""Pipeline: Reddit story -> vertical mp4 with a narrated title card.

    python main.py        one video
    python main.py 5      five
"""
import json
import logging
import sys

import render
import script
import source
import voice
from config import MIN_SEC

log = logging.getLogger("main")

MAX_SLOWDOWN = 0.15   # beyond this the voice starts sounding drugged


def make_video(post: dict):
    """One post end to end. Marks the post used only once the mp4 exists."""
    title, body, gender = script.write_script(post)
    mp3, words, title_end = voice.speak_parts(title, body, post["id"], gender=gender)

    # Word counts only approximate duration - the voice paced 167-214 wpm across
    # runs. Cheaper to re-synthesize slower than to ask the model for more words.
    total = voice.duration(mp3)
    if total < MIN_SEC:
        slow = min((MIN_SEC + 2) / total - 1, MAX_SLOWDOWN)
        log.info("%.1fs is under the %ds floor, re-voicing at -%.0f%%",
                 total, MIN_SEC, slow * 100)
        mp3, words, title_end = voice.speak_parts(
            title, body, post["id"], rate=f"-{slow * 100:.0f}%",
            speed=round(1 - slow, 2), gender=gender)
        total = voice.duration(mp3)
        if total < MIN_SEC:
            log.warning("still %.1fs after slowing down - story was too short", total)

    out = render.render(mp3, words, post["id"], title=title, title_end=title_end)
    # publishing runs separately and later, so the text has to survive on disk
    (out.with_suffix(".meta.json")).write_text(json.dumps(
        {"title": script.plain(title), "body": script.plain(body), "sub": post["sub"]},
        ensure_ascii=False), "utf-8")
    source.mark_used(post["id"], post["score"], post["sub"])
    return out


def main(count: int = 1) -> int:
    # most posts get rejected as unsuitable, so pull a pool rather than exactly count
    posts = source.fetch(count * 4)
    if not posts:
        log.error("no fresh stories - lower MIN_SCORE or add subreddits")
        return 1

    done, skipped, failed = [], 0, 0
    for p in posts:
        if len(done) >= count:
            break
        log.info("r/%s [%d] %s", p["sub"], p["score"], p["title"][:70])
        try:
            done.append(make_video(p))
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
