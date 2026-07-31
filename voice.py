"""Step 3: narration text -> mp3 + per-word timings.

Two backends, and they differ in where timings come from:

  edge  - edge-tts emits WordBoundary events while synthesizing, so timings
          are authoritative: the engine reports what it itself spoke.
  fish  - Fish Audio returns audio and nothing else. Timings are recovered
          locally with whisper. We take whisper's TIMES but never its TEXT -
          it mishears ("все семьи" for "всей семье"), so the script's own
          words are aligned onto its timeline with difflib.
"""
import asyncio
import difflib
import json
import logging
import random
import re
import subprocess
import urllib.error
import urllib.request

import edge_tts

import script
from config import (FISH_API_KEY, FISH_MODEL, FISH_SPEED, FISH_TITLE_CUE,
                    FISH_VOICES_FEMALE, FISH_VOICES_MALE, OUT_DIR, OUTPUT_LANG,
                    TTS_BACKEND, TTS_VOICE, WHISPER_SIZE)

TICKS_PER_SEC = 10_000_000
FISH_URL = "https://api.fish.audio/v1/tts"

# ponytail: pacing knob for edge. Fish has its own, FISH_SPEED.
RATE = "+0%"

log = logging.getLogger(__name__)
_whisper = None


# --------------------------------------------------------------- edge backend

async def _edge_stream(text: str, mp3_path, voice: str, rate: str) -> list[dict]:
    words = []
    with open(mp3_path, "wb") as f:
        # boundary defaults to SentenceBoundary in edge-tts 7.x - useless for us
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_SEC
                words.append({
                    "word": chunk["text"],
                    "start": round(start, 3),
                    "end": round(start + chunk["duration"] / TICKS_PER_SEC, 3),
                })
    return words


# --------------------------------------------------------------- fish backend

def _fish_synth(text: str, mp3_path, speed: float, voice: str = "") -> None:
    if not FISH_API_KEY:
        raise RuntimeError("FISH_API_KEY is empty - fill in .env")
    body = {"text": text, "format": "mp3", "mp3_bitrate": 128,
            "prosody": {"speed": speed, "normalize_loudness": True}}
    if voice:
        body["reference_id"] = voice

    req = urllib.request.Request(
        FISH_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {FISH_API_KEY}",
                 "Content-Type": "application/json", "model": FISH_MODEL},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            mp3_path.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"fish {e.code}: {e.read().decode()[:300]}") from None


def _norm(w: str) -> str:
    return re.sub(r"[^\w]", "", w).lower()


def _align(text: str, mp3_path) -> list[dict]:
    """Our words on whisper's timeline.

    Whisper's tokens never match ours exactly - it mishears, splits hyphens
    and drops punctuation. difflib anchors the parts that do match; the runs
    in between get spread evenly across the gap between anchors.
    """
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        log.info("loading whisper %s (first run downloads it)", WHISPER_SIZE)
        _whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8")

    segments, _ = _whisper.transcribe(str(mp3_path), language=OUTPUT_LANG,
                                      word_timestamps=True)
    heard = [(w.word, w.start, w.end) for s in segments for w in s.words]
    if not heard:
        raise RuntimeError(f"whisper heard nothing in {mp3_path.name}")

    ours = text.split()
    matcher = difflib.SequenceMatcher(
        None, [_norm(w) for w in ours], [_norm(h[0]) for h in heard], autojunk=False)

    timed: list[dict | None] = [None] * len(ours)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            _, start, end = heard[j1 + k]
            # whisper hands back numpy floats; keep the json plain
            timed[i1 + k] = {"word": ours[i1 + k], "start": round(float(start), 3),
                             "end": round(float(end), 3)}

    matched = sum(t is not None for t in timed)
    log.info("aligned %d/%d words (whisper heard %d)", matched, len(ours), len(heard))
    if matched < len(ours) * 0.5:
        log.warning("weak alignment - subtitles may drift")

    return _fill_gaps(timed, ours, heard[-1][2])


def _fill_gaps(timed: list, ours: list[str], audio_end: float) -> list[dict]:
    """Spread unmatched runs evenly between the anchors around them."""
    out = []
    i = 0
    while i < len(timed):
        if timed[i] is not None:
            out.append(timed[i])
            i += 1
            continue
        j = i
        while j < len(timed) and timed[j] is None:
            j += 1
        left = out[-1]["end"] if out else 0.0
        right = float(timed[j]["start"] if j < len(timed) else audio_end)
        step = (right - left) / (j - i) if j > i else 0
        for k in range(i, j):
            s = left + step * (k - i)
            out.append({"word": ours[k], "start": round(s, 3),
                        "end": round(s + step, 3)})
        i = j
    return out


# ------------------------------------------------------------------ interface

def duration(path) -> float:
    """Container duration via ffprobe. Longer than the last word: mp3s end in silence."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def pick_voice(gender: str = "male") -> str:
    """A fish voice matching the narrator. Empty list means fish's default."""
    pool = FISH_VOICES_FEMALE if gender == "female" else FISH_VOICES_MALE
    if not pool:
        pool = FISH_VOICES_MALE or FISH_VOICES_FEMALE
        if pool:
            log.warning("no %s voices configured, falling back", gender)
    return random.choice(pool) if pool else ""


def speak(text: str, name: str, voice: str = TTS_VOICE, rate: str = RATE,
          speed: float = FISH_SPEED, fish_voice: str | None = None) -> tuple:
    """Synthesize into out/<name>.mp3 and out/<name>.json. Returns (mp3, words)."""
    mp3 = OUT_DIR / f"{name}.mp3"

    # cues and stress marks steer the engine but are never seen or heard as
    # such, so subtitles and alignment only ever get the plain text
    readable = script.plain(text)

    if TTS_BACKEND == "fish":
        _fish_synth(text, mp3, speed, pick_voice() if fish_voice is None else fish_voice)
        words = _align(readable, mp3)
    else:
        # edge has no cue syntax and would read the brackets out loud
        words = asyncio.run(_edge_stream(readable, mp3, voice, rate))

    if not words or mp3.stat().st_size == 0:
        raise RuntimeError(f"{TTS_BACKEND} returned nothing for {name!r}")

    (OUT_DIR / f"{name}.json").write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    dur = duration(mp3)
    log.info("%s: %.1f sec, %d words, real pace %.0f wpm",
             name, dur, len(words), len(words) / dur * 60)
    return mp3, words


def speak_parts(title: str, body: str, name: str, gap: float = 0.0,
                rate: str = RATE, speed: float = FISH_SPEED,
                gender: str = "male") -> tuple:
    """Narrate the title card, then the story, as one track.

    Synthesized separately on purpose: it gives an exact title length to hand
    the renderer, instead of guessing where the title ends inside one track.

    `gap` inserts DIGITAL silence, which is not the same as a pause: the voice
    carries a faint room tone throughout, so a padded gap drops the noise floor
    to zero and the join is heard as a cut. Default is none - the title's own
    trailing decay and the full stop it ends on supply the beat.

    Returns (mp3, body_words_offset_to_the_track, title_end_sec).
    """
    # one voice for the whole video - title and body must not swap narrators
    fish_voice = pick_voice(gender)

    # The cue rides along to the engine and is stripped before anything is
    # displayed, so the card and the description stay clean either way.
    # A title with no terminal punctuation gets read as if the sentence carries
    # on, and the story then sounds like one unbroken take. The full stop is
    # what tells the engine to land it.
    spoken_title = title if title.rstrip()[-1:] in ".!?" else title.rstrip() + "."
    if TTS_BACKEND == "fish" and FISH_TITLE_CUE and "[" not in title:
        spoken_title = f"{FISH_TITLE_CUE} {spoken_title}"

    t_mp3, _ = speak(spoken_title, f"{name}_title", rate=rate, speed=speed,
                     fish_voice=fish_voice)
    b_mp3, b_words = speak(body, f"{name}_body", rate=rate, speed=speed, fish_voice=fish_voice)
    if fish_voice:
        log.info("%s: %s voice %s", name, gender, fish_voice[:8])

    title_end = duration(t_mp3) + gap
    merged = OUT_DIR / f"{name}.mp3"
    pad = f"[0:a]apad=pad_dur={gap}[t];[t]" if gap else "[0:a]"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(t_mp3), "-i", str(b_mp3),
         "-filter_complex", f"{pad}[1:a]concat=n=2:v=0:a=1",
         str(merged)], check=True)

    words = [{**w, "start": round(w["start"] + title_end, 3),
              "end": round(w["end"] + title_end, 3)} for w in b_words]
    (OUT_DIR / f"{name}.json").write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    log.info("%s: title %.1fs + body %.1fs = %.1fs total",
             name, title_end, duration(b_mp3), duration(merged))
    return merged, words, title_end


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # gap filling must stay ordered and cover every word
    t = [{"word": "a", "start": 0.0, "end": 1.0}, None,
         {"word": "c", "start": 3.0, "end": 4.0}]
    filled = _fill_gaps(t, ["a", "b", "c"], 4.0)
    assert [w["word"] for w in filled] == ["a", "b", "c"]
    assert filled[1]["start"] == 1.0 and abs(filled[1]["end"] - 3.0) < 0.01
    tail = _fill_gaps([None, None], ["x", "y"], 2.0)
    assert [w["word"] for w in tail] == ["x", "y"] and tail[1]["end"] == 2.0

    # full-length narration on purpose: pace on a two-sentence clip is not
    # representative, and this number is what WPM in script.py must match
    sample = (
        "Сегодня я облажался, когда подарил всей семье ДНК-тесты на Рождество. "
        "Я думал, это будет милый подарок, повод посмеяться за ужином. "
        "Мама открыла свой и улыбнулась. Отец поперхнулся и выплюнул напиток на стол. "
        "Через три недели пришли результаты. У нас с сестрой совпали оба родителя. "
        "У младшего брата, про которого все всегда говорили, что у него мамины глаза, "
        "совпал только один. Приложение услужливо предложило ему связаться "
        "с мужчиной из Огайо, который определился как близкий родственник. "
        "Сначала никто ничего не сказал. Мама очень тихо поднялась и ушла наверх. "
        "Отец пошёл за ней. Брат сидел и перечитывал экран снова и снова. "
        "Потом он поднял на меня глаза и спросил, что я наделал. "
        "У меня до сих пор нет для него хорошего ответа."
    )
    mp3, words = speak(sample, "_selftest")

    assert mp3.stat().st_size > 1000, "mp3 is suspiciously small"
    assert len(words) == len(sample.split()), "word count must match the script"
    assert all(w["start"] <= w["end"] for w in words), "negative-length word"
    assert all(a["start"] <= b["start"] for a, b in zip(words, words[1:])), "out of order"

    dur = duration(mp3)
    print(f"ok: {mp3.name} via {TTS_BACKEND}, {dur:.1f}s, {len(words)} words")
    print(f"measured pace: {len(words) / dur * 60:.0f} wpm  (WPM['{OUTPUT_LANG}'] in script.py)")
    print("first words:", [(w["word"], w["start"]) for w in words[:5]])
