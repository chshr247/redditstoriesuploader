"""Step 1: story sourcing. Three backends, tried in this order:

    redditapis.com   a paid proxy over the LIVE reddit API. The only one that
                     can ask a sub for its top posts, and the only one whose
                     numbers are today's rather than a crawl's snapshot.
    pullpush.io      an archive with a score index. Free, and 502 for days.
    arctic shift     an archive with no score index at all. Free, and the last
                     resort: it can only read a window of days and filter here.

The two archives are the emergency route. Both store the score as it stood when
they crawled the post, which for most windows is minutes after it was posted -
see config.REDDITAPIS_KEY for the measurement.

Harvesting and choosing are two different acts here, and the `pool` table is
what separates them. A run picks the best story out of everything harvested so
far; only a run that finds the choice too thin pays to widen it, and then it
pays in bulk. Before that split, one run meant one archive read per sub, and the
run was therefore only ever as good as the window it happened to draw.

Which posts a refill asks for depends on who answers. The live proxy is asked
for a sub's top of a drawn time window; pullpush is walked top-down by score,
with the lowest score already harvested as the cursor; arctic shift can only be
handed a window of days, and the band is filtered here afterwards.
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
from config import (DAILY_FILE, DB_PATH, DEFAULT_CHANNEL, LOUD_AT, MIN_COMMENTS,
                    MIN_SCORE, OUTPUT_LANG, PLAN_FILE, REDDITAPIS_KEY, SUBREDDITS,
                    SUBREDDITS_HORROR, TIKTOK_ENABLED)

REDDITAPIS = "https://api.redditapis.com"
API = "https://api.pullpush.io/reddit/search/submission/"
ARCTIC = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCTIC_IDS = "https://arctic-shift.photon-reddit.com/api/posts/ids"
UA = "StoryReader/0.1"
BATCH = 100                        # pullpush per-request cap
TOP_WINDOWS = ("week", "month", "year", "all")   # what the live proxy sorts by
THIN = 20                          # below this a window is not worth the call
MIN_CHARS, MAX_CHARS = 400, 4000   # ~40 sec .. ~4 min of narration
# The horror slot tells one story per video at up to config.HORROR_SEC, so its
# source is allowed to be longer than a feed post. 4000 characters is about
# five minutes told in full, which means the ceiling is only ever reachable
# above that - with the feed's cap the slot would top out at four and a half
# minutes and only one story in eight would even get there. Past this a post
# has to be compressed more than two to one, and compression is what takes the
# detail the genre lives on.
HORROR_CHARS = 9000
# How many horror candidates one day's slot may try before giving up. Four,
# which is what the pool path already asks for, and the ceiling matters only on
# the days every one of them is refused - a SKIP costs one short LLM call.
HORROR_TRIES = 4
ARCTIC_DAYS = 2                    # width of the window arctic shift reads
ARCTIC_BACK = 4 * 365              # ...taken from anywhere in the last 4 years

log = logging.getLogger(__name__)


def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, score INT, sub TEXT)")
    # When the post was used, added later: a daily count has to know whether
    # today already had one, and a row on its own cannot say. Rows written
    # before this exists keep ts NULL, which reads as "not today" - correct for
    # every one of them, since a run that old is over.
    if "ts" not in {c[1] for c in db.execute("PRAGMA table_info(seen)")}:
        db.execute("ALTER TABLE seen ADD COLUMN ts REAL")
    # A story split across videos: the TEXT of every part, written in one LLM
    # call and kept here until each part is published. It has to be text and not
    # files - a CI run throws out/ away with the runner, so part 2 is rendered
    # in the run that publishes it, hours after part 1 was written.
    db.execute("CREATE TABLE IF NOT EXISTS parts("
               "post_id TEXT, n INT, total INT, title TEXT, body TEXT, "
               "gender TEXT, voice TEXT, sub TEXT, ts REAL, "
               "done INT DEFAULT 0, tries INT DEFAULT 0, issue INT DEFAULT 0, "
               "PRIMARY KEY(post_id, n))")
    # The issue the story's title was chosen on, carried so every part's caption
    # lands back in it - one story is one case, and a split one would otherwise
    # open a fresh issue per part. Added later; rows written before this keep 0,
    # which reads as "no issue" and falls back to opening one.
    if "issue" not in {c[1] for c in db.execute("PRAGMA table_info(parts)")}:
        db.execute("ALTER TABLE parts ADD COLUMN issue INT DEFAULT 0")
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


def _api(base: str, envelope: str = "data", headers: dict | None = None,
         **params):
    """GET one page of posts. Both archives answer {"data": [...]}; the live
    proxy answers {"posts": [...]} and wants a bearer token, hence the two
    arguments in front."""
    url = base + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                left = r.headers.get("X-Credits-Remaining")
                if left and float(left) < 0.05:
                    log.warning("redditapis credit is down to $%s", left)
                return json.load(r)[envelope]
        except urllib.error.HTTPError as e:
            # all three throttle bursts; a short wait clears it
            if e.code != 429 or attempt == 2:
                raise
            wait = 5 * (attempt + 1)
            log.info("429 from %s, retrying in %ds", base, wait)
            time.sleep(wait)


# Anything that means "the archive is not answering right now" - the next
# backend gets a turn, and if there is none the sub is skipped for this run.
DOWN = (urllib.error.URLError, TimeoutError, json.JSONDecodeError)


def _redditapi(path: str, envelope: str = "posts", **params) -> list[dict]:
    """One call to the live proxy. Raises DOWN when it will not answer.

    An HTTPError is a URLError, so 402-out-of-credit and 5xx both land in the
    DOWN family and the caller falls through to the archives on their own.
    """
    if not REDDITAPIS_KEY:
        raise urllib.error.URLError("no REDDITAPIS_KEY set")
    return _api(REDDITAPIS + path, envelope,
                {"Authorization": f"Bearer {REDDITAPIS_KEY}"}, **params)


def _mapped(p: dict, sub: str) -> dict:
    """The proxy's field names in the shape the rest of this module reads.

    Their JSON is close to reddit's but not identical - upvotes, text and
    comments where the archives say score, selftext and num_comments - so the
    translation happens once, here, rather than in every reader downstream.
    """
    return {**p, "score": p.get("upvotes") or 0,
            "num_comments": p.get("comments") or 0,
            "selftext": p.get("text") or "",
            "subreddit": p.get("subreddit") or sub}


def _live(sub: str) -> list[dict]:
    """This sub's top posts for one time window, live.

    The window is drawn rather than fixed: `all` is the same hundred posts for
    ever and would be spent in a week, while `week` alone would never surface
    the years of good material behind it. Rotating them keeps both in reach and
    lets `seen` do the forgetting.

    A slow sub answers a narrow window with almost nothing - r/ProRevenge
    returned 0 posts for top/month and r/TalesFromRetail 2 for top/week, three
    of eight calls in one refill - so a thin answer is widened to top/all rather
    than left as a spent call. It is only ever one retry: `all` is the widest
    there is, and a sub that has nothing to say there has nothing to say.
    """
    window = random.choice(TOP_WINDOWS)
    posts = _redditapi(f"/api/reddit/sub/{sub}/top", t=window, limit=100)
    if len(posts) < THIN and window != "all":
        log.info("r/%s: %d posts from top/%s, widening to top/all",
                 sub, len(posts), window)
        window = "all"
        posts = _redditapi(f"/api/reddit/sub/{sub}/top", t=window, limit=100)
    log.info("r/%s: %d posts from top/%s (live)", sub, len(posts), window)
    return [_mapped(p, sub) for p in posts]


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


def _cap(sub: str) -> int:
    """The length ceiling for a post from `sub`. See HORROR_CHARS."""
    return HORROR_CHARS if sub in SUBREDDITS_HORROR else MAX_CHARS


def _linked(p: dict) -> bool:
    """True when the archive says this post is a link, a gallery or a video.

    Every filter here reads the TEXT, and a photo post has text: the caption
    under it. r/Paranormal is half gallery posts, their captions run 460 to
    2600 characters, and every one of them clears MIN_CHARS, ranks at the top
    of the sub on score - the photo is what the karma is for - and is not a
    story anyone can narrate. Only the model could say so, and by then the post
    had cost an LLM call and burned the day's horror slot, which is spent by
    the pick and not by the video: thirteen posts between 2026-08-21 and 08-27
    and not one scare made. All six that could still be looked up afterwards
    were is_self=False, five of them galleries - 1ljtrhk, "Blood-colored
    footprints keep appearing on my garage", among them.

    Asked from the positive answer only. Three backends fill these fields and
    a missing one arrives as None, which is "the archive did not say" and not
    "this is a link" - read the other way round, a backend that omits is_self
    would empty the pool on the next refill.
    """
    return (p.get("is_self") is False
            or bool(p.get("is_gallery")) or bool(p.get("is_video")))


def _usable(p: dict, sub: str = "") -> bool:
    text = p.get("selftext") or ""
    return (
        not p.get("over_18")
        and not p.get("stickied") and not p.get("locked")
        and not _linked(p)
        and not CONTEXT_BOUND.match(p.get("title") or "")
        and p.get("num_comments", 0) >= MIN_COMMENTS
        and text not in ("[removed]", "[deleted]")
        and MIN_CHARS <= len(text) <= _cap(sub or p.get("subreddit", ""))
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
    """0..4, higher when a post looks worth telling.

    The fourth term is the one that used to be a separate fetch path and a
    daily slot. It has to exist in some form: the second term divides comments
    BY score, so without it the loudest story of the year - 44832 upvotes and
    2331 comments - sorted below an ordinary 1500-upvote AITA post and would
    never have been picked at all. As a term it lifts that story into the
    middle of the list instead of the bottom, without letting it beat a real
    fight outright, which is what a dedicated slot did.
    """
    return (bool(CONTESTED.search(title))
            + min(comments * ARGUMENTATIVE / max(score, 1), 1.0)
            + _disputed(ratio)
            + min(score / LOUD_AT, 1.0))


# How thin the choice may get before a run pays to widen it, and how many
# archive reads it may pay. Measured 2026-08-12, twelve two-day windows of
# r/AmItheAsshole spread over four years: 166-253 posts each, of which 0-5
# cleared 3000 and NONE cleared 25000. One window per run is therefore not a
# pool, it is a coin flip - and the loud band cannot be reached by flipping it.
# The pool is what turns "what did this window happen to hold" into "the best of
# everything harvested so far", and it is also the only way a loud post is ever
# found at all while pullpush is down.
POOL_LOW = 40        # unused rows this channel can still choose from
POOL_WINDOWS = 8     # archive reads one refill is allowed to spend

# One refill per process PER POOL. A run asks a pool twice - once for the loud
# band and once for the ordinary one - and if the first refill did not lift it
# above the mark (every archive down, or the subs simply spent), the second must
# not pay for the same eight requests again. Keyed by the sub list rather than a
# single flag because there are two pools now: the horror slot is asked for
# first, and one flag would let its refill spend the run's only harvest and
# leave the feed - which is every other slot in the day - unstocked.
_refilled: set[tuple[str, ...]] = set()


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
        if p.get("score", 0) < MIN_SCORE or not _usable(p, sub):
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


def refill(db, windows: int = POOL_WINDOWS, subs: list[str] | None = None) -> int:
    """Harvest into the pool: `windows` archive reads spread over `subs`.

    Shuffled, so a tie in the ranking does not always fall the same way -
    twenty-six of the first twenty-seven stories came from one sub back when the
    order was fixed.

    The horror list is harvested by its own call with its own budget and is
    never mixed in here: eight reads spread over both lists would thin the feed
    on every run to keep one slot a day stocked.
    """
    # None rather than SUBREDDITS as the default: a default is bound once, at
    # import, and the self-test swaps the module global to plant its own subs.
    subs = subs or SUBREDDITS
    added = 0
    subs = random.sample(subs, len(subs))
    for i in range(windows):
        sub = subs[i % len(subs)]
        try:
            posts = _live(sub)
        except DOWN as e:
            # Both archives are the emergency route now, and in this order: the
            # score index first, the random window last. Neither can be trusted
            # on the numbers - see config.REDDITAPIS_KEY - so this is about
            # having something rather than having something good.
            log.warning("r/%s: live proxy unavailable (%s), falling back", sub, e)
            top = _cursor(db, sub)
            if top <= MIN_SCORE:
                log.info("r/%s: nothing left above %d, trying another sub",
                         sub, MIN_SCORE)
                continue
            try:
                posts = _pullpush(sub, top)
            except DOWN as e:
                log.warning("r/%s: pullpush unavailable (%s), trying arctic shift",
                            sub, e)
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
        # up to 100 in one call, and the numbers come back current, so a row
        # harvested weeks ago is re-read at today's score rather than at the
        # one it was stored with
        posts = _redditapi("/api/reddit/by_id/"
                           + ",".join(f"t3_{i}" for i in ids))
        return {p["id"]: _mapped(p, p.get("subreddit", "")) for p in posts}
    except DOWN as e:
        log.warning("live proxy unavailable (%s), falling back", e)
    try:
        data = _api(API, ids=joined)
    except DOWN as e:
        log.warning("pullpush unavailable (%s), trying arctic shift", e)
        data = _api(ARCTIC_IDS, ids=joined)
    return {p["id"]: p for p in data}


def _pick(limit: int, subs: list[str] | None = None) -> list[dict]:
    """Up to `limit` unused stories from `subs`, best first.

    "Best" is contested() - the argument, not the score. Nothing here spreads
    the answer across subs on purpose; that is refill()'s job, which shuffles
    and reads every sub in turn. A cap here would be theatre anyway - a run
    takes the FIRST story the prompt accepts, so the only row that really
    matters is the top one.
    """
    subs = subs or SUBREDDITS       # see refill() on why not a default argument
    db = _db()
    marks = ",".join("?" * len(subs))
    unused = (f"SELECT id, sub, score, comments, ratio, title FROM pool "
              f"WHERE sub IN ({marks}) AND id NOT IN "
              "(SELECT id FROM seen WHERE lang=?)")
    rows = db.execute(unused, (*subs, OUTPUT_LANG)).fetchall()

    if tuple(subs) not in _refilled and len(rows) < POOL_LOW:
        log.info("pool: %d left to choose from, refilling", len(rows))
        _refilled.add(tuple(subs))
        log.info("pool: +%d rows", refill(db, subs=subs))
        rows = db.execute(unused, (*subs, OUTPUT_LANG)).fetchall()

    ranked = [{"id": pid, "sub": sub, "score": score, "title": title,
               "comments": comments, "ratio": ratio,
               "rank": contested(title, comments, score, ratio)}
              for pid, sub, score, comments, ratio, title in
              sorted(rows, key=lambda r: contested(r[5], r[3], r[2], r[4]),
                     reverse=True)[:limit]]
    if not ranked:
        log.info("pool: nothing left for this channel")
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
        # The body came back with today's numbers next to it. Write them back:
        # a row harvested from an archive carries whatever the crawl saw, often
        # minutes after the post went up, and left alone it would rank on that
        # number for as long as it sits here. Each field falls back to what the
        # row already held - a backend that omits one must not blank it.
        score = raw.get("score") or p["score"]
        db.execute("UPDATE pool SET score=?, comments=?, ratio=? WHERE id=?",
                   (score, raw.get("num_comments") or p["comments"],
                    raw.get("upvote_ratio") or p["ratio"], p["id"]))
        out.append({**p, "score": score, "title": title, "text": text})
    db.commit()
    db.close()
    return out


def fetch(limit: int = 3) -> list[dict]:
    """Return up to `limit` unused stories, the most promising first."""
    return _pick(limit)


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
# at all. The pool, the cursor and contested() all go quiet - they are ways of
# picking, and the picking is already done.

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
    # _linked() here as well as in _usable(), and it earns its place twice: the
    # pool was filled before that check existed, so it still holds gallery rows
    # that _usable() will never see again. This is where the body comes back,
    # and a row that fails here is DELETED from the pool rather than marked
    # used - so the ones already in there are cleaned out as they surface, at
    # no cost to the day they surfaced on.
    text = p.get("selftext") or ""
    return (not p.get("over_18") and not _linked(p)
            and text not in ("[removed]", "[deleted]")
            and MIN_CHARS <= len(text) <= _cap(p.get("subreddit", "")))


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


# ------------------------------------------------------------- the daily reserve
#
# One slot a day from a hand-picked list, the rest of the day left to the pool -
# see config.DAILY_FILE for why. The list is read the same forgiving way the plan
# reads ids, so the file can be a readable checklist and only the `id` in
# backticks is load-bearing.
DAILY_ID = re.compile(r"\[`(\w+)`\]")


def daily_ids() -> list[str]:
    """The reserve list in order, deduped, or [] when this channel has none."""
    if not DAILY_FILE.exists():
        return []
    # dict.fromkeys keeps first-seen order and drops a repeated id
    return list(dict.fromkeys(DAILY_ID.findall(
        DAILY_FILE.read_text(encoding="utf-8"))))


def _seen_today() -> set[str]:
    """This channel's post ids marked used today, UTC - the day's boundary the
    rest of the scheduler already uses (see multipart_today)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    with _db() as db:
        rows = db.execute("SELECT id, ts FROM seen WHERE lang=?",
                          (OUTPUT_LANG,)).fetchall()
    return {i for i, ts in rows if ts and
            datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).date() == today}


def daily_left() -> int:
    """How many reserve stories this channel has not published yet."""
    ids = daily_ids()
    if not ids:
        return 0
    with _db() as db:
        seen = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                         (OUTPUT_LANG,))}
    return sum(1 for i in ids if i not in seen)


def next_daily() -> dict | None:
    """The reserve story to publish this run, or None to leave the slot to the
    pool. Ready for the same auto-split path a pool pick takes - no `parts`, so
    main.py sizes it from the length like any other story.

    None means one of three quiet things: the list is empty or spent, the day's
    reserve slot is already taken, or the archive is down this run. The first two
    are permanent-for-now and the pool fills the slot; the third retries next run.
    """
    ids = daily_ids()
    if not ids:
        return None
    # The one-a-day gate. Anything from the list already used today - published,
    # or split and now mid-flight - means the reserve has had its slot.
    # ponytail: a reserve pick the model SKIPs is mark_used'd like any dud and so
    # counts as today's slot, forfeiting it for the day. Rare on hand-picked
    # contested stories; if it bites, track published apart from burned.
    today = _seen_today()
    if any(i in today for i in ids):
        return None
    with _db() as db:
        seen = {r[0] for r in db.execute("SELECT id FROM seen WHERE lang=?",
                                         (OUTPUT_LANG,))}
    for i in ids:
        if i in seen:
            continue
        try:
            p = _by_id(i)
        except DOWN as err:
            log.error("daily: no archive available for %s (%s) - the slot waits",
                      i, err)
            return None
        if p is None or not _tellable(p):
            log.error("daily: %s is gone or can no longer be told, dropping it", i)
            mark_used(i, p.get("score", 0) if p else 0,
                      p.get("subreddit", "?") if p else "?")
            continue
        title, text = _clean(p["title"]), _clean(p["selftext"])
        if hit := safety.blocked(title, text):
            log.error("daily: %s trips the blocklist (%s), dropping it", i, hit)
            mark_used(i, p["score"], p["subreddit"])
            continue
        return {"id": p["id"], "sub": p["subreddit"], "score": p["score"],
                "title": title, "text": text,
                "rank": contested(title, p.get("num_comments", 0), p["score"],
                                  p.get("upvote_ratio"))}
    return None


# -------------------------------------------------------------- the horror slot

def next_horror() -> list[dict]:
    """Today's horror candidates, best first, or empty to leave the slot alone.

    Several and not one, because the SKIP gate throws most of these out and
    the slot only gets one go a day. r/Paranormal is half photo posts whose
    body is a caption - long enough for _tellable(), no story in it - and the
    model is the only thing that can tell: four slots in a row went to one
    such post, were skipped, and left the day with no scare at all (08-22 to
    08-25, ids 1k9x2et 1boovsb 1nzl86j 16467ky). The caller already walks a
    list and takes the first story the prompt accepts, so handing it the
    runners-up costs one archive request for four bodies instead of one, and
    an LLM call only on the days a pick is actually refused.

    A pool of its own, drawn on the same terms the daily reserve draws its
    slot: the gate is whether one of these subs is already in today's `seen`
    rows, so a story that split counts from the moment its first part is
    marked, and a pick the model SKIPs burns the day like any other.

    Ranked by contested() like everything else. It is a strange measure for a
    genre nobody argues about, but within one horror list it still puts the
    loud and the commented-on at the top, and a second ranking function would
    be a whole new thing to tune for one slot a day.
    """
    if not SUBREDDITS_HORROR:
        return []
    # These run past what YouTube will take as a Short, so youtube._too_long()
    # keeps them out of that queue entirely and TikTok is the only outlet they
    # have. With TikTok paused there is none: the slot would spend a story and
    # an LLM call a day on five-minute files nothing can publish, and they would
    # sit in out/ for as long as the pause lasted.
    if not TIKTOK_ENABLED:
        log.info("horror slot stands down: it publishes to TikTok alone, and TikTok is paused for %s", OUTPUT_LANG)
        return []
    today = datetime.datetime.now(datetime.timezone.utc).date()
    marks = ",".join("?" * len(SUBREDDITS_HORROR))
    with _db() as db:
        rows = db.execute(f"SELECT ts FROM seen WHERE lang=? AND sub IN ({marks})",
                          (OUTPUT_LANG, *SUBREDDITS_HORROR)).fetchall()
    if any(ts and datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).date() == today for (ts,) in rows):
        return []
    return _pick(HORROR_TRIES, SUBREDDITS_HORROR)


# ---------------------------------------------------------------- split stories

def queue_parts(post: dict, parts: list[tuple[str, str]], gender: str,
                voice: str, issue: int = 0) -> None:
    """Store every part of a split story. Part 1 is rendered straight away.

    The voice id is stored with them on purpose: pick_voice() draws at random,
    and a story that changes narrator halfway sounds like two different videos
    stitched together. `issue` travels for the same reason - the parts are one
    story, told under one title chosen in one place, and their captions belong
    back in that place.
    """
    now = time.time()
    with _db() as db:
        db.executemany(
            "INSERT OR REPLACE INTO parts(post_id, n, total, title, body, "
            "gender, voice, sub, ts, done, tries, lang, issue) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?)",
            [(post["id"], i, len(parts), title, body, gender, voice, post["sub"],
              now, OUTPUT_LANG, issue)
             for i, (title, body) in enumerate(parts, 1)])
    log.info("%s: queued %d parts", post["id"], len(parts))


_PART_COLS = ("post_id", "n", "total", "title", "body", "gender", "voice",
              "sub", "issue")


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


def parts_left() -> int:
    """Parts of this channel's split stories still to be published.

    The same filter next_part() selects on, counted rather than fetched. A
    story that has started publishing is no longer in `review` - its row went
    the moment part 1 rendered - so this is the only place the rest of it is
    still visible, and main._batch_room() has to see it or the day after a
    split begins looks emptier than it is.
    """
    with _db() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM parts WHERE done=0 AND lang=?",
                          (OUTPUT_LANG,)).fetchone()
    return n


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

    # A photo post has text - the caption under the picture - so every filter
    # above passes it, and it ranks at the top of a sub like r/Paranormal
    # because the photo is what the karma is for. These are the real shapes,
    # off the six horror posts the model refused between 08-21 and 08-27.
    assert not _usable({**ok, "is_self": False}), "179p9ug, a link post"
    assert not _usable({**ok, "is_gallery": True}), "1lcgx2h, Photo Evidence"
    assert not _usable({**ok, "is_video": True})
    assert not _tellable({**ok, "is_self": False}), "and again where bodies land"
    # ...but a field the backend did not send is not an answer. Read the other
    # way round this empties the pool on the first refill from a source that
    # does not carry is_self at all.
    assert _usable({**ok, "is_self": None}) and _usable({**ok, "is_gallery": None})
    assert _usable({**ok, "is_self": True}), "a text post is the whole point"

    assert _clean("don&#39;t") == "don't"

    # The pool is ordered by how much of a fight a post is, and every signal has
    # to earn its place. Each one is checked as a DIFFERENCE against the same
    # post with nothing going for it: the terms add up, so a bare total says
    # nothing about which of them moved.
    _plain = contested("My neighbor flooded my flat", 0, 5000)
    assert _plain == min(5000 / LOUD_AT, 1.0), \
        "a quiet, uncommented, plainly titled post scores on loudness alone"
    for _title in ("AITA for kicking out my sister?", "WIBTA if I skipped the wedding",
                   "Am I wrong for locking the door"):
        assert abs(contested(_title, 0, 5000) - _plain - 1) < 1e-9, _title
    # 1 comment per 10 upvotes is a room taking sides; 1 per 100 is a nod
    assert abs(contested("x", 500, 5000) - _plain - 1.0) < 1e-9
    assert contested("x", 50, 5000) - _plain < 0.2
    assert contested("AITA for this", 500, 5000) > contested("x", 500, 5000), \
        "the signals must add up, not replace each other"
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

    # The ranking. Ordering is the whole job here - main.py takes the first
    # story the prompt accepts, so whatever sorts to the top IS the choice.
    assert _disputed(None) == _disputed(0) == 0.0, "an absent ratio is not a calm one"
    assert _disputed(0.98) == 0.0 and _disputed(0.70) == 1.0
    assert _disputed(0.60) == 1.0, "past the far end it saturates, not overflows"
    assert abs(_disputed(0.85) - 0.4) < 0.01, _disputed(0.85)
    # the pair actually measured on 2026-08-12: 1054 upvotes with 897 comments
    # at 0.85 is the fight, 7555 with 862 at 0.96 is the story everyone shared
    _fight = contested("AITA for spraying my BIL", 897, 1054, 0.85)
    assert _fight > contested("AITA for spilling my wine", 862, 7555, 0.96), \
        "the argued-over story has to outrank the agreed-with one"
    # ...and the loudness term, which exists so the year's biggest story is not
    # buried by the comments-over-score one. The real numbers again: 44832 with
    # 2331 comments was the loudest post in a year of three subs, and before the
    # term it scored 0.60 - below every ordinary AITA row in the pool.
    _loud = contested("My wife's friend insulted her", 2331, 44832, 0.93)
    assert _loud > contested("A quiet story", 200, 1500, 0.99), \
        "a loud story must not sort below a bland one"
    assert _loud < _fight, "but it must not simply outrank a real fight either"

    # The pool: what a run chooses from. Rows are planted rather than harvested,
    # since what is being tested is the reading - a story already spent in THIS
    # language, the order, and a row whose post has gone since.
    _real = (SUBREDDITS, _refilled, _bodies)
    globals()["SUBREDDITS"] = ["_selftest_sub", "_selftest_other"]
    globals()["_refilled"] = {("_selftest_sub", "_selftest_other")}   # no archive
    # the stub answers like a real backend, numbers included, because _pick()
    # writes those numbers back into the row it just read
    globals()["_bodies"] = lambda ids: {
        i: {"id": i, "selftext": "x" * 500, "over_18": False, "title": "A story",
            "score": _planted_by_id[i][2], "num_comments": _planted_by_id[i][3],
            "upvote_ratio": _planted_by_id[i][4]}
        for i in ids if i != "_sp_gone"}
    _planted = [("_sp_quiet", "_selftest_sub", MIN_SCORE + 10, 20, 0.99),
                ("_sp_fight", "_selftest_sub", MIN_SCORE + 10, 900, 0.72),
                ("_sp_spent", "_selftest_sub", MIN_SCORE + 10, 900, 0.72),
                ("_sp_gone", "_selftest_other", MIN_SCORE + 10, 50, 0.72),
                ("_sp_loud", "_selftest_other", LOUD_AT + 1, 900, 0.72)]
    _planted_by_id = {row[0]: row for row in _planted}
    with _db() as db:
        db.executemany("INSERT OR REPLACE INTO pool(id, sub, score, comments, "
                       "ratio, title, ts) VALUES (?,?,?,?,?,?,?)",
                       [(*row, "A story", time.time()) for row in _planted])
        db.execute("INSERT OR REPLACE INTO seen(id, score, sub, ts, lang) "
                   "VALUES ('_sp_spent', 0, '_selftest_sub', 0, ?)", (OUTPUT_LANG,))

    # One list, so the loud row competes in it rather than waiting for a band
    # of its own: it wins here, the fight is second, the row whose post is gone
    # is ranked third and then dropped, and the bland one does not make the cut.
    got = [p["id"] for p in _pick(3)]
    assert got == ["_sp_loud", "_sp_fight"], got
    assert [p["id"] for p in _pick(1)] == ["_sp_loud"], "the top row is the choice"
    assert [p["id"] for p in _pick(4)][-1] == "_sp_quiet", "bland sorts last"
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

    # The horror slot may be longer at the source, because it is told as one
    # video of up to HORROR_SEC instead of being cut into 75-second parts. The
    # feed's own ceiling must not move with it, which is the half that breaks.
    _real_horror = SUBREDDITS_HORROR
    globals()["SUBREDDITS_HORROR"] = ["_selftest_horror"]
    _long = {**_base, "selftext": "x" * (MAX_CHARS + 1000)}
    assert not _tellable(_long), "an ordinary post past MAX_CHARS is too long"
    assert _tellable({**_long, "subreddit": "_selftest_horror"})
    assert not _tellable({**_long, "subreddit": "_selftest_horror",
                          "selftext": "x" * (HORROR_CHARS + 1)})
    # and the same seam on the discovery filter, where the sub is passed in
    assert not _usable(_long) and not _usable(_long, "AmItheAsshole")
    assert _usable(_long, "_selftest_horror")
    globals()["SUBREDDITS_HORROR"] = _real_horror

    # no file at all is not an error - it is a channel without a plan
    globals()["PLAN_FILE"] = _tmp.with_name("_no_such_plan.md")
    assert plan() == [] and plan_left() == 0
    _tmp.unlink()
    globals()["PLAN_FILE"] = _real_plan_file
    if plan():
        print(f"plan: {len(plan())} stories, {sum(e['parts'] for e in plan())} "
              f"videos, {plan_left()} still to publish")

    # The daily reserve. It hands out one story a day and then steps aside for
    # the pool, so the two failures that matter are giving out a second one the
    # same day and never stepping aside at all - both are gated on `seen`, so
    # both are checked here against a planted db.
    _real_daily_file, _real_by_id = DAILY_FILE, _by_id
    _dtmp = Path(tempfile.gettempdir()) / "_selftest_daily.md"
    _dtmp.write_text(
        "| 1 | [`_dl_a`](https://redd.it/_dl_a) | one |\n"
        "| 2 | [`_dl_a`](https://redd.it/_dl_a) | a repeat, must fold |\n"
        "| 3 | [`_dl_gone`](https://redd.it/_dl_gone) | archive has no such post |\n"
        "| 4 | [`_dl_b`](https://redd.it/_dl_b) | two |\n", encoding="utf-8")
    globals()["DAILY_FILE"] = _dtmp
    globals()["_by_id"] = lambda i: (None if i == "_dl_gone" else
        {"id": i, "subreddit": "_selftest_sub", "score": MIN_SCORE + 5,
         "num_comments": 500, "upvote_ratio": 0.8, "over_18": False,
         "title": "A story", "selftext": "x" * 500})
    assert daily_ids() == ["_dl_a", "_dl_gone", "_dl_b"], daily_ids()
    with _db() as db:                         # start clean
        db.execute("DELETE FROM seen WHERE id LIKE '_dl_%'")
    assert daily_left() == 3, daily_left()
    # first run of the day: the list's head, in order
    assert next_daily()["id"] == "_dl_a", "the reserve leads with the top row"
    # the model accepted it, so it is marked used today
    mark_used("_dl_a", MIN_SCORE + 5, "_selftest_sub")
    assert next_daily() is None, "only one reserve story a day"
    # a new day: nothing from the list marked today, so the next one is offered,
    # and the id whose post is gone is burned on the way past rather than stalling
    with _db() as db:
        db.execute("UPDATE seen SET ts=0 WHERE id='_dl_a'")   # used, but not today
    assert next_daily()["id"] == "_dl_b", "gone row skipped, next real one served"
    with _db() as db:
        (burned,) = db.execute(
            "SELECT COUNT(*) FROM seen WHERE id='_dl_gone' AND lang=?",
            (OUTPUT_LANG,)).fetchone()
    assert burned == 1, "a reserve post that is gone must be burned, not retried"
    # list spent: every id used (on another day), so the slot is the pool's again
    for i in ("_dl_a", "_dl_b", "_dl_gone"):
        mark_used(i, 0, "_selftest_sub")
    with _db() as db:
        db.execute("UPDATE seen SET ts=0 WHERE id LIKE '_dl_%'")
    assert daily_left() == 0 and next_daily() is None, "a spent list steps aside"
    with _db() as db:
        db.execute("DELETE FROM seen WHERE id LIKE '_dl_%'")
    globals()["DAILY_FILE"] = _dtmp.with_name("_no_such_daily.md")
    assert daily_ids() == [] and daily_left() == 0 and next_daily() is None
    _dtmp.unlink()
    globals()["DAILY_FILE"], globals()["_by_id"] = _real_daily_file, _real_by_id
    print("daily reserve ok")

    # The horror slot: one a day off a pool of its own, and the gate is the SUB
    # rather than the id - what has to be true is that no scary story went out
    # today, whichever one it was.
    _real_bodies, _real_refill = _bodies, refill
    _real_horror_subs = SUBREDDITS_HORROR
    globals()["SUBREDDITS_HORROR"] = ["_selftest_horror"]
    # a pool of one row is under POOL_LOW, and a refill here would go to the
    # network for a sub that does not exist
    globals()["refill"] = lambda db, **kw: 0
    globals()["_bodies"] = lambda ids: {
        i: {"id": i, "subreddit": "_selftest_horror", "score": MIN_SCORE + 5,
            "num_comments": 500, "upvote_ratio": 0.8, "over_18": False,
            "title": "The man on the stairs", "selftext": "x" * 500}
        for i in ids}
    with _db() as db:
        db.execute("DELETE FROM seen WHERE id LIKE '_hr_%'")
        db.execute("DELETE FROM pool WHERE id LIKE '_hr_%'")
        db.execute("INSERT INTO pool(id, sub, score, comments, ratio, title, ts)"
                   " VALUES (?,?,?,?,?,?,?)",
                   ("_hr_a", "_selftest_horror", MIN_SCORE + 5, 500, 0.8,
                    "The man on the stairs", time.time()))
    assert next_horror()[0]["id"] == "_hr_a", "the horror pool fills its own slot"
    # taken: the story was accepted and marked, so the day has had its scare
    mark_used("_hr_a", MIN_SCORE + 5, "_selftest_horror")
    assert not next_horror(), "only one horror story a day"
    # a new day, and a second row: the same sub used yesterday does not count
    with _db() as db:
        db.execute("UPDATE seen SET ts=0 WHERE id='_hr_a'")
        db.execute("INSERT INTO pool(id, sub, score, comments, ratio, title, ts)"
                   " VALUES (?,?,?,?,?,?,?)",
                   ("_hr_b", "_selftest_horror", MIN_SCORE + 5, 500, 0.8,
                    "The other man on the stairs", time.time()))
        # a second live row, so the SKIP of the top pick still leaves a story
        db.execute("INSERT INTO pool(id, sub, score, comments, ratio, title, ts)"
                   " VALUES (?,?,?,?,?,?,?)",
                   ("_hr_c", "_selftest_horror", MIN_SCORE + 4, 400, 0.8,
                    "A photo of the stairs", time.time()))
    assert [p["id"] for p in next_horror()] == ["_hr_b", "_hr_c"], (
        "yesterday's scare does not hold today, and the runner-up comes too")
    # and a channel with no horror subs configured never sees the slot at all
    globals()["SUBREDDITS_HORROR"] = []
    assert not next_horror(), "no horror subs means no horror slot"
    # ...and neither does a channel whose only outlet for them is switched off
    globals()["SUBREDDITS_HORROR"] = ["_selftest_horror"]
    globals()["TIKTOK_ENABLED"] = False
    assert not next_horror(), "no TikTok, no horror slot"
    globals()["TIKTOK_ENABLED"] = True
    with _db() as db:
        db.execute("DELETE FROM seen WHERE id LIKE '_hr_%'")
        db.execute("DELETE FROM pool WHERE id LIKE '_hr_%'")
    globals()["SUBREDDITS_HORROR"] = _real_horror_subs
    globals()["_bodies"], globals()["refill"] = _real_bodies, _real_refill
    print("horror slot ok")

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

    # The field translation. The proxy answers upvotes/text/comments where the
    # archives answer score/selftext/num_comments, so if it ever renames one of
    # them everything downstream reads a zero and the pool fills with rows that
    # cannot be ranked - silently, because a zero is a legal score.
    _theirs = {"id": "x", "upvotes": 4200, "comments": 310, "upvote_ratio": 0.8,
               "text": "body", "title": "t", "subreddit": "tifu"}
    _ours = _mapped(_theirs, "asked_for")
    assert (_ours["score"], _ours["num_comments"], _ours["selftext"]) \
        == (4200, 310, "body"), _ours
    assert _ours["upvote_ratio"] == 0.8, "the fields that already match must survive"
    assert _mapped({"id": "x"}, "asked_for")["subreddit"] == "asked_for", \
        "a post with no sub of its own belongs to the sub it was asked for"
    assert _mapped({"id": "x"}, "s")["score"] == 0, "a missing count reads 0, not None"

    # The chain, exercised on purpose. Two of the three backends are down as
    # often as they are up and the third costs money and can run out of it, so a
    # fallback nobody runs is a fallback that is broken when it is needed.
    _real_live, _real_redditapi = _live, _redditapi
    if REDDITAPIS_KEY:
        assert _live(SUBREDDITS[0]), "the live proxy answered with nothing"
        print("live proxy ok")
    # A slow sub must not cost a call for nothing: a thin window is widened to
    # top/all, and only once. Stubbed rather than measured - which sub happens
    # to be slow this week is not something a test should depend on.
    _asked, _real_choice = [], random.choice
    globals()["_redditapi"] = lambda path, envelope="posts", **p: (
        _asked.append(p.get("t")) or ([{"id": "x"}] * 100 if p.get("t") == "all"
                                      else [{"id": "x"}] * 2))
    random.choice = lambda seq: "week"
    assert len(_live("_slow")) == 100, "a thin window must be widened"
    assert _asked == ["week", "all"], _asked
    _asked.clear()
    random.choice = lambda seq: "all"
    assert len(_live("_slow")) == 100 and _asked == ["all"], \
        "top/all is already the widest - there is nothing to retry"
    random.choice = _real_choice
    globals()["_redditapi"] = _real_redditapi
    _live = lambda *a: (_ for _ in ()).throw(urllib.error.URLError("forced"))
    with _db() as db:
        refill(db, windows=1)          # must reach an archive rather than raise
    print("fallback ok: the archives answer with the proxy dead")
    _live = _real_live

    posts = fetch(3)
    assert posts, "source returned nothing - check network / SUBREDDITS"
    assert posts == sorted(posts, key=lambda p: p["rank"], reverse=True), \
        "the contested posts must come first"
    for p in posts:
        print(f"[{p['score']:>7}] {p['rank']:.2f} r/{p['sub']} "
              f"{len(p['text']):>5}ch  {p['title'][:60]}")
