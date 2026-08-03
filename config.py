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
# Subs that carry facts rather than personal stories. Searched alongside the
# rest, but tagged: script.py writes a fact with a different prompt, because the
# story prompt SKIPs anything that is not somebody's own experience - which is
# every post in here. They are NOT a third source of stories, they are the same
# pool with a flag on it, so a sub named twice would just be read twice.
# Picked for having a readable body: r/todayilearned and r/interestingasfuck are
# the obvious candidates and both are useless here - the fact is the title and
# the body is a link, so there is nothing to narrate for 75 seconds.
FACT_SUBREDDITS = [s.strip() for s in
                   chan_env("FACT_SUBREDDITS", "YouShouldKnow",
                            shared=True).split(",") if s.strip()]
MIN_SCORE = int(os.getenv("MIN_SCORE", 3000))
# Ceiling, not a typo. Above this a post went viral for Reddit-internal reasons
# - memes, meta drama, war, death - not because the story is good. The band
# between MIN and MAX is where ordinary relatable stories live.
MAX_SCORE = int(os.getenv("MAX_SCORE", 30000))
# ...with one deliberate exception a day. The ordinary band is chosen for being
# relatable, which is not the same as being watched: a day of nothing but
# ordinary stories has no peak in it. So the first video of each day is taken
# from ABOVE the ceiling instead - the loudest thing the subs have, whatever
# made it loud. Everything after it comes from the ordinary band as before.
VIRAL_MIN_SCORE = int(os.getenv("VIRAL_MIN_SCORE", 40000))
# Per UTC day, matching the allowance reset the rest of the pipeline uses. More
# than one and the exception stops being one.
VIRAL_PER_DAY = int(os.getenv("VIRAL_PER_DAY", 1))
MIN_COMMENTS = int(os.getenv("MIN_COMMENTS", 100))
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
PART_GAP_HOURS = float(os.getenv("PART_GAP_HOURS", 1.5))
HASHTAGS = chan_env("HASHTAGS", "#reddit #redditstories #storytime #fyp")

# Which transport actually sends the file. The queue above does not care - the
# count, the gap, the pause and the part exemption are the same either way.
#   api  the official Content Posting API: a draft in the app inbox that a
#        human publishes. Sanctioned, runs on CI, carries no caption.
#   tau  our patched fork of makiisthenes/TiktokAutoUploader, driven as a
#        subprocess: signs in with saved browser cookies and posts for real,
#        caption and all. Against TikTok's ToS and at the account's risk (see
#        todo.md section 11), so it is opt-in per channel and never the default.
TIKTOK_BACKEND = chan_env("TIKTOK_BACKEND", "api").strip().lower()
# Whether this channel's videos go up visible to everyone. Off by default, and
# it lives here rather than in the scheduler's command line on purpose: a
# forgotten --public is invisible, because a private video looks exactly like
# no video at all. Days of uploads can land in a hole nobody is looking at.
# Naming the intent per channel makes it survive rebuilding the scheduled task.
# --public still forces it on for one run; there is no flag to force it off,
# because a channel that publishes and a run that must not is not a real case.
TIKTOK_PUBLIC = chan_env("TIKTOK_PUBLIC", "0").strip().lower() in (
    "1", "true", "yes", "on")
# Checkout of the fork - see tau/README.md. Deliberately not vendored: it wants
# playwright, moviepy and undetected-chromedriver from git, and none of those
# may share this venv. Shared, because it is a path to code and not a
# credential; the account and the exit IP below are what must never be.
TIKTOK_TAU_DIR = chan_env("TIKTOK_TAU_DIR", shared=True)
# Interpreter of that checkout's own venv. Empty means "guess it from the dir".
TIKTOK_TAU_PYTHON = chan_env("TIKTOK_TAU_PYTHON", shared=True)
# The name the fork's cookie was saved under (`cli.py login -n <name>`), which
# is to say: the account. Per channel by the same rule as the refresh token - a
# silent fallback would post the English video to the Russian account.
TIKTOK_TAU_USER = chan_env("TIKTOK_TAU_USER")
# The user agent of the browser profile that minted that cookie. Left empty the
# fork picks a RANDOM one per upload, so the session is presented by a
# different browser than the one that logged in - which is exactly the tell an
# antidetect profile exists to avoid. Per channel: two profiles, two agents.
TIKTOK_TAU_UA = chan_env("TIKTOK_TAU_UA")
# One exit IP per account, and NOT shared for exactly that reason: two accounts
# reaching TikTok from one address is the single thing a proxy is here to
# prevent. Read by the fork's login browser and its signer too, so it covers
# the cookie and the signature, not just the upload.
TIKTOK_PROXY = chan_env("TIKTOK_PROXY")

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
# State written by a machine that is NOT CI, and therefore kept where CI cannot
# reach it. seen.db is committed back to the repo by every run - that is the
# point of tracking it - so a row this machine writes there survives exactly
# until the next pull. Losing a row that says "this video went out" is not a
# lost record, it is the video going out a second time.
# Untracked on purpose, and it never needs to travel: the machine that writes
# it is the only one that reads it. Per channel, like everything else here.
LOCAL_DB_PATH = ROOT / f"seen_local_{CHANNEL}.db"

OUT_DIR.mkdir(exist_ok=True)
BG_DIR.mkdir(parents=True, exist_ok=True)


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
    assert not (set(FACT_SUBREDDITS) & set(SUBREDDITS)), \
        "a sub in both lists is searched twice - keep the fact subs out of SUBREDDITS"
    # The cursor is per (sub, channel), so a sub named twice in one list is not
    # two sources - it is one source read twice, and _harvest's shuffle just
    # gives it two tickets in the draw.
    assert len(set(SUBREDDITS)) == len(SUBREDDITS), \
        f"{chan_key('SUBREDDITS', True)} repeats a sub"
    assert TIKTOK_BACKEND in ("api", "tau"), \
        f"{chan_key('TIKTOK_BACKEND')}={TIKTOK_BACKEND} is not api or tau"
    # A typo in the backend name would be caught above; a missing account would
    # not be caught until a video was already rendered and the run was spending
    # its allowance on it. The proxy is deliberately NOT required: running the
    # fork without one is a bad idea, not a broken config, and publish.py says
    # so out loud on every send.
    if TIKTOK_BACKEND == "tau":
        assert TIKTOK_TAU_DIR, f"{chan_key('TIKTOK_TAU_DIR', True)} is unset"
        assert TIKTOK_TAU_USER, f"{chan_key('TIKTOK_TAU_USER')} is unset"
        # A rotating exit IP is worse than none: the upload is a dozen requests
        # plus a page load, and this gateway hands out a new address per
        # request unless the login carries a session id.
        assert "dataimpulse" not in TIKTOK_PROXY or "sessid." in TIKTOK_PROXY, \
            (f"{chan_key('TIKTOK_PROXY')} has no sessid. - DataImpulse rotates "
             "the IP per request without one, so every step of one upload "
             "would come from a different address")
    print(f"OK: channel {CHANNEL}, {len(SUBREDDITS)} subs + "
          f"{len(FACT_SUBREDDITS)} fact subs, {TARGET_SEC}s (floor {MIN_SEC}s), "
          f"viral from {VIRAL_MIN_SCORE}")
    print(f"    voices: {len(FISH_VOICES_MALE)} male, {len(FISH_VOICES_FEMALE)} female"
          f"   yt token: {'set' if YT_REFRESH_TOKEN else 'MISSING'} ({YT_REFRESH_KEY})"
          f"   tiktok token: {'set' if TIKTOK_REFRESH_TOKEN else 'MISSING'}")
    print(f"    tiktok backend: {TIKTOK_BACKEND}"
          + (f" as {TIKTOK_TAU_USER}, proxy "
             f"{'set' if TIKTOK_PROXY else 'NONE - real IP'}"
             if TIKTOK_BACKEND == "tau" else "")
          + f", {'PUBLIC' if TIKTOK_PUBLIC else 'private'}")
