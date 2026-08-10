"""Step 4: mp3 + word timings + background clip -> vertical mp4.

Subtitles are ASS, not drawtext: one Dialogue line per word with a scale-up
transition, which is the whole kinetic-typography effect. ffmpeg burns them
in a single pass, so no frame ever reaches Python.
"""
import hashlib
import json
import logging
import random
import re
import statistics
import subprocess
from pathlib import Path

import safety
import script
from config import AD_DIR, BG_DIR, CHANNEL, CHANNELS, OUT_DIR, SUBTITLE_FONT
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
AD_AT = 1.0                # when it appears, seconds
AD_FADE = 0.5              # fade in, and out again if AD_SEC ends it
AD_SEC = 0.0               # how long it stays; 0 means to the end of the video
AD_CHANNELS = ("ru",)      # channels that carry one; the rest never do
AD_MIN_AREA = 1 / 6        # of the frame, below which _ad_size() warns
AD_MARGIN = 40             # side gap, so the banner is never flush with an edge
AD_Y = 240                 # below the platform's top interface strip, and word
                           # cards sit dead centre, so this band is free
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

log = logging.getLogger(__name__)

CARD_FONT_SIZE = 82
# ASS stores colour as &HAABBGGRR. The card is near-black on near-white, so the
# accent cannot come from SPEAKER_COLOURS below - those are picked to sit on
# video and yellow on a white card is invisible. Deep red reads on paper white.
CARD_TEXT = "&H00101010"
CARD_BOX = "&H00F2F2F2"
CARD_ACCENT = "&H001A1ACC"
# What prompts.py guarantees every title rests on: a figure, or a line somebody
# actually said. Those two are findable; the third (a named stake) is not.
# Both quote shapes, because the channels use different ones - «» and “”.
ACCENT = re.compile(r"\d+|«[^»]*»|“[^”]*”")
CARD_POP_MS = 70           # how fast the card's highlight moves onto a word

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
# BorderStyle 3 paints an opaque box in OutlineColour - a title card with no
# image files and no PIL. Outline doubles as the box padding.
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
Style: Card,{FONT},{CARD_FONT_SIZE},{CARD_TEXT},&H000000FF,{CARD_BOX},{CARD_BOX},-1,0,0,0,100,100,0,0,3,24,0,5,120,120,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(sec: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc"""
    cs = round(sec * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _sung(words: list[dict], title_end: float) -> list[tuple[float, float, str]]:
    """(start, end, card text) per word: the whole title, that word lit.

    This is what the card is for. A static block of text is read in about a
    second and then sits there for two more with nothing happening on it, which
    is most of the hook spent on a still image. Lighting the word being spoken
    puts movement exactly where the eye already is, and unlike _accent() it
    fires on EVERY title rather than the one in five that carries a figure.

    The whole title stays on screen throughout - this is not a word-by-word
    reveal. A viewer who reads faster than the narrator speaks has to be able
    to finish early; that is what a title card is for, and hiding the end of it
    would trade the hook for a gimmick.

    One Dialogue event per word rather than one animated line. An ASS override
    block applies from where it sits to the END of the line, so per-word \\t
    animations are inherited by every word after them and accumulate - the
    first attempt lit the whole tail of the title instead of one word. Static
    colour on separate events has no such reach.

    Timings come from the title's own take, so the highlight follows the actual
    voice rather than an estimate: a word the narrator lingers on stays lit.
    """
    clean = []
    for i, w in enumerate(words):
        text = safety.mask(re.sub(r"[{}\\]", "", w["word"]))
        if i == len(words) - 1:
            # _cued() ends the take on a full stop so the title does not run
            # into the story; the card never showed that stop and still must not
            text = re.sub(r"[.!?]+$", "", text)
        if text:
            clean.append((w["start"], text))
    if not clean:
        return []

    out = []
    for i, (start, _) in enumerate(clean):
        end = clean[i + 1][0] if i + 1 < len(clean) else title_end
        if end <= start:
            continue
        lit = " ".join(
            f"{{\\c{CARD_ACCENT}&}}{t}{{\\c{CARD_TEXT}&}}" if n == i else t
            for n, (_, t) in enumerate(clean))
        out.append((start, end, lit))
    # a card that starts before the first word would flash uncoloured, so the
    # first event simply opens with the card rather than waiting for it
    if out:
        out[0] = (0.0, out[0][1], out[0][2])
    return out


def _accent(text: str) -> str:
    """Colour the figure or the quoted line the title is built on.

    A digit is the one thing the eye catches while scrolling, and until now it
    was set in exactly the same near-black as the prepositions around it. The
    title rule in prompts.py already guarantees one of these is present, so
    this reads what the prompt promised rather than guessing at emphasis.

    Returns the text unchanged when neither is there - a title resting on a
    named stake has nothing to colour, and colouring a random word instead
    would tell the eye to catch the wrong thing.
    """
    return ACCENT.sub(
        lambda m: f"{{\\c{CARD_ACCENT}&}}{m.group(0)}{{\\c{CARD_TEXT}&}}", text)


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


def build_ass(words: list[dict], path, title: str = "", title_end: float = 0,
              title_words: list[dict] | None = None) -> None:
    """Title card for the intro, then one word card at a time."""
    lines = [ASS_HEADER]
    if title and title_end > 0:
        # the scale-up belongs to the card appearing, so it rides the first
        # event only - later ones are already at full size
        pop = r"{\fscx85\fscy85\t(0,180,\fscx100\fscy100)}"
        cards = _sung(title_words, title_end) if title_words else []
        if not cards:
            # No timings for the title - an older render, or an aligner that
            # came back empty. The card still has to carry emphasis, so it
            # falls back to colouring the figure or the quoted line.
            # the title reaches here straight from the model, cues and stray
            # accents and all - plain() is what keeps both off the card
            cards = [(0.0, title_end,
                      _accent(safety.mask(script.plain(re.sub(r"[{}\\]", "", title)))))]
        for i, (start, end, text) in enumerate(cards):
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Card,,0,0,0,,"
                         + (pop if i == 0 else "") + text)
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


def render(mp3, words: list[dict], name: str, bg=None,
           title: str = "", title_end: float = 0, key: str = "",
           title_words: list[dict] | None = None, ad=None):
    """Burn subtitles over a background clip and mux the narration.

    `key` identifies the STORY rather than the file: out/<id>_en.mp4 and
    out/<id>.mp4 are the same story on two channels, and that is exactly the
    pair that must not share footage.
    """
    bg = bg or _pick_bg(key)
    ad = ad or _pick_ad()
    dur = _dur(mp3)
    ass = OUT_DIR / f"{name}.ass"
    out = OUT_DIR / f"{name}.mp4"
    build_ass(words, ass, title, title_end, title_words)

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
    # the banner goes on last, over the burnt-in subtitles, so whatever it was
    # paid for is never half-covered by a word card
    last = "[base]" if ad else "[v]"
    if cut is None:
        inputs = ["-ss", str(seek), "-stream_loop", "-1", "-i", str(bg)]
        video, audio = f"[0:v]{chain},subtitles={ass.name}{last}", "1:a"
    else:
        # Two reads of the same file, joined where the title card leaves and
        # the story starts. Subtitles go on AFTER the join, so the word cards
        # run across it untouched and only the footage cuts.
        inputs = ["-ss", str(seek), "-t", f"{title_end:.3f}",
                  "-stream_loop", "-1", "-i", str(bg),
                  "-ss", str(cut), "-stream_loop", "-1", "-i", str(bg)]
        video = (f"[0:v]{chain}[hook];[1:v]{chain}[rest];"
                 f"[hook][rest]concat=n=2:v=1:a=0[cat];"
                 f"[cat]subtitles={ass.name}{last}")
        audio = "2:a"

    # after the mp3, so the audio input keeps the index the map above expects
    ad_in = _ad_input(ad) if ad else []
    if ad:
        # inputs are the backgrounds, then the mp3, then the banner
        video += ";" + _ad_chain((1 if cut is None else 2) + 1, dur, _ad_size(ad))

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-i", str(mp3),
        *ad_in,
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

    mp3 = OUT_DIR / "_selftest.mp3"
    assert mp3.exists(), "run `python voice.py` first"
    words = json.loads((OUT_DIR / "_selftest.json").read_text("utf-8"))

    def _w(t, s, e):
        return {"word": t, "start": s, "end": e}

    # The accent must find what prompts.py promised, and nothing else. Colour
    # is reset to the Card style's own primary, so the two are asserted equal
    # here - drift between them would tint the rest of the title.
    assert f",{CARD_TEXT}," in ASS_HEADER, "the card style stopped using CARD_TEXT"
    assert _accent("счёт на 80000 за потоп").count(CARD_ACCENT) == 1
    assert _accent("счёт на 80000 за потоп").endswith("за потоп")
    assert "80000" in _accent("счёт на 80000 за потоп")
    assert _accent("сказала «этот ребёнок не наш»").count(CARD_ACCENT) == 1
    assert _accent("said “that baby isn't ours”").count(CARD_ACCENT) == 1
    # a title resting on a named stake has nothing to catch, and must be left be
    assert _accent("Мать парня звала меня на обед") == "Мать парня звала меня на обед"
    # every override we open is closed again, or the rest of the card changes colour
    for t in ("счёт на 80000 за потоп", "сказала «этот ребёнок не наш»"):
        assert _accent(t).count(CARD_ACCENT) == _accent(t).count(CARD_TEXT)

    # The card lights up word by word, and every word gets both edges: one
    # animation onto the accent, one back. A word that only got the first would
    # stay lit and the card would fill up red instead of tracking the voice.
    tw = [_w("Соседка", 0.0, 0.4), _w("прислала", 0.4, 0.9), _w("счёт.", 0.9, 1.4)]
    cards = _sung(tw, 1.6)
    assert len(cards) == len(tw), cards
    # exactly ONE word is lit at a time. The first attempt animated a single
    # line and every word inherited the ones before it, so the whole tail of
    # the title lit up at once - this is the assertion that would have caught it.
    for start, end, text in cards:
        assert text.count(CARD_ACCENT) == 1, text
    # the whole title stays on screen the whole time - this is not a reveal
    for _, _, text in cards:
        assert all(w["word"].rstrip(".") in text for w in tw), text
    assert [round(s, 2) for s, _, _ in cards] == [0.0, 0.4, 0.9]
    assert [round(e, 2) for _, e, _ in cards] == [0.4, 0.9, 1.6]
    # the lit word walks forward, and the cued full stop never reaches the card
    assert cards[0][2].startswith(f"{{\\c{CARD_ACCENT}&}}Соседка")
    assert cards[2][2].endswith(f"{{\\c{CARD_ACCENT}&}}счёт{{\\c{CARD_TEXT}&}}")
    # timings absent: the card must still render, via the figure/quote accent
    assert _sung([], 3.0) == []

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

    out = render(mp3, words, "_selftest", bg=bg)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert probe == f"{W},{H}", f"wrong resolution: {probe}"
    assert abs(_dur(out) - _dur(mp3)) < 1.0, "video length does not match audio"
    print(f"ok: {out.name}, {_dur(out):.1f}s, {probe}")
