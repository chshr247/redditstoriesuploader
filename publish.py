"""Step 5: upload a finished mp4 to TikTok, through the Content Posting API.

Two targets, and the default is the quiet one:

  drafts (default) - scope video.upload, endpoint .../inbox/video/init/.
      Lands in the app's inbox; you tap publish yourself. Works with an
      unaudited app, which is what a new developer account has.
  direct  (--direct) - scope video.publish, endpoint .../video/init/.
      Posts for real. TikTok only grants that scope to audited apps.

    python publish.py --auth              one-time, gets the refresh token
    python publish.py --next              send the oldest unsent mp4
    python publish.py --due               may another one go out today?
    python publish.py --enabled           is this channel paused?
    python publish.py --status            what is queued, what already went
    python publish.py --stale [hours]     what TikTok took but nobody has seen
    python publish.py out/<id>.mp4 [--direct] [--public]

A draft carries no caption - TikTok's inbox endpoint takes the file and nothing
else, the text is typed in the app at publish time. So --next prints the caption
it would have used; that print is the only place it exists.

There was a second backend here for a while: a patched fork of
makiisthenes/TiktokAutoUploader driving the web endpoints with a saved browser
cookie, which posted for real, caption and all. It is gone - it never worked
outside a hand-held session (a headless Chromium that had to be reinstalled to
sign, a rotating proxy that timed out mid-upload), and every failure of it was
silent on CI. See todo.md section 11 for what it cost and what it taught.
"""
import hashlib
import json
import logging
import os
import random
import re
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

import facts as facts_        # `facts` is the local variable in caption()
import source
import tags as tags_          # `tags` is the local variable in caption()
from config import (CHANNEL, DB_PATH, DECLARE_AI, DEFAULT_CHANNEL, OUT_DIR,
                    PART_GAP_HOURS, PART_WORD, TIKTOK_CLIENT_KEY,
                    TIKTOK_CLIENT_SECRET, TIKTOK_ENABLED, TIKTOK_MIN_GAP_HOURS,
                    TIKTOK_PER_DAY, TIKTOK_PUBLIC, TIKTOK_REFRESH_KEY,
                    TIKTOK_REFRESH_TOKEN, chan_key, save_env)

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
    """What became of an upload."""
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
        # Ids from the retired local backend are not TikTok's and asking about
        # one gets an error, not an answer. See the note at the top of the file.
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

def _db():
    # Separate table from youtube's `uploaded`: the two platforms are not in
    # step. A video can sit in the TikTok inbox for days before it is published
    # by hand, and a failed TikTok run must not hide the video from YouTube.
    db = sqlite3.connect(DB_PATH)
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


# TikTok's ceiling, not ours: "There may be at most 5 pending shares within any
# 24-hour period" - the upload reference, same page as the 6-requests-a-minute
# limit. Two things about it are different from TIKTOK_PER_DAY and both matter.
#
# It is a ROLLING 24 hours, where our count is a UTC calendar day: three drafts
# at 23:00 and three at 01:00 never break a daily count of three and are six
# inside one rolling day. And it is TikTok's rule, so nothing on our side gets
# to bypass it - not --force, and not the part exemption that lets the middle of
# a split story past the daily count.
INBOX_CAP = int(os.getenv("TIKTOK_INBOX_CAP", 5))


def shares_24h() -> int:
    """Inbox shares this channel started in the last rolling 24 hours.

    ponytail: counts every share, not only the ones still unpublished, because
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
    # Ahead of the part exemption below, because this ceiling is not ours to
    # make exceptions to. Past it the upload does not fail - init still answers
    # with a publish_id and the status still reads SEND_TO_USER_INBOX - the
    # notification carrying the draft simply never arrives, which is a video
    # lost with every signal saying it was delivered.
    if (n := shares_24h()) >= INBOX_CAP:
        return f"TikTok's 24h inbox ceiling reached ({n}/{INBOX_CAP})"
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
    # Ahead of --force and ahead of the part exemption below, both deliberately.
    # A paused channel that still delivers when a run is forced, or whenever a
    # story happens to be split, is not paused - and the part exemption is
    # exactly the case a quota of zero would have missed.
    if not TIKTOK_ENABLED:
        return f"TikTok is paused for this channel ({chan_key('TIKTOK_ENABLED')}=0)"
    # Ahead of --force for the same reason as the pause: a limit TikTok enforces
    # is not one we can decide to spend anyway. Forcing past it does not deliver
    # the video sooner, it delivers it never.
    if (n := shares_24h()) >= INBOX_CAP:
        return f"TikTok's 24h inbox ceiling reached ({n}/{INBOX_CAP})"
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
        # Named columns rather than positional: the row has grown a field
        # before and the next one should not silently land in the wrong place.
        db.execute("INSERT OR REPLACE INTO tiktok(file, publish_id, ts, channel)"
                   " VALUES (?,?,?,?)",
                   (mp4.name, pid, time.time(), CHANNEL))
    _clear_part(meta)
    # Printed BEFORE the caption and never after it: the workflow reads from the
    # CAPTION line to the end of this output and takes the last line as the id,
    # so anything added below would land in the issue body.
    state = await_send(pid)
    print(f"STATE: {state}")
    # A draft gets no caption from the API, so this print IS the caption and
    # the workflow forwards it to be pasted by hand. Withheld on FAILED alone -
    # there is then no draft to publish, and a reminder about one is worse than
    # no reminder. A slow upload still gets its caption; see await_send().
    if not direct and state != "FAILED":
        print("\nCAPTION:\n" + caption(title, body=meta.get("body", "")) + "\n")
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

        # ...but TikTok's own ceiling wins over --force and over the part
        # exemption both, which is the whole difference between it and every
        # other limit above. A cap of zero is the cheapest way to stand at it.
        try:
            INBOX_CAP = 0
            TIKTOK_MIN_GAP_HOURS = PART_GAP_HOURS = 0
            TIKTOK_PER_DAY = 10_000
            for meta, force, who in [({}, False, "an ordinary video"),
                                     ({}, True, "--force"),
                                     ({"part": 2, "total": 2}, False, "a part"),
                                     ({"part": 2, "total": 2}, True, "a forced part")]:
                assert "inbox ceiling" in _blocked(meta, force=force), \
                    f"{who} must not get past TikTok's 24h ceiling"
            assert "inbox ceiling" in due(), "the CI gate must see it too"
        finally:
            INBOX_CAP = 10_000
    finally:
        TIKTOK_PER_DAY, TIKTOK_MIN_GAP_HOURS = _real_per_day, _real_gap
        PART_GAP_HOURS = _real_part_gap
        TIKTOK_ENABLED = _real_enabled
        INBOX_CAP = _real_cap
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
    # To stderr, not stdout. Every command's real output is read by something -
    # the workflow greps the caption out of --next and puts --stale straight
    # into an issue body - and a banner about the tests belongs in neither.
    print("chunking, caption, allowance and status logic ok", file=sys.stderr)

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
        elif "--enabled" in sys.argv:
            # The pause on its own, apart from the count and the gap that --due
            # folds in with it. A forced run overrides those two and must NOT
            # override this one - a paused channel has nowhere to put the file,
            # so forcing it renders a video, spends the story and throws it
            # away. Measured 2026-08-07: the first force_tiktok dispatch did
            # exactly that to the English channel.
            print("enabled" if TIKTOK_ENABLED else
                  f"paused ({chan_key('TIKTOK_ENABLED')}=0)")
            sys.exit(0 if TIKTOK_ENABLED else 1)
        elif "--status" in sys.argv:
            with _db() as db:
                rows = db.execute("SELECT file, publish_id FROM tiktok "
                                  "WHERE channel=? ORDER BY ts DESC",
                                  (CHANNEL,)).fetchall()
            print(f"channel {CHANNEL}: {len(rows)} sent, "
                  f"{sent_today()}/{TIKTOK_PER_DAY} today, {len(pending())} queued"
                  + ("" if TIKTOK_ENABLED else "  [PAUSED]"))
            for f, pid in rows[:5]:
                print("  sent  ", f, pid)
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
                           " channel) VALUES (?,?,?,?)",
                           (mp4.name, pid, time.time(), CHANNEL))
            # Same reason the row above is written: this path skips the gate,
            # not the bookkeeping. A part sent by hand and left in the queue
            # would be rendered and sent again by the next --next.
            _clear_part(meta)
            print(pid, status(pid))
        else:
            print("usage: python publish.py --auth | --whoami | --status | "
                  "--due | --enabled | --next | --stale [hours] | "
                  "out/<id>.mp4 [--direct] [--public]")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
