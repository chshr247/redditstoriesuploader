"""Step 5: upload a finished mp4 to TikTok via the Content Posting API.

Two targets, and the difference matters:

  drafts (default) - scope video.upload, endpoint .../inbox/video/init/.
      Lands in the app's inbox; you tap publish yourself. Works with an
      unaudited app, which is what a new developer account has.
  direct  (--direct) - scope video.publish, endpoint .../video/init/.
      Posts for real. TikTok only grants that scope to audited apps.

Default is drafts on purpose: nothing here posts publicly unless you ask.
"""
import hashlib
import json
import logging
import os
import random
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from config import (DECLARE_AI, TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
                    TIKTOK_REFRESH_TOKEN, YT_HASHTAGS, save_env)

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "video.publish,video.upload"
# Registered under the Login Kit product, and the app type decides what is
# legal here: a desktop client may use http://localhost:<port>/..., a web one
# must use https. Set TIKTOK_REDIRECT to whichever the app is configured with.
# A localhost value is caught by a local server; anything else falls back to
# pasting the redirected URL, which needs no reachable host at all.
REDIRECT = os.getenv("TIKTOK_REDIRECT", "http://localhost:8080/callback")
CHUNK = 10_000_000        # the size TikTok's own docs use in their example
TITLE_MAX = 2200          # UTF-16 runes, per the direct-post reference

log = logging.getLogger(__name__)


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
    # TikTok rotates the refresh token; keep the new one or the next run fails
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


def caption(title: str, hashtags=None) -> str:
    """Title plus a rotating slice of tags, trimmed to TikTok's limit.

    Deliberately shorter than the YouTube description: TikTok shows two lines
    before the fold, so anything past the hook is scrolled past anyway.
    """
    pool = list(YT_HASHTAGS if hashtags is None else hashtags)
    random.shuffle(pool)
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


def upload(mp4, title: str, direct: bool = False, private: bool = True) -> str:
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
            "title": caption(title),
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
    print("chunking and caption logic ok")

    try:
        if "--auth" in sys.argv:
            authorize()
        elif len(sys.argv) > 1 and sys.argv[1].endswith(".mp4"):
            mp4 = Path(sys.argv[1])
            # the real title lives beside the file, written by main.py; the stem
            # is only a post id, which is what the caption used to say
            from youtube import _meta_for, part_suffix
            meta = _meta_for(mp4)
            pid = upload(mp4, (meta.get("title") or mp4.stem) + part_suffix(meta),
                         direct="--direct" in sys.argv,
                         private="--public" not in sys.argv)
            print(pid, status(pid))
        else:
            print("usage: python publish.py --auth | out/<id>.mp4 [--direct] [--public]")
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
