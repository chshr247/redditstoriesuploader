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
      TikTok's ToS and can cost the account - see todo.md section 11 and the
      runbook in the tau checkout. Opt-in, never the default, no CI.

Default is drafts on purpose: nothing here posts publicly unless you ask, and
that holds for both backends - --public is what lifts it, on either.

    python publish.py --auth              one-time, gets the refresh token
    python publish.py --next              send the oldest unsent mp4
    python publish.py --due               may another one go out today?
    python publish.py --enabled           is this channel paused?
    python publish.py --status            what is queued, what already went
    python publish.py --stale [hours]     what TikTok took but nobody has seen
    python publish.py out/<id>.mp4 [--direct] [--public]

A draft carries no caption - TikTok's inbox endpoint takes the file and nothing
else, the text is typed in the app at publish time. So on the api backend
--next prints the caption it would have used; that print is the only place it
exists. The tau backend sends the caption with the video and prints nothing,
because there the print would read as a job still to do.
"""
import csv
import datetime
import hashlib
import json
import logging
import os
import random
import re
import secrets
import signal
import sqlite3
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import facts as facts_        # `facts` is the local variable in caption()
import source
import tags as tags_          # `tags` is the local variable in caption()
from config import (CHANNEL, DB_PATH, DECLARE_AI, DEFAULT_CHANNEL,
                    LOCAL_DB_PATH, OUT_DIR, PART_GAP_HOURS, PART_WORD, STOPPED,
                    STOP_REASON,
                    TIKTOK_BACKEND, TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
                    TIKTOK_ENABLED, TIKTOK_MIN_GAP_HOURS, TIKTOK_PER_DAY,
                    TIKTOK_PROXY, TIKTOK_PUBLIC, TIKTOK_REFRESH_KEY,
                    TIKTOK_REFRESH_TOKEN, TIKTOK_TAU_BROWSERS, TIKTOK_TAU_DIR,
                    TIKTOK_TAU_PYTHON, TIKTOK_TAU_UA, TIKTOK_TAU_UI, TIKTOK_TAU_USER,
                    chan_file, chan_key, save_env)

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
# Wall-clock cap on the whole node subprocess, both tau paths: the fork, which
# uploads and then waits on a Chromium to compute a signature, and the UI path,
# where it bounds the entire browser session end to end. Generous on purpose -
# what this guards against is a HUNG browser, and a real send on a slow line is
# not that. uipost.js keeps the finer limits itself (STEP_MS per step,
# TAU_UI_UPLOAD_WAIT for the bytes); this is only the outer envelope.
TAU_TIMEOUT = int(os.getenv("TIKTOK_TAU_TIMEOUT", 1800))

# How many times that whole send is attempted. See _tau_retryable below for
# what is being retried and why one attempt is not enough.
TAU_TRIES = int(os.getenv("TIKTOK_TAU_TRIES", 3))


def _tau_retryable(out: str) -> bool:
    """Whether a failed fork run is the transient rejection, not a real fault.

    The fork's final call - POST /tiktok/web/project/post/v1/ - answers 200
    with {"status_code":5,"status_msg":"Invalid parameters"} on roughly half of
    otherwise identical attempts. Measured 2026-08-29 on the Russian channel:
    six sends, three rejected and three accepted, with the same account,
    caption, hashtags, visibility and user agent, through both cli.py and a
    direct call. The signer is not the cause - it returned a well-formed
    signature on every run, including the rejected ones.

    The rejection is the server refusing to create the item, so nothing exists
    when it comes back and a retry cannot double-post. That is the whole reason
    this is safe to retry and an upload timeout is not.

    Scoped to this one answer on purpose: a dead cookie, a refused login or a
    broken checkout must still fail on the first attempt instead of three
    times slower.
    """
    return '"status_code":5' in out or "Invalid parameters" in out


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


def videos(fields: str = "id,title,create_time,view_count,like_count,"
                         "comment_count,share_count") -> list:
    """Every video on the account, newest first, with its counters.

    Needs video.list in TIKTOK_SCOPES. The portal grants it beside
    user.info.basic without an audit, for the app's own account - the only
    account this client ever touches. Studio's own CSV export is capped at a
    handful of top posts; this is not.
    """
    out, cursor = [], None
    while True:
        body = {"max_count": 20}
        if cursor:
            body["cursor"] = cursor
        data = _post(f"{API}/video/list/?fields={fields}", body,
                     access_token()).get("data", {})
        out += data.get("videos", [])
        cursor = data.get("cursor")
        if not data.get("has_more") or not cursor:
            return out


# "Часть 1/2 - " and "Часть 1/2. ", the two shapes the part marker has led a
# published caption in. Looser than _PART below, which matches what
# part_prefix() glued onto a title; this one reads the caption back off TikTok.
_MARKER = re.compile(r"^\W*(?:%s)\s*\d+\s*/\s*\d+\s*[-.—]*\s*"
                     % "|".join(map(re.escape, set(PART_WORD.values()))), re.I)


def _headline(caption_text: str) -> str:
    """The story's own title, back out of the caption that carried it.

    Both caption shapes are in the export and neither is written down anywhere
    else with a view count beside it: the old one was the title and its
    hashtags, the new one leads with the facts block and leaves the title after
    the last 👇. The review table keeps the title too - with its [emphasis]
    markup, and without ever learning how the video did.
    """
    t = caption_text.split("\U0001f447")[-1]
    t = re.split(r"\s+#", t)[0]
    return _MARKER.sub("", t.replace("​", "")).strip()


def hits(n: int = 12, fresh_hours: int = 12) -> str:
    """This channel's best and worst titles by views - the critic's examples.

    Read out of the --stats export rather than off the API: it is the file the
    refresh writes every third day, it can be read by eye, and it keeps videos
    that have since been taken down. The encoding is sniffed because
    PowerShell's `>` writes UTF-16 and every other shell writes UTF-8.

    `n` a side is the cap, and it is what keeps this from growing into the
    prompt: the file holds 2n lines whether the channel has fifty titles or
    five hundred, and every critic call carries all of them. The middle of the
    ranking is dropped on purpose - it is the top and the bottom that differ
    from each other, and a title sitting at the median teaches nothing.

    Twelve, and it was tried at twenty. On 86 stories that reached 1186 views
    at the bottom of the losing list against a median of 1841 - half the
    channel, with the two lists closing on the middle and the contrast the
    critic learns from going with it. Twelve keeps them apart. Raise it again
    only after the channel has enough titles that 2n is a small share of them.

    A title counts once, at its best showing. A story split over three videos
    is one title, and the later parts riding on the first one's audience are
    not three separate verdicts on how it was written. The last `fresh_hours`
    are dropped whole: a video an hour old has not been seen by anybody yet and
    would land in the bottom list on its age alone. It is a guard against
    counting nothing at all, not a wait for the number to settle - views keep
    climbing for days, and "Сосед потребовал сменить кличку собаки" read 70
    forty minutes after it went out. The refresh re-reads the whole export
    every time, so a video that was still young at one run is counted properly
    at the next.
    """
    raw = Path(chan_file("tiktok") + ".csv").read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    cutoff = time.time() - fresh_hours * 3600
    best: dict[str, int] = {}
    for r in csv.DictReader(raw.decode(enc).splitlines()):
        if datetime.datetime.fromisoformat(r["posted"]).timestamp() > cutoff:
            continue
        if title := _headline(r["title"]):
            best[title] = max(int(r["views"] or 0), best.get(title, 0))
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if len(ranked) < 2 * n:
        log.warning("%d titles is thin for %d examples a side", len(ranked), n)
        n = len(ranked) // 2
    if not n:
        raise SystemExit("no titles in the export - run --stats first")
    def rows(part: list) -> str:
        return "\n".join(f"- {v} — {t}" for t, v in part)

    return "\n".join([
        "# How titles have done on this channel", "",
        f"{len(ranked)} stories, views counted {datetime.date.today()}.", "",
        "## Landed", "", rows(ranked[:n]), "",
        "## Died", "", rows(ranked[-n:][::-1]), ""])


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


# The live token and the moment it stops being one. Per process, and that is
# the whole scope it needs: a run is one upload.
_token: tuple[str, float] = ("", 0.0)


def access_token() -> str:
    """Short-lived token from the long-lived refresh token in .env.

    Cached for as long as TikTok says it is good for. Fetching one per call was
    free while a run made exactly one API call; await_send() and stale() make a
    dozen each, and every one of them was a full OAuth round trip against an
    endpoint that has its own rate limit and no reason to be asked twice.
    """
    global _token
    tok, good_until = _token
    if tok and time.time() < good_until:
        return tok

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
    # A minute short of what TikTok promises, so a call that starts inside the
    # window cannot finish outside it. The default is only used if expires_in
    # ever goes missing; it never has.
    _token = (r["access_token"], time.time() + int(r.get("expires_in", 3600)) - 60)
    return _token[0]


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


# The word in front of "2/3" on a split story comes from config, shared with the
# title card render.py draws. The file's channel decides which one, not the
# process: a file rendered by the other channel has to be captioned as what it
# was made as.
def part_prefix(meta: dict) -> str:
    """"Часть 2/3 - " for a split story, empty for an ordinary one.

    Lives here rather than in youtube.py because splitting is TikTok's alone -
    see youtube._split(). Only the PUBLISHED caption carries the marker: the
    narrated title card stays clean, it is the hook and three syllables of
    "часть вторая" in front of it is dead air. In the feed the marker leads the
    title, which is why caption() must not trim it away.
    """
    if meta.get("total", 0) <= 1:
        return ""
    word = PART_WORD.get(meta.get("channel", DEFAULT_CHANNEL), PART_WORD["en"])
    return f"{word} {meta['part']}/{meta['total']} - "


# The part marker as caption() finds it: already glued to the front of the
# title by the caller, because that is where part_prefix() puts it. It has to
# come back off to lead the caption on its own line - see the note there.
_PART = re.compile(r"^(?:%s)\s+\d+/\d+\s+-\s+"
                   % "|".join(sorted(map(re.escape, set(PART_WORD.values())))))


def caption(title: str, hashtags=None, body: str = "") -> str:
    """Facts block, title and tags matched to the video, inside TikTok's limit.

    Two lines show before the fold, and they are spent on the facts block from
    facts.py rather than on the title. That is deliberate and it is the whole
    point of the block: the title is already burned onto the title card the
    viewer is looking at, so repeating it here buys nothing, while a line that
    promises three facts buys the seconds someone spends reading them - seconds
    the video is still playing. The title stays underneath, where it is worth
    keeping for the words in it.

    The part marker is the exception and still leads: someone who landed on
    part 2 has to learn that from the feed, not by tapping "ещё".

    Tags come from tags.py rather than from a shuffled flat pool - see the note
    in youtube.description_for(). `body` is what lets them match on more than
    the title; it sits in the meta file beside the mp4, so it costs nothing.
    """
    pool = (tags_.pick(title, body) if hashtags is None
            else random.sample(list(hashtags), min(5, len(hashtags))))
    tags = " ".join(pool[:5])
    title = title.strip()
    marker = _PART.match(title)
    mark = ""
    if marker:
        # Moved, not copied: left on the title as well it reads twice in one
        # caption, and the second one is the copy nobody meant to send.
        mark = marker.group(0).strip(" -")
        title = title[marker.end():]

    # The block is the first thing dropped if the text will not fit. It is 400
    # characters of someone else's trivia; the title and the tags are what the
    # video is about, and TITLE_MAX is 2200, so this branch is a guard rather
    # than something that happens.
    room = TITLE_MAX - len(tags) - 3
    block = facts_.block(lead=f"{mark}. " if mark else "")
    if block and len(block) + len(title) + 2 <= room:
        # Tags on the line straight under the title, not a paragraph below it:
        # they are part of the same line of text as far as a reader is
        # concerned, and a blank line there reads as a fourth section.
        return f"{block}\n\n{title}\n{tags}"

    lead = f"{mark}\n\n" if mark else ""
    room -= len(lead)
    if len(title) > room:
        title = title[:room - 3].rstrip() + "..."
    return f"{lead}{title}\n{tags}"


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
           body: str = "") -> str:
    """Send the file by whichever backend this channel runs. Returns an id.

    This is the whole of the difference between the two. Everything around it -
    which file is next, whether today's allowance has room, how long since the
    last one, whether the channel is paused - is about the channel and stays
    shared, which is why none of it had to move.
    """
    if TIKTOK_BACKEND == "tau":
        return _upload_tau(mp4, title, private=private, body=body)
    return _upload_api(mp4, title, direct=direct, private=private, body=body)


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
        f"no venv under {root / '.venv'} - create one as the tau runbook says, "
        f"or point {chan_key('TIKTOK_TAU_PYTHON', True)} at an interpreter")


# Printed by the fork on success, and only there - see tau-synergy.patch.
_CREATION_ID = re.compile(r"^creation_id=(\S+)", re.M)


# The signer's own endpoint, asked with a throwaway msToken: the answer is
# thrown away too, and only "did node and its headless Chromium come up" is
# read off it.
_SIGNER_PROBE = ("https://www.tiktok.com/api/v1/web/project/post/"
                 "?app_name=tiktok_web&channel=tiktok_web&device_platform=web"
                 "&aid=1988&msToken=warmup")
SIGNATURE_MARK = chr(34) + "signature" + chr(34)
SIGNER_TRIES = int(os.getenv("TIKTOK_TAU_SIGNER_TRIES", 4))
SIGNER_TIMEOUT = int(os.getenv("TIKTOK_TAU_SIGNER_TIMEOUT", 45))


def _run_capped(cmd, cwd, env, timeout) -> tuple[int, str]:
    """Run cmd under a timeout that actually ends it. (returncode, output).

    Popen and not subprocess.run, and the difference is the whole point of the
    timeout. Both callers spawn NODE, so the thing that hangs is a grandchild.
    run(timeout=) kills the direct child, then goes back to draining pipes the
    grandchild still holds open - and waits there with no deadline at all.
    Measured 2026-08-30: the 11:15 run sat in exactly that wait until 13:35, two
    hours past its own thirty-minute limit, and Task Scheduler's one-hour limit
    did not end it either, for the same reason. Worse than the lost video is
    what the wait takes with it - post.ps1 runs its watchdog only after
    publish.py returns, so a hang here is the one failure nobody is ever told
    about, and the channel went fourteen hours unnoticed.

    Raises TimeoutExpired, but not before the tree is gone.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        # POSIX needs the group to exist before there is anything to kill; on
        # Windows taskkill /T walks the parent chain and needs nothing.
        start_new_session=os.name != "nt")
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        # With the tree gone the pipes close and this returns at once.
        proc.communicate()
        raise
    return proc.returncode, ((stdout or "") + (stderr or "")).strip()


def _warm_signer(env) -> None:
    """Bring the fork's node signer up BEFORE the video is uploaded.

    The signer is a headless Chromium, and after the machine has been asleep it
    sometimes does not come up at all. Measured 2026-08-30 on one probe: 1.7s
    awake and unlocked, 2.0s awake and LOCKED, 4.3s after a two-minute sleep -
    then past 180s after fifteen minutes of sleep, and past two hours after the
    seven the channel lost a day to. So the lock screen is innocent and the
    resume is not; whether the rest is settling time or plain flakiness was left
    unsettled on purpose, because retrying covers both and measuring it would
    have cost an afternoon of sleep cycles.

    What this buys is WHEN the failure lands. A signer that hangs inside the
    real send hangs after the video is already uploaded - the one outcome
    nothing can clean up, because the post may or may not exist by then.
    Failing here costs nothing: the file has not been touched and goes out on
    the next run.
    """
    ua = TIKTOK_TAU_UA or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36")
    cwd = str(Path(TIKTOK_TAU_DIR) / "tiktok_uploader" / "tiktok-signature")
    for n in range(1, SIGNER_TRIES + 1):
        try:
            code, out = _run_capped(["node", "browser.js", _SIGNER_PROBE, ua],
                                    cwd, env, SIGNER_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("tau: signer did not come up in %ds (try %d of %d), "
                        "killed it and asking again", SIGNER_TIMEOUT, n,
                        SIGNER_TRIES)
            continue
        if SIGNATURE_MARK in out:
            if n > 1:
                log.info("tau: signer up on try %d", n)
            return
        log.warning("tau: signer exited %d without a signature (try %d of %d)",
                    code, n, SIGNER_TRIES)
    raise RuntimeError(
        f"the tau signer did not come up in {SIGNER_TRIES} tries - nothing was "
        f"uploaded and nothing was written to the queue, so the video goes out "
        f"on the next run. If this repeats it is node or "
        f"PLAYWRIGHT_BROWSERS_PATH, not the account.")


def _upload_ui(mp4, text: str, env: dict) -> str:
    """Post by DRIVING the upload page, not by replaying its requests.

    The fork's way died on 2026-08-30: TikTok replaced the `_signature`
    parameter on /tiktok/web/project/post/v1/ with `X-Gnarly`, and the fork's
    signer is a frozen copy of TikTok's SDK that can only produce `_signature`
    and `X-Bogus`. Every publish came back {"status_code":5,"status_msg":
    "Invalid parameters"} - nineteen in a row - while the upload half kept
    working, the cookie stayed valid and posting the same file by hand from the
    browser went through at once. Nobody upstream has patched it and no issue
    anywhere mentions X-Gnarly.

    Here there is no signature to get wrong: the page is TikTok's own, running
    TikTok's own current JavaScript, so it signs itself. The cost is minutes
    instead of seconds and a selector that can move - which is ten minutes of
    work, against reverse-engineering an anti-bot bundle every time it rotates.

    Everything around this is unchanged: same cookie from login.py, same queue,
    same seen_local_<channel>.db, same watchdog in post.ps1.
    """
    # The cookie is a pickle the fork reads; Playwright wants JSON, and the
    # sameSite spellings differ between the two worlds.
    import pickle
    cookie = Path(TIKTOK_TAU_DIR) / "CookiesDir" / f"tiktok_session-{TIKTOK_TAU_USER}.cookie"
    if not cookie.exists():
        raise RuntimeError(f"no cookie at {cookie} - run login.py for "
                           f"{TIKTOK_TAU_USER} first")
    same = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}
    jar = []
    for c in pickle.loads(cookie.read_bytes()):
        if not c.get("name") or c.get("value") is None:
            continue
        row = {"name": c["name"], "value": c["value"],
               "domain": c.get("domain") or ".tiktok.com",
               "path": c.get("path") or "/",
               "httpOnly": bool(c.get("httpOnly")),
               "secure": bool(c.get("secure")),
               "sameSite": same.get(str(c.get("sameSite")).lower(), "Lax")}
        if c.get("expirationDate"):
            row["expires"] = int(c["expirationDate"])
        jar.append(row)

    with tempfile.TemporaryDirectory() as tmp:
        jar_file = Path(tmp) / "cookies.json"
        cap_file = Path(tmp) / "caption.txt"
        jar_file.write_text(json.dumps(jar), encoding="utf-8")
        # A file and not an argument: the caption is multi-line and full of
        # emoji, and a command line is the wrong place for either.
        cap_file.write_text(text, encoding="utf-8")
        cmd = ["node", TIKTOK_TAU_UI, str(jar_file), TIKTOK_TAU_UA,
               str(Path(mp4).resolve()), str(cap_file),
               # uipost.js implements a "dry" rehearsal - it really
               # uploads, really types the caption and really reads it
               # back, then discards instead of clicking Post. It was
               # unreachable from here, which made the one safe way to
               # check a fingerprint change cost a real post.
               "dry" if os.getenv("TAU_UI_DRY") == "1" else
               "public" if TIKTOK_PUBLIC else "private"]
        # Per account, beside the cookie it belongs to. A profile shared
        # between channels would hand both accounts the same device.
        profile = (Path(TIKTOK_TAU_DIR) / "CookiesDir" /
                   f"ui-profile-{TIKTOK_TAU_USER}")
        env = {**env, "TIKTOK_TAU_DIR": TIKTOK_TAU_DIR,
               "TAU_UI_PROFILE": str(profile)}
        try:
            code, out = _run_capped(cmd, TIKTOK_TAU_DIR, env, TAU_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"the UI upload timed out after {TAU_TIMEOUT}s. The video may "
                f"or may not have posted - check the account before running "
                f"again, nothing was written to the queue.") from None
    if code != 0:
        raise RuntimeError(f"UI upload failed (exit {code}):\n{out[-1500:]}")
    if not (m := _CREATION_ID.search(out)):
        raise RuntimeError(
            f"the UI uploader exited 0 but printed no creation_id, so there is "
            f"no telling whether it posted:\n{out[-1500:]}")
    if m.group(1) == "dry-run":
        # A rehearsal must not cost a video. Everything downstream of a
        # return here records the post as sent, so this leaves by the
        # one door that writes nothing to the queue.
        # node's stderr is only surfaced on a non-zero exit, and a rehearsal
        # exits zero - so the fingerprint it measured, which is the entire
        # reason for running one, was being thrown away here.
        raise RuntimeError(
            "TAU_UI_DRY=1: rehearsal only, nothing was published and "
            "nothing was recorded.\n\n" + out.strip())
    log.info("ui: posted %s as %s", Path(mp4).name, m.group(1))
    return f"ui:{m.group(1)}"


def _upload_tau(mp4, title: str, private: bool = True, body: str = "") -> str:
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
            f"{chan_key('TIKTOK_TAU_USER')} set - see the tau runbook")
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

    text = caption(title, body=body)
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
    # The signer is node, spawned by the fork's python, and if that python came
    # from the Microsoft Store its children see a REDIRECTED AppData\Local -
    # so playwright's default browser directory is one the installer wrote to
    # and the signer cannot see. Pointing both at a path outside AppData is
    # what keeps them looking in the same place. See TIKTOK_TAU_BROWSERS.
    if TIKTOK_TAU_BROWSERS:
        env["PLAYWRIGHT_BROWSERS_PATH"] = TIKTOK_TAU_BROWSERS
    log.info("tau: posting %s as %s%s", Path(mp4).name, TIKTOK_TAU_USER,
             " (private)" if private else " (PUBLIC)")
    # The fork replays requests and needs a signature TikTok stopped accepting;
    # the UI path drives the page and needs none. Point TIKTOK_TAU_UI at
    # uipost.js to take it. Checked here rather than at the top so everything
    # above - the config, the proxy and UA warnings, the caption - is shared.
    if TIKTOK_TAU_UI:
        return _upload_ui(mp4, text, env)
    # Each attempt re-uploads the file, which is also the backoff: a send is a
    # minute of video bytes, so there is nothing to sleep for between tries.
    _warm_signer(env)
    for attempt in range(1, TAU_TRIES + 1):
        try:
            code, out = _run_capped(cmd, TIKTOK_TAU_DIR, env, TAU_TIMEOUT)
        except subprocess.TimeoutExpired:
            # NOT retried, unlike the rejection below: a timeout is the one
            # outcome where the post may already exist, and trying again is how
            # one video becomes two.
            raise RuntimeError(
                f"tau upload timed out after {TAU_TIMEOUT}s. The video may or "
                f"may not have posted - check the account before running "
                f"again, nothing was written to the queue.") from None
        if code == 0 or not _tau_retryable(out):
            break
        log.warning("tau: post rejected as 'Invalid parameters' "
                    "(attempt %d of %d), re-signing and sending again",
                    attempt, TAU_TRIES)
    if code != 0:
        raise RuntimeError(f"tau upload failed (exit {code}):\n{out[-1500:]}")
    if not (m := _CREATION_ID.search(out)):
        # Upstream exits 0 on paths that posted nothing, which is exactly what
        # the patch's creation_id line exists to tell apart. No line, no post:
        # do not write a row claiming there was one, or the file is marked sent
        # and never goes out again.
        raise RuntimeError(
            "tau exited 0 but printed no creation_id - either the checkout is "
            "unpatched (apply tau-synergy.patch) or the upload failed "
            f"quietly:\n{out[-1500:]}")
    log.info("tau: posted %s as %s", Path(mp4).name, m.group(1))
    return f"tau:{m.group(1)}"


def _upload_api(mp4, title: str, direct: bool = False, private: bool = True,
                body: str = "") -> str:
    """Upload the file. Returns publish_id. Drafts unless direct=True."""
    mp4 = Path(mp4)
    size = mp4.stat().st_size
    spans = _plan(size)
    token = access_token()

    # `src`, not `source`: this module imports the source module at the top now,
    # and a local of that name would hide it from anything added here later.
    src = {"source": "FILE_UPLOAD", "video_size": size,
           "chunk_size": spans[0][1] - spans[0][0] + 1,
           "total_chunk_count": len(spans)}

    if direct:
        body = {"source_info": src, "post_info": {
            "title": caption(title, body=body),
            "privacy_level": "SELF_ONLY" if private else "PUBLIC_TO_EVERYONE",
            # TikTok's rules are their own - see DECLARE_AI in config.py
            "is_aigc": DECLARE_AI,
        }}
        url = f"{API}/post/publish/video/init/"
    else:
        body = {"source_info": src}
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
    if publish_id.startswith(("tau:", "ui:")):
        raise RuntimeError(
            f"{publish_id} was posted through the tau backend; the Content "
            "Posting API knows nothing about it. Look at the account.")
    r = _post(f"{API}/post/publish/status/fetch/",
              {"publish_id": publish_id}, access_token())
    return r.get("data", r)


# The two states that mean the file is out of our hands for good: handed to the
# app as a draft, or already published from one. Everything else is either
# still moving or dead.
DELIVERED = ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")
# How long to keep asking, and how often. Deliberately short - see await_send()
# for why the answer is advisory and a timeout is not an error.
SEND_WAIT_S = int(os.getenv("TIKTOK_SEND_WAIT_S", "120"))
SEND_POLL_S = 10


def await_send(publish_id: str, timeout: int = SEND_WAIT_S) -> str:
    """Ask TikTok what it did with the upload. Returns the last status seen.

    A 201 from the last chunk means the bytes arrived and nothing else. The file
    is moderated after that and only then handed to the app, and TikTok
    guarantees no time for either step - their own words are that a time limit
    is not guaranteed and that moderation "may take a few hours". Until this
    module asks, a run that uploaded a file TikTok later rejected outright is
    indistinguishable from one that worked, which is what let a silent failure
    look exactly like a success for as long as this function did not exist.

    A timeout is NOT a failure and the caller must not treat it as one: the file
    is almost certainly fine and merely slow, and losing the caption reminder
    over TikTok being slow costs more than the uncertainty does. Only FAILED is
    an answer worth acting on. That is also why the deadline is two minutes
    rather than an hour - the honest states are reached fast or not at all, and
    a CI runner is not the place to wait out moderation.
    """
    deadline = time.time() + timeout
    state = "UNKNOWN"
    while True:
        try:
            state = status(publish_id).get("status") or state
        except RuntimeError as e:
            # 500s out of this endpoint are routine - one of the 08-05 ids
            # answers with one to this day - and they say nothing about the
            # upload itself. Keep asking; the deadline below ends it.
            log.warning("status fetch failed, asking again: %s", e)
        log.info("publish %s: %s", publish_id[-6:], state)
        if state in DELIVERED or state == "FAILED" or time.time() >= deadline:
            return state
        time.sleep(SEND_POLL_S)


def stale(hours: float = 6.0) -> list[tuple]:
    """This channel's uploads that a human still owes something, oldest first.

    The reminder issue is fire-and-forget: it is written the moment TikTok takes
    the bytes and never looks again. But the draft reaches the app on TikTok's
    schedule, not ours - measured 2026-08-06, the notification for one draft
    arrived an hour and a half late, batched together with the next one's - and
    a draft that never arrives leaves an issue nobody can act on and a video
    nobody will ever see. This is the second look nothing else was taking.

    Both kinds of stuck are reported, because both end the same way. A draft
    still PROCESSING_UPLOAD hours later is broken; one sitting at
    SEND_TO_USER_INBOX is merely unpublished, which after `hours` means the
    notification never surfaced or was missed. The state is printed either way
    and the reader decides which it is.
    """
    now = time.time()
    # A week back and no further. The point is what is stuck NOW, and an id old
    # enough that TikTok has forgotten it answers nothing worth reading.
    with _db() as db:
        rows = db.execute("SELECT file, publish_id, ts FROM tiktok WHERE"
                          " channel=? AND ts>? AND ts<? ORDER BY ts",
                          (CHANNEL, now - 7 * 86400, now - hours * 3600)
                          ).fetchall()
    out = []
    for f, pid, ts in rows:
        # A tau: id is not a publish_id and asking about one gets an error, not
        # an answer - see status(). Nor is there anything to be stuck on: that
        # backend posts to the profile directly, with no inbox in between and
        # nothing left for a human to tap.
        if not pid or not pid.startswith("v_"):
            continue
        # Twice, because this endpoint is genuinely flaky - measured 2026-08-06,
        # five of ten ids answered 500 on the first ask and every one of them
        # answered properly on the second, including ids that had answered fine
        # minutes earlier. One try would make the watchdog miss half of what it
        # exists to catch, and it would miss it silently.
        for attempt in (1, 2):
            try:
                state = status(pid).get("status", "UNKNOWN")
                break
            except RuntimeError as e:
                log.warning("status fetch failed for %s (try %d): %s",
                            f, attempt, e)
                time.sleep(2)
        else:
            # Two 500s say nothing about the draft, so say nothing about it
            # either. A row wrongly called stuck sends someone to look for a
            # video that is fine, which is how a watchdog stops being read.
            continue
        if state != "PUBLISH_COMPLETE":
            out.append((f, pid, (now - ts) / 3600, state))
    return out


# --------------------------------------------------------------------- queue

def _db_path() -> Path:
    """Which file holds this channel's TikTok state.

    The api backend runs on CI, and CI commits seen.db back to the repo, so
    that is where its rows belong. The tau backend runs on somebody's desk,
    writing into a file CI rewrites twice an hour - and the moment a pull is
    resolved in CI's favour, the row saying a video went out is gone, the file
    returns to pending(), and the next --next posts it again. Publicly, now.

    Observed rather than predicted: it happened on the first merge to main.

    ...but CI still runs this module for a tau channel - the gate asks due()
    before it renders anything - and on a runner the local file does not exist.
    Read literally that means "nothing was ever sent", so the count is 0 and
    the gap is infinite on every tick, and a channel meant to produce four
    videos a day produces one every half hour. So the split is by MACHINE and
    not by backend alone: the desk keeps its untracked file, CI counts its own
    handoffs in the tracked one. GITHUB_ACTIONS is set by GitHub on every
    runner and by nothing else, and the default when it is absent is the safe
    direction - a desk that reads it wrong writes to the file that is its own.
    """
    if TIKTOK_BACKEND == "tau" and not os.getenv("GITHUB_ACTIONS"):
        return LOCAL_DB_PATH
    return DB_PATH


def _adopt(db) -> None:
    """Move this channel's existing rows into the local file, once.

    Switching a channel to tau must not make its history look unsent, or every
    already-published video is a candidate to go out again.

    Handoff rows are the exception and must NOT come across. They say "CI made
    this and shipped it here", which is the opposite of "this went out" - the
    whole point of one is that the desk still owes it a post. Adopted as sent,
    every video CI had queued up would be marked done on the first run here and
    silently never posted, which is the same two-day silence in a new costume.
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
        f" FROM tiktok WHERE channel=? AND {backend}!='handoff'",
        (CHANNEL,)).fetchall()
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
    # things and only one of them can be asked about later. The id says which
    # for rows written before the column existed - a "tau:" prefix is minted
    # nowhere else - so they are backfilled from it rather than all called api,
    # which would label the one surviving tau post as something status() would
    # then be asked about.
    if "backend" not in cols:
        db.execute("ALTER TABLE tiktok ADD COLUMN backend TEXT")
    # Not only on the ALTER: the column was added once already and nothing
    # filled it, so the live db carries it NULL on every row. Keyed on the id
    # rather than on today's TIKTOK_BACKEND, which says nothing about how a row
    # written months ago went out. Idempotent, hence unconditional.
    db.execute("UPDATE tiktok SET backend="
               "CASE WHEN publish_id LIKE 'tau:%' THEN 'tau' ELSE 'api' END"
               " WHERE backend IS NULL")
    if first:
        _adopt(db)
    return db


def pending() -> list:
    """This channel's rendered videos not yet sent to TikTok, oldest first."""
    from youtube import _mine, _scratch          # local: avoids a cycle

    with _db() as db:
        done = {r[0] for r in db.execute("SELECT file FROM tiktok")}
    return sorted((p for p in OUT_DIR.glob("*.mp4")
                   if p.name not in done and _mine(p) and not _scratch(p)),
                  key=lambda p: p.stat().st_mtime)


def sent_today() -> int:
    """Drafts sent on today's UTC calendar day - same clock youtube.py uses."""
    from youtube import _utc_date

    today = _utc_date(time.time())
    with _db() as db:
        rows = db.execute("SELECT ts FROM tiktok WHERE channel=?",
                          (CHANNEL,)).fetchall()
    return sum(1 for (ts,) in rows if _utc_date(ts) == today)


# TikTok's ceiling, not ours: "There may be at most 5 pending shares within any
# 24-hour period" - the upload reference, same page as the 6-requests-a-minute
# limit. Two things about it are different from TIKTOK_PER_DAY and both matter.
#
# It is a ROLLING 24 hours, where our count is a UTC calendar day: three drafts
# at 23:00 and three at 01:00 never break a daily count of three and are six
# inside one rolling day. And it is TikTok's rule, so nothing on our side gets
# to bypass it - not --force, and not the part exemption that lets the middle of
# a split story past the daily count.
#
# An api-backend rule, and only that: tau drives the web endpoints and posts
# straight to the profile, so there is no inbox and nothing pending in one.
# Left in force there it would refuse a fifth video a day TikTok would have
# taken, on the strength of a limit that does not apply to how it went out.
INBOX_CAP = (int(os.getenv("TIKTOK_INBOX_CAP", 5))
             if TIKTOK_BACKEND == "api" else 10 ** 9)


def shares_24h() -> int:
    """Inbox shares this channel started in the last rolling 24 hours.

    NOTE: counts every share, not only the ones still unpublished, because
    "pending" would cost a status call per row on a gate that runs twice an hour
    per channel. That errs toward sending less, never more. Ask TikTok properly
    if the ceiling ever starts refusing videos there was really room for.
    """
    with _db() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM tiktok WHERE channel=? AND ts>?",
                          (CHANNEL, time.time() - 24 * 3600)).fetchone()
    return n


def _since_last_h() -> float:
    """Hours since the last draft was sent, or a large number if none ever was."""
    with _db() as db:
        (last,) = db.execute("SELECT MAX(ts) FROM tiktok WHERE channel=?",
                             (CHANNEL,)).fetchone()
    return (time.time() - last) / 3600 if last else 999


# The cron in publish.yml, in UTC: two ticks an hour through the working part
# of the day. Nothing publishes between them, so an estimate that ignores the
# grid promises 02:15 for a video that cannot go out before 08:07.
_TICKS = [(h, m) for h in range(8, 24) for m in (7, 37)]


def _next_tick(t: float) -> float:
    """The first cron minute at or after `t`."""
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    for day in (0, 1):
        for h, m in _TICKS:
            c = (d.replace(hour=h, minute=m, second=0, microsecond=0)
                 + datetime.timedelta(days=day))
            if c.timestamp() >= t:
                return c.timestamp()
    raise AssertionError("the grid always comes round")   # pragma: no cover


def eta(ahead: int = 0) -> float:
    """Unix time the (ahead+1)-th video still to go out can be published.

    The EARLIEST possible moment, and said as one: it answers the gap between
    sends, the daily count, the midnight-UTC reset and the hours the workflow
    actually runs, and it cannot know that a render will fail or that the story
    two places ahead of this one is still unwritten.

    TikTok's clock, because on a channel that has it that is the clock the
    pipeline runs on: a run happens when EITHER platform is due and TikTok is
    the more permissive of the two. With TikTok off, YouTube's is all there is.

    0 when there is no allowance at all to schedule against - the caller says
    nothing about time rather than inventing one.

    `ahead` is a count of VIDEOS, so callers behind a split story pass its part
    count and not one.

    NOTE: those parts are then spaced at the ordinary gap, where the real
    thing spaces them at PART_GAP_HOURS and lets them past the daily count -
    so an estimate standing behind a split story runs LATE, by up to two gaps.
    Late is the safe direction for a promise. Take a list of part counts here
    instead of an int if a batch with a three-parter in it starts misleading.
    """
    now = time.time()
    if TIKTOK_ENABLED:
        gap, per_day, used = TIKTOK_MIN_GAP_HOURS, TIKTOK_PER_DAY, sent_today()
        last = now - _since_last_h() * 3600
    else:
        import youtube
        st = youtube.status()
        gap, per_day, used = youtube.YT_MIN_GAP_HOURS, st["allowed"], st["today"]
        last = now - st["since_last_h"] * 3600
    if per_day <= 0:
        return 0.0

    from youtube import _utc_date

    t, day = max(now, last + gap * 3600), _utc_date(now)
    while True:
        if _utc_date(t) != day:                  # midnight UTC resets the count
            day, used = _utc_date(t), 0
        if used >= per_day:                      # nothing more goes out today
            t = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
                 .replace(hour=0, minute=0, second=0, microsecond=0)
                 + datetime.timedelta(days=1)).timestamp()
            continue
        t = _next_tick(t)
        if _utc_date(t) != day:                  # the tick fell into tomorrow
            continue
        if not ahead:
            return t
        ahead, used, t = ahead - 1, used + 1, t + gap * 3600


def _warn_cap() -> None:
    """Say so when the rolling window is full, and send anyway.

    TikTok's own rule is "at most 5 pending shares within any 24-hour period",
    and this used to refuse past it. It no longer does - asked for on
    2026-08-21, after the ceiling ate cnnuk5: the file that is refused here is
    already rendered and dies with the runner, so refusing costs a video for
    certain, while sending costs one only if TikTok drops it.

    What TikTok does past the ceiling is not an error: init still answers with
    a publish_id and the status still reads SEND_TO_USER_INBOX, but the
    notification carrying the draft never arrives (measured 2026-08-07). So
    this line in the log is the ONLY sign that a draft may have gone nowhere -
    if a video is missing from the inbox, look for it here first.
    """
    if (n := shares_24h()) >= INBOX_CAP:
        log.warning("TikTok's 24h inbox ceiling reached (%d/%d) - sending "
                    "anyway; a draft past it can vanish without an error", n,
                    INBOX_CAP)


def due() -> str:
    """Empty string if another draft may go out now, else why it may not.

    A count and a gap, same as YouTube. The gap was left out at first on the
    grounds that a draft has no publish time - it waits in the inbox until a
    human posts it, so spacing the deliveries spaces nothing. That reasoning
    skips a step: the human is prompted by the draft landing, and posts it
    shortly after. Two drafts forty minutes apart are two videos forty minutes
    apart, which is the thing the gap exists to prevent.
    """
    # First line of the first gate: a stopped channel is refused before the
    # count, the gap and the pause are even read.
    if STOPPED:
        return STOP_REASON
    if not TIKTOK_ENABLED:
        return f"TikTok is paused for this channel ({chan_key('TIKTOK_ENABLED')}=0)"
    # A warning and not a refusal, by decision - see _warn_cap().
    _warn_cap()
    # The awaited middle of a story answers to its own clock and to no count.
    # It has to be asked HERE and not only in _blocked(), because this is the
    # gate CI consults before anything is rendered: a run refused here renders
    # nothing, and the part is then never made at all. YouTube used to hold this
    # clock; it no longer sees parts, so nothing else would open a slot for one.
    return _due_part() if source.next_part() else _due_ordinary()


def _due_ordinary() -> str:
    """The count and the gap an ordinary video answers to."""
    n = sent_today()
    if n >= TIKTOK_PER_DAY:
        return f"daily allowance reached ({n}/{TIKTOK_PER_DAY})"
    if (h := _since_last_h()) < TIKTOK_MIN_GAP_HOURS:
        return (f"only {h:.1f}h since the last draft, "
                f"minimum is {TIKTOK_MIN_GAP_HOURS:.1f}h")
    return ""


def _due_part() -> str:
    """The clock the middle of a story runs on: no count, and a shorter gap.

    A part ignores the count - the inbox must not be left holding the middle of
    a story with no beginning - but not the clock. An answer to a cliffhanger is
    worth nothing hours later, and worth nothing minutes later either, because
    both halves then sit in the feed as one block.
    """
    if (h := _since_last_h()) < PART_GAP_HOURS:
        return (f"only {h:.1f}h since the last draft, "
                f"minimum is {PART_GAP_HOURS:.1f}h for a part")
    return ""


def _blocked(meta: dict, force: bool = False) -> str:
    """Why this particular file may not go out now, empty if it may."""
    # Ahead of everything, --force included: a stopped channel does not send a
    # file left over in out/ from before the stop either.
    if STOPPED:
        return STOP_REASON
    # Ahead of --force and ahead of the part exemption below, both deliberately.
    # A paused channel that still delivers when a run is forced, or whenever a
    # story happens to be split, is not paused - and the part exemption is
    # exactly the case a quota of zero would have missed.
    if not TIKTOK_ENABLED:
        return f"TikTok is paused for this channel ({chan_key('TIKTOK_ENABLED')}=0)"
    _warn_cap()
    if force:
        return ""
    # Keyed on what this FILE turned out to be, never on what due() sees
    # pending. A part queued in sqlite whose mp4 did not survive the run - a
    # failed render, a dead runner - would otherwise lend its count exemption
    # to whatever ordinary video happens to be next in the queue, and push it
    # past a spent allowance.
    return _due_part() if meta.get("total", 0) > 1 else _due_ordinary()


def _clear_part(meta: dict) -> None:
    """Mark a part published, once the send has actually happened.

    Called here and nowhere earlier: a send that fails has to leave the part
    pending, or the story loses its middle - on CI the mp4 is already gone with
    the runner and the text in sqlite is the only way back.

    The other end of that rule is main.py: a part nobody can send is dropped
    rather than left pending, or the queue holds a file that is re-rendered
    every run and published on none of them.
    """
    if meta.get("total", 0) > 1:
        source.finish_part(meta["post_id"], meta["part"])
        log.info("part %d of %d done for %s", meta["part"], meta["total"],
                 meta["post_id"])


def upload_next(direct: bool = False, private: bool = True,
                force: bool = False) -> str | None:
    """Send one video, if today's allowance still has room for it.

    The allowance is checked here and not only at the gate, because only here is
    it known WHAT the file is - and a part of a split story ignores it. Skipping
    a part leaves the inbox with the middle of a story and no beginning, and
    part 2 then arrives a day later against a fresh count. Three extra drafts on
    a split day are cheaper than that.

    This is also the only place a part is ever cleared from the queue now.
    YouTube used to do it, back when a split story went to both platforms.
    """
    from youtube import _meta_for                 # local: avoids a cycle

    queue = pending()
    if not queue:
        log.info("nothing pending for TikTok")
        return None

    mp4 = queue[0]
    # The awaited part owns this slot. due() shortened the gap on its account,
    # so an older ordinary video sitting in out/ must not spend it instead and
    # leave the story's middle waiting another day.
    if nxt := source.next_part():
        mp4 = next((p for p in queue
                    if _meta_for(p).get("post_id") == nxt["post_id"]
                    and _meta_for(p).get("part") == nxt["n"]), mp4)

    meta = _meta_for(mp4)
    if reason := _blocked(meta, force):
        log.info("%s", reason)
        return None

    title = part_prefix(meta) + (meta.get("title") or mp4.stem)
    pid = upload(mp4, title, direct=direct, private=private,
                 body=meta.get("body", ""))
    with _db() as db:
        # Named columns rather than positional: the row grew a fifth field and
        # the next one should not silently land in the wrong place.
        db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts, channel,"
                   " backend) VALUES (?,?,?,?,?)",
                   (mp4.name, pid, time.time(), CHANNEL, TIKTOK_BACKEND))
    _clear_part(meta)
    # Printed BEFORE the caption and never after it: the workflow reads from the
    # CAPTION line to the end of this output and takes the last line as the id,
    # so anything added below would land in the issue body.
    # Nothing to await on tau: there is no inbox to be handed to and no id the
    # status endpoint knows, so asking would spend the whole deadline collecting
    # the same refusal and answer UNKNOWN. The fork printing a creation_id IS
    # the confirmation - it only prints one after the post went through.
    state = "POSTED" if TIKTOK_BACKEND == "tau" else await_send(pid)
    print(f"STATE: {state}")
    # Where the caption goes: the issue the title was chosen on. A story is one
    # case there - asked for, answered, then published - so its caption belongs
    # in it rather than in an issue of its own, and every part of a split story
    # comments into the same one as it is sent. Nothing here closes it: a draft
    # sits in the app until a human taps publish, and an open issue is what says
    # so. Zero means the file predates the review flow and the workflow opens an
    # issue for it as it always did.
    print(f"ISSUE: {meta.get('issue', 0)}")
    # A draft gets no caption from the API, so this print IS the caption and
    # the workflow forwards it to be pasted by hand. Withheld on FAILED alone -
    # there is then no draft to publish, and a reminder about one is worse than
    # no reminder. A slow upload still gets its caption; see await_send().
    # Only worth printing where it is the only copy that exists: tau already
    # sent the caption with the video, and printing it there would read as a
    # job still to do and open an issue asking for it.
    if not direct and state != "FAILED" and TIKTOK_BACKEND == "api":
        print("\nCAPTION:\n" + caption(title, body=meta.get("body", "")) + "\n")
    return pid


def handoff() -> str | None:
    """Record on CI that a video was made and shipped to the local poster.

    The tau backend cannot send from a runner - the cookie is on a desk - but
    CI still has to be PACED, and the thing that paces it is the same count and
    the same gap as everything else. Those are read off the tiktok table, so
    without a row per video that table stays empty and due() answers "due" on
    every tick for ever.

    Deliberately not a send and named so: backend='handoff' is what keeps the
    desk's _adopt() from reading these as videos that already went out. The
    publish_id column carries the run id instead, because there is no id to
    have - nothing has been published yet.

    Refuses off CI. On a desk this file IS the queue, and writing "handed off"
    rows into it would mark the desk's own backlog as dealt with.
    """
    if not (run := os.getenv("GITHUB_RUN_ID")):
        raise RuntimeError("--handoff records what CI shipped to the local "
                           "poster; it only runs on a runner")
    queue = pending()
    if not queue:
        log.info("nothing pending to hand off")
        return None
    mp4 = queue[0]
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts,"
                   " channel, backend) VALUES (?,?,?,?,'handoff')",
                   (mp4.name, f"handoff:{run}", time.time(), CHANNEL))
    # The part queue is cleared here for the same reason upload_next() clears
    # it: on a runner the mp4 is gone with the job, and a part left pending is
    # rendered again next run and holds the slot doing it. The desk posting it
    # later is not something seen.db can wait for.
    _clear_part(_meta_of(mp4))
    log.info("handed off %s to the local poster", mp4.name)
    return mp4.name


def _meta_of(mp4: Path) -> dict:
    from youtube import _meta_for          # local: avoids a cycle
    return _meta_for(mp4)


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
    # These run before every CLI command, including in CI, so the facts block
    # is stubbed rather than fetched: caption() would otherwise make a dozen
    # calls to a third-party API and one to the model per invocation, and a
    # self-test that needs the network is a self-test that fails for weather.
    # facts.py tests the real thing against its own stubs.
    _real_block = facts_.block
    facts_.block = lambda lang="", lead="": ""
    assert caption("Короткий").splitlines()[0] == "Короткий"
    assert len(caption("x" * 3000)) <= TITLE_MAX

    # The part marker, which lives on this platform and nowhere else now:
    # absent for ordinary videos, and never trimmed off a long title.
    assert part_prefix({}) == "" and part_prefix({"part": 1, "total": 1}) == ""
    _pfx = part_prefix({"part": 2, "total": 3, "channel": "ru"})
    assert _pfx == "Часть 2/3 - ", _pfx
    # the marker speaks the language of the FILE, not of this process
    assert part_prefix({"part": 2, "total": 3, "channel": "en"}) == "Part 2/3 - "
    # a file from before channels existed is the default channel's
    assert part_prefix({"part": 2, "total": 3}) == "Часть 2/3 - "
    _long = caption(_pfx + "я" * 3000)
    assert len(_long) <= TITLE_MAX, len(_long)
    # the marker leads on its own line now, so it is the first line and not a
    # prefix of one - what must not happen is a long title swallowing it
    assert _long.splitlines()[0] == _pfx.strip(" -"), _long[:80]
    assert len({caption("Один и тот же") for _ in range(30)}) > 5, "tags must rotate"

    # With a block: the marker rides on the hook's line, the title sits under
    # the block and the tags on the line straight below it. That order and
    # those blank lines are the whole layout.
    facts_.block = lambda lang="", lead="": f"{lead}ХУК\n\n1 факт\n2 факт\n3 факт\n\nКОММЕНТ"
    _with = caption(_pfx + "Заголовок")
    assert _with.splitlines()[0] == "Часть 2/3. ХУК", _with
    assert _with.index("ХУК") < _with.index("Заголовок") < _with.index("#"), _with
    assert _with.count("Часть 2/3") == 1, "the marker is moved, not copied"
    assert _with.endswith("\nЗаголовок\n" + _with.splitlines()[-1]), _with
    assert caption("Заголовок").startswith("ХУК"), "no marker, the hook leads"
    # ...and without a block the marker still has to lead, on its own line
    facts_.block = lambda lang="", lead="": ""
    assert caption(_pfx + "Заголовок").splitlines()[0] == "Часть 2/3"
    facts_.block = lambda lang="", lead="": f"{lead}ХУК\n\n1 факт\n2 факт\n3 факт\n\nКОММЕНТ"
    # ...and a title long enough to crowd it out drops the block, not the title
    _tight = caption("я" * (TITLE_MAX - 20))
    assert len(_tight) <= TITLE_MAX and "ХУК" not in _tight, len(_tight)
    # The body is matched too, not just the title - it is where most of the
    # topic words are, and passing it is the whole reason caption() takes it.
    # Fixtures in this channel's language: caption() reads its own buckets.
    _TOPIC = {"ru": "Свекровь въехала в квартиру.",
              "en": "My mother-in-law moved into the apartment."}[CHANNEL]
    assert set(caption("Заголовок", body=_TOPIC).split()) & tags_.TOPIC_TAGS[CHANNEL]
    facts_.block = _real_block

    # A spent allowance stops an ordinary video and never a part: the inbox
    # must not end up holding the middle of a story with no beginning.
    # The restore is load-bearing - these run before every CLI command, and a
    # leaked ceiling would leave the gate saying "due" forever.
    _real_per_day, _real_gap = TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS
    _real_part_gap = PART_GAP_HOURS
    _real_enabled = TIKTOK_ENABLED
    # Lifted out of the way for everything below except the block that is about
    # it. It sits ahead of the pause, --force and the part exemption, so a run
    # made while the real window happens to be full - which is exactly when
    # these tests are most worth having - would otherwise fail every one of
    # them, and fail them with the wrong reason.
    _real_cap, INBOX_CAP = INBOX_CAP, 10_000
    # And the same treatment for the stop, for the same reason: it sits ahead
    # of every gate below, so on a stopped channel each of these would fail,
    # and fail with the wrong reason. It gets its own block further down.
    _real_stopped, STOPPED = STOPPED, False
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

        # A part PENDING in sqlite opens the pre-render gate on its own, and
        # that exemption must not leak to the file actually being sent. The
        # part's mp4 can be missing - a failed render, or a runner that died
        # with out/ on it - leaving the queue holding an ordinary video while
        # next_part() still answers yes.
        _real_next = source.next_part
        try:
            source.next_part = lambda: {"post_id": "x", "n": 2, "total": 3}
            assert due() == "", "the gate opens for the pending part"
            assert _blocked({}), "but an ordinary file still obeys the count"
            assert _blocked({"part": 2, "total": 3}) == "", "the part itself does not"
        finally:
            source.next_part = _real_next
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

        # ...and TikTok's own ceiling now blocks nothing at all: it warns
        # and the video goes anyway, which is what the ceiling costing
        # cnnuk5 on 2026-08-20 bought. Asserted rather than dropped because
        # the point is that a full window still reaches the send - a cap of
        # zero is the cheapest way to stand at one.
        try:
            INBOX_CAP = 0
            TIKTOK_MIN_GAP_HOURS = PART_GAP_HOURS = 0
            TIKTOK_PER_DAY = 10_000
            for meta, force, who in [({}, False, "an ordinary video"),
                                     ({}, True, "--force"),
                                     ({"part": 2, "total": 2}, False, "a part"),
                                     ({"part": 2, "total": 2}, True, "a forced part")]:
                assert _blocked(meta, force=force) == "", \
                    f"{who} must not be stopped by the ceiling any more"
            assert due() == "", "nor may the CI gate stop for it"
        finally:
            INBOX_CAP = 10_000
    finally:
        TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS = _real_per_day, _real_gap
        PART_GAP_HOURS = _real_part_gap
        TIKTOK_ENABLED = _real_enabled
        INBOX_CAP = _real_cap
    # eta(): what the user is told on the issue when a title is accepted. All
    # of it is clock arithmetic, so it is stubbed down to the clock - the real
    # counts would make the answer depend on what this channel published today.
    from youtube import _utc_date

    _real_sent, _real_since = sent_today, _since_last_h
    _real_per_day, _real_gap = TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS
    _real_enabled = TIKTOK_ENABLED
    # ...and the clock with them, because eta() counts forward from NOW across a
    # grid that stops at 23:37 UTC. Read at 15:00 the fourth video of the day is
    # already tomorrow's, so is the fifth, and "the count must roll the day"
    # failed - every evening, on every channel, in front of every CLI command
    # there is. A green run with no TikTok in it for eight hours a day
    # (2026-08-24, ru, run 32787877284). A self-check must not depend on the
    # minute it is read at; morning is simply a time all of these hold.
    # NOTE: the stdlib clock itself, restored below - the whole module is
    # single-threaded here and nothing else runs inside the block. Give eta() a
    # `now=` argument instead if anything ever reads the clock alongside it.
    _real_clock = time.time
    _MORNING = datetime.datetime(2026, 1, 1, 9, 15,
                                 tzinfo=datetime.timezone.utc).timestamp()
    try:
        time.time = lambda: _MORNING
        TIKTOK_ENABLED = True
        TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS = 4, 3
        sent_today = lambda: 0                              # noqa: E731
        _since_last_h = lambda: 999                         # noqa: E731

        def _at(t):
            d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
            return (d.hour, d.minute)

        # Nothing sent and the gap long expired: the next video goes out at the
        # next cron minute, never "now" - nothing publishes between the ticks.
        assert _at(eta()) in [(h, m) for h in range(8, 24) for m in (7, 37)], \
            _at(eta())
        # ...and each one after it is a gap further on, which is the whole
        # reason the queue position is quoted with the time.
        assert eta(1) - eta(0) >= TIKTOK_MIN_GAP_HOURS * 3600
        assert eta(2) > eta(1) > eta(0) >= time.time()
        # The day's allowance is a wall, not a slowdown: the fifth video of a
        # four-a-day channel is tomorrow's, whatever the gap says.
        assert _utc_date(eta(4)) > _utc_date(eta(3)), "the count must roll the day"
        # And a day already spent moves even the first one to tomorrow.
        sent_today = lambda: 4                              # noqa: E731
        assert _utc_date(eta()) > _utc_date(time.time()), "a spent day is spent"
        # Nothing to schedule against - the caller says nothing rather than
        # inventing a time.
        sent_today, TIKTOK_PER_DAY = (lambda: 0), 0
        assert eta() == 0.0
    finally:
        time.time = _real_clock
        sent_today, _since_last_h = _real_sent, _real_since
        TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS = _real_per_day, _real_gap
        TIKTOK_ENABLED = _real_enabled

    # What await_send() does with each kind of answer, faked out - these run
    # before every CLI command and must not touch the network.
    _real_status, _real_poll = status, SEND_POLL_S
    try:
        SEND_POLL_S = 0
        _seen = []

        def _fake(_pid, _seq=iter(["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"])):
            _seen.append(s := next(_seq))
            return {"status": s}

        status = _fake
        assert await_send("x") == "SEND_TO_USER_INBOX", _seen
        assert len(_seen) == 2, "it must keep asking while the answer moves"
        status = lambda _pid: {"status": "FAILED"}
        assert await_send("x") == "FAILED", "and stop dead on a refusal"
        # A deadline that has already passed answers once and gives up. The
        # caller still gets a state rather than an exception, because the
        # caption is printed on everything except FAILED - see upload_next().
        status = lambda _pid: {"status": "PROCESSING_UPLOAD"}
        assert await_send("x", timeout=0) == "PROCESSING_UPLOAD"

        def _boom(_pid):
            raise RuntimeError("500 internal_error")

        status = _boom
        assert await_send("x", timeout=0) == "UNKNOWN", "a 500 is not an answer"
    finally:
        status, SEND_POLL_S = _real_status, _real_poll
    # The window stale() looks through, from both ends at once. Nothing can be
    # newer than a week and older than a year, so this asks TikTok nothing.
    assert stale(24 * 365) == [], "the two ends of the window must not overlap"

    # A self-test render is not a story and must never reach an account. They
    # all write under a leading underscore and none of them carry meta, which
    # _mine() reads as the default channel's - so before this, `render.py
    # --check` put three test clips into the Russian channel's queue.
    from youtube import _scratch as _is_scratch
    assert _is_scratch(Path("_selftest.mp4")) and _is_scratch(Path("_pitch.mp4"))
    assert not _is_scratch(Path("1ny329y.mp4")), "a real post id has no underscore"
    assert not _is_scratch(Path("1s1evbb_p2.mp4")), "a part is a real video"
    assert not any(p.name.startswith("_") for p in pending()), \
        [p.name for p in pending() if p.name.startswith("_")]

    # --- the tau backend ------------------------------------------------
    # The fork prints one line we care about, and everything else it prints is
    # noise we must not mistake for it - including its own echo of the caption,
    # which can itself contain the word.
    assert _CREATION_ID.search("Uploading video...\ncreation_id=abc123\n").group(1) == "abc123"
    assert _CREATION_ID.search("creation_id=x\nPublished").group(1) == "x"
    assert not _CREATION_ID.search("see creation_id=abc in the docs"), \
        "the line must start the line, or a caption could forge one"
    assert not _CREATION_ID.search("[-] Publish failed to Tiktok")

    # The state of a channel CI does not publish must not live in the file CI
    # rewrites. This is the assertion that would have caught the merge that
    # dropped a real post's row and put the video back in the queue.
    assert LOCAL_DB_PATH != DB_PATH, "the local file must not BE the tracked one"
    _real_ci = os.environ.pop("GITHUB_ACTIONS", None)
    try:
        # On a desk, a tau channel keeps its own untracked file...
        assert (_db_path() == LOCAL_DB_PATH) == (TIKTOK_BACKEND == "tau"), _db_path()
        # ...and on a runner it does not, whatever the backend says. Getting
        # this backwards is not a lost row, it is a gate that reads an empty
        # table, answers "due" on every tick and renders a video every half
        # hour against an allowance of four a day.
        os.environ["GITHUB_ACTIONS"] = "true"
        assert _db_path() == DB_PATH, "CI must count its handoffs in seen.db"
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
        if _real_ci is not None:
            os.environ["GITHUB_ACTIONS"] = _real_ci

    # A handoff is not a send, and the difference has to survive the trip to
    # the desk: adopted as sent, everything CI has queued up is marked done on
    # the first local run and silently never posted.
    _probe = sqlite3.connect(":memory:")
    _probe.execute("CREATE TABLE tiktok(file TEXT PRIMARY KEY, publish_id TEXT,"
                   " ts REAL, channel TEXT, backend TEXT)")
    _adopt(_probe)
    assert not _probe.execute("SELECT 1 FROM tiktok WHERE backend='handoff'"
                              ).fetchone(), "a handoff row must not be adopted"
    _probe.close()
    # And it refuses to write one anywhere but a runner.
    if not os.getenv("GITHUB_RUN_ID"):
        try:
            handoff()
            raise AssertionError("handoff() must refuse off CI")
        except RuntimeError as e:
            assert "only runs on a runner" in str(e), e

    # A tau id is not a publish_id and must not be sent to the API as one -
    # not by status(), and not by the watchdog that walks the whole table.
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
            assert "tau runbook" in str(e), e
    # To stderr, not stdout. Every command's real output is read by something -
    # the workflow greps the caption out of --next and puts --stale straight
    # into an issue body - and a banner about the tests belongs in neither.
    # The stop, restored and tested last: it is the outermost gate, ahead of
    # the pause, the count, the clock and --force alike, so once it is back in
    # place nothing else here would be measuring what it says it measures.
    STOPPED = _real_stopped
    try:
        STOPPED = True
        assert due() == STOP_REASON, due()
        assert _blocked({}, force=True) == STOP_REASON, "--force must not lift a stop"
        assert _blocked({"part": 2, "total": 2}) == STOP_REASON, (
            "a part must not slip past a stop")
    finally:
        STOPPED = _real_stopped

    # Both caption shapes give the title back, marker and hashtags off. The
    # export is the only place a view count sits next to a title, so a shape
    # this misses is a story missing from what the critic is shown.
    assert _headline("Часть 1/2 - Друг выложил видео #семья #истории") == (
        "Друг выложил видео"), _headline("Часть 1/2 - Друг выложил видео #семья")
    assert _headline("Часть 1/2. Три факта 👇 Сосед сменил кличку #дом") == (
        "Сосед сменил кличку")
    assert _headline("Отец подал в суд #семья") == "Отец подал в суд"

    # The runner today's two-hour hang turned on. The child here spawns a
    # grandchild and both outlive the timeout, which is the shape that broke it:
    # killing only the child leaves the grandchild holding the pipes, and the
    # wait that follows has no deadline. Only on tau, because that is the only
    # backend that runs it, and it costs a second there.
    if TIKTOK_BACKEND == "tau":
        _hang = ("import subprocess, sys, time; "
                 "subprocess.Popen([sys.executable, '-c', "
                 "'import time; time.sleep(30)']); time.sleep(30)")
        _t0 = time.time()
        try:
            _run_capped([sys.executable, "-c", _hang], None,
                        dict(os.environ), 1)
            raise AssertionError("_run_capped returned instead of timing out")
        except subprocess.TimeoutExpired:
            pass
        assert time.time() - _t0 < 15, (
            "_run_capped waited past its own timeout - it is draining pipes a "
            "grandchild still holds, which is the 2026-08-30 hang")

    # The retry predicate: the transient is retried, a real fault is not.
    assert _tau_retryable('{"status_code":5,"status_msg":"Invalid parameters"}')
    assert not _tau_retryable("NOT logged in: Login expired")
    assert not _tau_retryable("[-] Failed to parse signature data")

    print("chunking, caption, allowance and status logic ok", file=sys.stderr)

    try:
        if "--auth" in sys.argv:
            authorize()
        elif "--whoami" in sys.argv:
            print(whoami())
        elif "--hits" in sys.argv:
            # Beside the prompt set rather than in this repo: it is one
            # channel's view counts, and this repo is public.
            out = Path(".private") / (chan_file("hits") + ".md")
            if not out.parent.is_dir():
                raise SystemExit("no .private/ - clone the prompt set first")
            out.write_text(hits(), encoding="utf-8")
            print(out)
        elif "--stats" in sys.argv:
            # captions carry emoji; the console pipe is cp1251 here
            sys.stdout.reconfigure(encoding="utf-8-sig", newline="")
            w = csv.writer(sys.stdout, lineterminator="\n")
            w.writerow(["id", "posted", "views", "likes", "comments",
                        "shares", "title"])
            for v in videos():
                w.writerow([
                    v.get("id"),
                    datetime.datetime.fromtimestamp(
                        v.get("create_time", 0)).isoformat(" ", "seconds"),
                    v.get("view_count"), v.get("like_count"),
                    v.get("comment_count"), v.get("share_count"),
                    (v.get("title") or "").replace("\n", " ")])
        elif "--due" in sys.argv:
            # exit code is the point: the workflow gate asks before it spends
            reason = due()
            print(reason or "due")
            sys.exit(1 if reason else 0)
        elif "--handoff" in sys.argv:
            print(handoff() or "nothing to hand off")
        elif "--since-last" in sys.argv:
            # Hours since anything went out on this channel, for the watchdog in
            # the local poster. It is the only honest sign that the desk half of
            # the tau setup has stopped: CI cannot see seen_local_<channel>.db,
            # so a workflow that is green says nothing about whether the videos
            # it made ever reached TikTok. That gap is what let the English
            # channel sit dead for two days with every signal reading normal.
            print(f"{_since_last_h():.2f}")
        elif "--enabled" in sys.argv:
            # The pause on its own, apart from the count and the gap that --due
            # folds in with it. A forced run overrides those two and must NOT
            # override this one - a paused channel has nowhere to put the file,
            # so forcing it renders a video, spends the story and throws it
            # away. Measured 2026-08-07: the first force_tiktok dispatch did
            # exactly that to the English channel.
            # STOPPED counts as not enabled here, or force_tiktok would walk
            # straight past the stop the same way it once walked past the pause.
            ok = TIKTOK_ENABLED and not STOPPED
            print("enabled" if ok else STOP_REASON if STOPPED else
                  f"paused ({chan_key('TIKTOK_ENABLED')}=0)")
            sys.exit(0 if ok else 1)
        elif "--status" in sys.argv:
            with _db() as db:
                rows = db.execute("SELECT file, publish_id, backend FROM tiktok "
                                  "WHERE channel=? ORDER BY ts DESC",
                                  (CHANNEL,)).fetchall()
            print(f"channel {CHANNEL} via {TIKTOK_BACKEND} ({_db_path().name}): "
                  f"{len(rows)} sent, "
                  f"{sent_today()}/{TIKTOK_PER_DAY} today, {len(pending())} queued"
                  + ("" if TIKTOK_ENABLED else "  [PAUSED]"))
            for f, pid, backend in rows[:5]:
                print("  sent  ", f, pid, f"({backend or 'api'})")
            for p in pending()[:10]:
                print("  queued", p.name)
            # The split-story queue is this platform's alone, so it is reported
            # here - youtube.py --status used to carry this line.
            if nxt := source.next_part():
                print(f"next: {nxt['post_id']} part {nxt['n']} of {nxt['total']}"
                      f" (gap {PART_GAP_HOURS:.1f}h)")
        elif "--stale" in sys.argv:
            # The hours are an argument because the right threshold is not a
            # property of the code: it is how long you are willing to let a
            # video sit unseen, and the workflow and a human asking by hand do
            # not answer that the same way.
            i = sys.argv.index("--stale") + 1
            hrs = float(sys.argv[i]) if len(sys.argv) > i and \
                not sys.argv[i].startswith("-") else 6.0
            rows = stale(hrs)
            for f, pid, age, st in rows:
                print(f"- `{f}` - {age:.1f}h ago, still `{st}` (`{pid}`)")
            if not rows:
                print(f"nothing older than {hrs:.1f}h is waiting")
            # Exit code is the point, same as --due: the workflow asks and only
            # writes an issue when the answer is yes.
            sys.exit(1 if rows else 0)
        elif "--next" in sys.argv:
            print(upload_next(direct="--direct" in sys.argv,
                              private=not _public(),
                              force="--force" in sys.argv))
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            # the real title lives beside the file, written by main.py; the stem
            # is only a post id, which is what the caption used to say
            from youtube import _meta_for
            meta = _meta_for(mp4)
            pid = upload(mp4, part_prefix(meta) + (meta.get("title") or mp4.stem),
                         direct="--direct" in sys.argv,
                         private=not _public(),
                         body=meta.get("body", ""))
            # Record it, exactly as upload_next() does. This path skips the
            # gate on purpose - it is the by-hand escape hatch - but skipping
            # the gate is not the same as leaving no trace: an unrecorded file
            # stays in pending() and the next --next sends it a second time.
            with _db() as db:
                db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts,"
                           " channel, backend) VALUES (?,?,?,?,?)",
                           (mp4.name, pid, time.time(), CHANNEL, TIKTOK_BACKEND))
            # Same reason the row above is written: this path skips the gate,
            # not the bookkeeping. A part sent by hand and left in the queue
            # would be rendered and sent again by the next --next.
            _clear_part(meta)
            # A tau post has no status to fetch, and asking is an error rather
            # than an empty answer - see status().
            print(pid, "(posted; the API has no status for a tau id)"
                  if pid.startswith("tau:") else status(pid))
        else:
            print("usage: python publish.py --auth | --whoami | --stats | "
                  "--status | "
                  "--due | --enabled | --next | --stale [hours] | "
                  "--since-last | --handoff | "
                  "out/<id>.mp4 [--direct] [--public]")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
