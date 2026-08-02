"""Config: .env -> module constants. No classes, no validators."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)


def save_env(key: str, value: str) -> None:
    """Rewrite one key in .env, leaving everything else alone.

    Both platforms hand back a fresh refresh token and expect the old one to be
    dropped; printing it and hoping someone copies it by hand is how a pipeline
    silently dies a month later.
    """
    line = f"{key}={value}"
    kept = [l for l in ENV_FILE.read_text("utf-8").splitlines()
            if not l.startswith(f"{key}=")] if ENV_FILE.exists() else []
    ENV_FILE.write_text("\n".join(kept + [line]) + "\n", "utf-8")

# empty key is fine: step 1 (source.py) runs without an LLM; script.py checks it
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5-mini")
# DeepSeek and friends speak the OpenAI protocol; empty means api.openai.com
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")

TTS_BACKEND = os.getenv("TTS_BACKEND", "fish")     # fish | edge
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-BrianMultilingualNeural")   # edge only
# narration language; the multilingual voices cover both
OUTPUT_LANG = os.getenv("OUTPUT_LANG", "ru")

# --- Fish Audio (https://fish.audio/app/developers) ---
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")
# voice ids from fish.audio/m/<id>, picked to match the narrator's gender
FISH_VOICES_MALE = [v.strip() for v in os.getenv("FISH_VOICES_MALE", "").split(",") if v.strip()]
FISH_VOICES_FEMALE = [v.strip() for v in os.getenv("FISH_VOICES_FEMALE", "").split(",") if v.strip()]
FISH_SPEED = float(os.getenv("FISH_SPEED", 1.0))
# Delivery cue applied to the title card only. The hook is the three seconds
# that decide whether anyone watches, so it gets read harder than the story.
# Applied at synthesis, never shown on screen. Empty string disables it.
# The "even weight" half is not decoration: urgency alone makes the engine
# punch the final word, which turns every hook into the same rising jab and
# gives away that the line was written to be a hook.
# KEEP CUES UNDER 60 CHARACTERS. That is the ceiling on script.py's TAG
# pattern; a longer one is not recognised as a cue, so it survives into the
# word count and gets burned into the subtitles instead of steering the voice.
FISH_TITLE_CUE = os.getenv(
    "FISH_TITLE_CUE",
    "[urgent hook, even weight, no stress on the last word]")
# Same idea for the closing question. It is synthesized apart from the story so
# it reads as the narrator turning to the viewer, not as one more sentence of
# the plot - which is the whole reason it is there.
# This is the delivery constraint, not the mood: the model writes the mood cue
# in front of the question, picked for how that story ended, and _cued() merges
# the two into one bracket. That merged shape is what won the listening test -
# mood plus the constraint. Mood on its own was a separate take, and it lost.
# Kept short because both halves count against the 60-char ceiling together.
FISH_CTA_CUE = os.getenv("FISH_CTA_CUE", "[calm, no stress on the last word]")
# Fish returns audio only, so word timings come from local whisper alignment.
# base is enough - we use its timings, never its text.
WHISPER_SIZE = os.getenv("WHISPER_SIZE", "base")

SUBREDDITS = os.getenv("SUBREDDITS", "tifu").split(",")
MIN_SCORE = int(os.getenv("MIN_SCORE", 3000))
# Ceiling, not a typo. Above this a post went viral for Reddit-internal reasons
# - memes, meta drama, war, death - not because the story is good. The band
# between MIN and MAX is where ordinary relatable stories live.
MAX_SCORE = int(os.getenv("MAX_SCORE", 30000))
MIN_COMMENTS = int(os.getenv("MIN_COMMENTS", 100))
TARGET_SEC = int(os.getenv("TARGET_SEC", 75))
# hard floor: under this a video loses monetization eligibility, so main.py
# re-synthesizes at a slower rate rather than shipping a 58-second clip
MIN_SEC = int(os.getenv("MIN_SEC", 62))

# --- TikTok (https://developers.tiktok.com/apps) ---
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN", "")
# Drafts a day, and deliberately above YouTube's 2-3: nothing here is published
# automatically, so the ceiling is not about flooding a feed - it is how many
# stories a day the pipeline is allowed to spend. Runs where only this is due
# produce a TikTok-only video; YouTube keeps its own, slower allowance.
TIKTOK_PER_DAY = int(os.getenv("TIKTOK_PER_DAY", 4))
# A draft has no publish time of its own, which is why this was a count and
# nothing else for a while. That was wrong: the notification is what a human
# acts on, and two drafts forty minutes apart become two videos forty minutes
# apart in the same feed - measured 2026-08-02, 11:34 and 12:11. Lower than
# YouTube's gap because the count here is higher and the day still has to fit:
# four drafts three hours apart span twelve of the grid's thirteen hours.
TIKTOK_MIN_GAP_HOURS = float(os.getenv("TIKTOK_MIN_GAP_HOURS", 3))
HASHTAGS = os.getenv("HASHTAGS", "#reddit #redditstories #storytime #fyp")

# --- YouTube (https://console.cloud.google.com -> OAuth client, type Desktop) ---
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN", "")
# Pool to draw a different set from per upload, so descriptions are not clones.
# The quotes .env needs around a "#..." value are literal in a CI variable, so
# strip them here rather than publish a hashtag that starts with a quote mark.
YT_HASHTAGS = [t for t in (h.strip().strip('"\'')
                           for h in os.getenv("YT_HASHTAGS", HASHTAGS).split()) if t]
# Five hours apart, so three uploads spread across a waking day instead of
# landing in one block and competing with each other in the same feed.
YT_MIN_GAP_HOURS = float(os.getenv("YT_MIN_GAP_HOURS", 5))
# Parts of one split story run on their own, much tighter clock. Five hours
# between a cliffhanger and its answer loses the viewer who saw the first half:
# by then the feed has moved on and part 2 reads as a stranger's video.
PART_GAP_HOURS = float(os.getenv("PART_GAP_HOURS", 1.5))
# YouTube requires the synthetic-content flag for realistic material: real
# people made to say things they did not, altered footage of real events,
# realistic scenes that never happened, AI-generated music. Voice-over, a
# generated script and captions are named as production assistance and are
# exempt - which is all this pipeline does, so the flag is off by default.
# Turn it on the moment a video contains any of the four cases above.
DECLARE_AI = os.getenv("DECLARE_AI", "").lower() in ("1", "true", "yes")

# Subtitle font. Present on Windows; a Linux runner needs it installed, or
# swap it for something that ships there and carries Cyrillic.
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT", "Arial Black")

BG_DIR = ROOT / "assets" / "bg"      # background clips, 1080x1920
OUT_DIR = ROOT / "out"               # audio, .ass, finished mp4
DB_PATH = ROOT / "seen.db"           # sqlite: post_ids already turned into videos

OUT_DIR.mkdir(exist_ok=True)
BG_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    assert SUBREDDITS and all(SUBREDDITS), "SUBREDDITS is empty"
    assert 15 <= TARGET_SEC <= 180, f"TARGET_SEC={TARGET_SEC} out of sane range"
    assert MIN_SEC < TARGET_SEC, "TARGET_SEC must aim above the MIN_SEC floor"
    print(f"OK: {len(SUBREDDITS)} subs, {TARGET_SEC}s (floor {MIN_SEC}s), voice {TTS_VOICE}")
