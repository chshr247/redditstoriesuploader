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
import subprocess

import safety
import script
from config import BG_DIR, CHANNEL, CHANNELS, OUT_DIR, SUBTITLE_FONT
from voice import duration as _dur

W, H = 1080, 1920
FPS = 30          # source clips run 30 or 60; 60 doubles encode time for nothing here
FONT = SUBTITLE_FONT       # libass silently falls back if it is missing
FONT_SIZE = 110
POP_MS = 120               # scale-up duration of a word appearing
HOLD_MAX = 0.18            # how long a card may outlive its own audio

log = logging.getLogger(__name__)

CARD_FONT_SIZE = 62

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
Style: Card,{FONT},{CARD_FONT_SIZE},&H00101010,&H000000FF,&H00F2F2F2,&H00F2F2F2,-1,0,0,0,100,100,0,0,3,24,0,5,120,120,0,1

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


def build_ass(words: list[dict], path, title: str = "", title_end: float = 0) -> None:
    """Title card for the intro, then one word card at a time."""
    lines = [ASS_HEADER]
    if title and title_end > 0:
        # the title reaches here straight from the model, cues and stray
        # accents and all - plain() is what keeps both off the card
        safe = safety.mask(script.plain(re.sub(r"[{}\\]", "", title)))
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(title_end)},Card,,0,0,0,,"
                     r"{\fscx85\fscy85\t(0,180,\fscx100\fscy100)}" + safe)
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


def render(mp3, words: list[dict], name: str, bg=None,
           title: str = "", title_end: float = 0, key: str = ""):
    """Burn subtitles over a background clip and mux the narration.

    `key` identifies the STORY rather than the file: out/<id>_en.mp4 and
    out/<id>.mp4 are the same story on two channels, and that is exactly the
    pair that must not share footage.
    """
    bg = bg or _pick_bg(key)
    dur = _dur(mp3)
    ass = OUT_DIR / f"{name}.ass"
    out = OUT_DIR / f"{name}.mp4"
    build_ass(words, ass, title, title_end)

    # start at a random point so consecutive videos don't share footage
    bg_dur = _dur(bg)
    seek = round(random.uniform(0, max(0, bg_dur - dur)), 2) if bg_dur > dur else 0

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(seek), "-stream_loop", "-1", "-i", str(bg),
        "-i", str(mp3),
        "-filter_complex",
        # 720p sources get upscaled ~2.7x to cover 1080 wide, so lanczos over
        # the default bilinear is a visible win for one flag. setsar guards
        # against clips with non-square pixels.
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H},setsar=1,subtitles={ass.name}[v]",
        "-map", "[v]", "-map", "1:a", "-t", f"{dur:.3f}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p",
        str(out.name),
    ]
    # run inside OUT_DIR: the subtitles filter chokes on Windows drive colons
    subprocess.run(cmd, cwd=OUT_DIR, check=True)
    log.info("%s: %.1f sec from %s at %.1fs", out.name, dur, bg.name, seek)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    assert _ts(0) == "0:00:00.00"
    assert _ts(65.43) == "0:01:05.43"
    assert _ts(3661.5) == "1:01:01.50"

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
