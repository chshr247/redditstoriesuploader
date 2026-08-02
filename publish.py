"""Step 5: upload a finished mp4 to TikTok via the Content Posting API.

Two targets, and the difference matters:

  drafts (default) - scope video.upload, endpoint .../inbox/video/init/.
      Lands in the app's inbox; you tap publish yourself. Works with an
      unaudited app, which is what a new developer account has.
  direct  (--direct) - scope video.publish, endpoint .../video/init/.
      Posts for real. TikTok only grants that scope to audited apps.

Default is drafts on purpose: nothing here posts publicly unless you ask.

    python publish.py --auth              one-time, gets the refresh token
    python publish.py --next              send the oldest unsent mp4 to drafts
    python publish.py --due               may another draft go out today?
    python publish.py --status            what is queued, what already went
    python publish.py out/<id>.mp4 [--direct] [--public]

A draft carries no caption - TikTok's inbox endpoint takes the file and nothing
else, the text is typed in the app at publish time. So --next prints the caption
it would have used; that print is the only place it exists.
"""
import hashlib
import json
import logging
import os
import random
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import tags as tags_          # `tags` is the local variable in caption()
from config import (DB_PATH, DECLARE_AI, OUT_DIR, PART_GAP_HOURS,
                    TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
                    TIKTOK_MIN_GAP_HOURS, TIKTOK_PER_DAY, TIKTOK_REFRESH_TOKEN,
                    save_env)

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
# Drafts only by default, because that is all a sandbox client is given. Asking
# for video.publish there does not fail at the token exchange - it fails at the
# consent screen, before anything is granted. Add it to TIKTOK_SCOPES the day
# the app is audited.
SCOPES = os.getenv("TIKTOK_SCOPES", "video.upload")
# Registered under the Login Kit product, and the app type decides what is
# legal here: a desktop client may use http://localhost:<port>/..., a web one
# must use https. Set TIKTOK_REDIRECT to whichever the app is configured with.
# A localhost value is caught by a local server; anything else falls back to
# pasting the redirected URL, which needs no reachable host at all.
REDIRECT = os.getenv("TIKTOK_REDIRECT", "http://localhost:8080/callback")
CHUNK = 10_000_000        # the size TikTok's own docs use in their example
TITLE_MAX = 2200          # UTF-16 runes, per the direct-post reference

log = logging.getLogger(__name__)


def whoami() -> dict:
    """Which account the refresh token actually belongs to.

    Worth the extra scope: an upload that answers SEND_TO_USER_INBOX and never
    shows up is either a sandbox that does not deliver or the wrong account
    logged into the phone, and nothing else tells those two apart.
    """
    req = urllib.request.Request(
        f"{API}/user/info/?fields=open_id,display_name",
        headers={"Authorization": f"Bearer {access_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("data", {})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"user/info -> {e.code}: {e.read().decode()[:400]}") from None


def _post(url: str, body: dict, token: str = "", form: bool = False) -> dict:
    data = (urllib.parse.urlencode(body).encode() if form
            else json.dumps(body).encode())
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form
               else "application/json; charset=UTF-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} -> {e.code}: {e.read().decode()[:400]}") from None


def access_token() -> str:
    """Short-lived token from the long-lived refresh token in .env."""
    for name, val in [("TIKTOK_CLIENT_KEY", TIKTOK_CLIENT_KEY),
                      ("TIKTOK_CLIENT_SECRET", TIKTOK_CLIENT_SECRET),
                      ("TIKTOK_REFRESH_TOKEN", TIKTOK_REFRESH_TOKEN)]:
        if not val:
            raise RuntimeError(f"{name} is empty - see the OAuth note in publish.py")

    r = _post(f"{API}/oauth/token/", {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": TIKTOK_REFRESH_TOKEN,
    }, form=True)
    if "access_token" not in r:
        raise RuntimeError(f"no access_token in response: {r}")
    # Measured: the same token comes back every time. But the docs only promise
    # it "may" be unchanged, so save a new one if it ever appears - the whole
    # cost of being wrong here is a dead pipeline discovered a day later.
    if r.get("refresh_token") and r["refresh_token"] != TIKTOK_REFRESH_TOKEN:
        save_env("TIKTOK_REFRESH_TOKEN", r["refresh_token"])
        log.info("refresh token rotated, saved to .env")
    return r["access_token"]


def _catch_locally() -> dict:
    """Serve one request on the redirect port and return its query string."""
    got = {}
    port = urllib.parse.urlparse(REDIRECT).port or 80

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write("Готово, можно закрывать вкладку.".encode())

        def log_message(self, *a):
            pass

    with HTTPServer(("localhost", port), Handler) as srv:
        srv.handle_request()
    return got


def authorize() -> None:
    """One-time browser round trip. Writes the refresh token into .env.

    Desktop clients must use PKCE, and TikTok wants the challenge HEX-encoded -
    not the base64url that the OAuth spec and every library default to. Getting
    that wrong fails at the token exchange, not at the consent screen.
    """
    if not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
        raise RuntimeError("set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first")

    verifier = secrets.token_hex(48)                       # 96 chars, within 43-128
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    state = secrets.token_urlsafe(16)

    url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_key": TIKTOK_CLIENT_KEY, "response_type": "code",
        "scope": SCOPES, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("opening:\n", url)
    webbrowser.open(url)

    if REDIRECT.startswith("http://localhost"):
        got = _catch_locally()
    else:
        # An https redirect points somewhere we cannot listen on. The code is
        # in the address bar either way, so read it from there.
        print("\nAfter approving, copy the FULL address from the browser bar.")
        got = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(input("paste it here: ").strip()).query))

    if got.get("state") != state:
        raise RuntimeError("state mismatch - the callback did not come from our request")
    if not got.get("code"):
        raise RuntimeError(f"no code came back: {got}")

    r = _post(f"{API}/oauth/token/", {
        "client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_CLIENT_SECRET,
        "code": urllib.parse.unquote(got["code"]),
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT, "code_verifier": verifier}, form=True)
    if "refresh_token" not in r:
        raise RuntimeError(f"no refresh_token in the response: {r}")

    save_env("TIKTOK_REFRESH_TOKEN", r["refresh_token"])
    print("\nrefresh token saved to .env")
    print(f"scopes granted: {r.get('scope')}")


def caption(title: str, hashtags=None, body: str = "",
            kind: str = "story") -> str:
    """Title plus tags matched to the video, trimmed to TikTok's limit.

    Deliberately shorter than the YouTube description: TikTok shows two lines
    before the fold, so anything past the hook is scrolled past anyway.

    Tags come from tags.py rather than from a shuffled flat pool - see the note
    in youtube.description_for(). `body` is what lets them match on more than
    the title; it sits in the meta file beside the mp4, so it costs nothing.
    """
    pool = (tags_.pick(title, body, kind) if hashtags is None
            else random.sample(list(hashtags), min(5, len(hashtags))))
    tags = " ".join(pool[:5])
    text = f"{title.strip()}\n\n{tags}"
    if len(text) > TITLE_MAX:
        text = title.strip()[:TITLE_MAX - len(tags) - 6].rstrip() + "...\n\n" + tags
    return text


def _plan(size: int) -> list[tuple[int, int]]:
    """Byte ranges per chunk. The last chunk absorbs the remainder."""
    if size <= CHUNK:
        return [(0, size - 1)]
    count = size // CHUNK
    spans = [(i * CHUNK, (i + 1) * CHUNK - 1) for i in range(count)]
    spans[-1] = (spans[-1][0], size - 1)      # tail rides along with the last one
    return spans


def _send(upload_url: str, path: Path, spans: list) -> None:
    size = path.stat().st_size
    with open(path, "rb") as f:
        for start, end in spans:
            f.seek(start)
            blob = f.read(end - start + 1)
            req = urllib.request.Request(upload_url, data=blob, method="PUT", headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(blob)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            })
            with urllib.request.urlopen(req, timeout=300) as r:
                log.info("chunk %d-%d/%d -> %s", start, end, size, r.status)


def upload(mp4, title: str, direct: bool = False, private: bool = True,
           body: str = "", kind: str = "story") -> str:
    """Upload the file. Returns publish_id. Drafts unless direct=True."""
    mp4 = Path(mp4)
    size = mp4.stat().st_size
    spans = _plan(size)
    token = access_token()

    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": spans[0][1] - spans[0][0] + 1,
              "total_chunk_count": len(spans)}

    if direct:
        body = {"source_info": source, "post_info": {
            "title": caption(title, body=body, kind=kind),
            "privacy_level": "SELF_ONLY" if private else "PUBLIC_TO_EVERYONE",
            # TikTok's rules are their own - see DECLARE_AI in config.py
            "is_aigc": DECLARE_AI,
        }}
        url = f"{API}/post/publish/video/init/"
    else:
        body = {"source_info": source}
        url = f"{API}/post/publish/inbox/video/init/"

    try:
        r = _post(url, body, token)
    except RuntimeError as e:
        # The code names the client, but the fix is on the account: until the
        # app is audited, a direct post only lands on an account set to private.
        if "unaudited_client_can_only_post_to_private_accounts" in str(e):
            raise RuntimeError(
                "TikTok refuses a direct post from an unaudited app to a public "
                "account. Either switch the account to private for the duration, "
                "or drop --direct and let the video land in the app inbox as a "
                "draft.") from None
        raise

    data = r.get("data") or {}
    if not data.get("publish_id"):
        raise RuntimeError(f"init failed: {r}")

    _send(data["upload_url"], mp4, spans)
    log.info("uploaded %s as %s", mp4.name, data["publish_id"])
    return data["publish_id"]


def status(publish_id: str) -> dict:
    r = _post(f"{API}/post/publish/status/fetch/",
              {"publish_id": publish_id}, access_token())
    return r.get("data", r)


# --------------------------------------------------------------------- queue

def _db():
    # Separate table from youtube's `uploaded`: the two platforms are not in
    # step. A video can sit in the TikTok inbox for days before it is published
    # by hand, and a failed TikTok run must not hide the video from YouTube.
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS tiktok("
               "file TEXT PRIMARY KEY, publish_id TEXT, ts REAL)")
    return db


def pending() -> list:
    """Rendered videos not yet sent to TikTok, oldest first."""
    with _db() as db:
        done = {r[0] for r in db.execute("SELECT file FROM tiktok")}
    return sorted((p for p in OUT_DIR.glob("*.mp4") if p.name not in done),
                  key=lambda p: p.stat().st_mtime)


def sent_today() -> int:
    """Drafts sent on today's UTC calendar day - same clock youtube.py uses."""
    from youtube import _utc_date

    today = _utc_date(time.time())
    with _db() as db:
        rows = db.execute("SELECT ts FROM tiktok").fetchall()
    return sum(1 for (ts,) in rows if _utc_date(ts) == today)


def _since_last_h() -> float:
    """Hours since the last draft was sent, or a large number if none ever was."""
    with _db() as db:
        (last,) = db.execute("SELECT MAX(ts) FROM tiktok").fetchone()
    return (time.time() - last) / 3600 if last else 999


def due() -> str:
    """Empty string if another draft may go out now, else why it may not.

    A count and a gap, same as YouTube. The gap was left out at first on the
    grounds that a draft has no publish time - it waits in the inbox until a
    human posts it, so spacing the deliveries spaces nothing. That reasoning
    skips a step: the human is prompted by the draft landing, and posts it
    shortly after. Two drafts forty minutes apart are two videos forty minutes
    apart, which is the thing the gap exists to prevent.
    """
    n = sent_today()
    if n >= TIKTOK_PER_DAY:
        return f"daily allowance reached ({n}/{TIKTOK_PER_DAY})"
    if (h := _since_last_h()) < TIKTOK_MIN_GAP_HOURS:
        return (f"only {h:.1f}h since the last draft, "
                f"minimum is {TIKTOK_MIN_GAP_HOURS:.1f}h")
    return ""


def _blocked(meta: dict, force: bool = False) -> str:
    """Why this particular file may not go out now, empty if it may."""
    if force:
        return ""
    if meta.get("total", 0) > 1:
        # A part still ignores the count - the inbox must not be left holding
        # the middle of a story with no beginning - but not the clock. It runs
        # on the shorter one YouTube gives parts: an answer to a cliffhanger is
        # worth nothing hours later, and worth nothing minutes later either,
        # because both halves then sit in the feed as one block.
        if (h := _since_last_h()) < PART_GAP_HOURS:
            return (f"only {h:.1f}h since the last draft, "
                    f"minimum is {PART_GAP_HOURS:.1f}h for a part")
        return ""
    return due()


def upload_next(direct: bool = False, private: bool = True,
                force: bool = False) -> str | None:
    """Send one video, if today's allowance still has room for it.

    The allowance is checked here and not at the gate, because only here is it
    known WHAT the file is - and a part of a split story ignores it. Skipping a
    part leaves the inbox with the middle of a story and no beginning: the run
    that made part 1 can hit a spent allowance while YouTube still had room,
    and part 2 then arrives a day later against a fresh count. Three extra
    drafts on a split day are cheaper than that.
    """
    from youtube import _meta_for, part_prefix     # local: avoids a cycle

    queue = pending()
    if not queue:
        log.info("nothing pending for TikTok")
        return None

    mp4 = queue[0]
    meta = _meta_for(mp4)
    if reason := _blocked(meta, force):
        log.info("%s", reason)
        return None

    title = part_prefix(meta) + (meta.get("title") or mp4.stem)
    pid = upload(mp4, title, direct=direct, private=private,
                 body=meta.get("body", ""), kind=meta.get("kind", "story"))
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO tiktok VALUES (?,?,?)",
                   (mp4.name, pid, time.time()))
    if not direct:
        print("\nCAPTION:\n" + caption(title, body=meta.get("body", ""),
                                       kind=meta.get("kind", "story")) + "\n")
    return pid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    assert _plan(5_000_000) == [(0, 4_999_999)]
    assert _plan(CHUNK) == [(0, CHUNK - 1)]
    p = _plan(54_100_000)
    assert len(p) == 5 and p[0] == (0, 9_999_999) and p[-1] == (40_000_000, 54_099_999)
    assert sum(e - s + 1 for s, e in p) == 54_100_000, "chunks must cover the file"
    assert all(a[1] + 1 == b[0] for a, b in zip(p, p[1:])), "gap between chunks"
    assert caption("Короткий").splitlines()[0] == "Короткий"
    assert len(caption("x" * 3000)) <= TITLE_MAX
    assert len({caption("Один и тот же") for _ in range(30)}) > 5, "tags must rotate"
    # the body is matched too, not just the title - it is where most of the
    # topic words are, and passing it is the whole reason caption() takes it
    assert set(caption("Заголовок", body="Свекровь въехала в квартиру.").split()) \
        & set(tags_.TOPIC_TAGS)
    # ...and a fact carries nothing from the story pool that is not also a
    # fact tag - #рекомендации belongs to both, #драма to exactly one
    _story_only = set(tags_.GENERIC["story"]) - set(tags_.GENERIC["fact"])
    assert not (set(caption("Факт", body="Кровь синеет из-за меди.",
                            kind="fact").split()) & _story_only)

    # A spent allowance stops an ordinary video and never a part: the inbox
    # must not end up holding the middle of a story with no beginning.
    # The restore is load-bearing - these run before every CLI command, and a
    # leaked ceiling would leave the gate saying "due" forever.
    _real_per_day, _real_gap = TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS
    _real_part_gap = PART_GAP_HOURS
    try:
        TIKTOK_MIN_GAP_HOURS = PART_GAP_HOURS = 0   # the count is these four
        TIKTOK_PER_DAY = 0
        assert _blocked({"part": 2, "total": 2}) == "", "a part ignores the count"
        assert _blocked({}), "an ordinary video obeys it"
        assert _blocked({}, force=True) == "", "--force overrides it"
        TIKTOK_PER_DAY = 10_000
        assert _blocked({}) == "", "room left, nothing to block"
        # And the gap, which room in the allowance must not be able to buy:
        # spacing is the half that was missing while this was a count alone.
        TIKTOK_MIN_GAP_HOURS = 10_000
        assert "since the last draft" in _blocked({}), "the gap must block too"
        assert _blocked({}, force=True) == "", "--force overrides the gap"
        assert _blocked({"part": 2, "total": 2}) == "", "a part keeps its own"
        # And that own clock is a clock, not an exemption: the thing a part
        # skips is the count and the ordinary spacing, never spacing itself.
        PART_GAP_HOURS = 10_000
        assert "for a part" in _blocked({"part": 2, "total": 2}), "parts are spaced"
        assert _blocked({"part": 2, "total": 2}, force=True) == "", "--force wins"
    finally:
        TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS = _real_per_day, _real_gap
        PART_GAP_HOURS = _real_part_gap
    print("chunking, caption and allowance logic ok")

    try:
        if "--auth" in sys.argv:
            authorize()
        elif "--whoami" in sys.argv:
            print(whoami())
        elif "--due" in sys.argv:
            # exit code is the point: the workflow gate asks before it spends
            reason = due()
            print(reason or "due")
            sys.exit(1 if reason else 0)
        elif "--status" in sys.argv:
            with _db() as db:
                rows = db.execute(
                    "SELECT file, publish_id FROM tiktok ORDER BY ts DESC").fetchall()
            print(f"{len(rows)} sent, {sent_today()}/{TIKTOK_PER_DAY} today, "
                  f"{len(pending())} queued")
            for f, pid in rows[:5]:
                print("  sent  ", f, pid)
            for p in pending()[:10]:
                print("  queued", p.name)
        elif "--next" in sys.argv:
            print(upload_next(direct="--direct" in sys.argv,
                              private="--public" not in sys.argv,
                              force="--force" in sys.argv))
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            # the real title lives beside the file, written by main.py; the stem
            # is only a post id, which is what the caption used to say
            from youtube import _meta_for, part_prefix
            meta = _meta_for(mp4)
            pid = upload(mp4, part_prefix(meta) + (meta.get("title") or mp4.stem),
                         direct="--direct" in sys.argv,
                         private="--public" not in sys.argv)
            print(pid, status(pid))
        else:
            print("usage: python publish.py --auth | --whoami | --status | "
                  "--due | --next | out/<id>.mp4 [--direct] [--public]")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
