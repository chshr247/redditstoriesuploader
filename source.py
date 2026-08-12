"""Step 1: story sourcing. Backends are the pullpush.io and arctic shift
archives: no keys, no approval.

Harvesting and choosing are two different acts here, and the `pool` table is
what separates them. A run picks the best story out of everything harvested so
far; only a run that finds the choice too thin pays to widen it, and then it
pays in bulk. Before that split, one run meant one archive read per sub, and the
run was therefore only ever as good as the window it happened to draw.

We walk each subreddit top-down by score, and the lowest score already harvested
is the cursor, so every refill picks up below the last one instead of re-reading
the same top posts. That only works on pullpush, which has a score index.

pullpush goes down for hours at a time (502s across every sub, and it has been
down more often than up lately), so arctic shift stands behind it - same
archive, same post shape, no key either. It cannot sort or filter by score, only
by time, so it reads a random window of days and the band is filtered here
instead. The pool matters most exactly then: a random window almost never holds
a loud post, but a few hundred windows do.
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
                    MIN_SCORE, OUTPUT_LANG, PLAN_FILE, SUBREDDITS,
                    VIRAL_MIN_SCORE, VIRAL_PER_DAY)

API = "https://api.pullpush.io/reddit/search/submission/"
ARCTIC = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_IDS = "https://arctic-shift.photon-reddit.com/api/posts/ids"
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
    # Candidates harvested ahead of time, so choosing a story and finding one
    # are no longer the same act - see POOL_LOW. Not per language: what has been
    # harvested is available to every channel whose sub list covers it, and
    # `seen` is what says who has already spent it. Rows are kept after use for
    # exactly that reason, so the second channel can still be offered the story.
    db.execute("CREATE TABLE IF NOT EXISTS pool("
               "id TEXT PRIMARY KEY, sub TEXT, score INT, comments INT, "
               "ratio REAL, title TEXT, ts REAL)")
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

# The third signal, and the only one that is a measurement rather than a guess:
# upvote_ratio is the share of votes that were up, i.e. how much of the sub
# disagreed with the post itself. It rides along in every archive response and
# costs nothing to read. Measured on a settled window of r/AmItheAsshole,
# 2026-08-12: the story everyone agreed with sat at 0.96-0.98, and the one that
# drew 897 comments on 1054 upvotes - the fight - sat at 0.85.
#
# Below AGREED_AT the signal starts counting and it saturates at DISPUTED_AT.
# A missing or zero ratio scores nothing rather than something: pullpush rows
# and old archive dumps do not always carry the field, and an absent measurement
# must not read as a calm one.
AGREED_AT, DISPUTED_AT = 0.95, 0.70


def _disputed(ratio: float | None) -> float:
    """0..1, how far this post's upvote ratio is into fight territory."""
    if not ratio:
        return 0.0
    return min(max((AGREED_AT - ratio) / (AGREED_AT - DISPUTED_AT), 0.0), 1.0)


def contested(title: str, comments: int, score: int,
              ratio: float | None = None) -> float:
    """0..3, higher when a post looks like a fight rather than a verdict."""
    return (bool(CONTESTED.search(title))
            + min(comments * ARGUMENTATIVE / max(score, 1), 1.0)
            + _disputed(ratio))


# How thin the choice may get before a run pays to widen it, and how many
# archive reads it may pay. Measured 2026-08-12, twelve two-day windows of
# r/AmItheAsshole spread over four years: 166-253 posts each, of which 0-5
# cleared 3000 and NONE cleared 25000. One window per run is therefore not a
# pool, it is a coin flip - and the loud band cannot be reached by flipping it.
# The pool is what turns "what did this window happen to hold" into "the best of
# everything harvested so far", and it is also the only way the viral slot ever
# fills while pullpush is down.
POOL_LOW = 40        # unused rows this channel can still choose from
POOL_WINDOWS = 8     # archive reads one refill is allowed to spend

# One refill per process. A run asks the pool twice - once for the loud band and
# once for the ordinary one - and if the first refill did not lift it above the
# mark (every archive down, or the subs simply spent), the second must not pay
# for the same eight requests again.
_refilled = False


def _cursor(db, sub: str) -> int:
    """Where pullpush's downward walk resumes for this sub.

    The weakest score already HARVESTED, not the weakest already used: the pool
    is the frontier now, and a walk keyed on `seen` would re-fetch everything
    harvested but not yet spent. Deliberately not per language either - what has
    been fetched has been fetched, whoever ends up telling it.
    """
    (low,) = db.execute(
        "SELECT MIN(score) FROM (SELECT score FROM pool WHERE sub=? "
        "UNION ALL SELECT score FROM seen WHERE sub=?)", (sub, sub)).fetchone()
    return low or 10_000_000


def _store(db, sub: str, posts: list[dict]) -> int:
    """Put the usable posts of one archive read into the pool, return how many.

    The body is filtered on here and then dropped: it is the only large field,
    seen.db is committed on every run, and a pool that carried 2 KB of text per
    row would put megabytes of binary churn into the history. What survives is
    an index - id, band, and the two signals ranking needs. _bodies() fetches
    the text back for the handful of rows a run actually looks at.
    """
    rows = []
    for p in posts:
        if p.get("score", 0) < MIN_SCORE or not _usable(p):
            continue
        title, text = _clean(p["title"]), _clean(p["selftext"])
        # cheapest possible gate: reject here and the story never costs an LLM call
        if hit := safety.blocked(title, text):
            log.info("skipping %s (%s)", p["id"], hit)
            continue
        rows.append((p["id"], sub, p["score"], p.get("num_comments", 0),
                     p.get("upvote_ratio") or 0.0, title, time.time()))
    before = db.total_changes
    db.executemany("INSERT OR IGNORE INTO pool(id, sub, score, comments, ratio, "
                   "title, ts) VALUES (?,?,?,?,?,?,?)", rows)
    return db.total_changes - before


def refill(db, windows: int = POOL_WINDOWS) -> int:
    """Harvest into the pool: `windows` archive reads spread over the subs.

    Shuffled, so a tie in the ranking does not always fall the same way -
    twenty-six of the first twenty-seven stories came from one sub back when the
    order was fixed. pullpush is asked first because its walk is ordered by
    score and therefore reaches the loud band on purpose; arctic shift only ever
    reaches it by luck, which is what the measurement above is about.
    """
    added = 0
    subs = random.sample(SUBREDDITS, len(SUBREDDITS))
    for i in range(windows):
        sub = subs[i % len(subs)]
        top = _cursor(db, sub)
        if top <= MIN_SCORE:
            log.info("r/%s: nothing left above %d, trying another sub", sub, MIN_SCORE)
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
        got = _store(db, sub, posts)
        log.info("r/%s: %d new candidates out of %d", sub, got, len(posts))
        added += got
    db.commit()
    return added


def _bodies(ids: list[str]) -> dict[str, dict]:
    """The full posts behind a handful of pool rows, in one request for all.

    Raises the DOWN family when neither archive answers: a run that cannot read
    the bodies has nothing to tell, and that is the same failure as an empty
    pool, not a reason to spend a story on a half-read post.
    """
    joined = ",".join(ids)
    try:
        data = _api(ARCTIC_IDS, ids=joined)
    except DOWN as e:
        log.warning("arctic shift unavailable (%s), trying pullpush", e)
        data = _api(API, ids=joined)
    return {p["id"]: p for p in data}


def _pick(limit: int, floor: int, ceiling: int) -> list[dict]:
    """Up to `limit` unused pool stories inside [floor, ceiling), best first.

    "Best" is contested() - the argument, not the score. Nothing here spreads
    the answer across subs on purpose; that is refill()'s job, which shuffles
    and reads every sub in turn. A cap here would be theatre anyway - a run
    takes the FIRST story the prompt accepts, so the only row that really
    matters is the top one.
    """
    global _refilled
    db = _db()
    subs = ",".join("?" * len(SUBREDDITS))
    unused = (f"SELECT id, sub, score, comments, ratio, title FROM pool "
              f"WHERE sub IN ({subs}) AND score >= ? AND score < ? AND id NOT IN "
              "(SELECT id FROM seen WHERE lang=?)")
    rows = db.execute(unused, (*SUBREDDITS, floor, ceiling, OUTPUT_LANG)).fetchall()

    # Two reasons to go harvesting, and the band one is not covered by the
    # count: a pool holding two hundred ordinary stories and no loud one is
    # comfortably above the mark and still has nothing to answer the viral slot
    # with. Without this the loud band would go quiet for good the moment the
    # ordinary one filled up.
    if not _refilled:
        (left,) = db.execute(
            f"SELECT COUNT(*) FROM pool WHERE sub IN ({subs}) AND id NOT IN "
            "(SELECT id FROM seen WHERE lang=?)",
            (*SUBREDDITS, OUTPUT_LANG)).fetchone()
        if not rows or left < POOL_LOW:
            log.info("pool: %d left to choose from, %d of them in [%d, %d) - "
                     "refilling", left, len(rows), floor, ceiling)
            _refilled = True
            log.info("pool: +%d rows", refill(db))
            rows = db.execute(unused,
                              (*SUBREDDITS, floor, ceiling, OUTPUT_LANG)).fetchall()

    ranked = [{"id": pid, "sub": sub, "score": score, "title": title,
               "rank": contested(title, comments, score, ratio)}
              for pid, sub, score, comments, ratio, title in
              sorted(rows, key=lambda r: contested(r[5], r[3], r[2], r[4]),
                     reverse=True)[:limit]]
    if not ranked:
        log.info("pool: nothing in [%d, %d) for this channel", floor, ceiling)
        db.close()
        return []

    try:
        full = _bodies([p["id"] for p in ranked])
    except DOWN as e:
        log.error("no archive available for the bodies (%s) - nothing this run", e)
        db.close()
        return []

    out = []
    for p in ranked:
        raw = full.get(p["id"])
        # Gone since it was harvested, or edited down to nothing. Drop the row:
        # left in place it would rank just as high next run and cost the same
        # request again.
        if raw is None or not _tellable(raw):
            log.info("%s is gone or can no longer be told, dropping it", p["id"])
            db.execute("DELETE FROM pool WHERE id=?", (p["id"],))
            continue
        title, text = _clean(raw["title"]), _clean(raw["selftext"])
        if hit := safety.blocked(title, text):
            log.info("skipping %s (%s)", p["id"], hit)
            db.execute("DELETE FROM pool WHERE id=?", (p["id"],))
            continue
        out.append({**p, "title": title, "text": text})
    db.commit()
    db.close()
    return out


def fetch(limit: int = 3) -> list[dict]:
    """Return up to `limit` unused stories from the ordinary band."""
    return _pick(limit, MIN_SCORE, MAX_SCORE)


def fetch_viral(limit: int = 3) -> list[dict]:
    """The same, from above the MAX_SCORE ceiling: the day's one loud story.

    Deliberately not filtered any further. What made a post reach the viral
    floor is the point of taking it, so the choice of which loud story works is
    left to the prompt's own SKIP gate, which throws out news and meta anyway.
    """
    return _pick(limit, VIRAL_MIN_SCORE, 10_000_000)


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


# ------------------------------------------------------------------- the plan
#
# Everything above chooses the next story; this chooses nothing. A channel with
# a plan file publishes the stories written in it, in the order they are written
# in, and the archive is asked for one post at a time by id rather than searched
# at all. The bands, the cursor, contested() and the viral slot all go quiet -
# they are ways of picking, and the picking is already done.

# One row of the schedule table in plan_<channel>.md:
#   | 2 | «Ты здесь не начальник» ... | 1/3 | [`istlsy`](https://redd.it/istlsy) | ...
# Three of its seven columns are load-bearing - the slot number, the part
# marker, and the post id. The rest is there for the person reading the file.
PLAN_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|[^|]*\|\s*(\d+)\s*/\s*(\d+)\s*\|\s*\[`(\w+)`\]"
    r"|^\|\s*(\d+)\s*\|[^|]*\|\s*[-–—]\s*\|\s*\[`(\w+)`\]", re.M)


def plan() -> list[dict]:
    """The running order as [{id, parts}], or [] when this channel has none.

    A malformed plan raises rather than publishing some readable subset of
    itself: the file exists to fix an order, and an order with a hole in it is
    the one thing it must not quietly become.
    """
    if not PLAN_FILE.exists():
        return []
    out, slot, run = [], 0, 0        # `run` counts the rows of the story open now
    for m in PLAN_ROW.finditer(PLAN_FILE.read_text(encoding="utf-8")):
        n, part, total, pid = m.group(1), m.group(2), m.group(3), m.group(4)
        if pid is None:                       # the single-video branch
            n, pid, part, total = m.group(5), m.group(6), "1", "1"
        slot, part, total = slot + 1, int(part), int(total)
        if int(n) != slot:
            raise ValueError(f"{PLAN_FILE.name}: row {slot} is numbered {n} - "
                             "the slots must run 1..N with no holes or repeats")
        if part == 1:
            if run and run != out[-1]["parts"]:
                raise ValueError(f"{PLAN_FILE.name}: {out[-1]['id']} promises "
                                 f"{out[-1]['parts']} parts but has {run} rows")
            if any(e["id"] == pid for e in out):
                raise ValueError(f"{PLAN_FILE.name}: {pid} appears twice - a "
                                 "story is told once, and its parts run together")
            out.append({"id": pid, "parts": total})
            run = 1
            continue
        # a later part continues the story directly above it, and nothing else
        if not out or out[-1]["id"] != pid or out[-1]["parts"] != total \
                or part != run + 1:
            raise ValueError(f"{PLAN_FILE.name}: row {slot} is {pid} part "
                             f"{part}/{total}, which does not follow the row "
                             "above it")
        run = part
    if run and run != out[-1]["parts"]:
        raise ValueError(f"{PLAN_FILE.name}: {out[-1]['id']} promises "
                         f"{out[-1]['parts']} parts but has {run} rows")
    return out


def plan_left() -> int:
    """How many of the plan's stories this channel has not published yet."""
    entries = plan()
    if not entries:
        return 0
    with _db() as db:
        seen = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                         (OUTPUT_LANG,))}
    return sum(1 for e in entries if e["id"] not in seen)


def _by_id(post_id: str) -> dict | None:
    """One post from the archive by id. None when the archive has no such post.

    Raises the DOWN family when neither backend answers, and that difference
    matters: a post that is gone is skipped and the plan moves on, while an
    archive that is down must leave the plan exactly where it was.
    """
    try:
        data = _api(ARCTIC_IDS, ids=post_id)
    except DOWN as e:
        log.warning("%s: arctic shift unavailable (%s), trying pullpush",
                    post_id, e)
        data = _api(API, ids=post_id)
    return data[0] if data else None


# What the plan path checks instead of _usable(). A planned story was chosen by
# hand, so the discovery heuristics have already been overruled: MIN_COMMENTS
# and CONTESTED are ways of guessing whether a story is worth telling, and
# `locked` and `stickied` describe the state of the THREAD - a comment lock says
# nothing about the story under it, and three of plan_ru.md's stories are locked.
# What is left is what makes a video impossible rather than unpromising.
def _tellable(p: dict) -> bool:
    text = p.get("selftext") or ""
    return (not p.get("over_18")
            and text not in ("[removed]", "[deleted]")
            and MIN_CHARS <= len(text) <= MAX_CHARS)


def next_planned() -> dict | None:
    """The next story the plan calls for, ready for script.write_script().

    Carries `parts` - how many videos the plan says this story is - so the
    split is the one that was planned rather than one recomputed from the
    length. None means the plan has nothing to give this run: either it is
    finished, or the archive is not answering, and those are told apart by
    plan_left() rather than here.

    A post that has since been deleted, or that trips the blocklist, is burned
    on the spot and the plan moves to the next one - it can never be published,
    and left in place it would hold the whole order behind it for ever.
    """
    entries = plan()
    if not entries:
        return None
    with _db() as db:
        seen = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                         (OUTPUT_LANG,))}

    for e in entries:
        if e["id"] in seen:
            continue
        try:
            p = _by_id(e["id"])
        except DOWN as err:
            log.error("plan: no archive available for %s (%s) - the order "
                      "waits rather than skipping it", e["id"], err)
            return None
        if p is None or not _tellable(p):
            log.error("plan: %s is gone or can no longer be told, dropping it",
                      e["id"])
            mark_used(e["id"], p.get("score", 0) if p else 0,
                      p.get("subreddit", "?") if p else "?")
            continue
        title, text = _clean(p["title"]), _clean(p["selftext"])
        hit = safety.blocked(title, text)
        if hit:
            log.error("plan: %s trips the blocklist (%s), dropping it",
                      e["id"], hit)
            mark_used(e["id"], p["score"], p["subreddit"])
            continue
        return {"id": p["id"], "sub": p["subreddit"], "score": p["score"],
                "title": title, "text": text, "parts": e["parts"],
                "rank": contested(title, p.get("num_comments", 0), p["score"],
                                  p.get("upvote_ratio"))}
    return None


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

    # The ranking. Ordering is the whole job here - main.py takes the first
    # story the prompt accepts, so whatever sorts to the top IS the choice.
    assert _disputed(None) == _disputed(0) == 0.0, "an absent ratio is not a calm one"
    assert _disputed(0.98) == 0.0 and _disputed(0.70) == 1.0
    assert _disputed(0.60) == 1.0, "past the far end it saturates, not overflows"
    assert abs(_disputed(0.85) - 0.4) < 0.01, _disputed(0.85)
    # the pair actually measured on 2026-08-12: 1054 upvotes with 897 comments
    # at 0.85 is the fight, 7555 with 862 at 0.96 is the story everyone shared
    assert (contested("AITA for spraying my BIL", 897, 1054, 0.85)
            > contested("AITA for spilling my wine", 862, 7555, 0.96)), \
        "the argued-over story has to outrank the agreed-with one"

    # The pool: what a run chooses from. Rows are planted rather than harvested,
    # since what is being tested is the reading - the band, a story already
    # spent in THIS language, the order, and a row whose post has gone since.
    _real = (SUBREDDITS, _refilled, _bodies)
    globals()["SUBREDDITS"] = ["_selftest_sub", "_selftest_other"]
    globals()["_refilled"] = True                 # no archive is to be touched
    globals()["_bodies"] = lambda ids: {
        i: {"id": i, "selftext": "x" * 500, "over_18": False, "title": "A story"}
        for i in ids if i != "_sp_gone"}
    _planted = [("_sp_quiet", "_selftest_sub", MIN_SCORE + 10, 20, 0.99),
                ("_sp_fight", "_selftest_sub", MIN_SCORE + 10, 900, 0.72),
                ("_sp_spent", "_selftest_sub", MIN_SCORE + 10, 900, 0.72),
                ("_sp_gone", "_selftest_other", MIN_SCORE + 10, 50, 0.72),
                ("_sp_loud", "_selftest_other", VIRAL_MIN_SCORE + 1, 900, 0.72)]
    with _db() as db:
        db.executemany("INSERT OR REPLACE INTO pool(id, sub, score, comments, "
                       "ratio, title, ts) VALUES (?,?,?,?,?,?,?)",
                       [(*row, "A story", time.time()) for row in _planted])
        db.execute("INSERT OR REPLACE INTO seen(id, score, sub, ts, lang) "
                   "VALUES ('_sp_spent', 0, '_selftest_sub', 0, ?)", (OUTPUT_LANG,))

    got = [p["id"] for p in _pick(3, MIN_SCORE, MAX_SCORE)]
    assert got == ["_sp_fight", "_sp_quiet"], got
    assert [p["id"] for p in _pick(1, MIN_SCORE, MAX_SCORE)] == ["_sp_fight"], \
        "the argued-over row has to come first"
    assert [p["id"] for p in _pick(3, VIRAL_MIN_SCORE, 10_000_000)] == ["_sp_loud"], \
        "the loud band answers on its own"
    with _db() as db:
        (left,) = db.execute("SELECT COUNT(*) FROM pool WHERE id='_sp_gone'").fetchone()
        assert left == 0, "a post that has gone must not be ranked again"
        db.execute("DELETE FROM pool WHERE id LIKE '_sp_%'")
        db.execute("DELETE FROM seen WHERE id LIKE '_sp_%'")
    globals()["SUBREDDITS"], globals()["_refilled"], globals()["_bodies"] = _real
    print("pool and ranking ok")

    # The plan reader. It decides publication ORDER, so a plan it reads wrong
    # is a plan that is not being followed - and nothing downstream would say
    # so. Every shape it must accept and every one it must refuse is here.
    import tempfile
    from pathlib import Path
    _real_plan_file = PLAN_FILE
    _tmp = Path(tempfile.gettempdir()) / "_selftest_plan.md"
    HEAD = "| № | Заголовок | Части | Пост | Саб | Score | Знаков |\n|--:|---|:--:|---|---|--:|--:|\n"

    def _plan(rows: str):
        _tmp.write_text(HEAD + rows, encoding="utf-8")
        globals()["PLAN_FILE"] = _tmp
        return plan()

    def _refuses(rows: str, why: str):
        try:
            _plan(rows)
        except ValueError:
            return
        raise AssertionError(why)

    ok = _plan(
        "| 1 | Один ролик | — | [`aaa`](https://redd.it/aaa) | r/tifu | 5000 | 900 |\n"
        "| 2 | Первая часть | 1/2 | [`bbb`](https://redd.it/bbb) | r/tifu | 6000 | 2500 |\n"
        "| 3 | Вторая часть | 2/2 | [`bbb`](https://redd.it/bbb) | r/tifu | 6000 | 2500 |\n"
        "| 4 | Ещё один | — | [`ccc`](https://redd.it/ccc) | r/tifu | 4000 | 800 |\n")
    assert ok == [{"id": "aaa", "parts": 1}, {"id": "bbb", "parts": 2},
                  {"id": "ccc", "parts": 1}], ok
    # a hole in the numbering is a row that was deleted by hand, and the rows
    # after it are then in an order nobody chose
    _refuses("| 1 | a | — | [`aaa`](https://redd.it/aaa) |\n"
             "| 3 | b | — | [`bbb`](https://redd.it/bbb) |\n", "a gap must fail")
    # a story whose parts do not run together would publish its middle later
    _refuses("| 1 | a | 1/2 | [`bbb`](https://redd.it/bbb) |\n"
             "| 2 | b | — | [`aaa`](https://redd.it/aaa) |\n"
             "| 3 | c | 2/2 | [`bbb`](https://redd.it/bbb) |\n",
             "split parts must fail")
    # ...and one that promises three and lists two would lose its ending
    _refuses("| 1 | a | 1/3 | [`bbb`](https://redd.it/bbb) |\n"
             "| 2 | b | 2/3 | [`bbb`](https://redd.it/bbb) |\n",
             "a short story must fail")
    _refuses("| 1 | a | — | [`aaa`](https://redd.it/aaa) |\n"
             "| 2 | b | — | [`aaa`](https://redd.it/aaa) |\n",
             "a repeated post must fail")
    # A planned story is exempt from the discovery filters but not from what
    # would make it unrenderable. A comment lock is the first kind; an empty
    # body is the second, and three of plan_ru.md's stories are locked.
    _base = {"selftext": "x" * 500, "over_18": False, "num_comments": 500,
             "title": "A story"}
    assert _tellable({**_base, "locked": True}) and not _usable({**_base, "locked": True})
    assert _tellable({**_base, "num_comments": 2}), "the plan already chose it"
    assert not _tellable({**_base, "selftext": "[removed]"})
    assert not _tellable({**_base, "over_18": True})
    assert not _tellable({**_base, "selftext": "x" * (MAX_CHARS + 1)})

    # no file at all is not an error - it is a channel without a plan
    globals()["PLAN_FILE"] = _tmp.with_name("_no_such_plan.md")
    assert plan() == [] and plan_left() == 0
    _tmp.unlink()
    globals()["PLAN_FILE"] = _real_plan_file
    if plan():
        print(f"plan: {len(plan())} stories, {sum(e['parts'] for e in plan())} "
              f"videos, {plan_left()} still to publish")

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
