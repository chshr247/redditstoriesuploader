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
from config import (FISH_API_KEY, FISH_CTA_CUE, FISH_MODEL, FISH_SPEED,
                    FISH_TITLE_CUE, FISH_VOICES_FEMALE, FISH_VOICES_MALE,
                    OUT_DIR, OUTPUT_LANG, TTS_BACKEND, TTS_VOICE, WHISPER_SIZE)

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

    # cues steer the engine and are never seen; accents steer nothing at all
    # (measured, see ACCENTS in script.py) and must not be seen either, so
    # subtitles and alignment only ever get the plain text
    readable = script.plain(text)

    if TTS_BACKEND == "fish":
        _fish_synth(text, mp3, speed, pick_voice() if fish_voice is None else fish_voice)
        words = _align(readable, mp3)
    else:
        # edge has no cue syntax and would read the brackets out loud
        words = asyncio.run(_edge_stream(readable, mp3, voice, rate))

    if not words or mp3.stat().st_size == 0:
        raise RuntimeError(f"{TTS_BACKEND} returned nothing for {name!r}")

    # Who says each word is known only here, where the cues are still attached;
    # the aligner works on the stripped text and produces the same word order.
    who = script.speakers(text)
    if len(who) == len(words):
        for w, label in zip(words, who):
            w["speaker"] = label
    else:
        log.warning("speaker map %d vs %d words, colouring skipped", len(who), len(words))

    (OUT_DIR / f"{name}.json").write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    dur = duration(mp3)
    log.info("%s: %.1f sec, %d words, real pace %.0f wpm",
             name, dur, len(words), len(words) / dur * 60)
    return mp3, words


CUE_MAX = 60                                  # the ceiling on script.TAG
LEAD_CUE = re.compile(r"^\[([^\]\n]{1,60})\]\s*")


def _cued(text: str, cue: str) -> str:
    """Apply a delivery cue, and make sure the line lands on a full stop.

    Text with no terminal punctuation gets read as if the sentence carries on,
    and the parts either side then sound like one unbroken take. Terminal
    punctuation is what tells the engine to land it.

    The cue rides along to the engine and is stripped before anything is
    displayed, so the card and the description stay clean either way.

    The closing question arrives with a mood cue of its own, chosen for how the
    story ended. That wording is the specific one, so it leads - but the
    delivery constraint in `cue` has to survive, because the take that won the
    listening test carried BOTH: mood plus "no stress on the last word". Mood
    alone was a different take, and it lost. So the two are merged into one
    bracket rather than one replacing the other.

    The test is for a cue at the FRONT rather than a bracket anywhere, so a
    stray cue mid-line cannot suppress the merge.
    """
    text = text.rstrip()
    if text[-1:] not in ".!?…»":
        text += "."
    if TTS_BACKEND != "fish" or not cue:
        return text

    # A cue longer than script.TAG allows is not recognised as a cue: it stays
    # in the word count and gets burned into the subtitles. Dropping it costs
    # delivery, keeping it corrupts the screen - so it goes.
    if script.plain(cue):
        log.error("cue %r is too long to be stripped (max %d chars inside the "
                  "brackets) - narrating without it", cue[:50], CUE_MAX)
        return text

    lead = LEAD_CUE.match(text)
    if not lead:
        return f"{cue} {text}"

    # Two cues back to back get read as one, with the second quietly dropped,
    # so they have to become a single bracket. If the join outgrows the ceiling
    # it stops being strippable and would be narrated as visible text, which is
    # worse than losing the constraint - then the model's cue stands alone.
    merged = f"[{lead.group(1)}, {cue.strip()[1:-1]}]"
    if len(merged) - 2 > CUE_MAX:
        log.warning("merged cue %r exceeds %d chars, keeping the model's",
                    merged[:50], CUE_MAX)
        return text
    return merged + " " + text[lead.end():]


def speak_parts(title: str, body: str, name: str, gap: float = 0.0,
                rate: str = RATE, speed: float = FISH_SPEED,
                gender: str = "male", fish_voice: str = "") -> tuple:
    """Narrate the title card, the story, then the closing question - one track.

    Three takes rather than one, for two different reasons. The title is split
    off to give the renderer an exact title length instead of guessing where
    the card ends inside a single track. The closing question is split off
    because read inline it comes out as one more sentence of the plot; on its
    own take, with its own cue, it lands as the narrator turning to the viewer.

    `gap` inserts DIGITAL silence, which is not the same as a pause: the voice
    carries a faint room tone throughout, so a padded gap drops the noise floor
    to zero and the join is heard as a cut. Default is none - each part's own
    trailing decay and the full stop it ends on supply the beat.

    `fish_voice` pins the narrator. Left empty it is drawn per video, which is
    what an ordinary story wants; a story split across several videos passes the
    id it was queued with, or the second half arrives in someone else's voice.

    Returns (mp3, story_and_question_words_offset_to_the_track, title_end_sec).
    """
    # one voice for the whole video - the takes must not swap narrators
    fish_voice = fish_voice or pick_voice(gender)
    story, cta = script.split_cta(body)
    if not cta:
        # Expected for every part of a split story except the last: those end on
        # the cliffhanger and address the viewer nowhere. For an ordinary video
        # _ending_fault() has already refused the text and write_script() has
        # already shouted, so a warning here would only be a second voice.
        log.info("%s: no closing question, narrating the body as one take", name)

    t_mp3, _ = speak(_cued(title, FISH_TITLE_CUE), f"{name}_title",
                     rate=rate, speed=speed, fish_voice=fish_voice)
    b_mp3, b_words = speak(story, f"{name}_body", rate=rate, speed=speed,
                           fish_voice=fish_voice)
    parts, words = [t_mp3, b_mp3], list(b_words)

    if cta:
        c_mp3, c_words = speak(_cued(cta, FISH_CTA_CUE), f"{name}_cta",
                               rate=rate, speed=speed, fish_voice=fish_voice)
        body_end = duration(b_mp3)
        words += [{**w, "start": round(w["start"] + body_end, 3),
                   "end": round(w["end"] + body_end, 3)} for w in c_words]
        parts.append(c_mp3)

    if fish_voice:
        log.info("%s: %s voice %s", name, gender, fish_voice[:8])

    title_end = duration(t_mp3) + gap
    merged = OUT_DIR / f"{name}.mp3"
    pad = f"[0:a]apad=pad_dur={gap}[t];[t]" if gap else "[0:a]"
    chain = pad + "".join(f"[{i}:a]" for i in range(1, len(parts)))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         *[a for p in parts for a in ("-i", str(p))],
         "-filter_complex", f"{chain}concat=n={len(parts)}:v=0:a=1",
         str(merged)], check=True)

    words = [{**w, "start": round(w["start"] + title_end, 3),
              "end": round(w["end"] + title_end, 3)} for w in words]
    (OUT_DIR / f"{name}.json").write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    log.info("%s: title %.1fs + story %.1fs%s = %.1fs total", name, title_end,
             duration(b_mp3),
             f" + question {duration(parts[-1]):.1f}s" if cta else "",
             duration(merged))
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

    # A cue must vanish completely on the way to the screen. Both defaults once
    # grew past TAG's 60-char ceiling and were narrated as visible text, so the
    # configured values - not just the shipped ones - get checked here.
    for cue in (FISH_TITLE_CUE, FISH_CTA_CUE):
        assert not script.plain(cue), f"cue too long to be stripped: {cue!r}"
    assert _cued("Заголовок", "[short]").endswith("Заголовок."), "must land on a stop"
    assert _cued("Вопрос?", "[short]").endswith("Вопрос?"), "a question keeps its mark"
    assert _cued("Без кью.", "[short]").startswith("[short] "), "no lead cue means prepend"

    # The model's mood leads, our delivery constraint rides along, and the
    # engine gets ONE bracket - the shape that won the listening test. Mood on
    # its own is a different delivery, so losing the constraint is a bug.
    own = _cued("[doubtful] А как бы вы поступили?", "[calm, flat]")
    assert own.startswith("[doubtful, calm, flat] "), own
    assert not script.plain("[doubtful, calm, flat]"), "the merge must stay strippable"
    assert own.count("[") == 1, f"two brackets reach the engine as one: {own}"
    # a cue mid-line is not a leading cue, so the fallback still applies
    assert _cued("А как бы [oops] вы поступили?", "[calm]").startswith("[calm] ")
    # a merge that would outgrow the ceiling leaves the model's cue alone,
    # because an unstrippable cue gets narrated instead of steering the voice
    huge = "[" + "x" * 55 + "]"
    assert _cued(f"{huge} Реплика.", "[calm, flat]").startswith(huge)

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
