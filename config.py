"""Config: .env -> module constants. No classes, no validators."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

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
FISH_TITLE_CUE = os.getenv("FISH_TITLE_CUE",
                           "[speaking with urgency, hooking the listener]")
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
HASHTAGS = os.getenv("HASHTAGS", "#reddit #redditstories #storytime #fyp")

# --- YouTube (https://console.cloud.google.com -> OAuth client, type Desktop) ---
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID", "")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET", "")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN", "")
# pool to draw a different set from per upload, so descriptions are not clones
YT_HASHTAGS = [h.strip() for h in os.getenv("YT_HASHTAGS", HASHTAGS).split() if h.strip()]
YT_MIN_GAP_HOURS = float(os.getenv("YT_MIN_GAP_HOURS", 3))

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
