"""Step 5 (YouTube): upload a finished mp4 as a Short.

    python youtube.py --auth              one-time, gets the refresh token
    python youtube.py out/<id>.mp4        upload (private)
    python youtube.py out/<id>.mp4 --public

A Short is just a normal upload: vertical, under three minutes, with #Shorts
in the text. There is no separate Shorts endpoint.

Two things to know before wiring this into a schedule:
  - An unverified API project can only produce PRIVATE videos. Making them
    public needs a compliance audit, same as TikTok. Default here is private
    anyway - nothing goes public without --public.
  - While the OAuth consent screen sits in "Testing", refresh tokens die after
    seven days. Switch it to "In production" (unverified is fine) or you will
    be re-authorising every week.
"""
import datetime
import json
import logging
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import source
import tags as tags_          # `tags` is the local variable in description_for
from config import (CHANNEL, DB_PATH, DECLARE_AI, DEFAULT_CHANNEL, OUT_DIR,
                    PART_GAP_HOURS, YT_CLIENT_ID, YT_CLIENT_SECRET,
                    YT_MIN_GAP_HOURS, YT_REFRESH_KEY, YT_REFRESH_TOKEN, save_env)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT = "http://localhost:8080"

TITLE_MAX = 100        # YouTube rejects longer titles
DESC_MAX = 5000
CATEGORY_PEOPLE_BLOGS = "22"

log = logging.getLogger(__name__)


def _form_post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(body).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode()[:400]}") from None


def access_token() -> str:
    # The token key is named for the channel away from the default one, and the
    # error has to say WHICH key: "YT_REFRESH_TOKEN is empty" while the English
    # channel is looking for YT_REFRESH_TOKEN_EN sends you to the wrong line.
    for name, val in [("YT_CLIENT_ID", YT_CLIENT_ID),
                      ("YT_CLIENT_SECRET", YT_CLIENT_SECRET),
                      (YT_REFRESH_KEY, YT_REFRESH_TOKEN)]:
        if not val:
            raise RuntimeError(f"{name} is empty - run `OUTPUT_LANG={CHANNEL} "
                               "python youtube.py --auth`")

    r = _form_post(TOKEN_URL, {
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN, "grant_type": "refresh_token"})
    if "access_token" not in r:
        raise RuntimeError(f"no access_token in response: {r}")
    return r["access_token"]


# The word in front of "2/3" on a split story, in the channel's own language.
# The file's channel decides it, not the process: --text on an old file has to
# print what that file was made as.
PART_WORD = {"ru": "Часть", "en": "Part"}


def part_prefix(meta: dict) -> str:
    """"Часть 2/3 - " for a split story, empty for an ordinary one.

    Only the PUBLISHED text carries the marker. The narrated title card stays
    clean: it is the hook, and three syllables of "часть вторая" in front of it
    is dead air. In the feed the marker leads the title, which is why it has to
    survive the length limit below.
    """
    if meta.get("total", 0) <= 1:
        return ""
    word = PART_WORD.get(meta.get("channel", DEFAULT_CHANNEL), PART_WORD["en"])
    return f"{word} {meta['part']}/{meta['total']} - "


def title_for(text: str, prefix: str = "") -> str:
    """#Shorts is what routes the upload into the Shorts shelf.

    The prefix is trimmed around, never trimmed off: a twelve-word title plus a
    part marker plus the tag overshoots TITLE_MAX, and the marker is the part a
    viewer needs most.
    """
    tag = " #Shorts"
    text = text.strip()
    if len(prefix) + len(text) + len(tag) > TITLE_MAX:
        text = text[:TITLE_MAX - len(prefix) - len(tag) - 3].rstrip() + "..."
    return prefix + text + tag


def description_for(title: str, body: str = "", hashtags=None,
                    kind: str = "story") -> str:
    """Teaser built from the story itself, plus tags matched to it.

    The first line is the title and nothing else. A rotating opener used to sit
    above it - "Смотрите, что произошло." - and it only pushed the conflict
    below the fold: the two lines a viewer sees before tapping "ещё" were spent
    on boilerplate. The variation it was there for comes from the story's own
    opening anyway, which differs by construction.

    The tags come from tags.py, which reads the text: a flat pool shuffled per
    upload put #отношения under stories about a boss. An explicit `hashtags`
    still overrides it wholesale, which is what the self-test uses.
    """
    pool = (tags_.pick(title, body, kind) if hashtags is None
            else random.sample(list(hashtags), min(5, len(hashtags))))
    tags = " ".join(pool[:5] + ["#Shorts"])

    teaser = " ".join(re.split(r"(?<=[.!?])\s+", body.strip())[:2]).strip()
    parts = [title.strip()] + ([teaser] if teaser else [])
    return (("\n\n".join(parts)) + "\n\n" + tags)[:DESC_MAX]


def upload(mp4, title: str, private: bool = True, body: str = "",
           prefix: str = "", kind: str = "story") -> str:
    """Resumable upload in one PUT. Returns the video id."""
    # Google's docs say an unaudited project can only produce private videos.
    # Measured 2026-07-31 on this project: privacyStatus=public went straight
    # to public, confirmed via the oEmbed endpoint. If that ever changes, the
    # symptom is a video that uploads fine but stays private in Studio.
    mp4 = Path(mp4)
    size = mp4.stat().st_size
    token = access_token()

    meta = {
        "snippet": {
            "title": title_for(title, prefix),
            "description": description_for(prefix + title, body, kind=kind),
            "categoryId": CATEGORY_PEOPLE_BLOGS,
        },
        "status": {
            "privacyStatus": "private" if private else "public",
            "selfDeclaredMadeForKids": False,
            # see DECLARE_AI in config.py for when this has to be true
            "containsSyntheticMedia": DECLARE_AI,
        },
    }

    init = urllib.request.Request(
        f"{UPLOAD_URL}?{urllib.parse.urlencode({'uploadType': 'resumable', 'part': 'snippet,status'})}",
        data=json.dumps(meta).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size),
                 "X-Upload-Content-Type": "video/mp4"},
        method="POST")
    try:
        with urllib.request.urlopen(init, timeout=60) as r:
            session = r.headers["Location"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"init -> {e.code}: {e.read().decode()[:400]}") from None
    if not session:
        raise RuntimeError("no resumable session URL in the response headers")

    # ponytail: one PUT for the whole file. Our videos are well under 100 MB;
    # if a upload ever dies mid-flight, the session URL supports byte-range resume.
    put = urllib.request.Request(
        session, data=mp4.read_bytes(), method="PUT",
        headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
    try:
        with urllib.request.urlopen(put, timeout=900) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"upload -> {e.code}: {e.read().decode()[:400]}") from None

    vid = body.get("id")
    log.info("uploaded %s as %s (%s)", mp4.name, vid, meta["status"]["privacyStatus"])
    return vid


# --------------------------------------------------------------------- queue

def _db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS uploaded("
               "file TEXT PRIMARY KEY, yt_id TEXT, ts REAL)")
    # Which channel the upload went to. Every count below is per channel, and
    # without this one channel's uploads spend the other's daily allowance and
    # hold it behind the gap - a second channel that silently never publishes.
    # The file name stays the key: only the default channel keeps the bare
    # <post_id>.mp4, so two channels cannot collide on one name.
    if "channel" not in {c[1] for c in db.execute("PRAGMA table_info(uploaded)")}:
        db.execute("ALTER TABLE uploaded ADD COLUMN channel TEXT")
        db.execute("UPDATE uploaded SET channel=?", (DEFAULT_CHANNEL,))
    return db


def daily_allowance(day: int) -> int:
    """Videos permitted on day N of the channel's life.

    Not a documented rule - YouTube publishes no such limit. Three a day is the
    working ceiling for Shorts on one channel; more reads as a firehose and each
    upload competes with the last one for the same audience. A young channel
    starts at two.
    """
    return 2 if day < 7 else 3


def _utc_date(ts: float):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def status() -> dict:
    """Counts run on UTC calendar days, not a trailing 24-hour window.

    A rolling window sounds equivalent and is not: three uploads bunched into
    one evening keep the next whole day blocked, because each only frees up
    24 hours after itself. Calendar days reset at a predictable moment and the
    allowance means what it says.
    """
    with _db() as db:
        rows = db.execute("SELECT ts FROM uploaded WHERE channel=? ORDER BY ts",
                          (CHANNEL,)).fetchall()
    now = time.time()
    today_date = _utc_date(now)
    first_date = _utc_date(rows[0][0]) if rows else today_date
    day = (today_date - first_date).days
    today = sum(1 for (ts,) in rows if _utc_date(ts) == today_date)
    last = rows[-1][0] if rows else 0
    return {"day": day, "today": today, "allowed": daily_allowance(day),
            "since_last_h": (now - last) / 3600 if last else 999, "total": len(rows)}


def _meta_for(mp4: Path) -> dict:
    p = mp4.with_suffix(".meta.json")
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def _mine(mp4: Path) -> bool:
    """Was this file made for the channel this process is?

    Both channels render into the same out/, and locally they sit there side by
    side. A file with no meta predates the second channel, so it belongs to the
    default one - which is also what its bare name says.
    """
    return _meta_for(mp4).get("channel", DEFAULT_CHANNEL) == CHANNEL


def pending() -> list:
    """This channel's rendered videos that have not been uploaded, oldest first.

    Parts of one story cannot get out of order: source.next_part() hands out
    part N+1 only after part N is uploaded, so at most one part of a given story
    is ever in this list. Which of the queued files goes first is decided in
    upload_next(), not here.
    """
    with _db() as db:
        done = {r[0] for r in db.execute("SELECT file FROM uploaded")}
    return sorted((p for p in OUT_DIR.glob("*.mp4")
                   if p.name not in done and _mine(p)),
                  key=lambda p: p.stat().st_mtime)


def show_text(mp4: Path | None = None) -> None:
    """Print title and description ready to paste into the upload form.

    Uploading by hand is the only way to publish while the API project is
    unaudited, so the text has to be obtainable without touching the API.
    """
    targets = [mp4] if mp4 else pending()
    if not targets:
        print("nothing pending - run main.py first")
        return
    for p in targets:
        meta = _meta_for(p)
        title = meta.get("title") or p.stem
        pfx = part_prefix(meta)
        print("=" * 70)
        print(p.name)
        print("=" * 70)
        print("\nTITLE:")
        print(title_for(title, pfx))
        print("\nDESCRIPTION:")
        print(description_for(pfx + title, meta.get("body", ""),
                              kind=meta.get("kind", "story")))
        print()


def mark_done(mp4: Path, yt_id: str = "manual") -> None:
    """Record a hand-made upload so the queue stops offering it."""
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO uploaded VALUES (?,?,?,?)",
                   (mp4.name, yt_id, time.time(),
                    _meta_for(mp4).get("channel", DEFAULT_CHANNEL)))
    print(f"{mp4.name} marked as uploaded")


def due() -> str:
    """Empty string if an upload is allowed right now, else the reason it is not.

    Split out so a caller can ask before doing the expensive part. On a CI
    runner the rendered file dies with the job, so generating first and finding
    out afterwards that the schedule says no burns a story for nothing.
    """
    s = status()
    if s["today"] >= s["allowed"]:
        return f"daily allowance reached ({s['today']}/{s['allowed']})"
    # The continuation of a split story runs on its own, much shorter clock:
    # five hours between a cliffhanger and its answer loses whoever saw the
    # first half. Asked before anything is rendered, same as the rest of due().
    gap = PART_GAP_HOURS if source.next_part() else YT_MIN_GAP_HOURS
    if s["since_last_h"] < gap:
        return (f"only {s['since_last_h']:.1f}h since the last upload, "
                f"minimum is {gap:.1f}h")
    return ""


def upload_next(private: bool = True, force: bool = False) -> str | None:
    """Upload at most one video, if the schedule allows it right now."""
    s = status()
    queue = pending()
    log.info("day %d, %d/%d used today, %.1fh since last, %d queued",
             s["day"], s["today"], s["allowed"], s["since_last_h"], len(queue))

    if not queue:
        log.info("nothing to upload - run main.py first")
        return None
    if not force and (reason := due()):
        log.info("%s", reason)
        return None

    mp4 = queue[0]
    # The awaited part owns this slot. due() shortened the gap on its account,
    # so an older ordinary video sitting in out/ must not spend it instead and
    # leave the story's middle waiting another day.
    nxt = source.next_part()
    if nxt:
        mp4 = next((p for p in queue
                    if _meta_for(p).get("post_id") == nxt["post_id"]
                    and _meta_for(p).get("part") == nxt["n"]), mp4)

    meta = _meta_for(mp4)
    yt_id = upload(mp4, meta.get("title") or mp4.stem, private=private,
                   body=meta.get("body", ""), prefix=part_prefix(meta),
                   kind=meta.get("kind", "story"))
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO uploaded VALUES (?,?,?,?)",
                   (mp4.name, yt_id, time.time(), CHANNEL))
    # Cleared here and nowhere earlier: an upload that fails has to leave the
    # part pending, or the story loses its middle - the mp4 is already gone.
    if meta.get("total", 0) > 1:
        source.finish_part(meta["post_id"], meta["part"])
        log.info("part %d of %d done for %s", meta["part"], meta["total"],
                 meta["post_id"])
    return yt_id


def authorize() -> None:
    """One-time browser round trip. Prints the refresh token for .env."""
    if not YT_CLIENT_ID or not YT_CLIENT_SECRET:
        raise RuntimeError("set YT_CLIENT_ID and YT_CLIENT_SECRET in .env first")

    params = {"client_id": YT_CLIENT_ID, "redirect_uri": REDIRECT,
              "response_type": "code", "scope": SCOPE,
              # without both of these Google returns no refresh token at all
              "access_type": "offline", "prompt": "consent"}
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("opening:\n", url)
    webbrowser.open(url)

    code = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code["v"] = q.get("code", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write("Готово, можно закрывать вкладку.".encode())

        def log_message(self, *a):
            pass

    with HTTPServer(("localhost", 8080), Handler) as srv:
        srv.handle_request()

    if not code.get("v"):
        raise RuntimeError("no code came back - was the consent screen cancelled?")

    r = _form_post(TOKEN_URL, {
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "code": code["v"], "grant_type": "authorization_code",
        "redirect_uri": REDIRECT})
    if "refresh_token" not in r:
        raise RuntimeError(f"no refresh_token: {r}\n"
                           "revoke the app's access and retry - Google only "
                           "sends it on a fresh consent")
    _save_refresh_token(r["refresh_token"])


def _save_refresh_token(token: str) -> None:
    """Write it into .env instead of asking the operator to copy it by hand.

    Under the channel's own key: a token written to YT_REFRESH_TOKEN while the
    English channel was being authorised would log the Russian one out.
    """
    save_env(YT_REFRESH_KEY, token)
    print(f"\nrefresh token saved to .env as {YT_REFRESH_KEY}")
    print("it is valid for a year, and only while the consent screen is out of Testing")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # kept behind a flag so normal runs print only what the operator needs
    if "--selftest" in sys.argv:
        assert title_for("Короткий заголовок").endswith(" #Shorts")
        assert len(title_for("я" * 200)) <= TITLE_MAX
        assert title_for("я" * 200).endswith(" #Shorts"), "the tag must survive trimming"

        # the part marker: absent for ordinary videos, and never trimmed away
        assert part_prefix({}) == "" and part_prefix({"part": 1, "total": 1}) == ""
        pfx = part_prefix({"part": 2, "total": 3, "channel": "ru"})
        assert pfx == "Часть 2/3 - ", pfx
        # the marker speaks the language of the FILE, not of this process
        assert part_prefix({"part": 2, "total": 3, "channel": "en"}) == "Part 2/3 - "
        # a file from before channels existed is the default channel's
        assert part_prefix({"part": 2, "total": 3}) == "Часть 2/3 - "
        long = title_for("я" * 200, pfx)
        assert len(long) <= TITLE_MAX and long.startswith(pfx), long
        assert long.endswith(" #Shorts"), long
        assert description_for(pfx + "Заголовок", "Текст.").startswith(pfx + "Заголовок")
        assert len(description_for("t", "x" * 9000)) <= DESC_MAX
        assert "#Shorts" in description_for("Заголовок", "Первое. Второе. Третье.")

        # the teaser must stop at two sentences, not dump the whole story
        d = description_for("Заголовок", "Первое. Второе. Третье. Четвёртое.")
        assert "Второе." in d and "Третье." not in d, d

        # the conflict is the first thing read, before any boilerplate
        assert d.startswith("Заголовок\n\n"), d

        # consecutive descriptions must not be clones
        variants = {description_for("Один и тот же", "Одно и то же.") for _ in range(30)}
        assert len(variants) > 5, "descriptions are not varying"

        # The tags answer to the text: a boss story carries work tags, and a
        # fact carries none of the story pool at all. Written in this channel's
        # language - description_for() reads the buckets of the channel it runs
        # on, so a Russian fixture under OUTPUT_LANG=en would match nothing and
        # the test would be asserting the wrong thing.
        BOSS, FACT_ = {
            "ru": (("Начальник вычел из зарплаты за опоздание",
                    "Директор орал при всём офисе."),
                   ("У осьминога три сердца", "Кровь синеет из-за меди.")),
            "en": (("My boss docked my pay for being late",
                    "The manager yelled at me in front of everyone."),
                   ("An octopus has three hearts", "The blue comes from copper.")),
        }[CHANNEL]
        boss = description_for(*BOSS)
        assert set(boss.split()) & tags_.TOPIC_TAGS[CHANNEL], boss
        fact = description_for(*FACT_, kind="fact")
        # #рекомендации is in both pools on purpose; #драма is in one
        story_only = (set(tags_.GENERIC[CHANNEL]["story"])
                      - set(tags_.GENERIC[CHANNEL]["fact"]))
        assert not (set(fact.split()) & story_only), fact

        assert daily_allowance(0) == 2 and daily_allowance(6) == 2
        assert daily_allowance(7) == 3 and daily_allowance(365) == 3, "3 is the ceiling"

        # The case that broke it: two uploads two hours apart, either side of
        # midnight. A trailing 24h window counts both as "today"; calendar days
        # do not, which is the whole point.
        midnight = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        before = (midnight - datetime.timedelta(hours=1)).timestamp()
        after = (midnight + datetime.timedelta(hours=1)).timestamp()
        assert after - before < 24 * 3600, "the two are inside one rolling window"
        assert _utc_date(before) != _utc_date(after), "but they are different days"
        assert _utc_date(0) == datetime.date(1970, 1, 1)
        print("title, description and schedule logic ok")
        sys.exit(0)

    try:
        if "--auth" in sys.argv:
            authorize()
        elif "--status" in sys.argv:
            s = status()
            print(f"channel {CHANNEL}: day {s['day']}, {s['today']}/{s['allowed']} "
                  f"today, {s['total']} uploaded, {len(pending())} queued")
            for p in pending()[:10]:
                print("  ", p.name)
            if nxt := source.next_part():
                print(f"next: {nxt['post_id']} part {nxt['n']} of {nxt['total']}"
                      f" (gap {PART_GAP_HOURS:.1f}h)")
        elif "--due" in sys.argv:
            # exit code is the point: lets a scheduler check before it spends
            reason = due()
            print(reason or "due")
            sys.exit(1 if reason else 0)
        elif "--text" in sys.argv:
            arg = next((a for a in sys.argv if a.endswith(".mp4")), None)
            show_text(Path(arg) if arg else None)
        elif "--done" in sys.argv:
            arg = next((a for a in sys.argv if a.endswith(".mp4")), None)
            if not arg:
                print("usage: python youtube.py --done out/<id>.mp4")
            else:
                mark_done(Path(arg))
        elif "--next" in sys.argv:
            print(upload_next(private="--public" not in sys.argv,
                              force="--force" in sys.argv))
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            print(upload(mp4, mp4.stem, private="--public" not in sys.argv))
        else:
            print("usage: python youtube.py --auth | --status | --due | "
                  "--text [file] | --done <file> | --next [--public] "
                  "| out/<id>.mp4 | --selftest")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
