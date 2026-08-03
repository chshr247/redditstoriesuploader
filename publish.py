"""Step 5: upload a finished mp4 to TikTok.

Two backends, chosen per channel by TIKTOK_BACKEND, sharing every queue rule
between them - the count, the gap, the pause and the part exemption are about
the channel, not about how the bytes leave.

  api (default) - the official Content Posting API. Two targets of its own:

      drafts (default) - scope video.upload, endpoint .../inbox/video/init/.
          Lands in the app's inbox; you tap publish yourself. Works with an
          unaudited app, which is what a new developer account has.
      direct  (--direct) - scope video.publish, endpoint .../video/init/.
          Posts for real. TikTok only grants that scope to audited apps.

  tau - our patched fork of makiisthenes/TiktokAutoUploader, run as a
      subprocess. It drives the web endpoints with a saved browser cookie, so
      it posts for real WITH the caption and needs nobody. It also breaks
      TikTok's ToS and can cost the account - see todo.md section 11 and
      tau/README.md. Opt-in, never the default, and it cannot run on CI.

Default is drafts on purpose: nothing here posts publicly unless you ask, and
that holds for both backends - --public is what lifts it, on either.

    python publish.py --auth              one-time, gets the refresh token
    python publish.py --next              send the oldest unsent mp4
    python publish.py --due               may another one go out today?
    python publish.py --status            what is queued, what already went
    python publish.py out/<id>.mp4 [--direct] [--public]

A draft carries no caption - TikTok's inbox endpoint takes the file and nothing
else, the text is typed in the app at publish time. So on the api backend
--next prints the caption it would have used; that print is the only place it
exists. The tau backend sends the caption with the video and prints nothing,
because there the print would read as a job still to do.
"""
import hashlib
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import tags as tags_          # `tags` is the local variable in caption()
from config import (CHANNEL, DB_PATH, DECLARE_AI, DEFAULT_CHANNEL,
                    LOCAL_DB_PATH, OUT_DIR,
                    PART_GAP_HOURS, TIKTOK_BACKEND, TIKTOK_CLIENT_KEY,
                    TIKTOK_CLIENT_SECRET, TIKTOK_ENABLED, TIKTOK_MIN_GAP_HOURS,
                    TIKTOK_PER_DAY, TIKTOK_PROXY, TIKTOK_PUBLIC,
                    TIKTOK_REFRESH_KEY,
                    TIKTOK_REFRESH_TOKEN, TIKTOK_TAU_DIR, TIKTOK_TAU_PYTHON,
                    TIKTOK_TAU_UA, TIKTOK_TAU_USER, chan_key, save_env)

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
# The tau backend uploads the file, then waits on a headless Chromium to
# compute a signature. Generous on purpose: the failure this guards against is
# a hung browser, and a real send on a slow line is not it.
TAU_TIMEOUT = int(os.getenv("TIKTOK_TAU_TIMEOUT", 1800))

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
                      (TIKTOK_REFRESH_KEY, TIKTOK_REFRESH_TOKEN)]:
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
    # Under this channel's own key: the two accounts are unrelated, and writing
    # a rotated English token over TIKTOK_REFRESH_TOKEN would take the Russian
    # account offline the next time its token was needed.
    if r.get("refresh_token") and r["refresh_token"] != TIKTOK_REFRESH_TOKEN:
        save_env(TIKTOK_REFRESH_KEY, r["refresh_token"])
        log.info("refresh token rotated, saved to .env as %s", TIKTOK_REFRESH_KEY)
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

    save_env(TIKTOK_REFRESH_KEY, r["refresh_token"])
    print(f"\nrefresh token saved to .env as {TIKTOK_REFRESH_KEY}")
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
    """Send the file by whichever backend this channel runs. Returns an id.

    This is the whole of the difference between the two. Everything around it -
    which file is next, whether today's allowance has room, how long since the
    last one, whether the channel is paused - is about the channel and stays
    shared, which is why none of it had to move.
    """
    if TIKTOK_BACKEND == "tau":
        return _upload_tau(mp4, title, private=private, body=body, kind=kind)
    return _upload_api(mp4, title, direct=direct, private=private,
                       body=body, kind=kind)


def _tau_python() -> str:
    """The fork's own interpreter, never ours.

    It wants playwright, moviepy and undetected-chromedriver from git. Letting
    that share this venv is how both end up broken, so the checkout carries its
    own and we go looking for it rather than importing anything.
    """
    if TIKTOK_TAU_PYTHON:
        return TIKTOK_TAU_PYTHON
    root = Path(TIKTOK_TAU_DIR)
    for rel in ("Scripts/python.exe", "bin/python"):
        if (exe := root / ".venv" / rel).exists():
            return str(exe)
    raise RuntimeError(
        f"no venv under {root / '.venv'} - create one as tau/README.md says, "
        f"or point {chan_key('TIKTOK_TAU_PYTHON', True)} at an interpreter")


# Printed by the fork on success, and only there - see tau/tau-synergy.patch.
_CREATION_ID = re.compile(r"^creation_id=(\S+)", re.M)


def _upload_tau(mp4, title: str, private: bool = True, body: str = "",
                kind: str = "story") -> str:
    """Post for real through the patched fork. Returns "tau:<creation_id>".

    There is no draft here and no --direct to ask for one: the fork drives the
    web endpoints and those publish. `private` is the brake, and a real one -
    visibility_type=1 puts the video up private, which is what --next does
    unless told --public. Same default as the API path and for the same reason.

    The caption goes WITH the video here, which is the point of the whole
    exercise: on the API path it can only be printed and retyped by hand.
    """
    if not TIKTOK_TAU_DIR or not TIKTOK_TAU_USER:
        raise RuntimeError(
            f"backend tau needs {chan_key('TIKTOK_TAU_DIR', True)} and "
            f"{chan_key('TIKTOK_TAU_USER')} set - see tau/README.md")
    if not TIKTOK_PROXY:
        # Not fatal. Running without a proxy is a bad idea, not a broken
        # config - but it is a bad idea that is invisible unless something
        # says so, and with two channels it means both accounts share an IP.
        log.warning("%s is unset: this posts from the real IP, and every "
                    "channel on this machine shares it",
                    chan_key("TIKTOK_PROXY"))
    if not TIKTOK_TAU_UA:
        # Same shape of problem: it works, and it works while presenting a
        # browser the account has never been seen in.
        log.warning("%s is unset: the fork will invent a random user agent, "
                    "so this upload will not look like the browser that "
                    "logged in", chan_key("TIKTOK_TAU_UA"))

    text = caption(title, body=body, kind=kind)
    cmd = [_tau_python(), "cli.py", "upload",
           "-u", TIKTOK_TAU_USER,
           "-v", str(Path(mp4).resolve()),
           "-t", text,
           "-vi", "1" if private else "0",
           "-ai", "1" if DECLARE_AI else "0"]
    if TIKTOK_PROXY:
        cmd += ["-p", TIKTOK_PROXY]

    # cwd is load-bearing: the fork reads ./config.txt and resolves CookiesDir
    # against the working directory, so it has to run from its own root. The
    # proxy is passed twice on purpose - the flag reaches the uploader's
    # requests session, the environment variable is what the patched login
    # browser and the patched signer read. Miss the second and the signature
    # subprocess still goes out over the real IP, which was the whole bug.
    env = {**os.environ}
    if TIKTOK_PROXY:
        env["TIKTOK_PROXY"] = TIKTOK_PROXY
    if TIKTOK_TAU_UA:
        # Read by the patched upload_video in place of a random one, and by the
        # signer, so the signature is computed under the agent it is signing
        # for rather than a second unrelated browser.
        env["TIKTOK_UA"] = TIKTOK_TAU_UA
    # We read the child as UTF-8. On Windows a piped python writes cp1252 by
    # default, and the one thing we would want to read is the failure it prints
    # - which contains the caption, in Cyrillic. Without this the id still
    # parses (it is ASCII) and the error message arrives as mojibake, which is
    # the worst of both.
    env["PYTHONIOENCODING"] = "utf-8"
    log.info("tau: posting %s as %s%s", Path(mp4).name, TIKTOK_TAU_USER,
             " (private)" if private else " (PUBLIC)")
    try:
        r = subprocess.run(cmd, cwd=TIKTOK_TAU_DIR, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=TAU_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"tau upload timed out after {TAU_TIMEOUT}s. The video may or may "
            f"not have posted - check the account before running again, "
            f"nothing was written to the queue.") from None
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise RuntimeError(f"tau upload failed (exit {r.returncode}):\n{out[-1500:]}")
    if not (m := _CREATION_ID.search(out)):
        # Upstream exits 0 on paths that posted nothing, which is exactly what
        # the patch's creation_id line exists to tell apart. No line, no post:
        # do not write a row claiming there was one, or the file is marked sent
        # and never goes out again.
        raise RuntimeError(
            "tau exited 0 but printed no creation_id - either the checkout is "
            "unpatched (apply tau/tau-synergy.patch) or the upload failed "
            f"quietly:\n{out[-1500:]}")
    log.info("tau: posted %s as %s", Path(mp4).name, m.group(1))
    return f"tau:{m.group(1)}"


def _upload_api(mp4, title: str, direct: bool = False, private: bool = True,
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
    """What became of an upload. API backend only, and that is not a gap.

    A tau: row has no publish_id to ask about - the fork drives the web
    endpoints, which hand back a creation id that /status/fetch/ has never
    heard of. Saying so beats a 400 that reads like a broken token.
    """
    if publish_id.startswith("tau:"):
        raise RuntimeError(
            f"{publish_id} was posted through the tau backend; the Content "
            "Posting API knows nothing about it. Look at the account.")
    r = _post(f"{API}/post/publish/status/fetch/",
              {"publish_id": publish_id}, access_token())
    return r.get("data", r)


# --------------------------------------------------------------------- queue

def _db_path() -> Path:
    """Which file holds this channel's TikTok state.

    The api backend runs on CI, and CI commits seen.db back to the repo, so
    that is where its rows belong. The tau backend runs on somebody's desk,
    writing into a file CI rewrites twice an hour - and the moment a pull is
    resolved in CI's favour, the row saying a video went out is gone, the file
    returns to pending(), and the next --next posts it again. Publicly, now.

    Observed rather than predicted: it happened on the first merge to main.
    """
    return LOCAL_DB_PATH if TIKTOK_BACKEND == "tau" else DB_PATH


def _adopt(db) -> None:
    """Move this channel's existing rows into the local file, once.

    Switching a channel to tau must not make its history look unsent, or every
    already-published video is a candidate to go out again.
    """
    if not DB_PATH.exists():
        return
    src = sqlite3.connect(DB_PATH)
    try:
        cols = {c[1] for c in src.execute("PRAGMA table_info(tiktok)")}
    except sqlite3.Error:
        return
    if "file" not in cols:
        return
    # A db written before the backend column existed is all api by definition.
    backend = "backend" if "backend" in cols else "'api'"
    rows = src.execute(
        "SELECT file, publish_id, ts, channel, " + backend +
        " FROM tiktok WHERE channel=?", (CHANNEL,)).fetchall()
    src.close()
    if rows:
        db.executemany("INSERT OR IGNORE INTO tiktok(file, publish_id, ts,"
                       " channel, backend) VALUES (?,?,?,?,?)", rows)
        log.info("adopted %d row(s) for channel %s into %s",
                 len(rows), CHANNEL, LOCAL_DB_PATH.name)


def _db():
    # Separate table from youtube's `uploaded`: the two platforms are not in
    # step. A video can sit in the TikTok inbox for days before it is published
    # by hand, and a failed TikTok run must not hide the video from YouTube.
    path = _db_path()
    first = path is LOCAL_DB_PATH and not path.exists()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS tiktok("
               "file TEXT PRIMARY KEY, publish_id TEXT, ts REAL)")
    # Which TikTok account it went to. The two are unrelated accounts with a
    # daily count each, so every query below is scoped - see the same note in
    # youtube.py. Rows written before the second channel existed are the
    # default channel's, which is where they went.
    cols = {c[1] for c in db.execute("PRAGMA table_info(tiktok)")}
    if "channel" not in cols:
        db.execute("ALTER TABLE tiktok ADD COLUMN channel TEXT")
        db.execute("UPDATE tiktok SET channel=?", (DEFAULT_CHANNEL,))
    # Which transport sent it, because the id in publish_id means two different
    # things and only one of them can be asked about later. Every row that
    # already exists predates the second backend, so they are all the API's.
    if "backend" not in cols:
        db.execute("ALTER TABLE tiktok ADD COLUMN backend TEXT")
        db.execute("UPDATE tiktok SET backend='api'")
    if first:
        _adopt(db)
    return db


def pending() -> list:
    """This channel's rendered videos not yet sent to TikTok, oldest first."""
    from youtube import _mine          # local: avoids a cycle

    with _db() as db:
        done = {r[0] for r in db.execute("SELECT file FROM tiktok")}
    return sorted((p for p in OUT_DIR.glob("*.mp4")
                   if p.name not in done and _mine(p)),
                  key=lambda p: p.stat().st_mtime)


def sent_today() -> int:
    """Drafts sent on today's UTC calendar day - same clock youtube.py uses."""
    from youtube import _utc_date

    today = _utc_date(time.time())
    with _db() as db:
        rows = db.execute("SELECT ts FROM tiktok WHERE channel=?",
                          (CHANNEL,)).fetchall()
    return sum(1 for (ts,) in rows if _utc_date(ts) == today)


def _since_last_h() -> float:
    """Hours since the last draft was sent, or a large number if none ever was."""
    with _db() as db:
        (last,) = db.execute("SELECT MAX(ts) FROM tiktok WHERE channel=?",
                             (CHANNEL,)).fetchone()
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
    if not TIKTOK_ENABLED:
        return f"TikTok is paused for this channel ({chan_key('TIKTOK_ENABLED')}=0)"
    n = sent_today()
    if n >= TIKTOK_PER_DAY:
        return f"daily allowance reached ({n}/{TIKTOK_PER_DAY})"
    if (h := _since_last_h()) < TIKTOK_MIN_GAP_HOURS:
        return (f"only {h:.1f}h since the last draft, "
                f"minimum is {TIKTOK_MIN_GAP_HOURS:.1f}h")
    return ""


def _blocked(meta: dict, force: bool = False) -> str:
    """Why this particular file may not go out now, empty if it may."""
    # Ahead of --force and ahead of the part exemption below, both deliberately.
    # A paused channel that still delivers when a run is forced, or whenever a
    # story happens to be split, is not paused - and the part exemption is
    # exactly the case a quota of zero would have missed.
    if not TIKTOK_ENABLED:
        return f"TikTok is paused for this channel ({chan_key('TIKTOK_ENABLED')}=0)"
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
        # Named columns rather than positional: the row grew a fifth field and
        # the next one should not silently land in the wrong place.
        db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts, channel,"
                   " backend) VALUES (?,?,?,?,?)",
                   (mp4.name, pid, time.time(), CHANNEL, TIKTOK_BACKEND))
    # Only worth printing where it is the only copy that exists. A draft gets
    # no caption from the API, so this print IS the caption and the workflow
    # forwards it to be pasted by hand; the tau backend already sent it with
    # the video, and printing it there would read as "still to do".
    if not direct and TIKTOK_BACKEND == "api":
        print("\nCAPTION:\n" + caption(title, body=meta.get("body", ""),
                                       kind=meta.get("kind", "story")) + "\n")
    return pid


def _public() -> bool:
    """Does this run publish visibly? The channel decides, --public overrides.

    A single source for the question, because it is asked from two CLI branches
    and getting different answers out of them is the kind of bug that shows up
    as "where did last Tuesday go".
    """
    return TIKTOK_PUBLIC or "--public" in sys.argv


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
    # The body is matched too, not just the title - it is where most of the
    # topic words are, and passing it is the whole reason caption() takes it.
    # Fixtures in this channel's language: caption() reads its own buckets.
    _TOPIC, _FACT = {
        "ru": ("Свекровь въехала в квартиру.", "Кровь синеет из-за меди."),
        "en": ("My mother-in-law moved into the apartment.",
               "The blue comes from copper."),
    }[CHANNEL]
    assert set(caption("Заголовок", body=_TOPIC).split()) & tags_.TOPIC_TAGS[CHANNEL]
    # ...and a fact carries nothing from the story pool that is not also a
    # fact tag - #рекомендации belongs to both, #драма to exactly one
    _story_only = (set(tags_.GENERIC[CHANNEL]["story"])
                   - set(tags_.GENERIC[CHANNEL]["fact"]))
    assert not (set(caption("Факт", body=_FACT, kind="fact").split()) & _story_only)

    # A spent allowance stops an ordinary video and never a part: the inbox
    # must not end up holding the middle of a story with no beginning.
    # The restore is load-bearing - these run before every CLI command, and a
    # leaked ceiling would leave the gate saying "due" forever.
    _real_per_day, _real_gap = TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS
    _real_part_gap = PART_GAP_HOURS
    _real_enabled = TIKTOK_ENABLED
    try:
        # A paused channel sends nothing at all: not an ordinary video, not a
        # part of a split story, and not on --force. The part is the one that
        # matters - it is exempt from the daily count, so pausing by setting
        # the count to zero would have kept delivering the middles of stories.
        TIKTOK_ENABLED = False
        assert _blocked({}), "a paused channel must refuse"
        assert _blocked({}, force=True), "--force must not resume a paused channel"
        assert _blocked({"part": 2, "total": 2}), "a part must not slip past a pause"
        assert "paused" in due(), due()
        TIKTOK_ENABLED = True
    finally:
        TIKTOK_ENABLED = _real_enabled
    try:
        # These are about the count and the gap, so they run as an ENABLED
        # channel whatever this one is - otherwise the whole block starts
        # failing the day a channel is paused, which is when it is least
        # helpful to lose the tests.
        TIKTOK_ENABLED = True
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
        TIKTOK_ENABLED = _real_enabled
    # The fork prints one line we care about, and everything else it prints is
    # noise we must not mistake for it - including its own echo of the caption,
    # which can itself contain the word.
    assert _CREATION_ID.search("Uploading video...\ncreation_id=abc123\n").group(1) == "abc123"
    assert _CREATION_ID.search("creation_id=x\nPublished") .group(1) == "x"
    assert not _CREATION_ID.search("see creation_id=abc in the docs"), \
        "the line must start the line, or a caption could forge one"
    assert not _CREATION_ID.search("[-] Publish failed to Tiktok")

    # The state of a channel CI does not publish must not live in the file CI
    # rewrites. This is the assertion that would have caught the merge that
    # dropped a real post's row and put the video back in the queue.
    assert (_db_path() == LOCAL_DB_PATH) == (TIKTOK_BACKEND == "tau"), _db_path()
    assert LOCAL_DB_PATH != DB_PATH, "the local file must not BE the tracked one"

    # A tau id is not a publish_id and must not be sent to the API as one.
    try:
        status("tau:abc")
        raise AssertionError("status() must refuse a tau id")
    except RuntimeError as e:
        assert "tau backend" in str(e), e

    # And the tau path refuses to run half-configured rather than shelling out
    # to a checkout that is not there.
    if not (TIKTOK_TAU_DIR and TIKTOK_TAU_USER):
        try:
            _upload_tau("out/nothing.mp4", "t")
            raise AssertionError("_upload_tau must refuse without a checkout")
        except RuntimeError as e:
            assert "tau/README.md" in str(e), e

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
                rows = db.execute("SELECT file, publish_id, backend FROM tiktok "
                                  "WHERE channel=? ORDER BY ts DESC",
                                  (CHANNEL,)).fetchall()
            print(f"channel {CHANNEL} via {TIKTOK_BACKEND}: {len(rows)} sent, "
                  f"{sent_today()}/{TIKTOK_PER_DAY} today, {len(pending())} queued"
                  + ("" if TIKTOK_ENABLED else "  [PAUSED]"))
            for f, pid, backend in rows[:5]:
                print("  sent  ", f, pid, f"({backend or 'api'})")
            for p in pending()[:10]:
                print("  queued", p.name)
        elif "--next" in sys.argv:
            print(upload_next(direct="--direct" in sys.argv,
                              private=not _public(),
                              force="--force" in sys.argv))
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            # the real title lives beside the file, written by main.py; the stem
            # is only a post id, which is what the caption used to say
            from youtube import _meta_for, part_prefix
            meta = _meta_for(mp4)
            pid = upload(mp4, part_prefix(meta) + (meta.get("title") or mp4.stem),
                         direct="--direct" in sys.argv,
                         private=not _public(),
                         body=meta.get("body", ""),
                         kind=meta.get("kind", "story"))
            # Record it, exactly as upload_next() does. This path skips the
            # gate on purpose - it is the by-hand escape hatch - but skipping
            # the gate is not the same as leaving no trace: an unrecorded file
            # stays in pending() and the next --next sends it a second time.
            # Cheap when that meant a duplicate draft, not cheap at all now
            # that a tau send is a published video.
            with _db() as db:
                db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts,"
                           " channel, backend) VALUES (?,?,?,?,?)",
                           (mp4.name, pid, time.time(), CHANNEL, TIKTOK_BACKEND))
            # A tau post has no status to fetch, and asking is an error rather
            # than an empty answer - see status().
            print(pid, "(posted; the API has no status for a tau id)"
                  if pid.startswith("tau:") else status(pid))
        else:
            print("usage: python publish.py --auth | --whoami | --status | "
                  "--due | --next | out/<id>.mp4 [--direct] [--public]")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
