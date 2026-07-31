"""Step 4: mp3 + word timings + background clip -> vertical mp4.

Subtitles are ASS, not drawtext: one Dialogue line per word with a scale-up
transition, which is the whole kinetic-typography effect. ffmpeg burns them
in a single pass, so no frame ever reaches Python.
"""
import json
import logging
import random
import re
import subprocess

import safety
import script
from config import BG_DIR, OUT_DIR, SUBTITLE_FONT
from voice import duration as _dur

W, H = 1080, 1920
FPS = 30          # source clips run 30 or 60; 60 doubles encode time for nothing here
FONT = SUBTITLE_FONT       # libass silently falls back if it is missing
FONT_SIZE = 110
POP_MS = 120               # scale-up duration of a word appearing

log = logging.getLogger(__name__)

CARD_FONT_SIZE = 62
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


def _group(words: list[dict], min_chars: int = 3, max_words: int = 3) -> list[dict]:
    """Glue filler words onto the next one.

    Straight one-word-per-card gives "a" and "I" a full beat of full-screen
    time, which reads as a stutter. A card keeps absorbing words while it
    still ends on a stub, so it never trails off on an article either.
    """
    out = []
    for w in words:
        prev = out[-1] if out else None
        if (prev and len(prev["word"].split()[-1]) < min_chars
                and len(prev["word"].split()) < max_words):
            prev["word"] += " " + w["word"]
            prev["end"] = w["end"]
        else:
            out.append(dict(w))
    return out


def build_ass(words: list[dict], path, title: str = "", title_end: float = 0) -> None:
    """Title card for the intro, then one word card at a time."""
    lines = [ASS_HEADER]
    if title and title_end > 0:
        # the title reaches here straight from the model, stress marks and all
        safe = safety.mask(script.plain(re.sub(r"[{}\\]", "", title)))
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(title_end)},Card,,0,0,0,,"
                     r"{\fscx85\fscy85\t(0,180,\fscx100\fscy100)}" + safe)
    words = _group(words)
    for i, w in enumerate(words):
        # stretch to the next word so pauses don't blank the screen
        end = words[i + 1]["start"] if i + 1 < len(words) else w["end"]
        text = safety.mask(re.sub(r"[{}\\]", "", w["word"]).strip())
        if not text or end <= w["start"]:
            continue
        pop = r"{\fscx70\fscy70\t(0,%d,\fscx100\fscy100)}" % POP_MS
        lines.append(f"Dialogue: 0,{_ts(w['start'])},{_ts(end)},Main,,0,0,0,,{pop}{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pick_bg() -> "Path":
    clips = [p for p in BG_DIR.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".webm")]
    if not clips:
        raise RuntimeError(f"no background clips in {BG_DIR} - drop a vertical mp4 there")
    return random.choice(clips)


def render(mp3, words: list[dict], name: str, bg=None,
           title: str = "", title_end: float = 0):
    """Burn subtitles over a background clip and mux the narration."""
    bg = bg or _pick_bg()
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

    cards = _group(words)
    assert sum(len(c["word"].split()) for c in cards) == len(words), "lost a word"
    assert all(len(c["word"]) >= 3 for c in cards), "single-letter card survived"

    build_ass(words, OUT_DIR / "_check.ass")
    body = (OUT_DIR / "_check.ass").read_text("utf-8").splitlines()
    events = [l for l in body if l.startswith("Dialogue:")]
    assert len(events) == len(cards), f"{len(events)} lines for {len(cards)} cards"

    bg = _pick_bg() if any(BG_DIR.iterdir()) else None
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
