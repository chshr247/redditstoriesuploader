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

# The English channel is stopped: its videos are not landing, so it writes,
# renders and sends nothing until this goes False. Deliberately NOT
# TIKTOK_ENABLED - that closes one destination and still pays the LLM, the TTS
# and the render for a story, then leaves the file with nowhere to go. This is
# the whole channel, checked before anything is generated and again before
# anything is published. Flip to False to bring it back; no env key, because a
# stop nobody can lift by accident is the point.
EN_STOPPED = True
STOPPED = EN_STOPPED and CHANNEL == "en"
STOP_REASON = f"channel {CHANNEL} is stopped (EN_STOPPED)"


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

# Thinking is ON by default on deepseek-v4 and is billed as output. Measured in
# publish run 32461703786: 24 calls, ~500k completion tokens, two of them at the
# model's own 65535 ceiling - under narrations of about 500 tokens each. That is
# ~95% of what a story costs, spent on reasoning nobody reads. "none" turns it
# off; "low"/"medium" buy it back if the rewrite rate in the logs climbs.
# `or` and not a getenv default: an unset repo variable reaches the workflow
# env as an empty string, not as an absent one.
LLM_REASONING = os.getenv("LLM_REASONING") or "none"
# And a ceiling of our own, so a runaway costs a failed story instead of a
# quarter of a day's budget. The longest thing asked for is a HORROR_SEC
# narration, about 2k tokens; polish re-emits it once more with markup.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS") or 4000)

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
# The horror slot reads with one fixed voice instead of drawing from the pool.
# A recurring narrator is what the genre runs on - the same voice every night is
# half of why a scary story sounds like one - and it is the reason this id was
# taken OUT of FISH_VOICES_MALE rather than left there: a voice that also reads
# the drama slot is nobody in particular. Unset falls back to the pool, so a
# channel can run the slot without pinning anything.
FISH_VOICE_HORROR = chan_env("FISH_VOICE_HORROR").strip()
FISH_SPEED = float(os.getenv("FISH_SPEED", 1.0))
# Chipmunk knob, and deliberately NOT the same thing as FISH_SPEED: the engine's
# prosody speed changes the pace and leaves the pitch where it was, which is what
# a Russian drama channel wants. The English one is there to be funny, and the
# thing that holds a viewer there is the voice sitting slightly above human -
# faster AND higher, which is one operation on the finished track (asetrate) and
# not two. Per channel, default 1.0 = off, so ru is untouched.
# Word timings are scaled by the same factor in voice.speak_parts(), or every
# subtitle would arrive late by the amount the track was shortened.
VOICE_SPEEDUP = float(chan_env("VOICE_SPEEDUP", "1.0"))
# ...and the price of doing it that way, paid back. asetrate moves EVERY
# frequency up by the same factor, so the voice's own sibilance and breath climb
# with it and the take reads as hissy. It is not noise and a denoiser is the
# wrong tool: measured 2026-08-16, the pauses in a Fish take sit at -91 dB, i.e.
# digital silence, and afftdn at three settings moved the result by nothing.
# What did move it is a high shelf after the shift. Same measurement, band
# energy above 9 kHz: -49.9 dB before the speed-up, -41.1 after it, and -48.0
# with -7 dB from 6 kHz - the top back roughly where it started while 300-3400
# never moves. Only applied when the speed-up is, since it exists to undo it.
# A taste knob on purpose. Too little and it hisses, too much and the voice goes
# dull, and where that line falls is a listening call - 0 disables it.
VOICE_DEHISS_DB = float(chan_env("VOICE_DEHISS_DB", "-7"))
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
# Horror is a pool of its OWN and deliberately not part of the list above.
# _pick() ranks on contested() - how hard the comments fought over the post -
# and a scary story is not argued about, so mixed into one list these subs would
# lose every slot to a family row and never surface at all. Per channel rather
# than shared: the pin above is a Russian-reading voice, and the English channel
# has no horror slot until SUBREDDITS_HORROR_EN says otherwise. Empty means no
# slot, which is the default and what every channel had before this existed.
SUBREDDITS_HORROR = [s.strip() for s in
                     chan_env("SUBREDDITS_HORROR").split(",") if s.strip()]
# Floor, and a much lower one since 2026-08-12. It was never the thing judging
# whether a story is good - source.contested() ranks on the votes and comments
# the post already has, and since 2026-08-14 that is the ONLY thing ranking on
# interest: the prompt's SKIP gate used to refuse a post for being dull and no
# longer does, because a model reading the text guesses at what a few thousand
# people already answered. What 3000 mostly did was throw away the fights:
# measured on a settled window of r/AmItheAsshole, the post with 897 comments
# against 1054 upvotes - the most argued-over story in it - sat below the floor.
MIN_SCORE = int(os.getenv("MIN_SCORE", 1000))
# redditapis.com - a pay-per-call proxy over the live reddit API, and the
# primary source. It is the only one of the three that can ask for a sub's top
# posts at all: pullpush sorts by score but has been 502 for days, and arctic
# shift cannot sort by score in any way, so it draws a random window of days
# and hopes. Worse, the archives store the score as it was when they crawled
# the post - 16 of 20 sampled windows came back with medians of 1-20 upvotes
# against 10-33 comments, i.e. snapshots taken minutes after posting. This
# returns today's numbers: ixjjl6 reads 54093 here against 53775 in the
# archive. Unset, and everything falls through to the archives as before.
REDDITAPIS_KEY = os.getenv("REDDITAPIS_KEY", "")
# Where "loud" starts, and nothing more than that: the score at which
# source.contested() hands out its full loudness point. There used to be a
# ceiling here as well, with a second fetch path and a daily slot behind it, on
# the grounds that a post above it went viral for Reddit-internal reasons -
# memes, meta drama, war, death - rather than for the story.
#
# Measured 2026-08-12 and it is not so. A year of r/tifu, r/AmItheAsshole and
# r/pettyrevenge - 32867 posts - held exactly 12 at or above 25000, and all 12
# were tellable stories: no memes, no link posts, no updates, no news. The
# ceiling was not keeping noise out, it was keeping the best material out. So
# there is one band now, [MIN_SCORE, inf), and loudness is a term in the
# ranking instead of a gate in front of it.
#
# The same measurement is why a daily "loud slot" made no sense either: twelve
# a year across three subs is one a month, and the slot promised one a day.
LOUD_AT = int(os.getenv("LOUD_AT", 25000))
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

# A softer plan, and the opposite trade to PLAN_FILE: instead of REPLACING the
# pool, it reserves ONE of the day's slots. A list of hand-picked post ids that
# main.py works down at one story a day - if that story splits, its parts take
# the day's next slots as usual - while the other slots still come from the pool
# ranked by contested(). When the list is spent every slot is the pool's again,
# so it is a way to seed a new sub into the rotation without handing it the whole
# feed. Per channel by name, same reasoning as PLAN_FILE; absent means no
# reservation and the pool fills every slot as before.
DAILY_FILE = ROOT / chan_env("DAILY_FILE", f"daily_{CHANNEL}.md")

# The word in front of a part number, in the channel's own language. Two places
# need it now and they are not the same surface: render.py writes it on the
# title card, publish.py writes it into the caption. One dict, so a third
# language cannot arrive in one of them and not the other.
PART_WORD = {"ru": "Часть", "en": "Part"}
# Per channel, because a second of video is not the same amount of story in
# both. The budget is words and they come out of TARGET_SEC at _heard_wpm():
# en is voiced at VOICE_SPEEDUP 1.25 and gets 288 of them, ru is not sped up
# at all and gets 195 - the same post told with a third less of the connective
# tissue that says who is who, which is what "reads like a translation" turned
# out to mean. ru buys those words back with runtime rather than with speed:
# the voice stays where it is and the video runs longer. Unsuffixed, so a bare
# TARGET_SEC in the env moves the default channel only and en holds 75 until
# TARGET_SEC_EN says otherwise.
TARGET_SEC = int(chan_env("TARGET_SEC", "75"))
# The ceiling for the horror slot, and only a ceiling: script.target_sec()
# scales the target by the length of the source and stops here. A scary story
# lives on the detail that a squeeze into 75 seconds throws away first, and it
# is told as ONE video rather than split - a cliffhanger with its answer an
# hour later is a device for a feed, not for a story somebody is watching to
# find out what was on the stairs. Note that past 180 seconds YouTube no
# longer treats the upload as a Short: youtube.py drops the #Shorts tag for
# these, which is the correct label and also a smaller audience.
HORROR_SEC = int(os.getenv("HORROR_SEC", 330))
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
# How big the morning batch is, counted in VIDEOS and not in questions. One
# story at a time was a question every three hours all day, each arriving
# minutes before its video was due, so an unanswered one held the whole
# pipeline. A batch is read in one sitting instead, and a story turned down is
# replaced immediately rather than at the next slot.
#
# Videos, because a split story is ONE question and two or three sends: four
# issues where one of them is a three-parter is six videos against an allowance
# of four, and the two that do not fit are a question asked for nothing. So a
# three-parter fills three of these and the day asks two questions instead of
# four - the same output, fewer things to read.
#
# The publishing allowance and not a number of its own: a video parked past it
# cannot go out today whatever the answer is, so writing it only pays the LLM
# early for a question that keeps until tomorrow.
REVIEW_BATCH = int(os.getenv("REVIEW_BATCH", TIKTOK_PER_DAY))
# Hours from UTC for the times quoted back on an issue. Everything internal
# runs on UTC and stays there; this is display only, and it exists because
# "публикация сегодня в 14:07" is read on a phone by someone who is not going
# to convert it. Default MSK, where the channel is.
REVIEW_TZ_H = float(os.getenv("REVIEW_TZ_H", 3))
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

# Which transport actually sends the file. The queue above does not care - the
# count, the gap, the pause and the part exemption are the same either way.
#   api  the official Content Posting API: a draft in the app inbox that a
#        human publishes. Sanctioned, runs on CI, carries no caption.
#   tau  our patched fork of makiisthenes/TiktokAutoUploader, driven as a
#        subprocess: signs in with saved browser cookies and posts for real,
#        caption and all. Against TikTok's ToS and at the account's risk (see
#        todo.md section 11), so it is opt-in per channel and never the default.
#        It cannot run on CI - the cookie and the browser profile are on a desk.
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
# Checkout of the fork - see the tau runbook. Deliberately not vendored: it
# wants playwright, moviepy and undetected-chromedriver from git, and none of
# those may share this venv. Shared, because it is a path to code and not a
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
# Where the signer's browsers live, and deliberately NOT playwright's default
# of %LOCALAPPDATA%\ms-playwright.
#
# The fork's venv is usually built on the Python that Windows offers first,
# which is the Microsoft Store one - and a Store app gets a VIRTUALISED
# AppData\Local, redirected into its package's LocalCache. Every child it
# spawns inherits that view. So the browsers install into the real directory,
# where cmd and node can see them, and the node the fork spawns looks into the
# redirected copy and finds nothing. Diagnosed 2026-08-16: the same
# fs.existsSync on the same path answered true from cmd and false from the
# fork's python, which is the whole of the "Failed to parse signature data"
# this project spent two days on in August.
#
# Anywhere outside AppData is immune, so it goes beside the checkout. Shared,
# like TIKTOK_TAU_DIR: it is a path to a browser, not a credential.
TIKTOK_TAU_BROWSERS = chan_env("TIKTOK_TAU_BROWSERS", shared=True) or (
    str(Path(TIKTOK_TAU_DIR) / "ms-playwright") if TIKTOK_TAU_DIR else "")

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
#
# The bar itself is gone. YT_VIRAL_ONLY offered YouTube nothing below the old
# viral floor, on the theory that TikTok would still take the rest - true for a
# channel with TikTok on, and for the English one it meant the channel had no
# outlet at all and burned ten stories in a day rendering for nobody. With the
# bands collapsed there is no viral floor left to be picky about either.
YT_PER_DAY = int(os.getenv("YT_PER_DAY", 0))
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
# State written by a machine that is NOT CI, and therefore kept where CI cannot
# reach it. seen.db is committed back to the repo by every run - that is the
# point of tracking it - so a row this machine writes there survives exactly
# until the next pull. Losing a row that says "this video went out" is not a
# lost record, it is the video going out a second time. Observed rather than
# predicted: it happened on the first merge to main, publicly.
# Untracked on purpose, and it never needs to travel: the machine that writes
# it is the only one that reads it. Per channel, like everything else here.
LOCAL_DB_PATH = ROOT / f"seen_local_{CHANNEL}.db"

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
    # Below TARGET_SEC it would not be a ceiling at all, and past ten minutes
    # TikTok refuses the upload outright.
    assert TARGET_SEC < HORROR_SEC <= 600, \
        f"HORROR_SEC={HORROR_SEC} must sit between TARGET_SEC and 600"
    # Loudness is a ranking term, so it has to sit above the floor to mean
    # anything: at or below it every candidate scores the full point and the
    # term stops separating anything at all.
    assert MIN_SCORE < LOUD_AT, f"LOUD_AT={LOUD_AT} is not above MIN_SCORE={MIN_SCORE}"
    # The cursor is per (sub, channel), so a sub named twice in one list is not
    # two sources - it is one source read twice, and _harvest's shuffle just
    # gives it two tickets in the draw.
    assert len(set(SUBREDDITS)) == len(SUBREDDITS), \
        f"{chan_key('SUBREDDITS', True)} repeats a sub"
    # A sub in both lists would be harvested twice and, worse, could take the
    # horror slot AND an ordinary one on the same day - the two pools are kept
    # apart precisely so one cannot rank against the other.
    assert not set(SUBREDDITS) & set(SUBREDDITS_HORROR), (
        f"{sorted(set(SUBREDDITS) & set(SUBREDDITS_HORROR))} is in both "
        f"{chan_key('SUBREDDITS', True)} and {chan_key('SUBREDDITS_HORROR')}")
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
        # A rotating exit IP is worse than none: one upload is a dozen requests
        # plus a page load by the signer, and a gateway that hands out a new
        # address per request makes every step of it come from somewhere else.
        #
        # DataImpulse offers two ways to pin it and this accepts either.
        # Measured 2026-08-16 against api.ipify.org, four calls each:
        #   :823    the rotating gateway - four calls, four different
        #           addresses. Pinned only by a session id written into the
        #           LOGIN, `;sessid.<name>`.
        #   :10000  a sticky port - four calls, one address. The port itself
        #           holds it, so no sessid is needed.
        # Either way it must be pinned SOMEHOW, and per account: two channels
        # behind one address is the single thing a proxy is here to prevent.
        #
        # The addresses themselves are deliberately NOT written down here. This
        # repository is public - it serves the policy pages the platforms
        # review - so an exit IP recorded next to a TikTok posting tool would
        # tie the account to the address the proxy exists to separate it from.
        # Measurements go in the private runbook; only what they proved stays.
        if "dataimpulse" in TIKTOK_PROXY:
            from urllib.parse import urlsplit as _split
            assert "sessid." in TIKTOK_PROXY or _split(TIKTOK_PROXY).port != 823, \
                (f"{chan_key('TIKTOK_PROXY')} is on the rotating gateway (:823) "
                 "with no ;sessid. in the login - every step of one upload "
                 "would come from a different IP. Add ;sessid.<name> after the "
                 "country, or move to a sticky port (:10000 and up).")
        # Both halves of the address have to survive parsing, or the failure is
        # a login that quietly goes out on the real IP. The fork's login browser
        # is plain Chrome and Chrome's --proxy-server takes NO credentials: it
        # drops everything between :// and @ and answers the challenge with a
        # dialog no driver can fill. The patched fork now generates a tiny
        # extension to answer it instead, which needs the two halves separable.
        if TIKTOK_PROXY:
            from urllib.parse import urlsplit
            _p = urlsplit(TIKTOK_PROXY)
            assert _p.scheme and _p.hostname and _p.port, \
                (f"{chan_key('TIKTOK_PROXY')} must be "
                 f"scheme://[user:pass@]host:port, got {TIKTOK_PROXY!r}")
            assert bool(_p.username) == bool(_p.password), \
                (f"{chan_key('TIKTOK_PROXY')} has a user without a password or "
                 "the other way round - the login challenge cannot be answered")
    print(f"OK: channel {CHANNEL}, {len(SUBREDDITS)} subs, "
          f"{TARGET_SEC}s (floor {MIN_SEC}s), score from {MIN_SCORE}, "
          f"loud at {LOUD_AT}")
    _hv = FISH_VOICE_HORROR[:8] if FISH_VOICE_HORROR else "from the male pool"
    print(f"    horror: {len(SUBREDDITS_HORROR)} subs, voice {_hv}"
          if SUBREDDITS_HORROR else "    horror: off")
    print(f"    voices: {len(FISH_VOICES_MALE)} male, {len(FISH_VOICES_FEMALE)} female"
          f"   yt token: {'set' if YT_REFRESH_TOKEN else 'MISSING'} ({YT_REFRESH_KEY})"
          f"   tiktok token: {'set' if TIKTOK_REFRESH_TOKEN else 'MISSING'}")
    print(f"    tiktok: {'on' if TIKTOK_ENABLED else 'PAUSED'} via "
          f"{TIKTOK_BACKEND}"
          + (f" as {TIKTOK_TAU_USER}, proxy "
             f"{'set' if TIKTOK_PROXY else 'NONE - real IP'}"
             if TIKTOK_BACKEND == "tau" else "")
          + f", {'PUBLIC' if TIKTOK_PUBLIC else 'private'}")
