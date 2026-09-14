"""Step 4: mp3 + word timings + background clip -> vertical mp4.

Subtitles are ASS, not drawtext: one Dialogue line per word with a scale-up
transition, which is the whole kinetic-typography effect. ffmpeg burns them
in a single pass, so no frame ever reaches Python.

The title is the exception: it is a drawn reddit post rather than text, so it
arrives as a stack of PNGs from card.py and goes over the burnt-in subtitles
as overlays. That is the only place a frame is built outside ffmpeg.
"""
import hashlib
import json
import logging
import os
import random
import re
import statistics
import subprocess
from pathlib import Path

import card
import safety
from config import (AD_DIR, BG_DIR, CHANNEL, CHANNELS, MUSIC_DIR, OUT_DIR, SFX,
                    SUBREDDITS_HORROR, SUBTITLE_FONT)
from voice import duration as _dur

W, H = 1080, 1920
FPS = 30          # source clips run 30 or 60; 60 doubles encode time for nothing here
FONT = SUBTITLE_FONT       # libass silently falls back if it is missing
FONT_SIZE = 110
POP_MS = 120               # scale-up duration of a word appearing
HOLD_MAX = 0.18            # how long a card may outlive its own audio
SKIP_HEAD = 30.0           # seconds of every background clip that are off limits
SKIP_TAIL = 30.0           # seconds of footage a seek has to leave after it

# --- encoder ceiling ---
# TikTok re-encodes everything, so the job here is to hand its encoder a clean
# source rather than a big one. crf 18 under a 12 Mbit/s cap is the band its
# web uploader transcodes most gently; above the cap the extra bits never reach
# a viewer, and x264 spends render time making them anyway.
#
# The cap was 12M for one afternoon and that was one afternoon too many. The
# first part rendered under it - yt_N09wqiI_O4w_1_p1, 243 seconds - came out
# 347 MB, sitting at 11.4 Mbit/s, exactly where crf 18 lands on this footage.
# The upload is the constraint and always was: measured 2026-08-03 over this
# uplink at 0.75 Mbit/s, 72 MB was thirteen minutes and did not survive them -
# the only file that got through was the smallest. 347 MB there is an hour of
# uploading against a poster that runs hourly.
#
# 5M is the middle the two measurements leave: roughly 150 MB for that same
# part, twice the bitrate the channel shipped at for weeks and less than half
# the upload of the 12M experiment. Budget ~0.6 MB per second of video.
#
# If renders still stop reaching TikTok, VIDEO_MAXRATE=2500k and VIDEO_CRF=26
# restore exactly what shipped before that, no code change and no redeploy -
# but note that publish.yml passes NONE of these three, so a repo variable
# does nothing: they are read here, from the environment of whatever runs
# render.py, and these defaults are the only value CI ever sees.
CRF = os.getenv("VIDEO_CRF", "18")
MAXRATE = os.getenv("VIDEO_MAXRATE", "5M")
BUFSIZE = os.getenv("VIDEO_BUFSIZE", "10M")     # 2x maxrate, x264's usual pairing
PROBE_FPS = 4              # frames sampled per second when mapping a clip's motion
HOOK_WINDOW = 3            # seconds a seek is judged on: the hook, and nothing after it
MOTION_DIR = BG_DIR / ".motion"   # one json per clip, next to the footage it describes
CUT_MIN_CARD = 1.0         # a card shorter than this is a cover, and nothing to cut on
CUT_MIN_GAP = 90.0         # how far the post-title footage must be from the opening
CUT_TRIES = 40             # draws allowed to find that gap before the cut is dropped

# --- overlay banner ---
# An optional image or clip the render STOPS for: the story freezes, the mix
# goes quiet, the banner plays centred over the held frame, and the story picks
# up where it left off. None of these numbers are taste: each one comes from a
# placement spec, and the reasoning behind every one of them - why this
# instant, why the pause, and which of its rules are knowingly not met - is in
# DOCS.ru.md, not here.
#
# The banner arrives as a green screen rather than as a file with an alpha
# channel, which is what every number below is shaped by.
AD_EVERY = 60.0            # one banner per FULL minute of video, each in the
                           # middle of its own minute - the programme's rule,
                           # and its own example: a 2 minute video carries two,
                           # at 0:30 and 1:30. See _ad_times().
AD_CHANNELS = ("ru",)      # channels that carry one; the rest never do
AD_HOLD_STILL = 5.0        # how long the story is paused for a banner that is
                           # a still and has no length of its own. A clip is
                           # asked how long it is - see _ad_hold().
AD_SPEED = 1.3             # how much the banner is sped up, picture and sound
                           # together. The programme allows 1.4 at the most and
                           # the self-check holds that ceiling; the point of
                           # using any of it is the pause, which is this much
                           # shorter for it - 6.0s of banner becomes 4.6s of
                           # stopped story.
AD_SCALE = 1.0             # share of the frame's WIDTH the green screen is
                           # scaled to. The programme wants the banner over a
                           # quarter of the screen: at 1.0 it covers 29.6% of
                           # the frame, and 0.85 - the old offer's setting -
                           # would put it at 21%. The self-check holds the full
                           # width, so this is not a free knob any more.
AD_KEY = "0x00FE00"        # the green it is delivered on, sampled off the file
AD_SIM = 0.25              # key tolerance. The artwork's own darkest pixel
                           # sits 0.99 away from that green, so this is about
                           # the anti-aliased edges and nothing else.
AD_VOL = 1.0               # the banner's own sound, against the narration.
                           # Not a taste setting: no audible voice-over on the
                           # banner means the video is not paid for at all.
AD_BAND_Y = 0.0            # the banner's band inside that screen, as a share
AD_BAND_H = 1.0            # of its height - see _ad_chain() for why a band.
                           # This artwork uses the whole screen (measured: it
                           # reaches y 38..961 of 1000 over its six seconds),
                           # so the crop is a no-op and the numbers are here
                           # for the next banner, not for this one.
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

# The whoosh runs under the card's first frames, and voice.py has already left
# room for it - the narration starts where the sound ends, so the two are never
# heard over each other. See config.SFX.
SFX_VOL = 0.5              # against the narration, which is the thing being heard

# --- background music ---
# Barely audible on purpose. Measured 2026-08-23: the narration means -22.4 dB
# and the tracks mean about -16, so the music arrives LOUDER than the voice and
# the gain here is what puts it under. 0.045 is about -27 dB, landing the bed
# some 20 dB below the speech - present in the pauses, gone under a sentence.
# Turn this UP only after listening on a phone speaker: headphones flatter it.
MUSIC_VOL = 0.045
MUSIC_FADE = 2.0           # in after the whoosh, out over the closing question
# Ducking. The narration is the key: every time the voice comes in the bed is
# pushed down further, so the two never compete for the same instant. Without
# it a constant -27 dB still fights consonants, which is what makes cheap
# voice-overs sound muddy rather than quiet.
MUSIC_DUCK = "threshold=0.03:ratio=8:attack=5:release=300"
# Compressed only, and wav is left out deliberately. The masters sit in the
# same folders untracked (see .gitignore), so counting them would give this
# desk eight tracks where CI has four - and _pick_music() indexes into that
# list, so the two machines would disagree about which track a story gets and
# this one could pick a file that is not in the repo at all.
MUSIC_EXT = (".mp3", ".m4a", ".opus", ".ogg")

# --- output loudness ---
# What the finished mix is normalised to, last, after the voice, the bed and
# the ducking. Measured on a published render (yt_Zk7zfGDnZEM_2, 2026-09-08):
# -22.4 LUFS integrated, against roughly -14 for the feed it lands in. TikTok
# turns a loud upload DOWN and does not turn a quiet one up, so those eight
# decibels are simply lost - the video plays flat and far away next to the one
# before it, during the seconds it is being decided on.
#
# Applied to the whole mix rather than to the narration, so the bed keeps the
# ratio MUSIC_VOL was tuned to: everything moves by the same amount.
# -1.5 dBTP leaves the headroom their encoder needs to not clip on the way in.
#
# ponytail: one pass, which measures as it goes and lands near the target
# rather than on it. Two passes would mean decoding the whole track first, for
# a video nobody is mastering. If a render comes out audibly off, that is the
# upgrade - not a different number here.
LUFS_I, LUFS_TP, LUFS_LRA = -14, -1.5, 11

log = logging.getLogger(__name__)

# The title card is not an ASS style any more - it is a stack of PNGs drawn by
# card.py and laid over the footage here. See _card_chain().

# ASS stores colour as &HAABBGGRR - byte order reversed from RGB. Narrator stays
# white; each speaker takes the next colour the first time they say anything.
SPEAKER_COLOURS = ["&H0000FFFF",   # yellow
                   "&H00FFFF00",   # cyan
                   "&H0055FF55",   # light green
                   "&H008888FF"]   # salmon
# Values, not placeholders: this string is interpolated INTO the header, so a
# {FONT} left here would reach libass verbatim and silently drop the style back
# to a default font at a default size.
SPEECH_STYLES = "\n".join(
    f"Style: Speech{i},{FONT},{FONT_SIZE},{c},&H000000FF,&H00000000,"
    "&H80000000,-1,0,0,0,100,100,0,0,1,8,3,5,80,80,0,1"
    for i, c in enumerate(SPEAKER_COLOURS))
# The NARRATOR's own colour, which is what lets one voice read every story:
# the job of saying whose story this is moves off the ear and onto the eye.
# Muted on purpose - these are full-screen word cards over footage, and a
# saturated pink is unreadable at the size they run. An unknown gender keeps
# the white it always had.
NARRATOR_WHITE = "&H00FFFFFF"
NARRATOR_COLOURS = {"female": "&H00B48CFF",   # soft pink
                    "male": "&H00FFC88C"}     # soft blue
ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{FONT},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,5,80,80,0,1
{SPEECH_STYLES}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _header(gender: str = "") -> str:
    """The header with Main tinted for the narrator's gender.

    Substituted rather than templated: ASS_HEADER is an f-string, so a
    placeholder left in it for this would have to survive that pass, and the
    one time a {FONT} did not, libass quietly fell back to a default font at a
    default size. The colour appears once, in Main's PrimaryColour - the
    speaker styles carry their own - and the selftest holds that to one.
    """
    return ASS_HEADER.replace(NARRATOR_WHITE, NARRATOR_COLOURS.get(gender, NARRATOR_WHITE), 1)


def _ts(sec: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc"""
    cs = round(sec * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _styles(words: list[dict]) -> dict:
    """Map each speaker to a style name, in order of first appearance.

    A line in someone else's colour is readable with the sound off, which is
    how most of these are first watched. Speakers past the palette wrap around
    rather than fall back to white - two sharing a colour is a smaller lie than
    a speaker looking like the narrator.
    """
    names = []
    for w in words:
        s = w.get("speaker")
        if s and s not in names:
            names.append(s)
    return {s: f"Speech{i % len(SPEAKER_COLOURS)}" for i, s in enumerate(names)}


def _group(words: list[dict], min_chars: int = 3, max_words: int = 2,
           max_chars: int = 14) -> list[dict]:
    """Glue filler words onto the next one, without overflowing the line.

    Straight one-word-per-card gives "a" and "I" a full beat of full-screen
    time, which reads as a stutter. A card keeps absorbing words while it
    still ends on a stub, so it never trails off on an article either.

    max_chars is the width limit: past roughly fourteen characters the card
    wraps onto a second line, which looks like a mistake at this font size.
    """
    out = []
    for w in words:
        prev = out[-1] if out else None
        if (prev and len(prev["word"].split()[-1]) < min_chars
                and len(prev["word"].split()) < max_words
                and len(prev["word"]) + 1 + len(w["word"]) <= max_chars
                # never let one card mix two voices
                and prev.get("speaker") == w.get("speaker")):
            prev["word"] += " " + w["word"]
            prev["end"] = w["end"]
        else:
            out.append(dict(w))
    return out


def build_ass(words: list[dict], path, gender: str = "") -> None:
    """One word card at a time, over whatever the footage is doing.

    The title is not here any more: it is a drawn post, laid over the video as
    images rather than set as text - see card.py.
    """
    lines = [_header(gender)]
    styles = _styles(words)
    words = _group(words)
    for i, w in enumerate(words):
        # A card follows the voice: it goes when the words stop. Holding it to
        # the next card would leave text standing over silence. The small hold
        # only bridges the gaps between words inside a phrase, which are too
        # short to blank out without making the screen flicker.
        nxt = words[i + 1]["start"] if i + 1 < len(words) else w["end"]
        end = min(nxt, w["end"] + HOLD_MAX)
        # Punctuation earns nothing on a one-word card and costs width; commas
        # and dashes in particular read as specks. Words and digits only.
        text = safety.mask(re.sub(r"[^\w\s]", "", w["word"]).strip())
        if not text or end <= w["start"]:
            continue
        pop = r"{\fscx70\fscy70\t(0,%d,\fscx100\fscy100)}" % POP_MS
        style = styles.get(w.get("speaker"), "Main")
        lines.append(f"Dialogue: 0,{_ts(w['start'])},{_ts(end)},{style},,0,0,0,,{pop}{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pick_bg(key: str = "", channel: str = CHANNEL) -> "Path":
    """A background clip. With a `key`, one that the other channels did not get.

    The channels tell the same story in different languages, and the two videos
    must not be the same footage with a different soundtrack - that is one video
    posted twice as far as a platform is concerned, and as far as anyone who
    sees both is concerned too. Drawing at random is not enough: with three
    clips a third of the pairs would collide.

    So the clip is chosen from the story, offset by the channel. Same key on two
    channels lands on two different clips as long as there are at least as many
    clips as channels, and the seek inside the clip is still random, so nothing
    repeats frame for frame either. No state: two runs hours apart on different
    machines agree without having to have met.

    md5 rather than hash(): the built-in is salted per process, so it would give
    a different answer every run and the guarantee would be gone.
    """
    # recursive on purpose: an archive that carries its own top folder unpacks
    # to assets/bg/bg/*.mp4, and a flat search would silently find nothing
    clips = sorted(p for p in BG_DIR.rglob("*")
                   if p.suffix.lower() in (".mp4", ".mov", ".webm"))
    if not clips:
        raise RuntimeError(f"no background clips under {BG_DIR} - drop a vertical mp4 there")
    if not key or len(clips) < 2:
        return random.choice(clips)
    seed = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
    offset = CHANNELS.index(channel) if channel in CHANNELS else 0
    return clips[(seed + offset) % len(clips)]


def _motion(clip: Path) -> list[float] | None:
    """Mean scene-change score for every whole second of `clip`, or None.

    A satisfying compilation is not uniformly satisfying: it has peaks, and it
    has a minute of someone slowly unwrapping something. _seek() puts the hook
    on a peak instead of taking whatever the dice gave, and to do that it has
    to know which second is which.

    Frames are sampled at PROBE_FPS and scaled to 160px wide before scoring.
    The question here is "how much is going on around here", not where the cuts
    are, and decoding twenty minutes of 1080p60 in full buys nothing for it -
    measured 44x realtime this way, so about 25 seconds per clip, once.

    Cached next to the footage, keyed on size and mtime. In CI that cache lives
    or dies with the backgrounds cache it sits inside; a miss costs one clip's
    measurement, not all three, because only the chosen clip is ever measured.

    None on any failure, never an exception: a background that cannot be
    measured is one that falls back to the old uniform seek, not a dead render.
    """
    try:
        st = clip.stat()
    except OSError:
        return None
    stamp = {"size": st.st_size, "mtime": int(st.st_mtime)}
    cache = MOTION_DIR / f"{clip.stem}.json"
    if cache.exists():
        try:
            got = json.loads(cache.read_text("utf-8"))
            # size+mtime, not a content hash: re-reading twenty minutes of video
            # to decide whether to re-read twenty minutes of video is absurd
            if all(got.get(k) == v for k, v in stamp.items()):
                return got["seconds"]
            log.info("%s changed, measuring its motion again", clip.name)
        except (json.JSONDecodeError, KeyError, OSError):
            log.warning("%s: motion cache unreadable, measuring again", clip.name)

    log.info("measuring motion in %s (once, then cached)", clip.name)
    try:
        # file=- puts the metadata on stdout, which sidesteps the Windows drive
        # colon that a file= path would smuggle into the filter description
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(clip), "-an", "-vf",
             f"fps={PROBE_FPS},scale=160:-2,select='gt(scene,0)',"
             "metadata=print:file=-", "-f", "null", "-"],
            capture_output=True, text=True, check=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("could not measure %s (%s) - falling back to a plain seek",
                    clip.name, e)
        return None

    # the filter prints two lines per frame: the frame, then its score
    seconds: dict[int, list[float]] = {}
    at = None
    for line in proc.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            at = int(float(m.group(1)))
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and at is not None:
            seconds.setdefault(at, []).append(float(m.group(1)))
    if not seconds:
        log.warning("%s: no scene scores came back - falling back", clip.name)
        return None

    # a gap-free list indexed by second, so callers can slice it by time
    out = [statistics.fmean(seconds.get(s, [0.0]))
           for s in range(max(seconds) + 1)]
    try:
        MOTION_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({**stamp, "seconds": out}), "utf-8")
    except OSError as e:
        log.warning("could not cache motion for %s (%s)", clip.name, e)
    return out


def _live(scores: list[float], lo: float, hi: float) -> list[int]:
    """Seconds in [lo, hi] whose next HOOK_WINDOW is livelier than this clip's median.

    The median rather than a fixed threshold: scene scores are not comparable
    between clips - a fast-cut compilation sits an order of magnitude above a
    single slow shot - so "lively" can only mean lively FOR THIS CLIP.

    A window is worth only its WEAKEST second. Averaging looks right and is
    wrong: a scene score spikes on a CUT, so a still shot, a hard cut and
    another still shot average out well above the median while what the viewer
    gets is a freeze, a blink and another freeze. Measured on a real clip -
    seconds 573-575 scored 0.0015, 0.0667, 0.0008, averaged to 0.023 against a
    median of 0.014, and shipped three frozen seconds under the hook. The
    question is whether the whole window moves, and only the minimum asks it.
    """
    starts = range(int(lo) + 1, int(hi) + 1)
    windows = {s: min(scores[s:s + HOOK_WINDOW])
               for s in starts if len(scores[s:s + HOOK_WINDOW]) == HOOK_WINDOW}
    if len(windows) < 10:
        # too few to have a meaningful middle; let the caller draw uniformly
        return []
    bar = statistics.median(windows.values())
    return [s for s, v in windows.items() if v > bar]


def _seek(bg_dur: float, name: str = "",
          scores: list[float] | None = None) -> float:
    """Where inside the background clip to start, at random.

    Random so consecutive videos don't share footage, and never inside the
    first SKIP_HEAD seconds: a background clip opens on an intro, a title card
    or a menu, the one stretch of it that looks like a clip off YouTube.

    The window is [SKIP_HEAD, bg_dur - SKIP_TAIL]. The narration may well run
    past the end of the footage - -stream_loop wraps it back to the seek, and
    the head stays skipped when it does - but a seek in the last SKIP_TAIL
    seconds wraps almost immediately and reads as a glitch, not as a loop.

    Given `scores` from _motion(), the draw narrows to the livelier half of
    that window. Uniform, roughly half the videos open on the slow stretch of
    a compilation, and those are three seconds the hook does not get back.
    Still a DRAW over the good seconds, not the single best one: three clips
    serve every video this channel makes, and a clip that always opens at its
    one peak is a repeat the viewer notices.
    """
    latest = bg_dur - SKIP_TAIL
    if latest >= SKIP_HEAD:
        live = _live(scores, SKIP_HEAD, latest) if scores else []
        if live:
            return float(random.choice(live))
        return round(random.uniform(SKIP_HEAD, latest), 2)
    if bg_dur > SKIP_HEAD:
        # too short to leave SKIP_TAIL after the head; start right past the
        # head anyway and let it loop, which re-seeks here rather than to 0
        log.warning("%s is only %.0fs - looping from %.0fs", name, bg_dur, SKIP_HEAD)
        return SKIP_HEAD
    log.warning("%s is only %.0fs - shorter than the %.0fs head skip",
                name, bg_dur, SKIP_HEAD)
    return 0.0


def _cut(bg_dur: float, dur: float, title_end: float, name: str,
         scores: list[float] | None, first: float) -> float | None:
    """Where the footage jumps to when the title card leaves, or None for no cut.

    A whole video off one unbroken stretch of one clip is what makes these read
    as a conveyor. The eye re-engages at a cut, and there is exactly one moment
    worth spending that on: the title card going away and the story starting.
    Before it the viewer is reading, after it they are listening, and the
    footage changing underneath says so.

    The second stretch is drawn the same way as the first, so it is livelier
    than this clip's median too, and it must land CUT_MIN_GAP away from where
    the video opened - two seeks thirty seconds apart in a compilation of long
    takes are the same shot, and the cut lands as a glitch instead.

    None whenever there is nothing to gain or no room to do it safely: no title
    card to cut on, a story too short to have two parts, or a clip that could
    not offer a far enough second window in CUT_TRIES draws.
    """
    # The card is a COVER now, not a passage of narration: it is gone inside a
    # second, and there is no "before it the viewer is reading" left to cut on.
    # A jump three tenths of a second in is a glitch, not a beat.
    #
    # ponytail: this drops the one cut these videos had, and with it the reason
    # the footage was not one unbroken stretch. If they start reading as a
    # conveyor again, the beat to cut on is the end of the opening line - which
    # is in `words` and would be passed in here the way title_end is now.
    if title_end < CUT_MIN_CARD or dur - title_end < HOOK_WINDOW:
        return None
    for _ in range(CUT_TRIES):
        pick = _seek(bg_dur, name, scores)
        if abs(pick - first) >= CUT_MIN_GAP:
            return pick
    log.info("%s: no second window %.0fs clear of %.0fs, leaving the cut out",
             name, CUT_MIN_GAP, first)
    return None


def _pick_ad(channel: str = CHANNEL) -> Path | None:
    """A banner from AD_DIR, or None when there is nothing to show.

    No config switch: an empty directory is the off position. Several files are
    drawn between at random, which is what a rotation of banners wants.

    A channel outside AD_CHANNELS never gets one, whatever is in the directory.
    Both channels render out of the same assets/ad, so the check has to be here
    - CI fetching the banner for every job is a cached download, not a decision.
    """
    if channel not in AD_CHANNELS:
        return None
    ads = sorted(p for p in AD_DIR.rglob("*")
                 if p.suffix.lower() in IMAGE_EXT + (".gif", ".mp4", ".mov", ".webm"))
    return random.choice(ads) if ads else None


def _ad_hold(ad: Path) -> float:
    """How long the story is paused for this banner - its own length, sped up.

    A still has no length of its own and nothing to speed up, so AD_SPEED does
    not touch it.
    """
    if ad.suffix.lower() in IMAGE_EXT:
        return AD_HOLD_STILL
    return _dur(ad) / AD_SPEED


def _ad_times(dur: float, hold: float = 0.0) -> list[float]:
    """Where the NARRATION is cut for each banner, seconds into the narration.

    One banner per FULL minute, never fewer than one. The programme says two
    things, and they are not the same rule - which one applies is decided by
    how many banners there are:

    * under two minutes, one banner, "strictly in the MIDDLE of the video" -
      so 1:59 of video carries it at 0:59, not at 0:30;
    * two minutes and up, the worked example is absolute: "0:30 and 1:30, and
      so on", one banner in the middle of each MINUTE. Spreading them over
      equal slices of the video instead drifts the last one out of its minute
      (2:50 of video put it at 2:07, leaving 1:00-2:00 bare) and that is
      exactly what the programme refuses to pay for.

    The count is solved for rather than divided out, because the finished video
    is longer than the narration by `hold` per banner - the story stops while
    the banner plays - and the minutes the programme counts are the finished
    ones: 1:58 of story is 2:04 on screen, and that is two banners, not one.

    The cut points are in the NARRATION, which is that much shorter, so each
    one steps back by the pauses already taken (`i*hold`) and by half of its
    own (the banner wants its MIDDLE on the mark, not its first frame). The
    single-banner case needs no such correction: the middle of the narration
    is the middle of the video, because the one pause sits on it and grows it
    symmetrically.
    """
    n = 1
    while n < int((dur + n * hold) // AD_EVERY):
        n += 1
    if n == 1:
        return [dur / 2]
    return [AD_EVERY * (i + 0.5) - hold * (i + 0.5) for i in range(n)]


def _snap(at: float, words: list[dict]) -> float:
    """`at` moved to the nearest gap between words.

    The story stops dead for the banner, and stopping it in the middle of a
    word is the one way to make a pause sound like a glitch instead of a break.
    Word ends rather than starts: the gap AFTER a word is where the narrator
    has already finished saying it.
    """
    ends = [w["end"] for w in words if w.get("end")]
    return min(ends, key=lambda e: abs(e - at)) if ends else at


def _pause(src: str, out: str, cuts: list[float], hold: float,
           audio: bool = False) -> str:
    """Stop `src` dead for `hold` seconds at each of `cuts` -> `out`.

    setpts pushes everything after a cut later by `hold`, which leaves a hole
    in the timestamps; what fills the hole is what makes it a pause rather than
    a jump. On the picture that is `fps`, which repeats the last frame it was
    given until the next one is due - so the frame the story stopped on stays
    on screen under the banner. On the sound it is `aresample`, which fills a
    gap in the timestamps with silence.

    Both of those are ordinary behaviour of filters already in the chain, which
    is the whole reason it is done this way: the obvious build - split, trim
    each stretch, concat with a frozen frame between - buffers a whole video of
    1080x1920 frames on the branches concat is not reading yet.
    """
    if not cuts:
        return f"{src}{'anull' if audio else 'null'}[{out}]"
    shift = "+".join(f"gte(T,{c:.3f})" for c in cuts)
    if audio:
        return (f"{src}asetpts='PTS+({shift})*{hold:.3f}/TB',"
                f"aresample=48000:async=1:first_pts=0[{out}]")
    return f"{src}setpts='PTS+({shift})*{hold:.3f}/TB',fps={FPS}[{out}]"


def _ad_input(ad: Path) -> list[str]:
    """Input flags for ONE playing of the banner.

    A still frame has to be told to repeat at all or it is gone after a single
    video frame, and a gif carries a loop count the demuxer honours only when
    asked. A clip is left alone: it plays once, where it was put, and the
    banner appearing again later is another input of the same file - looping it
    would restart the animation every six seconds for the rest of the video.
    """
    ext = ad.suffix.lower()
    if ext in IMAGE_EXT:
        return ["-loop", "1", "-i", str(ad)]
    if ext == ".gif":
        return ["-ignore_loop", "0", "-i", str(ad)]
    return ["-i", str(ad)]


def _has_audio(p: Path) -> bool:
    """Whether the file carries a sound track at all - a still never does."""
    return bool(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True).stdout.strip())


def _ad_w() -> int:
    """The banner's width in pixels, even - x264 will not take an odd one."""
    return int(W * AD_SCALE) // 2 * 2


def _ad_h(ad: Path) -> int:
    """How tall the banner's band lands on the frame, once scaled to _ad_w().

    Measured off the file rather than assumed: the old artwork was 9:16 like
    the frame, this one is 1902x1000, and the difference is the whole of what
    the placement checks are about.
    """
    w, h = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(ad)],
        capture_output=True, text=True, check=True).stdout.strip().split(",")
    return round(_ad_w() * int(h) * AD_BAND_H / int(w))


def _ad_chain(idx: int, times: list[float], src: str = "base") -> str:
    """Filter graph putting the banner over [src] once per `times` -> [v].

    One input per appearance, starting at `idx`: the same file opened again is
    cheaper than it looks - six seconds of 1902x1000 - and it is the only way
    each showing can start from its own first frame (see tpad below).

    The banner is delivered on a green screen rather than with an alpha
    channel, so the chain keys it. colorkey, not chromakey: chromakey compares
    chroma alone, and the artwork's dark navy is close enough to the green in
    U/V that it goes with the background - measured, it took the whole banner
    and left the corners.

    The crop is the BAND the banner lives in, not the banner: it travels up and
    down inside that band as part of its own animation, and a crop to the
    artwork slices it in half halfway through the loop. Whatever the band
    leaves around the artwork is green and keys out with the rest. The band is
    what the crop is for at all - the file carries a stray sparkle outside it,
    drawn in a green far enough from the key to survive it.

    Cropping before keying rather than after is not an optimisation: the key
    has to run on the whole strip either way, and this way it never sees the
    sparkle to begin with.

    Scaling the green screen by the frame's WIDTH is the whole size question.
    Pasting the artwork at its own pixel size on a bigger frame would quietly
    shrink it against everything around it; stretching the screen to W lands it
    at the share of the frame it was drawn to cover. AD_SCALE takes a share of
    that, and the programme's quarter-of-the-screen floor is what decides how
    small that share may get - see the self-check.

    tpad rather than overlay's `enable`: enable only hides the banner while its
    stream runs on underneath, so the clip would arrive halfway through the
    video already halfway through itself. Padding the FRONT with transparent
    frames delays the stream itself, so the banner's own entry animation starts
    on its first frame the moment it appears. Nothing is faded over that on
    purpose - the banner animates itself in, and a second fade on top reads as
    a stutter.
    """
    parts, cur = [], src
    for i, at in enumerate(times):
        nxt = "v" if i == len(times) - 1 else f"adon{i}"
        parts += [
            # setpts ahead of fps, not after it: this is where the clip is sped
            # up, and fps is then what hands the graph a clean stream at FPS
            # rather than one with the frames of a 60 fps source bunched up.
            f"[{idx + i}:v]setpts=PTS/{AD_SPEED},fps={FPS},"
            f"crop=iw:ih*{AD_BAND_H}:0:ih*{AD_BAND_Y},"
            f"colorkey={AD_KEY}:{AD_SIM}:0.03,format=rgba,scale={_ad_w()}:-2,"
            f"tpad=start_duration={at:.3f}:start_mode=add:color=black@0[ad{i}]",
            # eof_action=pass, not shortest: a banner that runs out must leave
            # the video alone, not cut it off wherever it happened to end
            # dead centre, both ways: the story is frozen underneath, so there
            # is nothing left for the banner to keep out of the way of
            f"[{cur}][ad{i}]overlay=(W-w)/2:(H-h)/2:format=auto:"
            f"eof_action=pass[{nxt}]",
        ]
        cur = nxt
    return ";".join(parts)


def _pick_music(key: str = "", sub: str = "", channel: str = CHANNEL) -> Path | None:
    """A track for this story's mood, or None when the folder is empty.

    The mood is the subreddit's: a horror story gets the horror bed, everything
    else gets the ordinary one. Same test voice.py uses to pin the horror
    narrator, so the voice and the music can never disagree about what kind of
    story this is.

    Chosen from the story like the footage, and for the same reason - two runs
    of the same story agree without having met - but off a DIFFERENT seed. Left
    on the same one, a story would always pair track 3 with clip 3 and the two
    would repeat as a set.
    """
    folder = MUSIC_DIR / ("horror" if sub in SUBREDDITS_HORROR else "simple")
    tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXT)
    if not tracks:
        return None
    if not key:
        return random.choice(tracks)
    seed = int(hashlib.md5(f"music:{key}".encode()).hexdigest()[:8], 16)
    offset = CHANNELS.index(channel) if channel in CHANNELS else 0
    return tracks[(seed + offset) % len(tracks)]


def _music_chain(idx: int, spoken: str, dur: float, at: float) -> list[str]:
    """Filter graph laying a ducked music bed under `spoken` -> [a].

    The narration is needed twice - once as what you hear, once as the key the
    compressor listens to - so it is split rather than referenced twice: a
    filter output feeds exactly one input, and wiring it to two is a graph
    ffmpeg refuses to build.

    adelay before the fade, not `afade` with a late start: a fade that begins
    at 0.7s plays the first 0.7s at FULL level and only then starts moving.
    Delaying the stream puts real silence there instead, which is where the
    whoosh goes.
    """
    return [
        f"[{idx}:a]volume={MUSIC_VOL},adelay={int(at * 1000)}:all=1,"
        f"afade=t=in:st={at:.2f}:d={MUSIC_FADE},"
        f"afade=t=out:st={max(0.0, dur - MUSIC_FADE):.2f}:d={MUSIC_FADE}[mus]",
        f"{spoken}asplit=2[sp][key]",
        f"[mus][key]sidechaincompress={MUSIC_DUCK}[duck]",
        # duration=first ends on the narration: the bed is looped and would
        # otherwise decide how long the video is
        "[sp][duck]amix=inputs=2:duration=first:normalize=0[a]",
    ]


def _card_chain(idx: int, cards: list[tuple[float, float, Path]],
                src: str, out: str) -> str:
    """Lay one card PNG per lit word over `src`, each on its own window -> `out`.

    `enable` rather than one animated file: ffmpeg reads an APNG's per-frame
    delays as a fixed rate and plays the whole card in a blink, and these
    windows are not a rate - they are the narrator's own word timings.

    Every input is a still, so `enable` costs a decode of one frame each and
    nothing per video frame it is hidden for. loop=1 on each input holds that
    frame for the whole window instead of ending after one video frame.

    The stack goes on AFTER the story's subtitles are burnt in, because it
    covers the middle of the frame where the word cards live - not that the two
    ever share a moment, the story starts where the card leaves.
    """
    if not cards:
        return f"[{src}]null[{out}]"
    chain, cur = [], src
    for i, (start, end, _) in enumerate(cards):
        nxt = out if i == len(cards) - 1 else f"cd{i}"
        chain.append(f"[{cur}][{idx + i}:v]overlay=0:0:format=auto:"
                     f"enable='between(t,{start},{end})'[{nxt}]")
        cur = nxt
    return ";".join(chain)


def render(mp3, words: list[dict], name: str, bg=None,
           title: str = "", title_end: float = 0, key: str = "",
           title_words: list[dict] | None = None, ad=None, part: int = 0,
           sub: str = "", gender: str = "", bed: bool = True):
    """Burn subtitles over a background clip and mux the narration.

    `key` identifies the STORY rather than the file: out/<id>_en.mp4 and
    out/<id>.mp4 are the same story on two channels, and that is exactly the
    pair that must not share footage.

    `sub` is the subreddit, and it reaches exactly one decision: which music
    folder the bed comes from. A story with no sub gets the ordinary one.

    `bed` is False for a story whose mp3 already carries music of its own - a
    harvested reading off YouTube, which arrives with the original creator's
    bed baked in. Laying ours over theirs gives two tracks fighting under one
    voice. The caller decides, because the caller is the one that knows where
    the audio came from: main.py already asks upvote.heard(key) for it.

    `part` is which video of a split story this is, 0 for an ordinary one. It
    reaches the title card and nothing else.
    """
    bg = bg or _pick_bg(key)
    ad = ad or _pick_ad()
    dur = _dur(mp3)
    ass = OUT_DIR / f"{name}.ass"
    out = OUT_DIR / f"{name}.mp4"
    build_ass(words, ass, gender)
    cards = (card.build(title_words or [], title, title_end, name, part)
             if title and title_end > 0 else [])

    # A random point past the head, so two videos off the same clip do not
    # open on the same frame. -stream_loop below covers a narration longer than
    # what is left after the seek.
    #
    # ponytail: the draw is uniform - no _motion() probe to weight it towards
    # the livelier seconds, and no _cut(). Both are kept whole with their
    # self-tests; put back `scores = _motion(bg)` and pass it here to weight
    # the draw again.
    seek, cut = _seek(_dur(bg), bg.name), None

    # 720p sources get upscaled ~2.7x to cover 1080 wide, so lanczos over the
    # default bilinear is a visible win for one flag. setsar guards against
    # clips with non-square pixels. The footage is NOT mirrored: hflip was here
    # to make one clip serve twice without reading as the same shot, and it
    # flipped whatever writing the source carried - a creator watermark came
    # out backwards, which was the one thing that made the mirroring obvious.
    # See DOCS.ru.md.
    # fps is pinned per branch rather than left to -r: concat below refuses to
    # join streams that disagree about it, and with one branch it costs nothing.
    chain = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={W}:{H},setsar=1,fps={FPS}")
    # the banner goes on last, over the burnt-in subtitles and over the title
    # card, so whatever it was paid for is never half-covered by either
    last = "base" if ad else "v"
    if cut is None:
        inputs = ["-ss", str(seek), "-stream_loop", "-1", "-i", str(bg)]
        video, audio = f"[0:v]{chain},subtitles={ass.name}[sub]", "1:a"
    else:
        # Two reads of the same file, joined where the title card leaves and
        # the story starts. Subtitles go on AFTER the join, so the word cards
        # run across it untouched and only the footage cuts.
        inputs = ["-ss", str(seek), "-t", f"{title_end:.3f}",
                  "-stream_loop", "-1", "-i", str(bg),
                  "-ss", str(cut), "-stream_loop", "-1", "-i", str(bg)]
        video = (f"[0:v]{chain}[hook];[1:v]{chain}[rest];"
                 f"[hook][rest]concat=n=2:v=1:a=0[cat];"
                 f"[cat]subtitles={ass.name}[sub]")
        audio = "2:a"

    # inputs are the backgrounds, then the mp3, then the card frames, then the
    # banner - and everything after the mp3 has to keep the index `audio` above
    # was written for, so nothing is ever inserted before it
    nbg = 1 if cut is None else 2
    # loop=1 with no -t: the still is held for as long as the graph asks, and
    # `enable` is what decides how long that is
    card_in = [a for _, _, p in cards
               for a in ("-loop", "1", "-i", str(p))]
    video += ";" + _card_chain(nbg + 1, cards, "sub", last)

    # The story stops for the banner rather than running on behind it, so the
    # finished video is longer than the narration: `cuts` are moments in the
    # narration, `ad_at` the same moments once every earlier pause has pushed
    # them later. Snapped to a word end, because a pause that lands in the
    # middle of a word reads as a broken file rather than as a break.
    #
    # One input per showing, so each starts from its own first frame.
    hold = _ad_hold(ad) if ad else 0.0
    cuts = [_snap(c, words) for c in _ad_times(dur, hold)] if ad else []
    ad_at = [c + i * hold for i, c in enumerate(cuts)]
    total = dur + len(cuts) * hold
    ad_idx = nbg + 1 + len(cards)
    ad_in = [a for _ in cuts for a in _ad_input(ad)]
    if ad:
        video += f";{_pause('[' + last + ']', 'held', cuts, hold)}"
        video += ";" + _ad_chain(ad_idx, ad_at, "held")

    # Audio inputs come after every video one, in the order they are appended
    # below. `spoken` is whatever the voice has become so far - the bare mp3, or
    # the mp3 with the whoosh already in it - and each stage hands the next one
    # its label.
    aidx = ad_idx + len(cuts)
    spoken = f"[{audio}]"

    # normalize=0, or amix halves the narration to make room for half a second
    # of whoosh and the whole video comes out quiet. duration=first ends the
    # mix on the narration, so a long sfx can never extend the track. It plays
    # from zero because that is where the card arrives - no delay to set.
    sfx_in = []
    if SFX.exists() and cards:
        sfx_in = ["-i", str(SFX)]
        video += (f";[{aidx}:a]volume={SFX_VOL}[sfx];"
                  f"{spoken}[sfx]amix=inputs=2:duration=first:normalize=0[said]")
        spoken, audio = "[said]", "[said]"
        aidx += 1

    music = _pick_music(key, sub) if bed else None
    music_in = []
    if music:
        # -stream_loop, because a 90 second bed under a 2 minute horror story
        # would otherwise simply stop halfway and leave the rest bare
        music_in = ["-stream_loop", "-1", "-i", str(music)]
        video += ";" + ";".join(_music_chain(
            aidx, spoken, dur, _dur(SFX) if SFX.exists() else 0.0))
        audio = "[a]"

    # The story stops for the banner, so the whole mix stops with it - voice,
    # whoosh and bed alike, in one place, on the finished mix rather than on
    # any one leg of it. What plays over the frozen frame is the banner and
    # nothing else, which is what a break is.
    src = audio if audio.startswith("[") else f"[{audio}]"
    if cuts:
        video += f";{_pause(src, 'gapped', cuts, hold, audio=True)}"
        audio = src = "[gapped]"

    # The banner's own sound, over the top of everything and NOT ducked. The
    # programme pays on the voice-over being audible, so this is the one leg of
    # the mix that is not a taste decision: silent banner, unpaid video.
    # adelay puts each showing where its picture already is - the same seconds
    # _ad_chain() padded the video leg to - and the audio rides along on the
    # inputs the banner is already open on, so nothing new is read.
    if ad and _has_audio(ad):
        legs = ";".join(
            # atempo before the delay, or the banner's sound would run at its
            # own speed under a picture running at AD_SPEED
            f"[{ad_idx + i}:a]atempo={AD_SPEED},adelay={round(at * 1000)}:all=1,"
            f"volume={AD_VOL}[adsnd{i}]" for i, at in enumerate(ad_at))
        taps = "".join(f"[adsnd{i}]" for i in range(len(ad_at)))
        # duration=first, as everywhere else here: the story with its pauses in
        # it decides how long the video is, and a banner must not stretch it
        video += (f";{legs};{src}{taps}amix=inputs={1 + len(ad_at)}"
                  ":duration=first:normalize=0[withad]")
        audio, src = "[withad]", "[withad]"

    # Last, over whatever the mix turned out to be - see LUFS_I. `audio` is a
    # bare stream spec when nothing above filtered it, and a filter graph wants
    # the brackets either way.
    video += f";{src}loudnorm=I={LUFS_I}:TP={LUFS_TP}:LRA={LUFS_LRA}[loud]"
    audio = "[loud]"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-i", str(mp3),
        *card_in,
        *ad_in,
        *sfx_in,
        *music_in,
        "-filter_complex", video,
        "-map", "[v]", "-map", audio, "-t", f"{total:.3f}", "-r", str(FPS),
        # The ceiling is the point, not the CRF - see CRF/MAXRATE above for why
        # this one is where it is, and how to put it back if the upload cannot
        # carry it.
        # preset slow rather than veryfast because at a CAPPED bitrate the
        # preset is what buys quality: at the old 2500k ceiling medium held
        # SSIM 0.965 against veryfast's 0.955 for about 30 seconds per render,
        # and slow is the next step up the same curve.
        # profile/level and faststart are TikTok's own upload spec: high@4.2,
        # yuv420p, moov atom in front. A file that already matches it gives
        # their encoder nothing to "fix" on the way in, and faststart is what
        # lets the web uploader read the header before the last byte lands.
        "-c:v", "libx264", "-preset", "slow", "-crf", CRF,
        "-profile:v", "high", "-level", "4.2",
        "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out.name),
    ]
    # run inside OUT_DIR: the subtitles filter chokes on Windows drive colons
    subprocess.run(cmd, cwd=OUT_DIR, check=True)
    log.info("%s: %.1f sec from %s at %.1fs%s%s", out.name, total, bg.name, seek,
             f", cutting to %.1fs at %.1fs" % (cut, title_end) if cut else "",
             f", banner {ad.name} at {', '.join(f'{a:.1f}s' for a in ad_at)}"
             f", holding {hold:.1f}s each" if ad else "")
    if cards:
        log.info("%s: title card over %.1fs in %d frames%s", out.name, title_end,
                 len(cards), f", {SFX.name} under it" if sfx_in else "")
    if music:
        log.info("%s: %s/%s ducked under the voice at %.3f", out.name,
                 music.parent.name, music.name, MUSIC_VOL)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    assert _ts(0) == "0:00:00.00"
    assert _ts(65.43) == "0:01:05.43"
    assert _ts(3661.5) == "1:01:01.50"

    # The head is off limits and the seek leaves SKIP_TAIL seconds of footage
    # after it, both across the range and not just on average - a bound that
    # only holds for the mean is the bound that ships the menu screen once a week.
    picks = [_seek(600) for _ in range(2000)]
    assert all(SKIP_HEAD <= s <= 570 for s in picks), (min(picks), max(picks))
    assert len(set(picks)) > 100, "seek is not actually random"
    assert min(picks) < 60 and max(picks) > 500, (min(picks), max(picks))
    # clip with no room for both: head still wins, looping covers the rest
    assert _seek(50, "(expected warning)") == SKIP_HEAD
    assert _seek(20, "(expected warning)") == 0   # shorter than the head skip
    assert _seek(60.0) == SKIP_HEAD   # exactly enough room, no randomness left

    # One loud stretch in an otherwise quiet clip: every live second must come
    # from it, and every seek must come from the live seconds. A window starting
    # up to HOOK_WINDOW-1 early still overlaps the loud part, so the band opens
    # that much before it.
    quiet = [0.01] * 600
    quiet[100:200] = [0.5] * 100
    live = _live(quiet, SKIP_HEAD, 540)
    assert live, "a clip with an obvious peak produced no live seconds"
    assert all(100 - HOOK_WINDOW < s < 200 for s in live), (min(live), max(live))
    seeks = {_seek(600, scores=quiet) for _ in range(200)}
    assert seeks <= {float(s) for s in live}, sorted(seeks - {float(s) for s in live})
    assert len(seeks) > 20, "the draw collapsed onto a handful of seconds"
    # A cut between two still shots is not motion. This shipped once: the mean
    # over such a window beat the median and put three frozen seconds under a
    # hook, so the spike must not be able to carry its neighbours.
    frozen = [0.001] * 600
    frozen[300:400] = [0.03] * 100          # the one genuinely moving stretch
    for s in (200, 500):                    # lone cuts in the dead parts
        frozen[s] = 0.9
    spiked = _live(frozen, SKIP_HEAD, 540)
    assert spiked, "the moving stretch was rejected too"
    assert all(300 - HOOK_WINDOW < s < 400 for s in spiked), sorted(spiked)[:5]
    # A clip that is lively everywhere has no better half to prefer, and must
    # hand the choice back rather than narrow it to nothing.
    assert _live([0.02] * 600, SKIP_HEAD, 540) == []
    assert _live(quiet, SKIP_HEAD, 40) == []      # too short a window to have a middle
    # ...and the fallback is the old behaviour, untouched
    flat = [_seek(600, scores=[0.02] * 600) for _ in range(200)]
    assert len(set(flat)) > 100 and all(SKIP_HEAD <= s <= 570 for s in flat)

    # The cut is far from the opening or it is not made at all, and the second
    # stretch still has to hold everything after the title card.
    cuts = [_cut(1100, 75, 3.0, "bg", None, 100.0) for _ in range(200)]
    assert all(c is None or abs(c - 100.0) >= CUT_MIN_GAP for c in cuts)
    assert all(c is None or SKIP_HEAD <= c <= 1100 - SKIP_TAIL for c in cuts)
    assert sum(c is not None for c in cuts) > 190, "the cut is being dropped too often"
    # Nothing to cut on, and nothing to cut into
    assert _cut(1100, 75, 0, "bg", None, 100.0) is None      # no title card
    assert _cut(1100, 75, 74.0, "bg", None, 100.0) is None   # nothing after the card
    # A clip with no window CUT_MIN_GAP clear of the opening gives up quietly
    assert _cut(150, 75, 3.0, "(expected info)", None, 60.0) is None

    # A channel outside AD_CHANNELS gets no banner even with the directory
    # full, and one inside it gets whatever is there. Both channels read the
    # same assets/ad, so this check is all that keeps them apart.
    for c in CHANNELS:
        got = _pick_ad(c)
        assert (got is not None) == (c in AD_CHANNELS and any(AD_DIR.rglob("*.*"))), \
            f"{c}: {got}"

    # One banner alone sits in the middle of the video, whatever its length...
    assert _ad_times(75) == [37.5], _ad_times(75)
    assert _ad_times(119) == [59.5], "1:59 is one banner, and in the MIDDLE"
    assert _ad_times(20) == [10.0], "a short video still carries one"
    # ...and from two minutes up they go on the minute grid instead, which is
    # the programme's own worked example: 0:30, 1:30, and so on.
    assert _ad_times(120) == [30.0, 90.0], _ad_times(120)
    assert _ad_times(179) == [30.0, 90.0], _ad_times(179)
    assert all(0 < a < d for d in (45, 75, 120, 200) for a in _ad_times(d)), \
        "a banner landed outside the video"
    # The count is taken off the FINISHED length, pauses included: 1:58 of
    # story with a six second break in it is 2:04 on screen, which the
    # programme reads as two minutes and wants two banners.
    assert _ad_times(118, 6) == [27.0, 81.0], _ad_times(118, 6)
    assert _ad_times(52, 6) == [26.0], "a minute is a minute, not 58 seconds"
    # ...and the cut points are in the NARRATION, so what has to land on the
    # mark is the middle of the banner on the FINISHED timeline. This is the
    # check that would catch the pauses not being taken back out of the cuts -
    # the drift that left 1:00-2:00 of a 2:50 video with no banner at all.
    for _d, _h in ((75, 6), (118, 6), (240, 6), (95, 5), (169.8, 4.63)):
        _c = _ad_times(_d, _h)
        _tot = _d + len(_c) * _h
        assert _c == sorted(_c) and 0 < _c[0] and _c[-1] < _d, \
            f"cuts outside the narration: {_c} of {_d}s"
        for _i, _one in enumerate(_c):
            _mid = _one + _i * _h + _h / 2          # middle of the banner
            _want = _tot / 2 if len(_c) == 1 else AD_EVERY * _i + AD_EVERY / 2
            assert abs(_mid - _want) < 1e-6, \
                f"banner {_i} of {_d}s+{_h}s is at {_mid}, wanted {_want}"

    # The pause lands between words, never inside one
    _w = [{"word": "a", "start": 0.0, "end": 1.0},
          {"word": "b", "start": 1.2, "end": 2.4},
          {"word": "c", "start": 2.6, "end": 4.0}]
    assert _snap(1.9, _w) == 2.4 and _snap(1.3, _w) == 1.0, _snap(1.9, _w)
    assert _snap(9.0, _w) == 4.0, "the last word end is as far as it can go"
    assert _snap(1.9, []) == 1.9, "no words, nothing to snap to"

    # Nothing to pause for is a pass-through, on either leg - a dangling label
    # here is a failed render, not a wrong picture
    assert _pause("[base]", "held", [], 6) == "[base]null[held]"
    assert _pause("[a]", "gapped", [], 6, audio=True) == "[a]anull[gapped]"
    _p = _pause("[base]", "held", [30.0, 90.0], 6)
    assert "gte(T,30.000)+gte(T,90.000)" in _p and f"fps={FPS}" in _p, _p
    assert _pause("[a]", "gapped", [30.0], 6, audio=True).count("aresample") == 1

    # The banner: nothing on screen before it is due, the green keyed out
    # around it, and a moving source starting from its own first frame rather
    # than mid-animation. Run on the real filter graph over a flat frame, so it
    # costs one short encode instead of a whole render.
    _at = 2.5
    ad = OUT_DIR / "_selftest_ad.mp4"
    # a green screen that MOVES: a box in the banner's band, black for its
    # first second and white after. Overlaid at `_at` it must show the black
    # opening, not the white it would be showing if the stream had been running
    # underneath all along. Narrower than the screen, so the strip beside it
    # stays green and has to key out. Shaped like the real artwork rather than
    # like the frame, because that ratio is what decides the placement below.
    box = (f"drawbox=x=iw*0.1:y=ih*{AD_BAND_Y}:w=iw*0.8:h=ih*{AD_BAND_H}"
           ":t=fill:color=")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color={AD_KEY}:s=720x378:r={FPS}:d=1", "-f", "lavfi",
                    "-i", f"color={AD_KEY}:s=720x378:r={FPS}:d=8",
                    "-filter_complex", f"[0:v]{box}black[a];[1:v]{box}white[b];"
                                       "[a][b]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(ad)], check=True)

    # The banner is centred, so the only thing left to hold is that it fits.
    # Measured off the banner that will actually be used, falling back to the
    # synthetic one when the directory is empty - the two are the same shape on
    # purpose. Nothing about the word cards any more: the story is frozen while
    # the banner is up, so there is nothing underneath to stay clear of.
    band = _ad_h(_pick_ad("ru") or ad)
    assert band <= H, f"the banner is {band} tall and the frame is {H}"
    top = (H - band) // 2
    # ...and it is as big as this artwork can be made. The programme wants a
    # quarter of the screen; measured on the file, the drawing is full-bleed -
    # it touches both edges of its green screen and stands 840 of its 1000
    # rows, 920 at the pop and the logo. At the frame's full width that is
    # 24.9% of the frame steady and 27.2% at the peak, so the frame's own width
    # is the ceiling and anything under it drops the banner under the bar
    # outright. This is what stops AD_SCALE being turned down for taste.
    assert _ad_w() == W, "the banner is not laid out at the full width of the frame"

    over = OUT_DIR / "_selftest_banner.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=gray:s={W}x{H}:r={FPS}:d=9",
                    *_ad_input(ad),
                    "-filter_complex", f"[0:v]null[base];{_ad_chain(1, [_at])}",
                    "-map", "[v]", "-t", "9", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(over)],
                   check=True)

    def _lum(at: float, x: int = W // 2) -> int:
        """Mean brightness of one strip of the banner's band, 0-255."""
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(over), "-vf",
             f"crop=40:100:{x}:{top + 100},scale=1:1,format=gray",
             "-frames:v", "1", "-f", "rawvideo", "-"],
            capture_output=True, check=True).stdout[0]

    # grey is the base showing through, and it has to show through twice: once
    # before the banner is due, and once beside it for the whole run, where the
    # source is green and nothing but the key can be taking it away
    assert 100 < _lum(_at / 2) < 160, "the banner is on screen before it should be"
    assert 100 < _lum(_at + 1.5, 20) < 160, "the green screen was not keyed out"
    # its own first second, which is black - if tpad had been swapped for
    # overlay's `enable` this would already be white and the banner would
    # arrive mid-animation
    assert _lum(_at + 0.2) < 40, "the banner did not start from frame one"
    assert _lum(_at + 1.5) > 200, "the banner never arrived"

    # Two showings, two inputs, chained and ending on the label the rest of the
    # graph expects - the wiring a second banner rides on, checked without an
    # encode. Each gets its own tpad, or they would arrive together.
    _two = _ad_chain(5, [30.0, 90.0])
    assert _two.count("overlay=") == 2 and _two.endswith("[v]"), _two
    assert "[5:v]" in _two and "[6:v]" in _two, _two
    assert "start_duration=30.000" in _two and "start_duration=90.000" in _two, _two

    # The programme allows 1.4x at the most, and the pause is exactly as long
    # as the banner turns out to be once sped up - a hold measured on the
    # unsped clip would leave the story frozen after the banner had finished.
    assert AD_SPEED <= 1.4, f"the banner runs at {AD_SPEED}x, over the 1.4 limit"
    assert f"setpts=PTS/{AD_SPEED}" in _two, _two
    assert abs(_ad_hold(ad) - _dur(ad) / AD_SPEED) < 1e-6, _ad_hold(ad)

    # The card is images now, so the ass file must carry nothing of it - a
    # leftover Card style would draw its own box UNDER the png and show as a
    # pale border around it. Ahead of the mp3 gate on purpose: it needs no
    # audio, so it must run even when out/ is empty.
    assert "Card" not in ASS_HEADER, ASS_HEADER
    _ass = OUT_DIR / "_check_part.ass"
    build_ass([], _ass)
    assert "Dialogue" not in _ass.read_text("utf-8")
    _ass.unlink()

    # One overlay per card frame, chained, ending on the label the rest of the
    # graph expects. The window is what carries the timing - an overlay wired
    # up without `enable` would hold the first frame for the whole video.
    _cards = [(0.0, 0.7, Path("a.png")), (0.7, 1.4, Path("b.png"))]
    _ch = _card_chain(3, _cards, "sub", "v")
    assert _ch.count("overlay=") == 2, _ch
    assert _ch.startswith("[sub][3:v]"), _ch
    assert "[4:v]" in _ch and _ch.endswith("[v]"), _ch
    assert _ch.count("enable=") == 2, _ch
    assert "between(t,0.0,0.7)" in _ch and "between(t,0.7,1.4)" in _ch, _ch
    # ...and with no card at all the graph still has to reach [v], or ffmpeg
    # fails on a filter output nothing maps to
    assert _card_chain(3, [], "sub", "v") == "[sub]null[v]"

    # The bed is split off the narration and fed back as the compressor's key:
    # one filter output cannot feed two inputs, and forgetting the split is a
    # graph ffmpeg refuses rather than a quiet mistake.
    _mc = ";".join(_music_chain(5, "[said]", 60.0, 0.7))
    assert "[said]asplit=2[sp][key]" in _mc, _mc
    assert f"sidechaincompress={MUSIC_DUCK}" in _mc, _mc
    assert _mc.endswith("[a]"), _mc
    # silence first, THEN the fade - a fade starting at 0.7 would play the
    # whoosh's own 0.7 seconds at full level before it began moving
    assert _mc.index("adelay=700") < _mc.index("afade=t=in:st=0.70"), _mc
    # and the bed is gone before the video is
    assert "afade=t=out:st=58.00" in _mc, _mc

    # Mood follows the subreddit, exactly as the horror voice does.
    if (MUSIC_DIR / "simple").is_dir() and (MUSIC_DIR / "horror").is_dir():
        _horror_sub = (SUBREDDITS_HORROR or ["nosleep"])[0]
        assert _pick_music("k", _horror_sub).parent.name == "horror"
        assert _pick_music("k", "AITAH").parent.name == "simple"
        # same story, same track, on any machine and any run - no state
        assert _pick_music("k", "AITAH") == _pick_music("k", "AITAH")
        # ...and the music seed is not the footage seed, or a story would keep
        # arriving as the same clip-and-track pair
        _pairs = {(_pick_bg(k).name, _pick_music(k, "AITAH").name)
                  for k in ("a", "b", "c", "d", "e", "f")}
        assert len({b for b, _ in _pairs}) > 1 or len({m for _, m in _pairs}) > 1
        # Only what ships is counted. The wav masters live in these folders
        # untracked, and a desk that counted them would index a different list
        # than CI - the same story would get a different track per machine.
        assert all(p.suffix != ".wav" for p in MUSIC_DIR.rglob("*")
                   if p.suffix in MUSIC_EXT), "wav is back in MUSIC_EXT"

    mp3 = OUT_DIR / "_selftest.mp3"
    assert mp3.exists(), "run `python voice.py` first"
    words = json.loads((OUT_DIR / "_selftest.json").read_text("utf-8"))

    def _w(t, s, e):
        return {"word": t, "start": s, "end": e}

    wide = _group([_w("через", 0, 1), _w("двадцать", 1, 2), _w("минут", 2, 3)])
    assert all(len(c["word"]) <= 14 for c in wide), wide
    assert all(len(c["word"].split()) <= 2 for c in wide), wide

    import script
    body = ("Он вошёл. [husband, shouting] «Где ужин» Я не встала. "
            "[me, cold] «На столе» Он ушёл.")
    who = script.speakers(body)
    assert len(who) == len(script.plain(body).split()), (who, script.plain(body))
    tagged = [{**_w(t, i, i + 1), "speaker": s}
              for i, (t, s) in enumerate(zip(script.plain(body).split(), who))]
    st = _styles(tagged)
    assert st == {"husband": "Speech0", "me": "Speech1"}, st
    # a card must never mix two voices
    assert all(len({c["speaker"]}) == 1 for c in _group(tagged))

    cards = _group(words)
    assert sum(len(c["word"].split()) for c in cards) == len(words), "lost a word"
    assert all(len(c["word"]) >= 3 for c in cards), "single-letter card survived"

    build_ass(words, OUT_DIR / "_check.ass")
    body = (OUT_DIR / "_check.ass").read_text("utf-8").splitlines()
    events = [l for l in body if l.startswith("Dialogue:")]
    assert len(events) == len(cards), f"{len(events)} lines for {len(cards)} cards"

    # The narrator's tint is a one-shot substitution, so the colour it replaces
    # must occur exactly once - a second white anywhere in the header and the
    # tint silently lands on the wrong style instead of Main.
    assert ASS_HEADER.count(NARRATOR_WHITE) == 1, "Main is no longer the only white"
    assert NARRATOR_WHITE not in NARRATOR_COLOURS.values(), "a gender tint is white"
    for _g, _c in (("female", NARRATOR_COLOURS["female"]),
                   ("male", NARRATOR_COLOURS["male"]),
                   ("", NARRATOR_WHITE), ("nonsense", NARRATOR_WHITE)):
        _main = [l for l in _header(_g).splitlines() if l.startswith("Style: Main,")]
        assert len(_main) == 1 and _main[0].split(",")[3] == _c, (_g, _main)
        # the speaker palette must not move when the narrator is tinted
        assert all(c in _header(_g) for c in SPEAKER_COLOURS), _g

    try:
        clips = sorted(p for p in BG_DIR.rglob("*")
                       if p.suffix.lower() in (".mp4", ".mov", ".webm"))
        if len(clips) >= len(CHANNELS):
            # The same story must not land on the same clip on two channels, or
            # the two videos are one video with two soundtracks. Checked over
            # several keys: one key agreeing proves nothing about the next.
            for k in ("abc123", "def456", "ghi789", "_selftest"):
                picked = {c: _pick_bg(k, c).name for c in CHANNELS}
                assert len(set(picked.values())) == len(CHANNELS), (k, picked)
            # and the answer must not change between runs, or two runs of the
            # same channel hours apart would disagree about what they picked
            assert _pick_bg("abc123") == _pick_bg("abc123")
        else:
            print(f"only {len(clips)} clip(s) in {BG_DIR} - channels will share footage")
        bg = _pick_bg()
    except RuntimeError:
        bg = None

    if bg:
        # Measures the clip for real the first time and caches it; the second
        # call must come back from that cache rather than decode again, which
        # is the whole reason the cache exists.
        first = _motion(bg)
        assert first is None or (len(first) > 1 and all(s >= 0 for s in first))
        assert _motion(bg) == first, "the motion cache did not round-trip"
    if bg is None:
        bg = OUT_DIR / "_testbg.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc2=size={W}x{H}:rate=30:duration=60",
                        "-c:v", "libx264", "-preset", "ultrafast", str(bg)], check=True)
        print(f"no clips in {BG_DIR}, using generated test pattern")

    # With a title, so the overlay stack is exercised for real: the graph is
    # built by hand here and a wrong input index or a dangling label is a
    # failed ffmpeg run, not a wrong picture, so this is what catches it.
    _title = "Соседка прислала счёт на 80000 за потоп"
    _tw = [{"word": w, "start": round(i * 0.45, 2), "end": round(i * 0.45 + 0.4, 2)}
           for i, w in enumerate(_title.split())]
    _tend = round(_tw[-1]["end"] + 0.3, 2)
    out = render(mp3, words, "_selftest", bg=bg, title=_title,
                 title_end=_tend, title_words=_tw)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert probe == f"{W},{H}", f"wrong resolution: {probe}"
    # The narration, plus the pause each banner holds the story for. No banner
    # and the two are simply the same length, which is what this used to say.
    _ad = _pick_ad("ru")
    _hold = _ad_hold(_ad) if _ad else 0.0
    _want = _dur(mp3) + len(_ad_times(_dur(mp3), _hold)) * _hold if _ad else _dur(mp3)
    assert abs(_dur(out) - _want) < 1.0, \
        f"video is {_dur(out):.1f}s, wanted {_want:.1f}s"

    def _centre(at: float, scale: float = 1.0) -> int:
        """Mean brightness of the card's left gutter at `scale`, 0-255.

        The gutter rather than the middle: the middle is type, which is white
        whatever is behind it, while this strip is flat card - dark while the
        card is up, and whatever the gameplay is doing once it leaves. It sits
        inside the padding at the card's own vertical centre, which is the one
        spot that stays blank however tall the title set.

        `scale` follows the pop: the card grows out of the centre, so its left
        edge is somewhere else entirely on those first two frames.
        """
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(out), "-vf",
             f"crop=20:200:{round((W - card.CARD_W * scale) / 2 + 8 * scale)}:"
             f"{H // 2 - 100},scale=1:1,format=gray",
             "-frames:v", "1", "-f", "rawvideo", "-"],
            capture_output=True, check=True).stdout[0]

    # The card is the first frame of the video, arriving mid-pop - scaled down
    # and already on screen, never absent.
    assert _centre(0.02, card.POP_SCALES[0]) < 45, "no card on the opening frame"
    # ...and where the settled card's edge will be, there is still footage,
    # which is the assertion that catches a pop that never scaled anything
    assert _centre(0.02) != _centre(_tend / 2), "the pop frame is full size"
    assert _centre(_tend / 2) < 45, "the card went away mid-title"
    # ...and it leaves the moment the narration reaches the story, or the
    # word cards would be reading out from behind it
    assert _centre(_tend + 1.0) != _centre(0.05), "the card outlived the title"
    print(f"ok: {out.name}, {_dur(out):.1f}s, {probe}")
