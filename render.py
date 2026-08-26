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
PROBE_FPS = 4              # frames sampled per second when mapping a clip's motion
HOOK_WINDOW = 3            # seconds a seek is judged on: the hook, and nothing after it
MOTION_DIR = BG_DIR / ".motion"   # one json per clip, next to the footage it describes
CUT_MIN_GAP = 90.0         # how far the post-title footage must be from the opening
CUT_TRIES = 40             # draws allowed to find that gap before the cut is dropped

# --- overlay banner ---
# An optional image or clip laid over every render. None of these numbers are
# taste: each one comes from a placement spec, and the reasoning behind every
# one of them - why this instant, this share of the frame, this band of it, and
# which of its rules are knowingly not met - is in DOCS.ru.md, not here.
AD_AT = 3.0                # when it appears, seconds
AD_FADE = 0.5              # fade in, and out again if AD_SEC ends it
AD_SEC = 0.0               # how long it stays; 0 means to the end of the video
AD_CHANNELS = ("ru",)      # channels that carry one; the rest never do
AD_MIN_AREA = 1 / 6        # of the frame, below which _ad_size() warns
AD_MARGIN = 120            # side gap - and the only width control there is, so
                           # this is the number to turn when the banner has to
                           # come down a size. At 120 a 2:1 banner is 840x420,
                           # which still clears AD_MIN_AREA, but only just.
AD_Y = 240                 # below the platform's top interface strip, and word
                           # cards sit dead centre, so this band is free
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


def _seek(bg_dur: float, dur: float, name: str = "",
          scores: list[float] | None = None) -> float:
    """Where inside the background clip to start, at random.

    Random so consecutive videos don't share footage, and never inside the
    first SKIP_HEAD seconds: a background clip opens on an intro, a title card
    or a menu, the one stretch of it that looks like a clip off YouTube.

    The window is [SKIP_HEAD, bg_dur - dur], so the narration also fits before
    the end and -stream_loop never fires - that is what makes the head skip a
    guarantee for the whole video rather than only for its first frame.

    Given `scores` from _motion(), the draw narrows to the livelier half of
    that window. Uniform, roughly half the videos open on the slow stretch of
    a compilation, and those are three seconds the hook does not get back.
    Still a DRAW over the good seconds, not the single best one: three clips
    serve every video this channel makes, and a clip that always opens at its
    one peak is a repeat the viewer notices.
    """
    latest = bg_dur - dur
    if latest >= SKIP_HEAD:
        live = _live(scores, SKIP_HEAD, latest) if scores else []
        if live:
            return float(random.choice(live))
        return round(random.uniform(SKIP_HEAD, latest), 2)
    if bg_dur > SKIP_HEAD:
        # too short to fit the narration after the head; start right past the
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
    if title_end <= 0 or dur - title_end < HOOK_WINDOW:
        return None
    for _ in range(CUT_TRIES):
        pick = _seek(bg_dur, dur - title_end, name, scores)
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


def _ad_input(ad: Path) -> list[str]:
    """Input flags that make the banner last as long as the video does.

    Each source type loops by its own flag: a still frame has to be told to
    repeat at all, a gif carries a loop count the demuxer honours only when
    asked, and a video has to be re-read from the top. Without this the banner
    plays once and the rest of the video runs with an empty slot.
    """
    ext = ad.suffix.lower()
    if ext in IMAGE_EXT:
        return ["-loop", "1", "-i", str(ad)]
    if ext == ".gif":
        return ["-ignore_loop", "0", "-i", str(ad)]
    return ["-stream_loop", "-1", "-i", str(ad)]


def _ad_size(ad: Path) -> int:
    """The full frame width bar a margin - and a warning if that misses 1/6.

    Solving for the AD_MIN_AREA minimum instead would be exactly backwards: it
    is a floor to stay above, not a size to hit, and the widest placement that
    still clears the interface is the one the spec asks for. See DOCS.ru.md.

    Only the aspect ratio is measured, and only to tell whether that width can
    reach the floor at all - a banner too wide to qualify at any size is a
    file that has to be swapped, so this warns and renders rather than failing
    a whole run over it.
    """
    wide = W - 2 * AD_MARGIN
    try:
        wh = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(ad)],
            capture_output=True, text=True, check=True).stdout.strip().split(",")
        ratio = int(wh[0]) / int(wh[1])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError,
            ZeroDivisionError) as e:
        log.warning("could not measure %s (%s) - placing it at full width anyway",
                    ad.name, e)
        return wide
    got = wide * wide / ratio
    if got < W * H * AD_MIN_AREA:
        log.warning("%s is %.1f:1 - %dx%d covers 1/%.1f of the frame, under the "
                    "1/%.0f it is paid for. Use a less wide banner.",
                    ad.name, ratio, wide, round(wide / ratio), W * H / got,
                    1 / AD_MIN_AREA)
    return wide


def _ad_chain(idx: int, dur: float, wide: int) -> str:
    """Filter graph putting input `idx` over [base] as a fading banner -> [v].

    tpad rather than overlay's `enable`: enable only hides the banner while its
    stream runs on underneath, so a gif or a video would arrive three seconds
    into itself, mid-motion. Padding the FRONT with transparent frames delays
    the stream itself, so the animation starts on its first frame the moment the
    banner appears - which is the whole point of the delay for anything that
    moves, and costs a still image nothing.

    The fade is on alpha, so it dissolves against the footage instead of
    fading through black, and it sits after the pad so both timestamps are read
    off the same padded timeline as AD_AT.
    """
    gone = AD_AT + AD_SEC
    out = (f",fade=t=out:st={gone - AD_FADE:.2f}:d={AD_FADE}:alpha=1"
           if AD_SEC and gone < dur else "")
    return (f"[{idx}:v]fps={FPS},scale={wide}:-2,format=rgba,"
            f"tpad=start_duration={AD_AT}:start_mode=add:color=black@0,"
            f"fade=t=in:st={AD_AT}:d={AD_FADE}:alpha=1{out}[ad];"
            # eof_action=pass, not shortest: a banner that runs out must leave
            # the video alone, not cut it off wherever it happened to end
            f"[base][ad]overlay=(W-w)/2:{AD_Y}:format=auto:eof_action=pass[v]")


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
           sub: str = "", gender: str = ""):
    """Burn subtitles over a background clip and mux the narration.

    `key` identifies the STORY rather than the file: out/<id>_en.mp4 and
    out/<id>.mp4 are the same story on two channels, and that is exactly the
    pair that must not share footage.

    `sub` is the subreddit, and it reaches exactly one decision: which music
    folder the bed comes from. A story with no sub gets the ordinary one.

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

    scores = _motion(bg)
    bg_dur = _dur(bg)
    seek = _seek(bg_dur, dur, bg.name, scores)
    cut = _cut(bg_dur, dur, title_end, bg.name, scores, seek)

    # 720p sources get upscaled ~2.7x to cover 1080 wide, so lanczos over the
    # default bilinear is a visible win for one flag. setsar guards against
    # clips with non-square pixels. Every background is mirrored; hflip sits
    # before subtitles so the footage flips and the burnt-in subtitles do not.
    # Whatever writing the SOURCE carries does flip - a creator watermark comes
    # out backwards, which is the one thing that makes the mirroring obvious.
    # See DOCS.ru.md.
    # fps is pinned per branch rather than left to -r: concat below refuses to
    # join streams that disagree about it, and with one branch it costs nothing.
    chain = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={W}:{H},setsar=1,hflip,fps={FPS}")
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

    ad_in = _ad_input(ad) if ad else []
    if ad:
        video += ";" + _ad_chain(nbg + 1 + len(cards), dur, _ad_size(ad))

    # Audio inputs come after every video one, in the order they are appended
    # below. `spoken` is whatever the voice has become so far - the bare mp3, or
    # the mp3 with the whoosh already in it - and each stage hands the next one
    # its label.
    aidx = nbg + 1 + len(cards) + bool(ad)
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

    music = _pick_music(key, sub)
    music_in = []
    if music:
        # -stream_loop, because a 90 second bed under a 2 minute horror story
        # would otherwise simply stop halfway and leave the rest bare
        music_in = ["-stream_loop", "-1", "-i", str(music)]
        video += ";" + ";".join(_music_chain(
            aidx, spoken, dur, _dur(SFX) if SFX.exists() else 0.0))
        audio = "[a]"

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-i", str(mp3),
        *card_in,
        *ad_in,
        *sfx_in,
        *music_in,
        "-filter_complex", video,
        "-map", "[v]", "-map", audio, "-t", f"{dur:.3f}", "-r", str(FPS),
        # The ceiling is the point, not the CRF. crf 23 alone let high-motion
        # backgrounds run to 7.1 Mbit/s - a 1.4 min video came out 72 MB - and
        # the upload is the constraint here: measured 2026-08-03 over a slow
        # link at 0.75 Mbit/s, 72 MB is thirteen minutes on one connection and
        # it did not survive them; the only file that got through was the
        # smallest.
        # maxrate/bufsize cap the peak without touching resolution - 1080x1920
        # is kept whole - and bring the same clip to 26.9 MB, five minutes.
        # TikTok re-encodes everything anyway, so the bits above this ceiling
        # were never going to reach a viewer.
        # preset medium rather than veryfast because at a CAPPED bitrate the
        # preset is what buys quality: same 26.9 MB, SSIM 0.965 against 0.955,
        # for about 30 seconds more per render.
        "-c:v", "libx264", "-preset", "medium", "-crf", "26",
        "-maxrate", "2500k", "-bufsize", "5000k",
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        str(out.name),
    ]
    # run inside OUT_DIR: the subtitles filter chokes on Windows drive colons
    subprocess.run(cmd, cwd=OUT_DIR, check=True)
    log.info("%s: %.1f sec from %s at %.1fs%s%s", out.name, dur, bg.name, seek,
             f", cutting to %.1fs at %.1fs" % (cut, title_end) if cut else "",
             f", banner {ad.name} from {AD_AT:.0f}s" if ad else "")
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

    # The head is off limits and the tail must still hold the whole narration,
    # both across the range and not just on average - a bound that only holds
    # for the mean is the bound that ships the menu screen once a week.
    picks = [_seek(600, 60) for _ in range(2000)]
    assert all(SKIP_HEAD <= s <= 540 for s in picks), (min(picks), max(picks))
    assert len(set(picks)) > 100, "seek is not actually random"
    assert min(picks) < 60 and max(picks) > 500, (min(picks), max(picks))
    # clip with no room for both: head still wins, looping covers the rest
    assert _seek(100, 90, "(expected warning)") == SKIP_HEAD
    assert _seek(20, 60, "(expected warning)") == 0   # shorter than the head skip
    assert _seek(90.0, 60.0) == SKIP_HEAD   # exactly enough room, no randomness left

    # One loud stretch in an otherwise quiet clip: every live second must come
    # from it, and every seek must come from the live seconds. A window starting
    # up to HOOK_WINDOW-1 early still overlaps the loud part, so the band opens
    # that much before it.
    quiet = [0.01] * 600
    quiet[100:200] = [0.5] * 100
    live = _live(quiet, SKIP_HEAD, 540)
    assert live, "a clip with an obvious peak produced no live seconds"
    assert all(100 - HOOK_WINDOW < s < 200 for s in live), (min(live), max(live))
    seeks = {_seek(600, 60, scores=quiet) for _ in range(200)}
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
    flat = [_seek(600, 60, scores=[0.02] * 600) for _ in range(200)]
    assert len(set(flat)) > 100 and all(SKIP_HEAD <= s <= 540 for s in flat)

    # The cut is far from the opening or it is not made at all, and the second
    # stretch still has to hold everything after the title card.
    cuts = [_cut(1100, 75, 3.0, "bg", None, 100.0) for _ in range(200)]
    assert all(c is None or abs(c - 100.0) >= CUT_MIN_GAP for c in cuts)
    assert all(c is None or SKIP_HEAD <= c <= 1100 - 72 for c in cuts)
    assert sum(c is not None for c in cuts) > 190, "the cut is being dropped too often"
    # Nothing to cut on, and nothing to cut into
    assert _cut(1100, 75, 0, "bg", None, 100.0) is None      # no title card
    assert _cut(1100, 75, 74.0, "bg", None, 100.0) is None   # nothing after the card
    # A clip with no window CUT_MIN_GAP clear of the opening gives up quietly
    assert _cut(200, 75, 3.0, "(expected info)", None, 60.0) is None

    # A channel outside AD_CHANNELS gets no banner even with the directory
    # full, and one inside it gets whatever is there. Both channels read the
    # same assets/ad, so this check is all that keeps them apart.
    for c in CHANNELS:
        got = _pick_ad(c)
        assert (got is not None) == (c in AD_CHANNELS and any(AD_DIR.rglob("*.*"))), \
            f"{c}: {got}"

    # The banner: nothing on screen before AD_AT, fully there once the fade is
    # over, and a moving source starting from its own first frame rather than
    # three seconds into itself. Run on the real filter graph over a black
    # frame, so it costs one short encode instead of a whole render.
    ad = OUT_DIR / "_selftest_ad.mp4"
    # a source that MOVES: black for its first second, then white. Overlaid at
    # AD_AT it must show its black opening, not the white it would be showing
    # if the stream had been running underneath all along.
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=black:s=400x200:r={FPS}:d=1", "-f", "lavfi",
                    "-i", f"color=white:s=400x200:r={FPS}:d=4",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(ad)], check=True)
    # a 2:1 banner at full width clears the sixth the rules ask for, and stays
    # inside the frame with its margins
    wide = _ad_size(ad)
    assert wide * (wide / 2) >= W * H * AD_MIN_AREA, wide
    assert wide == W - 2 * AD_MARGIN, wide
    assert AD_Y + wide / 2 < H / 2, "the banner reaches down into the word cards"
    over = OUT_DIR / "_selftest_banner.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={FPS}:d=9",
                    *_ad_input(ad),
                    "-filter_complex", f"[0:v]null[base];{_ad_chain(1, 9.0, wide)}",
                    "-map", "[v]", "-t", "9", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(over)],
                   check=True)

    def _lum(at: float) -> int:
        """Mean brightness of one strip of the banner area, 0-255."""
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(over), "-vf",
             f"crop={wide}:100:{(W - wide) // 2}:{AD_Y + 60},scale=1:1,format=gray",
             "-frames:v", "1", "-f", "rawvideo", "-"],
            capture_output=True, check=True).stdout[0]

    if AD_AT:
        assert _lum(AD_AT / 2) < 40, "the banner is on screen before it should be"
    # its own first second, which is black - if tpad had been swapped for
    # overlay's `enable` this would already be white and the gif would arrive
    # mid-animation
    assert _lum(AD_AT + AD_FADE + 0.2) < 40, "the banner did not start from frame one"
    assert _lum(AD_AT + 1.5) > 200, "the banner never faded in"

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
    assert abs(_dur(out) - _dur(mp3)) < 1.0, "video length does not match audio"

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
