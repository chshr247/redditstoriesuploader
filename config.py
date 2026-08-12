"""Config: .env -> module constants. No classes, no validators.

One process serves ONE channel, the same way one process makes one video. The
channel is the narration language: `ru` publishes to the Russian YouTube and
TikTok accounts, `en` to the English ones. They are separate accounts with
nothing linking them, and every count, cursor and credential below is scoped to
whichever channel this process is.

Anything a channel owns is read through chan_env(). The default channel keeps
the bare key names it always had, so the existing .env and the existing repo
secrets keep working untouched; a second channel is opt-in and explicit.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

# The channel is the language. Two knobs for one thing would only let them
# disagree, so OUTPUT_LANG is it: narration language, prompt set, hashtag
# buckets, credentials, state - all keyed off this one value.
CHANNELS = ("ru", "en")
DEFAULT_CHANNEL = "ru"
OUTPUT_LANG = os.getenv("OUTPUT_LANG", DEFAULT_CHANNEL)
CHANNEL = OUTPUT_LANG
# Non-empty only away from the default channel. That asymmetry is deliberate:
# the Russian channel was here first and owns YT_REFRESH_TOKEN, out/<id>.mp4 and
# the untagged rows in seen.db, so nothing about it has to move.
SUFFIX = "" if CHANNEL == DEFAULT_CHANNEL else f"_{CHANNEL.upper()}"


def chan_key(key: str, shared: bool = False) -> str:
    """The env key this channel actually reads for `key`.

    `shared=True` means one value can serve every channel - an API key, an
    OAuth client, a subreddit list - so a missing YT_CLIENT_ID_EN quietly falls
    back to YT_CLIENT_ID.

    Everything else must be per-channel, and for a non-default channel the
    suffixed name is the ONLY name that counts. Falling back would be worse
    than failing: YT_REFRESH_TOKEN belongs to one account, so an English video
    would upload itself to the Russian channel and nothing would look wrong.
    """
    if not SUFFIX:
        return key
    scoped = key + SUFFIX
    return scoped if not shared or os.getenv(scoped) else key


def chan_env(key: str, default: str = "", shared: bool = False) -> str:
    """Empty counts as absent, which is not pedantry.

    An unset repository variable does not arrive unset: `${{ vars.X }}` renders
    to an empty string, so the env var exists and is "". os.getenv's default
    never fires, and a key whose default matters - YT_HASHTAGS, TIKTOK_ENABLED -
    silently comes out empty instead.
    """
    return os.getenv(chan_key(key, shared), "") or default


def chan_file(base: str) -> str:
    """out/ name for this channel: one story renders once per channel.

    The default channel keeps the bare <post_id>.mp4 - renaming it would make
    every already-uploaded file on disk look unpublished again.
    """
    return f"{base}_{CHANNEL}" if SUFFIX else base


def save_env(key: str, value: str) -> None:
    """Rewrite one key in .env, leaving everything else alone.

    Both platforms hand back a fresh refresh token and expect the old one to be
    dropped; printing it and hoping someone copies it by hand is how a pipeline
    silently dies a month later.

    Callers pass the CHANNEL-SCOPED name (chan_key), or a rotated English token
    would land on the Russian channel's key and log both channels out at once.
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

# --- Fish Audio (https://fish.audio/app/developers) ---
FISH_API_KEY = os.getenv("FISH_API_KEY", "")
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")
# Voice ids from fish.audio/m/<id>, picked to match the narrator's gender - and
# per channel, because a voice is chosen by listening to it read one language.
# The same id on another language is a different performance, usually accented.
FISH_VOICES_MALE = [v.strip() for v in chan_env("FISH_VOICES_MALE").split(",") if v.strip()]
FISH_VOICES_FEMALE = [v.strip() for v in chan_env("FISH_VOICES_FEMALE").split(",") if v.strip()]
FISH_SPEED = float(os.getenv("FISH_SPEED", 1.0))
# Delivery cue applied to the title card only. The hook is the three seconds
# that decide whether anyone watches, so it gets read harder than the story.
# Applied at synthesis, never shown on screen. Empty string disables it.
# The "even weight" half is not decoration: urgency alone makes the engine
# punch the final word, which turns every hook into the same rising jab and
# gives away that the line was written to be a hook.
# KEEP CUES UNDER script.CUE_MAX CHARACTERS, and remember the model's own mood
# cue is merged INTO this one, so the two share the budget. Past that ceiling a
# cue is not recognised as a cue: it survives into the word count and gets
# burned into the subtitles instead of steering the voice.
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
# And the story itself, which had no cue at all until now. The model spends its
# emotion cues where the story turns - three to six across twenty-odd sentences,
# which is the right rule - so most of the narration ran in the engine's default
# register and the take opened on whatever the first sentence happened to carry,
# usually a bare [emphasis]. Word-level peaks over a flat bed is what "reads
# like grey stone" sounds like. This sets the bed; the model's cues still do
# the turns.
# NO COMMAS IN THIS ONE. Every cue here is merged into the model's leading
# bracket, and script.speakers() reads the text before the first comma as the
# name of whoever speaks the next quoted line. Title and question have no
# dialogue so a comma there is free; in the story it would invent a speaker
# called "emphasis" and shift every real speaker's subtitle colour after it.
# Hence the space join at the call site too - see voice._cued().
FISH_BODY_CUE = os.getenv(
    "FISH_BODY_CUE", "[telling this to one person not reading it]")
# Fish returns audio only, so word timings come from local whisper alignment.
# base is enough - we use its timings, never its text.
WHISPER_SIZE = os.getenv("WHISPER_SIZE", "base")

# Per channel, and shared=True because one list CAN serve both: the posts are
# English either way, and the Russian channel retells them. What differs is what
# each audience is there for - the Russian channel runs on drama and recognition,
# the English one on being funny - so SUBREDDITS_EN leans on the subs where the
# story is meant to be laughed at. Unset it and both channels read the same subs
# again, exactly as before.
SUBREDDITS = [s.strip() for s in
              chan_env("SUBREDDITS", "tifu", shared=True).split(",") if s.strip()]
MIN_SCORE = int(os.getenv("MIN_SCORE", 3000))
# The day's one deliberate exception. The ordinary band below is chosen for
# being relatable, which is not the same as being watched: a day of nothing but
# ordinary stories has no peak in it. So the first video of each day is taken
# from ABOVE the ceiling instead - the loudest thing the subs have, whatever
# made it loud. Everything after it comes from the ordinary band as before.
#
# 25000 rather than 40000 since 2026-08-12: with YT_VIRAL_ONLY this is also the
# bar YouTube publishes at, and at 40000 the English channel's subs were not
# clearing it - its loudest story on 08-12 scored 25312 and the viral fetch came
# back empty all day.
VIRAL_MIN_SCORE = int(os.getenv("VIRAL_MIN_SCORE", 25000))
# Ceiling, not a typo. Above this a post went viral for Reddit-internal reasons
# - memes, meta drama, war, death - not because the story is good. The band
# between MIN and MAX is where ordinary relatable stories live.
#
# It follows VIRAL_MIN_SCORE rather than carrying a number of its own, because
# the two have to meet exactly: fetch() takes [MIN_SCORE, MAX_SCORE) and
# fetch_viral() takes [VIRAL_MIN_SCORE, inf), so a GAP between them is a band of
# posts neither call can reach - at 30000 against 40000 that lost everything
# between the two, six of the stories in plan_ru.md alone - and an OVERLAP is a
# band both reach, where an ordinary pick quietly spends the day's viral slot.
# Moving the floor now moves this with it; setting MAX_SCORE is opting out.
MAX_SCORE = int(os.getenv("MAX_SCORE", VIRAL_MIN_SCORE))
# Per UTC day, matching the allowance reset the rest of the pipeline uses. More
# than one and the exception stops being one.
VIRAL_PER_DAY = int(os.getenv("VIRAL_PER_DAY", 1))
MIN_COMMENTS = int(os.getenv("MIN_COMMENTS", 100))

# A hand-written running order, and when there is one it REPLACES the sourcing
# above: source.py stops choosing and main.py works down the list. That is the
# whole point of it - a plan whose stories can be overtaken by whatever the
# archive happened to return is not an order, it is a suggestion.
#
# Per channel by name rather than by value, so the English channel does not
# inherit the Russian one's plan through the default: chan_env() hands the SAME
# default to every channel, and a plan is the last thing that may be shared.
# A file that is not there means no plan and the pool as before, which is also
# what an exhausted channel falls back to.
PLAN_FILE = ROOT / chan_env("PLAN_FILE", f"plan_{CHANNEL}.md")

# The word in front of a part number, in the channel's own language. Two places
# need it now and they are not the same surface: render.py writes it on the
# title card, publish.py writes it into the caption. One dict, so a third
# language cannot arrive in one of them and not the other.
PART_WORD = {"ru": "Часть", "en": "Part"}
TARGET_SEC = int(os.getenv("TARGET_SEC", 75))
# hard floor: under this a video loses monetization eligibility, so main.py
# re-synthesizes at a slower rate rather than shipping a 58-second clip
MIN_SEC = int(os.getenv("MIN_SEC", 62))

# --- TikTok (https://developers.tiktok.com/apps) ---
# One app can hold tokens for several accounts, so the client pair is shared by
# default and only the token is per-channel. Override TIKTOK_CLIENT_KEY_EN and
# its secret if the second account ever ends up under its own app.
TIKTOK_CLIENT_KEY = chan_env("TIKTOK_CLIENT_KEY", shared=True)
TIKTOK_CLIENT_SECRET = chan_env("TIKTOK_CLIENT_SECRET", shared=True)
TIKTOK_REFRESH_TOKEN = chan_env("TIKTOK_REFRESH_TOKEN")
TIKTOK_REFRESH_KEY = chan_key("TIKTOK_REFRESH_TOKEN")   # for errors and rotation
# A pause switch per channel, and a real one rather than a quota of zero:
# publish.py lets a PART of a split story past the daily count on purpose, so
# TIKTOK_PER_DAY=0 would still deliver the middle of a story. This is checked
# ahead of everything, --force included - a pause that --force overrides is not
# a pause. Sending one file by hand (`python publish.py out/x.mp4`) still works.
TIKTOK_ENABLED = chan_env("TIKTOK_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
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
# Parts of one split story run on their own, much tighter clock, and they run it
# here: splitting is a TikTok arrangement and YouTube never sees a part. Three
# hours between a cliffhanger and its answer loses the viewer who saw the first
# half - by then the feed has moved on and part 2 reads as a stranger's video.
# It also has to be small enough that a three-parter fits inside one day, which
# the ordinary gap is not.
PART_GAP_HOURS = float(os.getenv("PART_GAP_HOURS", 1))
HASHTAGS = chan_env("HASHTAGS", "#reddit #redditstories #storytime #fyp")

# Whether this channel's videos go up visible to everyone. Off by default, and
# it lives here rather than in the scheduler's command line on purpose: a
# forgotten --public is invisible, because a private video looks exactly like
# no video at all. Days of uploads can land in a hole nobody is looking at.
# Naming the intent per channel makes it survive rebuilding the scheduled task.
# --public still forces it on for one run; there is no flag to force it off,
# because a channel that publishes and a run that must not is not a real case.
TIKTOK_PUBLIC = chan_env("TIKTOK_PUBLIC", "0").strip().lower() in (
    "1", "true", "yes", "on")

# --- YouTube (https://console.cloud.google.com -> OAuth client, type Desktop) ---
# The OAuth client belongs to the Cloud project, not to a channel: any Google
# account may consent to it, so one pair covers both. The refresh token is the
# opposite - it IS the channel - hence no fallback on that one.
# While the consent screen sits in "Testing", every Google account behind a
# channel also has to be listed as a test user, or its --auth simply refuses.
YT_CLIENT_ID = chan_env("YT_CLIENT_ID", shared=True)
YT_CLIENT_SECRET = chan_env("YT_CLIENT_SECRET", shared=True)
YT_REFRESH_TOKEN = chan_env("YT_REFRESH_TOKEN")
YT_REFRESH_KEY = chan_key("YT_REFRESH_TOKEN")
# EXTRA generic tags, added to the pool in tags.py - not the pool itself any
# more. tags.py owns the topic buckets and matches them against the text, and
# anything in here that a topic can earn (#семья, #работа, #отношения) is
# dropped rather than handed out at random: that is what put #отношения under
# a story about a boss. What is left is the broad stuff, and it still counts.
# The quotes .env needs around a "#..." value are literal in a CI variable, so
# strip them here rather than publish a hashtag that starts with a quote mark.
# Per channel: Russian tags under an English video reach nobody who can read
# them. An unset YT_HASHTAGS_EN falls through to HASHTAGS, whose default is
# already English.
YT_HASHTAGS = [t for t in (h.strip().strip('"\'')
                           for h in chan_env("YT_HASHTAGS", HASHTAGS).split()) if t]
# Five hours apart, so three uploads spread across a waking day instead of
# landing in one block and competing with each other in the same feed.
YT_MIN_GAP_HOURS = float(os.getenv("YT_MIN_GAP_HOURS", 5))
# Uploads a day. 0 means the ramp in youtube.daily_allowance decides (2 in the
# channel's first week, 3 after) - a knob because "how many" turned out to be
# an editorial call, not a platform limit: three ordinary stories a day landed
# on 0-8 views each, so the count came down to one and the bar went up.
YT_PER_DAY = int(os.getenv("YT_PER_DAY", 0))
# ...and that bar. On, only stories from the viral band (VIRAL_MIN_SCORE up)
# are offered to YouTube. The ordinary band still gets made and still goes to
# TikTok - this narrows one platform's queue, it does not narrow the pipeline.
# Nothing forces the two to agree, so a day whose viral slot came back empty
# simply has no YouTube upload in it, which is the intended trade.
# shared=True: one value pauses/raises the bar on every channel at once, and a
# suffixed one overrides a single channel. Safe to fall back on - unlike a
# token, the worst a shared value can do is apply where it was already meant.
YT_VIRAL_ONLY = chan_env("YT_VIRAL_ONLY", "0", shared=True).strip().lower() in (
    "1", "true", "yes", "on")
# Pause with an expiry: ISO-8601, UTC assumed, e.g. 2026-08-12T14:00. Past that
# moment the value is inert, which is the whole point - a pause that has to be
# lifted by hand is a pause somebody forgets to lift, and a channel that
# quietly stopped publishing looks exactly like a channel with no runs due.
YT_PAUSED_UNTIL = chan_env("YT_PAUSED_UNTIL", shared=True)
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
# Ad banners: png/jpg/webp for a still, gif/mp4/mov/webm for one that moves.
# Empty directory means no banner, which is the default - drop a file in and
# every render from then on carries it. See render._pick_ad().
AD_DIR = ROOT / "assets" / "ad"
OUT_DIR = ROOT / "out"               # audio, .ass, finished mp4
DB_PATH = ROOT / "seen.db"           # sqlite: post_ids already turned into videos

OUT_DIR.mkdir(exist_ok=True)
BG_DIR.mkdir(parents=True, exist_ok=True)
AD_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    assert CHANNEL in CHANNELS, f"OUTPUT_LANG={CHANNEL} is not one of {CHANNELS}"
    # The point of the suffix rule: away from the default channel, a credential
    # must be named for its channel or be absent. A silent fallback would put
    # one channel's videos on the other channel's account.
    assert chan_key("YT_CLIENT_ID", shared=True) in ("YT_CLIENT_ID", f"YT_CLIENT_ID{SUFFIX}")
    assert chan_key("YT_REFRESH_TOKEN") == f"YT_REFRESH_TOKEN{SUFFIX}"
    assert chan_file("abc") == ("abc" if not SUFFIX else f"abc_{CHANNEL}")
    assert SUBREDDITS and all(SUBREDDITS), "SUBREDDITS is empty"
    assert 15 <= TARGET_SEC <= 180, f"TARGET_SEC={TARGET_SEC} out of sane range"
    assert MIN_SEC < TARGET_SEC, "TARGET_SEC must aim above the MIN_SEC floor"
    # The two bands must not overlap, or the same post is both an ordinary
    # story and the day's one exception, and the cursors fight over it.
    assert MIN_SCORE < MAX_SCORE <= VIRAL_MIN_SCORE, \
        f"bands overlap: {MIN_SCORE} < {MAX_SCORE} <= {VIRAL_MIN_SCORE}"
    # The cursor is per (sub, channel), so a sub named twice in one list is not
    # two sources - it is one source read twice, and _harvest's shuffle just
    # gives it two tickets in the draw.
    assert len(set(SUBREDDITS)) == len(SUBREDDITS), \
        f"{chan_key('SUBREDDITS', True)} repeats a sub"
    print(f"OK: channel {CHANNEL}, {len(SUBREDDITS)} subs, "
          f"{TARGET_SEC}s (floor {MIN_SEC}s), viral from {VIRAL_MIN_SCORE}")
    print(f"    voices: {len(FISH_VOICES_MALE)} male, {len(FISH_VOICES_FEMALE)} female"
          f"   yt token: {'set' if YT_REFRESH_TOKEN else 'MISSING'} ({YT_REFRESH_KEY})"
          f"   tiktok token: {'set' if TIKTOK_REFRESH_TOKEN else 'MISSING'}")
    print(f"    tiktok: {'on' if TIKTOK_ENABLED else 'PAUSED'}, "
          f"{'PUBLIC' if TIKTOK_PUBLIC else 'private'}")
