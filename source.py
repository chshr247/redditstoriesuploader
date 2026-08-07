"""Step 1: story sourcing. Backend is the pullpush.io archive: no keys, no approval.

We walk each subreddit top-down by score. Used posts go into sqlite, and the
lowest used score becomes the cursor, so every run picks up the next batch
instead of the same top posts.

pullpush goes down for hours at a time (502s across every sub, and then the run
makes no video at all), so arctic shift stands behind it - same archive, same
post shape, no key either. It cannot sort or filter by score, only by time, so
it reads a random window of days and the band is filtered here instead.
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
from config import (DB_PATH, DEFAULT_CHANNEL, MAX_SCORE, MIN_COMMENTS,
                    MIN_SCORE, OUTPUT_LANG, SUBREDDITS, VIRAL_MIN_SCORE,
                    VIRAL_PER_DAY)

API = "https://api.pullpush.io/reddit/search/submission/"
ARCTIC = "https://arctic-shift.photon-reddit.com/api/posts/search"
UA = "StoryReader/0.1"
BATCH = 100                        # pullpush per-request cap
MIN_CHARS, MAX_CHARS = 400, 4000   # ~40 sec .. ~4 min of narration
ARCTIC_DAYS = 2                    # width of the window arctic shift reads
ARCTIC_BACK = 4 * 365              # ...taken from anywhere in the last 4 years

log = logging.getLogger(__name__)


def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, score INT, sub TEXT)")
    # When the post was used, added later: the daily viral slot has to know
    # whether today already had one, and a row on its own cannot say. Rows
    # written before this exists keep ts NULL, which reads as "not today" -
    # correct for every one of them, since a run that old is over.
    if "ts" not in {c[1] for c in db.execute("PRAGMA table_info(seen)")}:
        db.execute("ALTER TABLE seen ADD COLUMN ts REAL")
    # A story split across videos: the TEXT of every part, written in one LLM
    # call and kept here until each part is published. It has to be text and not
    # files - a CI run throws out/ away with the runner, so part 2 is rendered
    # in the run that publishes it, hours after part 1 was written.
    db.execute("CREATE TABLE IF NOT EXISTS parts("
               "post_id TEXT, n INT, total INT, title TEXT, body TEXT, "
               "gender TEXT, voice TEXT, sub TEXT, ts REAL, "
               "done INT DEFAULT 0, tries INT DEFAULT 0, "
               "PRIMARY KEY(post_id, n))")
    _add_lang(db)
    return db


# One post becomes one video per channel: the same story told in Russian and in
# English is two videos for two audiences that never meet. So "used" is a fact
# about (post, language), not about the post - and that is a change of PRIMARY
# KEY, which sqlite cannot do with ALTER TABLE. Hence a rebuild.
#
# Rows written before this are the Russian channel's by definition, so they are
# carried over as such and nothing is lost or re-told.
def _add_lang(db) -> None:
    for table, cols, pk in [
            ("seen", "id, score, sub, ts", "id, lang"),
            ("parts", "post_id, n, total, title, body, gender, voice, sub, ts, "
                      "done, tries", "post_id, n, lang")]:
        if "lang" in {c[1] for c in db.execute(f"PRAGMA table_info({table})")}:
            continue
        # name, type and default carried over from the live schema; the old
        # PRIMARY KEY is deliberately NOT, it is being replaced
        decl = [f"{c[1]} {c[2]}" + (f" DEFAULT {c[4]}" if c[4] is not None else "")
                for c in db.execute(f"PRAGMA table_info({table})")]
        db.execute(f"CREATE TABLE {table}_new({', '.join(decl)}, "
                   f"lang TEXT NOT NULL DEFAULT '{DEFAULT_CHANNEL}', "
                   f"PRIMARY KEY({pk}))")
        db.execute(f"INSERT INTO {table}_new({cols}, lang) "
                   f"SELECT {cols}, '{DEFAULT_CHANNEL}' FROM {table}")
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        log.info("%s: migrated to per-channel rows", table)


def _api(base: str, **params):
    """Both archives answer {"data": [...]} to a plain GET, so one helper does."""
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            # both throttle bursts; a short wait clears it
            if e.code != 429 or attempt == 2:
                raise
            wait = 5 * (attempt + 1)
            log.info("429 from %s, retrying in %ds", base, wait)
            time.sleep(wait)


# Anything that means "the archive is not answering right now" - the next
# backend gets a turn, and if there is none the sub is skipped for this run.
DOWN = (urllib.error.URLError, TimeoutError, json.JSONDecodeError)


def _pullpush(sub: str, top: int) -> list[dict]:
    """The band's best unread posts, straight from the score index."""
    return _api(API, subreddit=sub, size=BATCH, sort="desc",
                sort_type="score", score=f"<{top}")


def _arctic(sub: str) -> list[dict]:
    """The same posts from arctic shift, which has no score index at all.

    So it reads a random ARCTIC_DAYS-day window instead and _harvest filters the
    band out of it. Random, not the newest days and not the score cursor's
    position: the cursor is a score and means nothing to a time-ordered API,
    while a fixed window would hand back the same posts every run until the
    whole window was used up. A couple of days of a busy sub carries a handful
    of posts above MIN_SCORE, which is all a run needs.
    """
    start = datetime.date.today() - datetime.timedelta(
        days=random.randint(ARCTIC_DAYS, ARCTIC_BACK))
    # "auto" is arctic's own batch size, 100..1000 - the busy subs cap out on it
    return _api(ARCTIC, subreddit=sub, limit="auto", sort="desc",
                after=str(start), before=str(start + datetime.timedelta(days=ARCTIC_DAYS)))


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


# A story where it is OBVIOUS who was in the wrong is watched once and agreed
# with. One where the comments cannot decide is argued about, and the argument
# is the engagement - it is what the closing question is asking for anyway.
# So the pool is ordered by how contested a post looks before anything is spent
# on it. Two signals, both already in the response, so neither costs a request:
#
#   the post asks for a verdict in so many words - AITA, WIBTA, "was I wrong"
#   it drew unusually many comments for its score, which on Reddit means people
#   came to take sides rather than to upvote and move on
#
# Ranking, not filtering: an uncontested story is still a story, it just goes
# behind the contested ones in the pool main.py works through.
CONTESTED = re.compile(
    r"\b(?:ai[tw]a|aitah|wibta|yta|nta"
    r"|am\s+i\s+(?:the\s+)?(?:asshole|ah|jerk|wrong|bad\s+guy|overreacting"
    r"|being\s+unreasonable|in\s+the\s+wrong)"
    r"|was\s+i\s+(?:the\s+)?(?:asshole|wrong|in\s+the\s+wrong)"
    r"|who\s*'?s\s+(?:right|wrong)|did\s+i\s+overreact)\b", re.I)

# What one comment per this many upvotes is worth, i.e. where the ratio signal
# saturates. Measured over a fetch of the current subs: an ordinary post sits
# near 1 comment per 40 upvotes, a fight near 1 in 10.
# ponytail: a flat constant, not a per-sub baseline. Subs whose normal ratio is
# far off this one would need their own; add that when a sub's stories start
# crowding out everything else.
ARGUMENTATIVE = 10


def contested(title: str, comments: int, score: int) -> float:
    """0..2, higher when a post looks like a fight rather than a verdict."""
    return (bool(CONTESTED.search(title))
            + min(comments * ARGUMENTATIVE / max(score, 1), 1.0))


def _harvest(limit: int, floor: int, ceiling: int) -> list[dict]:
    """Up to `limit` unused posts scored inside [floor, ceiling), best first.

    Every subreddit is walked top-down independently: the cursor for one is the
    weakest post already used out of ITS OWN band, so a sub whose band is spent
    returns nothing and the next one answers instead. That is what makes the
    viral band survivable at all - no single sub has a year of 40k posts in it.

    "Best" is contested() - the argument, not the score. Every sub is read
    rather than stopping at the first one that fills the pool: a pool taken from
    whichever sub was drawn first cannot be ranked, since the ranking's whole
    job is to choose BETWEEN the subs. That costs one request per sub per run.
    """
    db = _db()
    out = []
    seen = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                     (OUTPUT_LANG,))}
    # Still shuffled, so that a tie in the ranking does not always fall the same
    # way - twenty-six of the first twenty-seven stories came from one sub back
    # when the order was fixed and the loop stopped at the first full pool.
    for sub in random.sample(SUBREDDITS, len(SUBREDDITS)):
        # Per channel, like the seen set above. A shared cursor would start the
        # second channel wherever the first one has already got to, and every
        # story between the two positions would never be told in that language.
        row = db.execute(
            "SELECT MIN(score) FROM seen WHERE sub=? AND lang=? AND score>=?",
            (sub, OUTPUT_LANG, floor)).fetchone()
        top = min(row[0], ceiling) if row[0] else ceiling
        if top <= floor:
            log.info("r/%s: nothing left above %d, trying another sub", sub, floor)
            continue

        try:
            posts = _pullpush(sub, top)
        except DOWN as e:
            log.warning("r/%s: pullpush unavailable (%s), trying arctic shift", sub, e)
            try:
                posts = _arctic(sub)
            except DOWN as e:
                log.warning("r/%s: no source available (%s), skipping", sub, e)
                continue

        found = 0
        for p in posts:
            # Capped per sub, or one sub with a hundred usable posts fills the
            # pool on its own again and the ranking has nothing to choose from.
            if found >= limit:
                break
            # The ceiling is enforced here and not only in the request, because
            # arctic shift cannot filter on score - its window comes back with
            # the whole band range in it. pullpush already caps at `top`, which
            # is the tighter of the two, so this changes nothing for it.
            if p["id"] in seen or not floor <= p["score"] < ceiling or not _usable(p):
                continue
            title, text = _clean(p["title"]), _clean(p["selftext"])
            # cheapest possible gate: reject here and the story never costs an LLM call
            hit = safety.blocked(title, text)
            if hit:
                log.info("skipping %s (%s)", p["id"], hit)
                continue
            found += 1
            out.append({"id": p["id"], "sub": sub, "score": p["score"],
                        "title": title, "text": text,
                        "rank": contested(title, p.get("num_comments", 0),
                                          p["score"])})
        log.info("r/%s: %d candidates out of %d", sub, found, len(posts))
    db.close()
    # Contested first. main.py walks this list in order and stops once it has
    # its videos, so the order IS the priority.
    out.sort(key=lambda p: p["rank"], reverse=True)
    return out[:limit]


def fetch(limit: int = 3) -> list[dict]:
    """Return up to `limit` unused stories from the ordinary band."""
    return _harvest(limit, MIN_SCORE, MAX_SCORE)


def fetch_viral(limit: int = 3) -> list[dict]:
    """The same, from above the MAX_SCORE ceiling: the day's one loud story.

    Deliberately not filtered any further. What made a post reach 40k is the
    point of taking it, so the choice of which loud story works is left to the
    prompt's own SKIP gate, which throws out news and meta anyway.
    """
    return _harvest(limit, VIRAL_MIN_SCORE, 10_000_000)


def viral_due() -> bool:
    """True when today has not had its loud story yet.

    Counted off `seen`, i.e. off stories that were actually rendered - a run
    that fetched one and then failed on it has not spent the day's slot.

    The day is the UTC one, like every other daily count here, and that is not
    an accident: it turns over at 03:00 MSK, in the middle of the nightly gap
    between the last run at 23:37 and the first at 10:07. A local day would
    reset at 00:00 MSK - twenty minutes after the last run of the evening, so
    the slot would open with nothing left awake to spend it.
    """
    start = datetime.datetime.combine(
        datetime.datetime.now(datetime.timezone.utc).date(),
        datetime.time.min, datetime.timezone.utc).timestamp()
    with _db() as db:
        used = db.execute(
            "SELECT COUNT(*) FROM seen WHERE lang=? AND score>=? AND ts>=?",
            (OUTPUT_LANG, VIRAL_MIN_SCORE, start)).fetchone()[0]
    return used < VIRAL_PER_DAY


def mark_used(post_id: str, score: int, sub: str) -> None:
    """Call only after a successful render, otherwise the story is lost."""
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO seen(id, score, sub, ts, lang) "
                   "VALUES (?,?,?,?,?)",
                   (post_id, score, sub, time.time(), OUTPUT_LANG))


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
            "INSERT OR REPLACE INTO parts(post_id, n, total, title, body, "
            "gender, voice, sub, ts, done, tries, lang) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,0,?)",
            [(post["id"], i, len(parts), title, body, gender, voice, post["sub"],
              now, OUTPUT_LANG)
             for i, (title, body) in enumerate(parts, 1)])
    log.info("%s: queued %d parts", post["id"], len(parts))


_PART_COLS = ("post_id", "n", "total", "title", "body", "gender", "voice", "sub")


def next_part() -> dict | None:
    """The earliest unpublished part of THIS channel's queue, or None.

    Strictly in order: a part clears only once it is UPLOADED, so part 2 is
    invisible here until part 1 is actually out. That is the whole guard against
    publishing a story's middle before its beginning.

    Scoped to the channel because it decides what the next video is: without
    that, an English run would render the Russian channel's part 2 and publish
    it to an audience that never saw part 1 and could not read it anyway.
    """
    with _db() as db:
        row = db.execute(
            f"SELECT {','.join(_PART_COLS)} FROM parts "
            "WHERE done=0 AND lang=? ORDER BY ts, n LIMIT 1",
            (OUTPUT_LANG,)).fetchone()
    return dict(zip(_PART_COLS, row)) if row else None


def finish_part(post_id: str, n: int) -> None:
    """Call after the part is PUBLISHED, not after it renders.

    A render that succeeds and an upload that then fails must leave the part
    pending: in CI the mp4 dies with the runner, so the only way back is to
    build it again from the text stored here.
    """
    with _db() as db:
        db.execute("UPDATE parts SET done=1 WHERE post_id=? AND n=? AND lang=?",
                   (post_id, n, OUTPUT_LANG))


def drop_parts(post_id: str) -> None:
    """Give up on the rest of a split story, for this channel only.

    Only a send clears a part - see finish_part - so a part nobody can send is
    handed back by next_part() every run, for ever: the same file re-rendered
    while no new story is ever made. Both callers below are that case, one
    where the render keeps failing and one where the platform is not there.

    The other channel's copy is a different set of rows and may be publishing
    fine, hence the lang scope.
    """
    with _db() as db:
        db.execute("UPDATE parts SET done=1 WHERE post_id=? AND lang=?",
                   (post_id, OUTPUT_LANG))


MAX_TRIES = 3


def fail_part(post_id: str, n: int) -> None:
    """Count a failed attempt, and give up on the story after MAX_TRIES.

    Without this a part that can never be rendered holds the queue forever -
    next_part() would keep handing it back and no other video would ever go out.
    Losing the tail of one story beats a channel that quietly stops publishing.
    """
    with _db() as db:
        db.execute("UPDATE parts SET tries=tries+1 "
                   "WHERE post_id=? AND n=? AND lang=?", (post_id, n, OUTPUT_LANG))
        row = db.execute("SELECT tries FROM parts WHERE post_id=? AND n=? AND lang=?",
                         (post_id, n, OUTPUT_LANG)).fetchone()
    if row and row[0] >= MAX_TRIES:
        drop_parts(post_id)
        log.error("%s part %d failed %d times - dropping the rest of the "
                  "story so the queue can move on", post_id, n, row[0])


def multipart_today() -> bool:
    """True if a story was already split today.

    One a day. A feed of nothing but two-parters reads as padding, and each
    part costs an upload slot the ordinary stories need.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    with _db() as db:
        rows = db.execute("SELECT ts FROM parts WHERE n=1 AND lang=?",
                          (OUTPUT_LANG,)).fetchall()
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

    # the pool is ordered by how much of a fight a post is, and both signals
    # have to earn their place: the verdict question and the argument ratio
    assert contested("AITA for kicking out my sister?", 0, 5000) >= 1
    assert contested("WIBTA if I skipped the wedding", 0, 5000) >= 1
    assert contested("Am I wrong for locking the door", 0, 5000) >= 1
    assert contested("My neighbor flooded my flat", 0, 5000) == 0, \
        "a plain story must not score on the title"
    # 1 comment per 10 upvotes is a room taking sides; 1 per 100 is a nod
    assert contested("x", 500, 5000) == 1.0
    assert contested("x", 50, 5000) < 0.2
    assert contested("AITA for this", 500, 5000) > contested("x", 500, 5000), \
        "both signals must add up, not replace each other"
    assert contested("x", 0, 0) == 0, "a scoreless post must not divide by zero"

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

    # the day's viral slot: due until a post from that band is marked used, and
    # an ordinary one must not close it. Skipped when today already spent it -
    # the fixture would then be testing a state the run is not in.
    if viral_due():
        mark_used("_selftest_small", MIN_SCORE, "test")
        assert viral_due(), "an ordinary story must not spend the viral slot"
        mark_used("_selftest_loud", VIRAL_MIN_SCORE, "test")
        assert not viral_due(), "one loud story a day, and today had it"
        with _db() as db:
            db.execute("DELETE FROM seen WHERE id LIKE '_selftest%'")
        assert viral_due()

    # A story spent on one channel must stay available on the other - that is
    # the whole point of keying `seen` by (id, lang), and the failure mode is
    # invisible: the second channel would simply never be offered the post.
    other = "en" if OUTPUT_LANG != "en" else "ru"
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO seen(id, score, sub, ts, lang) "
                   "VALUES (?,?,?,?,?)",
                   ("_selftest_other", MIN_SCORE, "test", time.time(), other))
        mine = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                         (OUTPUT_LANG,))}
        assert "_selftest_other" not in mine, "the other channel's row leaked in"
        db.execute("DELETE FROM seen WHERE id='_selftest_other'")

    # The fallback is the whole point: pullpush answers 502 for hours at a time,
    # and a backup nobody exercises is a backup that is broken when it is needed.
    # So the run below is done twice, once with pullpush forcibly dead.
    real_pullpush = _pullpush
    _pullpush = lambda *a: (_ for _ in ()).throw(urllib.error.URLError("forced"))
    arctic = fetch(3)
    assert arctic, "arctic shift returned nothing - the fallback is dead"
    for p in arctic:
        assert MIN_SCORE <= p["score"] < MAX_SCORE, f"out of band: {p['score']}"
    print(f"fallback ok: {len(arctic)} posts from arctic shift")
    _pullpush = real_pullpush

    posts = fetch(3)
    assert posts, "source returned nothing - check network / SUBREDDITS"
    assert posts == sorted(posts, key=lambda p: p["rank"], reverse=True), \
        "the contested posts must come first"
    for p in posts:
        print(f"[{p['score']:>7}] {p['rank']:.2f} r/{p['sub']} "
              f"{len(p['text']):>5}ch  {p['title'][:60]}")
