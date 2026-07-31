"""Step 5: upload a finished mp4 to TikTok via the Content Posting API.

Two targets, and the difference matters:

  drafts (default) - scope video.upload, endpoint .../inbox/video/init/.
      Lands in the app's inbox; you tap publish yourself. Works with an
      unaudited app, which is what a new developer account has.
  direct  (--direct) - scope video.publish, endpoint .../video/init/.
      Posts for real. TikTok only grants that scope to audited apps.

Default is drafts on purpose: nothing here posts publicly unless you ask.
"""
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import (HASHTAGS, TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET,
                    TIKTOK_REFRESH_TOKEN)

API = "https://open.tiktokapis.com/v2"
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
        log.warning("refresh token rotated - update TIKTOK_REFRESH_TOKEN in .env:\n%s",
                    r["refresh_token"])
    return r["access_token"]


def caption(title: str, hashtags: str = HASHTAGS) -> str:
    """Story title plus tags, trimmed to TikTok's limit."""
    text = f"{title} {hashtags}".strip()
    if len(text) > TITLE_MAX:
        text = text[:TITLE_MAX - len(hashtags) - 4].rstrip() + "... " + hashtags
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
            # the narration is synthetic, so declare it - TikTok penalises
            # undisclosed AI content harder than disclosed
            "is_aigc": True,
        }}
        url = f"{API}/post/publish/video/init/"
    else:
        body = {"source_info": source}
        url = f"{API}/post/publish/inbox/video/init/"

    r = _post(url, body, token)
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
    assert caption("Short one").endswith(HASHTAGS)
    assert len(caption("x" * 3000)) <= TITLE_MAX
    print("chunking and caption logic ok")

    if len(sys.argv) < 2:
        print("usage: python publish.py out/<id>.mp4 [--direct] [--public]")
        sys.exit(0)

    mp4 = Path(sys.argv[1])
    pid = upload(mp4, mp4.stem, direct="--direct" in sys.argv,
                 private="--public" not in sys.argv)
    print(pid, status(pid))
