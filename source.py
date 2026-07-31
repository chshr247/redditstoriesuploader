"""Step 1: story sourcing. Backend is the pullpush.io archive: no keys, no approval.

We walk each subreddit top-down by score. Used posts go into sqlite, and the
lowest used score becomes the cursor, so every run picks up the next batch
instead of the same top posts.
"""
import html
import json
import logging
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
    for sub in SUBREDDITS:
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

    posts = fetch(3)
    assert posts, "source returned nothing - check network / SUBREDDITS"
    for p in posts:
        print(f"[{p['score']:>7}] r/{p['sub']} {len(p['text']):>5}ch  {p['title'][:60]}")
