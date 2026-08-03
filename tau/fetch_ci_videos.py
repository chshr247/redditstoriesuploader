"""Pull the videos CI made into the local out/, so the tau backend has work.

CI builds the video, ships it to YouTube and throws out/ away with the runner.
The machine that holds the TikTok cookies never sees that disk, so without this
the local queue drains in a day while CI keeps producing.

The workflow uploads each video as `video-<channel>-<run id>` when the channel
is listed in the TIKTOK_LOCAL_CHANNELS repo variable. This downloads the ones
this channel has not taken yet.

    python tau/fetch_ci_videos.py            # then: publish.py --next

Needs the `gh` CLI, authenticated. Artifacts expire after three days - if the
machine was off longer than that, those videos are gone and the only cost is
that they never reached TikTok.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CHANNEL, OUT_DIR   # noqa: E402

# Which runs we already took. Kept beside the videos, in a directory that is
# already gitignored and already local-only, because that is exactly the
# lifetime this fact has.
SEEN = OUT_DIR / ".ci_runs_fetched"


def _gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{(r.stderr or '').strip()}")
    return r.stdout


def main() -> None:
    prefix = f"video-{CHANNEL}-"
    taken = set(SEEN.read_text("utf-8").split()) if SEEN.exists() else set()

    raw = _gh("api", "--paginate",
              "repos/{owner}/{repo}/actions/artifacts?per_page=100")
    # --paginate concatenates one JSON object per page rather than merging.
    arts = [a for page in raw.replace("}{", "}\n{").splitlines() if page.strip()
            for a in json.loads(page).get("artifacts", [])]

    # Oldest first, so the queue keeps the order the stories were made in -
    # publish.py sorts by mtime and a batch downloaded backwards would post
    # the newest story first and the split parts out of order.
    todo = sorted((a for a in arts
                   if a["name"].startswith(prefix) and not a["expired"]
                   and a["name"] not in taken),
                  key=lambda a: a["created_at"])

    if not todo:
        print(f"nothing new for channel {CHANNEL} "
              f"({len([a for a in arts if a['name'].startswith(prefix)])} "
              f"artifacts seen, all taken or expired)")
        return

    OUT_DIR.mkdir(exist_ok=True)
    got = 0
    for a in todo:
        run_id = a["name"].removeprefix(prefix)
        print(f"downloading {a['name']} ({a['size_in_bytes'] / 1e6:.0f} MB)")
        try:
            _gh("run", "download", run_id, "--name", a["name"], "--dir", str(OUT_DIR))
        except SystemExit as e:
            # One bad artifact must not stop the rest: the next run of this
            # script would hit it again and stall the queue behind it forever.
            print(f"  skipped: {e}")
            continue
        # Marked only after it landed, so an interrupted download is retried
        # rather than silently written off.
        with SEEN.open("a", encoding="utf-8") as f:
            f.write(a["name"] + "\n")
        got += 1

    mp4s = sorted(OUT_DIR.glob("*.mp4"))
    missing = [p.name for p in mp4s if not p.with_suffix(".meta.json").exists()]
    print(f"\nfetched {got} run(s); out/ now holds {len(mp4s)} mp4")
    if missing:
        # Worth shouting about: _mine() reads the channel out of the meta, so
        # an mp4 without one is not a video with a worse caption - it belongs
        # to the default channel and this one will never see it.
        print(f"WARNING: no meta.json beside {missing} - those will not appear "
              f"in channel {CHANNEL}'s queue at all")


if __name__ == "__main__":
    main()
