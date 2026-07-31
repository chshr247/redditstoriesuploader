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

from config import (DB_PATH, OUT_DIR, YT_AUDITED, YT_CLIENT_ID,
                    YT_CLIENT_SECRET, YT_HASHTAGS, YT_MIN_GAP_HOURS,
                    YT_REFRESH_TOKEN)

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
    for name, val in [("YT_CLIENT_ID", YT_CLIENT_ID),
                      ("YT_CLIENT_SECRET", YT_CLIENT_SECRET),
                      ("YT_REFRESH_TOKEN", YT_REFRESH_TOKEN)]:
        if not val:
            raise RuntimeError(f"{name} is empty - run `python youtube.py --auth`")

    r = _form_post(TOKEN_URL, {
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN, "grant_type": "refresh_token"})
    if "access_token" not in r:
        raise RuntimeError(f"no access_token in response: {r}")
    return r["access_token"]


def title_for(text: str) -> str:
    """#Shorts is what routes the upload into the Shorts shelf."""
    tag = " #Shorts"
    text = text.strip()
    if len(text) + len(tag) > TITLE_MAX:
        text = text[:TITLE_MAX - len(tag) - 3].rstrip() + "..."
    return text + tag


# Openers rotate so consecutive uploads do not share a first line. The body of
# the description is the story's own opening, which differs by construction -
# that is where the real variation comes from, not from shuffling boilerplate.
OPENERS = [
    "История с Reddit.",
    "Реальный случай с Reddit.",
    "Из обсуждения на Reddit.",
    "Кто здесь неправ?",
    "А вы бы как поступили?",
    "Рассказ от первого лица.",
]


def description_for(title: str, body: str = "", hashtags=None) -> str:
    """Teaser built from the story itself, plus a rotating slice of tags."""
    pool = list(YT_HASHTAGS if hashtags is None else hashtags)
    random.shuffle(pool)
    tags = " ".join(pool[:5] + ["#Shorts"])

    teaser = " ".join(re.split(r"(?<=[.!?])\s+", body.strip())[:2]).strip()
    parts = [random.choice(OPENERS), title.strip()]
    if teaser:
        parts.append(teaser)
    return (("\n\n".join(parts)) + "\n\n" + tags)[:DESC_MAX]


def upload(mp4, title: str, private: bool = True, body: str = "",
           allow_unaudited: bool = False) -> str:
    """Resumable upload in one PUT. Returns the video id."""
    if not YT_AUDITED and not allow_unaudited:
        raise RuntimeError(
            "The API project has not passed its compliance audit, so anything "
            "uploaded now is locked private permanently - Studio cannot undo it, "
            "only a re-upload can. Upload through the YouTube site meanwhile.\n"
            "Set YT_AUDITED=true in .env once approved, or pass --i-know to "
            "override deliberately.")

    mp4 = Path(mp4)
    size = mp4.stat().st_size
    token = access_token()

    meta = {
        "snippet": {
            "title": title_for(title),
            "description": description_for(title, body),
            "categoryId": CATEGORY_PEOPLE_BLOGS,
        },
        "status": {
            "privacyStatus": "private" if private else "public",
            "selfDeclaredMadeForKids": False,
            # the narration is synthetic; YouTube wants that declared
            "containsSyntheticMedia": True,
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
    return db


def daily_allowance(day: int) -> int:
    """Videos permitted on day N of the channel's life.

    A warm-up, not a documented rule - YouTube publishes no such limit. It costs
    nothing and keeps a brand-new channel from going from zero to a firehose,
    which is the pattern that looks automated to a human reviewer.
    """
    if day < 3:
        return 3
    if day < 7:
        return 4
    if day < 14:
        return 6
    return 8


def status() -> dict:
    with _db() as db:
        rows = db.execute("SELECT ts FROM uploaded ORDER BY ts").fetchall()
    now = time.time()
    first = rows[0][0] if rows else now
    day = int((now - first) // 86400)
    today = sum(1 for (ts,) in rows if ts > now - 86400)
    last = rows[-1][0] if rows else 0
    return {"day": day, "today": today, "allowed": daily_allowance(day),
            "since_last_h": (now - last) / 3600 if last else 999, "total": len(rows)}


def pending() -> list:
    """Rendered videos that have not been uploaded, oldest first."""
    with _db() as db:
        done = {r[0] for r in db.execute("SELECT file FROM uploaded")}
    return sorted((p for p in OUT_DIR.glob("*.mp4") if p.name not in done),
                  key=lambda p: p.stat().st_mtime)


def _meta_for(mp4: Path) -> dict:
    p = mp4.with_suffix(".meta.json")
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


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
        print("=" * 70)
        print(p.name)
        print("=" * 70)
        print("\nTITLE:")
        print(title_for(title))
        print("\nDESCRIPTION:")
        print(description_for(title, meta.get("body", "")))
        print()


def mark_done(mp4: Path, yt_id: str = "manual") -> None:
    """Record a hand-made upload so the queue stops offering it."""
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO uploaded VALUES (?,?,?)",
                   (mp4.name, yt_id, time.time()))
    print(f"{mp4.name} marked as uploaded")


def upload_next(private: bool = True, force: bool = False,
                allow_unaudited: bool = False) -> str | None:
    """Upload at most one video, if the schedule allows it right now."""
    s = status()
    queue = pending()
    log.info("day %d, %d/%d used today, %.1fh since last, %d queued",
             s["day"], s["today"], s["allowed"], s["since_last_h"], len(queue))

    if not queue:
        log.info("nothing to upload - run main.py first")
        return None
    if not force:
        if s["today"] >= s["allowed"]:
            log.info("daily allowance reached, try tomorrow")
            return None
        if s["since_last_h"] < YT_MIN_GAP_HOURS:
            log.info("only %.1fh since the last upload, minimum is %.1fh",
                     s["since_last_h"], YT_MIN_GAP_HOURS)
            return None

    mp4 = queue[0]
    meta_path = mp4.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {}
    title = meta.get("title") or mp4.stem

    yt_id = upload(mp4, title, private=private, body=meta.get("body", ""),
                   allow_unaudited=allow_unaudited)
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO uploaded VALUES (?,?,?)",
                   (mp4.name, yt_id, time.time()))
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
    """Write it into .env instead of asking the operator to copy it by hand."""
    env = Path(__file__).parent / ".env"
    line = f"YT_REFRESH_TOKEN={token}"
    if env.exists():
        kept = [l for l in env.read_text("utf-8").splitlines()
                if not l.startswith("YT_REFRESH_TOKEN=")]
        env.write_text("\n".join(kept + [line]) + "\n", "utf-8")
    else:
        env.write_text(line + "\n", "utf-8")
    print(f"\nrefresh token saved to {env}")
    print("it is valid for a year, and only while the consent screen is out of Testing")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # kept behind a flag so normal runs print only what the operator needs
    if "--selftest" in sys.argv:
        assert title_for("Короткий заголовок").endswith(" #Shorts")
        assert len(title_for("я" * 200)) <= TITLE_MAX
        assert title_for("я" * 200).endswith(" #Shorts"), "the tag must survive trimming"
        assert len(description_for("t", "x" * 9000)) <= DESC_MAX
        assert "#Shorts" in description_for("Заголовок", "Первое. Второе. Третье.")

        # the teaser must stop at two sentences, not dump the whole story
        d = description_for("Заголовок", "Первое. Второе. Третье. Четвёртое.")
        assert "Второе." in d and "Третье." not in d, d

        # consecutive descriptions must not be clones
        variants = {description_for("Один и тот же", "Одно и то же.") for _ in range(30)}
        assert len(variants) > 5, "descriptions are not varying"

        assert daily_allowance(0) == 3 and daily_allowance(2) == 3
        assert daily_allowance(3) == 4 and daily_allowance(30) == 8
        print("title, description and schedule logic ok")
        sys.exit(0)

    try:
        if "--auth" in sys.argv:
            authorize()
        elif "--status" in sys.argv:
            s = status()
            print(f"day {s['day']}, {s['today']}/{s['allowed']} today, "
                  f"{s['total']} uploaded, {len(pending())} queued, "
                  f"audited={YT_AUDITED}")
            for p in pending()[:10]:
                print("  ", p.name)
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
                              force="--force" in sys.argv,
                              allow_unaudited="--i-know" in sys.argv))
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            print(upload(mp4, mp4.stem, private="--public" not in sys.argv,
                         allow_unaudited="--i-know" in sys.argv))
        else:
            print("usage: python youtube.py --auth | --status | --text [file] "
                  "| --done <file> | --next [--public] | out/<id>.mp4 | --selftest")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
