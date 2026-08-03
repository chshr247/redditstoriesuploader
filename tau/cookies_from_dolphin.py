"""Turn a cookie export from an antidetect browser into the fork's cookie file.

The fork's own `cli.py login` drives a plain Chrome through
undetected-chromedriver. That is no use when the profile lives in Dolphin Anty
(or GoLogin, or Octo - the export shape is the same Chrome one): the session has
to be minted by the profile whose fingerprint and proxy the account is known by,
and Chrome's --proxy-server cannot even carry the proxy's credentials.

So: log in inside the antidetect profile by hand, export that profile's cookies,
and run this. It writes CookiesDir/tiktok_session-<name>.cookie, which is the
pickle the fork reads, and after that `publish.py --next` needs nothing else.

    python tau/cookies_from_dolphin.py cookies.json reddit_ru

The export is a normal Chrome cookie dump: a JSON array of objects with `name`,
`value` and `domain`, or an object with those under "cookies" / "data". Both are
accepted, because every exporter picks a different one.
"""
import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TIKTOK_TAU_DIR, chan_key   # noqa: E402

# What the fork looks for. sessionid is the session; tt-target-idc is the
# datacenter TikTok assigned it, and uploading without it means guessing
# "useast2a" and hoping - see the warning the fork prints when it is absent.
NEEDED = ("sessionid", "tt-target-idc")


def _cookies(blob) -> list:
    """The cookie list, whichever shape the exporter chose."""
    if isinstance(blob, list):
        return blob
    for key in ("cookies", "data", "Cookies"):
        if isinstance(blob.get(key), list):
            return blob[key]
    raise SystemExit("no cookie array in that file: expected a JSON list, or an "
                     "object with the list under 'cookies' or 'data'")


def main(export: str, name: str) -> None:
    if not TIKTOK_TAU_DIR:
        raise SystemExit(f"{chan_key('TIKTOK_TAU_DIR', True)} is unset - it is "
                         "where the cookie has to land. See tau/README.md.")
    out_dir = Path(TIKTOK_TAU_DIR) / "CookiesDir"
    if not out_dir.is_dir():
        raise SystemExit(f"{out_dir} does not exist - is TIKTOK_TAU_DIR really "
                         "the fork's checkout?")

    raw = _cookies(json.loads(Path(export).read_text(encoding="utf-8")))
    # Domain rather than name alone: an export can hold a dozen sites, and a
    # `sessionid` belonging to some other one would be saved as TikTok's.
    kept = [c for c in raw
            if isinstance(c, dict) and "tiktok.com" in str(c.get("domain", ""))]

    have = {c.get("name") for c in kept}
    if missing := [n for n in NEEDED if n not in have]:
        raise SystemExit(
            f"the export has no {', '.join(missing)} for tiktok.com. Log in "
            "inside the profile first, and export AFTER the login - a fresh "
            "profile has cookies but no session.")

    # Kept whole, not trimmed to the two: the fork reads more of them back into
    # its requests session, and a session that presents two cookies where the
    # browser had thirty is its own kind of odd.
    dest = out_dir / f"tiktok_session-{name}.cookie"
    if dest.exists():
        # Overwriting silently would log the account out of the fork with no
        # way back except another export, which is a bad thing to do by typo.
        print(f"{dest.name} already exists.")
        if input("overwrite? [y/N] ").strip().lower() != "y":
            raise SystemExit("left alone")
    dest.write_bytes(pickle.dumps(kept))

    print(f"wrote {dest} ({len(kept)} cookies for tiktok.com)")
    idc = next(c["value"] for c in kept if c.get("name") == "tt-target-idc")
    print(f"datacenter: {idc}")
    print("\nSet TIKTOK_TAU_USER to", repr(name), "and make sure "
          f"{chan_key('TIKTOK_TAU_UA')} matches the user agent of the profile "
          "you exported from - the upload has to present the browser that\n"
          "minted this cookie, not a different one.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python tau/cookies_from_dolphin.py "
                         "<export.json> <account-name>")
    main(sys.argv[1], sys.argv[2])
