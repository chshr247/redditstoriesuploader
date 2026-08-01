"""Step 1: story sourcing. Backend is the pullpush.io archive: no keys, no approval.

We walk each subreddit top-down by score. Used posts go into sqlite, and the
lowest used score becomes the cursor, so every run picks up the next batch
instead of the same top posts.
"""
import datetime
import html
import json
import logging
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import safety
from config import (DB_PATH, MAX_SCORE, MIN_COMMENTS, MIN_SCORE, SUBREDDITS)

API = "https://api.pullpush.io/reddit/search/submission/"
UA = "StoryReader/0.1"
BATCH = 100                        # pullpush per-request cap
MIN_CHARS, MAX_CHARS = 400, 4000   # ~40 sec .. ~4 min of narration

log = logging.getLogger(__name__)


def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, score INT, sub TEXT)")
    # A story split across videos: the TEXT of every part, written in one LLM
    # call and kept here until each part is published. It has to be text and not
    # files - a CI run throws out/ away with the runner, so part 2 is rendered
    # in the run that publishes it, hours after part 1 was written.
    db.execute("CREATE TABLE IF NOT EXISTS parts("
               "post_id TEXT, n INT, total INT, title TEXT, body TEXT, "
               "gender TEXT, voice TEXT, sub TEXT, ts REAL, "
               "done INT DEFAULT 0, tries INT DEFAULT 0, "
               "PRIMARY KEY(post_id, n))")
    return db


def _api(**params):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            # pullpush throttles bursts; a short wait clears it
            if e.code != 429 or attempt == 2:
                raise
            wait = 5 * (attempt + 1)
            log.info("pullpush 429, retrying in %ds", wait)
            time.sleep(wait)


# "UPDATE: ..." only makes sense to someone who read the original post, and the
# top of relationship_advice is almost entirely these. META is mod/sub drama.
CONTEXT_BOUND = re.compile(r"^\s*[\[(]?\s*(update|meta|part\s*\d|final\s+update)\b", re.I)


def _usable(p: dict) -> bool:
    text = p.get("selftext") or ""
    return (
        not p.get("over_18")
        and not p.get("stickied") and not p.get("locked")
        and not CONTEXT_BOUND.match(p.get("title") or "")
        and p.get("num_comments", 0) >= MIN_COMMENTS
        and text not in ("[removed]", "[deleted]")
        and MIN_CHARS <= len(text) <= MAX_CHARS
    )


def _clean(s: str) -> str:
    # pullpush returns raw markdown with html entities; the LLM handles the rest
    return html.unescape(s).replace("&#x200B;", "").strip()


def fetch(limit: int = 3) -> list[dict]:
    """Return up to `limit` unused stories, highest score first."""
    db = _db()
    out = []
    # Shuffled, not in order: the loop stops as soon as it has enough, so a
    # fixed order means the first subreddit answers every single time and the
    # rest are never read. Twenty-six of the first twenty-seven stories came
    # from one sub before this line existed.
    for sub in random.sample(SUBREDDITS, len(SUBREDDITS)):
        if len(out) >= limit:
            break
        # cursor: drop below the weakest post already used, but never above the
        # MAX_SCORE ceiling - the viral stratum is not what we are after
        row = db.execute("SELECT MIN(score) FROM seen WHERE sub=?", (sub,)).fetchone()
        ceiling = min(row[0], MAX_SCORE) if row[0] else MAX_SCORE

        params = {"subreddit": sub, "size": BATCH,
                  "sort": "desc", "sort_type": "score", "score": f"<{ceiling}"}
        try:
            posts = _api(**params)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("r/%s: source unavailable (%s), skipping", sub, e)
            continue

        seen = {r[0] for r in db.execute("SELECT id FROM seen")}
        for p in posts:
            if len(out) >= limit:
                break
            if p["id"] in seen or p["score"] < MIN_SCORE or not _usable(p):
                continue
            title, text = _clean(p["title"]), _clean(p["selftext"])
            # cheapest possible gate: reject here and the story never costs an LLM call
            hit = safety.blocked(title, text)
            if hit:
                log.info("skipping %s (%s)", p["id"], hit)
                continue
            out.append({"id": p["id"], "sub": sub, "score": p["score"],
                        "title": title, "text": text})
        log.info("r/%s: %d candidates out of %d", sub, len(out), len(posts))
    db.close()
    return out


def mark_used(post_id: str, score: int, sub: str) -> None:
    """Call only after a successful render, otherwise the story is lost."""
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (post_id, score, sub))


# ---------------------------------------------------------------- split stories

def queue_parts(post: dict, parts: list[tuple[str, str]], gender: str,
                voice: str) -> None:
    """Store every part of a split story. Part 1 is rendered straight away.

    The voice id is stored with them on purpose: pick_voice() draws at random,
    and a story that changes narrator halfway sounds like two different videos
    stitched together.
    """
    now = time.time()
    with _db() as db:
        db.executemany(
            "INSERT OR REPLACE INTO parts VALUES (?,?,?,?,?,?,?,?,?,0,0)",
            [(post["id"], i, len(parts), title, body, gender, voice, post["sub"], now)
             for i, (title, body) in enumerate(parts, 1)])
    log.info("%s: queued %d parts", post["id"], len(parts))


_PART_COLS = ("post_id", "n", "total", "title", "body", "gender", "voice", "sub")


def next_part() -> dict | None:
    """The earliest unpublished part, or None.

    Strictly in order: a part clears only once it is UPLOADED, so part 2 is
    invisible here until part 1 is actually out. That is the whole guard against
    publishing a story's middle before its beginning.
    """
    with _db() as db:
        row = db.execute(
            f"SELECT {','.join(_PART_COLS)} FROM parts "
            "WHERE done=0 ORDER BY ts, n LIMIT 1").fetchone()
    return dict(zip(_PART_COLS, row)) if row else None


def finish_part(post_id: str, n: int) -> None:
    """Call after the part is PUBLISHED, not after it renders.

    A render that succeeds and an upload that then fails must leave the part
    pending: in CI the mp4 dies with the runner, so the only way back is to
    build it again from the text stored here.
    """
    with _db() as db:
        db.execute("UPDATE parts SET done=1 WHERE post_id=? AND n=?", (post_id, n))


MAX_TRIES = 3


def fail_part(post_id: str, n: int) -> None:
    """Count a failed attempt, and give up on the story after MAX_TRIES.

    Without this a part that can never be rendered holds the queue forever -
    next_part() would keep handing it back and no other video would ever go out.
    Losing the tail of one story beats a channel that quietly stops publishing.
    """
    with _db() as db:
        db.execute("UPDATE parts SET tries=tries+1 WHERE post_id=? AND n=?",
                   (post_id, n))
        row = db.execute("SELECT tries FROM parts WHERE post_id=? AND n=?",
                         (post_id, n)).fetchone()
        if row and row[0] >= MAX_TRIES:
            db.execute("UPDATE parts SET done=1 WHERE post_id=?", (post_id,))
            log.error("%s part %d failed %d times - dropping the rest of the "
                      "story so the queue can move on", post_id, n, row[0])


def multipart_today() -> bool:
    """True if a story was already split today.

    One a day. A feed of nothing but two-parters reads as padding, and each
    part costs an upload slot the ordinary stories need.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    with _db() as db:
        rows = db.execute("SELECT ts FROM parts WHERE n=1").fetchall()
    return any(
        datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date() == today
        for (ts,) in rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ok = {"selftext": "x" * 500, "over_18": False, "num_comments": 500, "title": "A story"}
    assert _usable(ok)
    assert not _usable({**ok, "selftext": "[removed]"})
    assert not _usable({**ok, "over_18": True})
    assert not _usable({**ok, "selftext": "too short"})
    assert not _usable({**ok, "num_comments": 3}), "dead post should be dropped"
    assert not _usable({**ok, "stickied": True})
    for bad in ["UPDATE: my sister finally called", "[Update] we talked",
                "META: about this sub", "Part 2 of my saga"]:
        assert not _usable({**ok, "title": bad}), f"context-bound: {bad!r}"
    for good in ["Updating my resume ruined my week", "My dad is kicking me out"]:
        assert _usable({**ok, "title": good}), f"false positive: {good!r}"
    assert _clean("don&#39;t") == "don't"

    # parts come back one at a time, in order, and only once the one before is
    # published. Skipped when a real story is mid-flight - it would shadow the
    # fixture and the ordering check would be testing the wrong rows.
    if not next_part():
        fake = {"id": "_selftest", "sub": "test", "score": 0}
        queue_parts(fake, [("t1", "b1"), ("t2", "b2")], "female", "voice1")
        p = next_part()
        assert (p["n"], p["total"], p["voice"]) == (1, 2, "voice1"), p
        assert multipart_today(), "a story queued now counts as today's"
        finish_part("_selftest", 1)
        assert next_part()["n"] == 2, "part 2 must wait for part 1"
        # three failures drop the whole story rather than block the queue
        for _ in range(MAX_TRIES):
            fail_part("_selftest", 2)
        assert next_part() is None, "a hopeless story must not hold the queue"
        with _db() as db:
            db.execute("DELETE FROM parts WHERE post_id='_selftest'")

    posts = fetch(3)
    assert posts, "source returned nothing - check network / SUBREDDITS"
    for p in posts:
        print(f"[{p['score']:>7}] r/{p['sub']} {len(p['text']):>5}ch  {p['title'][:60]}")
