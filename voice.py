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
from num2words import num2words

import script
from config import (FISH_API_KEY, FISH_BODY_CUE, FISH_CTA_CUE, FISH_MODEL,
                    FISH_SPEED, FISH_TITLE_CUE, FISH_VOICES_FEMALE,
                    FISH_VOICES_MALE, FISH_VOICE_HORROR, OUT_DIR, OUTPUT_LANG,
                    SFX, SUBREDDITS_HORROR, TTS_BACKEND,
                    TTS_VOICE, VOICE_DEHISS_DB, VOICE_SPEEDUP, WHISPER_SIZE)

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


# A bare digit is language-neutral on the page and English in fish's mouth:
# "после 5 минут" comes back as "после five минут". The engine never gets to
# guess - numbers are spelled out in OUTPUT_LANG before synthesis. Only the
# engine's copy is rewritten; the card and the subtitles keep the digits,
# which is why this lives here and not in script.py.
#
# Grouped thousands are matched whole ("20 000" is one number, not 20 and 0).
# The word before and the word after are looked at but never consumed: the
# preposition picks the case, the counted noun picks the gender.
NUMBER = re.compile(
    r"(?P<prep>[А-Яа-яЁёA-Za-z]+\s+)?(?P<num>\d+(?:[  ]\d{3})+|\d+)"
    r"(?=(?:\s+(?P<noun>[А-Яа-яЁё]+))?)")

# Russian prepositions that govern one case and one only. The ambiguous ones -
# в, на, с, за, под, по, через - are left out on purpose: with a quantity they
# read as accusative, and the accusative of a numeral is its nominative form,
# which is what num2words already hands back ("в пять часов", "за пять минут").
PREP_CASE = {
    "gent": "без до из из-за из-под от у для около возле вокруг кроме после "
            "среди против вместо ради свыше сверх мимо вдоль насчёт больше "
            "меньше более менее",
    "datv": "к ко благодаря вопреки согласно навстречу",
    "ablt": "над перед пред между",
    "loct": "о об обо при",
}
PREP_CASE = {p: case for case, ps in PREP_CASE.items() for p in ps.split()}

# only these two agree in gender with what they count ("две машины", "одна минута")
GENDERED = ("один", "два")

_morph = None


def _parse(word: str):
    """pymorphy's reading of a word, preferring the numeral one.

    "сто" and "тысяча" are both nouns to pymorphy as well, and the noun reading
    declines differently ("сто" stays "сто" where the numeral gives "ста").
    """
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    parses = _morph.parse(word)
    return next((p for p in parses if p.tag.POS == "NUMR"), parses[0])


def _agree(spelled: str, prep: str, noun: str) -> str:
    """Decline a spelled-out numeral to fit the phrase around it."""
    case = PREP_CASE.get(prep.strip().lower())
    gender = _parse(noun).tag.gender if noun else None
    if not case and gender not in ("femn", "neut"):
        return spelled

    words = spelled.split()
    out = []
    for i, w in enumerate(words):
        wants = []
        # gender lands on the last word only: "двадцать одна", not "двадцатая"
        if i == len(words) - 1 and w in GENDERED and gender in ("femn", "neut"):
            wants.append({case, gender} if case else {gender})
        if case:
            # outside the nominative these two lose the gender distinction -
            # "двух" covers all three - and asking for both at once finds
            # nothing, so the case alone is the fallback
            wants.append({case})
        form = next((f for f in (_parse(w).inflect(x) for x in wants) if f), None)
        out.append(form.word if form else w)
    return " ".join(out)


def spell(text: str) -> str:
    def one(m):
        digits = re.sub(r"\D", "", m.group("num"))
        prep = m.group("prep") or ""
        # past a sane length it is an id or a phone number, not a quantity, and
        # num2words starts raising instead of returning
        if len(digits) > 15:
            return m.group()
        spelled = num2words(int(digits), lang=OUTPUT_LANG)
        if OUTPUT_LANG == "ru":
            spelled = _agree(spelled, prep, m.group("noun") or "")
        return prep + spelled

    return NUMBER.sub(one, text)


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

SR = 44100      # the rate the finished track is put onto before it is faked
# Where the shelf starts. 6 kHz rather than 8: measured 2026-08-16, a shelf at
# 8 kHz barely moves the band it is aimed at - -3, -5 and -7 dB all landed
# within 0.3 dB of each other above 9 kHz - while the same gains from 6 kHz
# track properly. Still well clear of the 300-3400 the voice lives in.
DEHISS_HZ = 6000


def _speedup(k: float = VOICE_SPEEDUP, dehiss: float = VOICE_DEHISS_DB) -> str:
    """Filter tail that makes the track faster and higher in one operation.

    asetrate reinterprets the samples at a different rate, so pace and pitch
    move together - which IS the effect wanted here, and one filter rather than
    atempo plus a pitch shifter fighting each other.

    It takes an absolute rate, not a factor, so the input is resampled to a
    known one first: fish returns whatever it returns, and a 24 kHz take handed
    to asetrate=44100*k comes out nearly twice too fast. The second aresample
    puts the container back on a normal rate afterwards.

    The shelf on the end is not a separate effect, which is why it lives in this
    function and not beside it: the shift is the only reason it is needed. Every
    frequency moves up by k, the voice's own sibilance and breath included, and
    that is heard as hiss. It is not noise - the pauses in a Fish take measure
    -91 dB, and afftdn changed nothing - so it is taken back down rather than
    denoised. See VOICE_DEHISS_DB.
    """
    if k == 1.0:
        return ""
    chain = f",aresample={SR},asetrate={int(SR * k)},aresample={SR}"
    if dehiss:
        chain += f",treble=g={dehiss}:f={DEHISS_HZ}:width_type=h:w=4000"
    return chain


def _scaled(words: list[dict], offset: float = 0.0,
            k: float = VOICE_SPEEDUP, lead: float = 0.0) -> list[dict]:
    """Timings measured on the unsped takes, moved onto the finished track.

    `offset` is on the unsped clock and scales with everything else; `lead` is
    the silence the finished track is delayed by, so it is added AFTER the
    division - it is real seconds on the video's own timeline, not narration
    that got faster.
    """
    if k == 1.0 and not offset and not lead:
        return words
    return [{**w, "start": round((w["start"] + offset) / k + lead, 3),
             "end": round((w["end"] + offset) / k + lead, 3)} for w in words]


def duration(path) -> float:
    """Container duration via ffprobe. Longer than the last word: mp3s end in silence."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def pick_voice(gender: str = "male", sub: str = "") -> str:
    """A fish voice matching the narrator. Empty list means fish's default.

    A horror sub overrules the narrator outright. That slot is read by one
    fixed voice on purpose - a recurring narrator is half of what makes a
    scary story sound like one - so the gender the model tagged belongs to
    the character in the story and not to whoever reads it out.
    """
    if sub in SUBREDDITS_HORROR and FISH_VOICE_HORROR:
        return FISH_VOICE_HORROR
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
        # the engine hears words, whisper hears words, but `readable` still
        # carries the digits - difflib just misses those tokens and _fill_gaps
        # times them from the anchors either side
        _fish_synth(spell(text), mp3, speed,
                    pick_voice() if fish_voice is None else fish_voice)
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


CUE_MAX = script.CUE_MAX                      # the ceiling on script.TAG
LEAD_CUE = re.compile(rf"^\[([^\]\n]{{1,{CUE_MAX}}})\]\s*")


def _cued(text: str, cue: str, sep: str = ", ") -> str:
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

    `sep` is how the two halves are joined. The comma default is the shape the
    listening test picked, and it is safe for the title and the question because
    neither carries dialogue. The story does: script.speakers() reads whatever
    precedes the first comma of a bracket as the name of the speaker of the next
    quoted line, so a comma joined onto a bare [emphasis] would invent a speaker
    of that name and shift every real one's colour. The story passes " ".
    """
    text = text.rstrip()
    if text[-1:] not in script.TERMINAL:
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
    merged = f"[{lead.group(1)}{sep}{cue.strip()[1:-1]}]"
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

    Returns (mp3, story_and_question_words_offset_to_the_track, title_end_sec,
    title_words). The last are NOT offset: they time the title card, which
    starts at zero, while the rest are offset past it.
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

    # the title's own timings are what light the card up word by word - they
    # were always computed here and thrown away
    t_mp3, t_words = speak(_cued(title, FISH_TITLE_CUE), f"{name}_title",
                           rate=rate, speed=speed, fish_voice=fish_voice)
    b_mp3, b_words = speak(_cued(story, FISH_BODY_CUE, " "), f"{name}_body",
                           rate=rate, speed=speed, fish_voice=fish_voice)
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

    # Where the story starts, on the takes as they were synthesized. Every
    # timing above is on that same unsped clock, so the whole set is offset here
    # and scaled once, after the speed-up is applied to the audio below.
    offset = duration(t_mp3) + gap
    merged = OUT_DIR / f"{name}.mp3"
    pad = f"[0:a]apad=pad_dur={gap}[t];[t]" if gap else "[0:a]"
    chain = pad + "".join(f"[{i}:a]" for i in range(1, len(parts)))
    # Silence in front of everything, the length of the whoosh render.py lays
    # over the same opening: the sound finishes, then the first word. AFTER
    # _speedup(), or the pause would be sped up along with the voice and come
    # out shorter than the sound it is making room for.
    lead = round(duration(SFX), 3) if SFX.exists() else 0.0
    delay = f",adelay={int(lead * 1000)}:all=1" if lead else ""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         *[a for p in parts for a in ("-i", str(p))],
         "-filter_complex",
         f"{chain}concat=n={len(parts)}:v=0:a=1{_speedup()}{delay}",
         str(merged)], check=True)

    title_end = offset / VOICE_SPEEDUP + lead
    words = _scaled(words, offset, lead=lead)
    # the card's own highlight rides the sped-up title audio too, so these move
    # by the same factor - and by the lead as well, because the card is on
    # screen from frame zero while the voice it follows is not
    t_words = _scaled(t_words, lead=lead)
    (OUT_DIR / f"{name}.json").write_text(json.dumps(words, ensure_ascii=False), "utf-8")
    if lead:
        log.info("%s: %.2fs of %s in front of the first word", name, lead, SFX.name)
    log.info("%s: title %.1fs + story %.1fs%s = %.1fs total%s", name, title_end,
             duration(b_mp3),
             f" + question {duration(parts[-1]):.1f}s" if cta else "",
             duration(merged),
             f" (sped up x{VOICE_SPEEDUP})" if VOICE_SPEEDUP != 1.0 else "")
    return merged, words, title_end, t_words


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # The horror slot is read by ONE voice, and the narrator's gender does not
    # get a say: the gender belongs to the character in the story, the voice to
    # the channel. Both directions matter - a horror sub must override a female
    # narrator, and an ordinary sub must not pick the horror voice up.
    _real_pin = (SUBREDDITS_HORROR, FISH_VOICE_HORROR)
    globals()["SUBREDDITS_HORROR"] = ["_selftest_horror"]
    globals()["FISH_VOICE_HORROR"] = "_pinned"
    assert pick_voice("female", "_selftest_horror") == "_pinned"
    assert pick_voice("male", "_selftest_horror") == "_pinned"
    assert pick_voice("male", "AmItheAsshole") != "_pinned"
    assert pick_voice("male") != "_pinned", "no sub means the ordinary pool"
    # unpinned, the horror sub falls back to the pool like anything else
    globals()["FISH_VOICE_HORROR"] = ""
    assert pick_voice("male", "_selftest_horror") != "_pinned"
    globals()["SUBREDDITS_HORROR"], globals()["FISH_VOICE_HORROR"] = _real_pin

    # gap filling must stay ordered and cover every word
    t = [{"word": "a", "start": 0.0, "end": 1.0}, None,
         {"word": "c", "start": 3.0, "end": 4.0}]
    filled = _fill_gaps(t, ["a", "b", "c"], 4.0)
    assert [w["word"] for w in filled] == ["a", "b", "c"]
    assert filled[1]["start"] == 1.0 and abs(filled[1]["end"] - 3.0) < 0.01
    tail = _fill_gaps([None, None], ["x", "y"], 2.0)
    assert [w["word"] for w in tail] == ["x", "y"] and tail[1]["end"] == 2.0

    # The speed-up shortens the audio, so the subtitles have to shorten with it
    # or every word lands later than it is spoken - by seconds, by the end.
    assert _speedup(1.0) == "" and _scaled([{"start": 1.0, "end": 2.0}], k=1.0)[0]["start"] == 1.0
    # The shelf exists only to undo the shift, so it must never appear without
    # one - at k=1.0 there is nothing to correct and cutting the top would just
    # be dulling the voice for free.
    assert "treble" not in _speedup(1.0, -7), _speedup(1.0, -7)
    assert "treble" in _speedup(1.25, -7) and "treble" not in _speedup(1.25, 0)
    moved = _scaled([{"word": "w", "start": 1.0, "end": 2.0}], offset=3.0, k=2.0)[0]
    assert (moved["start"], moved["end"]) == (2.0, 2.5), moved
    # The lead is silence on the FINISHED track, so it must not be divided by
    # the speed-up along with the narration - half a whoosh of room is a whoosh
    # playing over the first word.
    led = _scaled([{"word": "w", "start": 1.0, "end": 2.0}], offset=3.0, k=2.0,
                  lead=0.7)[0]
    assert (led["start"], led["end"]) == (2.7, 3.2), led
    # and with nothing to make room for, nothing moves
    assert _scaled([{"start": 1.0, "end": 2.0}], k=1.0, lead=0.0)[0]["start"] == 1.0
    # ...and the filter really does shorten it by that factor, whatever rate the
    # take arrives at - the 24 kHz case is exactly what asetrate gets wrong when
    # it is not resampled first
    for rate in (24000, 44100):
        src = OUT_DIR / f"_speed_{rate}.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration=4:sample_rate={rate}",
                        str(src)], check=True)
        fast = OUT_DIR / f"_speed_{rate}_fast.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-af", _speedup(1.25).lstrip(","), str(fast)], check=True)
        assert abs(duration(fast) - duration(src) / 1.25) < 0.15, \
            f"{rate} Hz: {duration(src):.2f}s -> {duration(fast):.2f}s"
    print("speed-up ok")

    # a digit that reaches fish is read in English, so none may survive - and
    # the spelled form has to fit the phrase, or the fix trades an English
    # number for a Russian one in the wrong case
    if OUTPUT_LANG == "ru":
        for phrase, want in [
            ("после 5 минут", "после пяти минут"),
            ("без 2 минут полночь", "без двух минут полночь"),
            ("у 5 друзей", "у пяти друзей"),
            ("к 3 часам", "к трём часам"),
            ("между 2 домами", "между двумя домами"),
            ("20 000 рублей", "двадцать тысяч рублей"),
            ("2 машины", "две машины"),
            ("прошло 21 минута", "прошло двадцать одна минута"),
            # ambiguous prepositions stay nominative, which is the accusative
            # they actually want
            ("в 5 часов", "в пять часов"),
            ("за 2 недели", "за две недели"),
        ]:
            assert spell(phrase) == want, f"{phrase!r} -> {spell(phrase)!r}"
    assert not re.search(r"\d", spell("Мачеха: 5 минут, 20 000 руб., 1990 год")), \
        "digits must not reach the engine"
    assert spell("id 1234567890123456") == "id 1234567890123456", "absurd runs stay"

    # A cue must vanish completely on the way to the screen. Both defaults once
    # grew past TAG's 60-char ceiling and were narrated as visible text, so the
    # configured values - not just the shipped ones - get checked here.
    for cue in (FISH_TITLE_CUE, FISH_CTA_CUE, FISH_BODY_CUE):
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
    # sized against CUE_MAX rather than written out, or the fixture goes stale
    # the next time the ceiling moves - at 55 chars and CUE_MAX 80 this stopped
    # overflowing and quietly tested nothing
    huge = "[" + "x" * (CUE_MAX - len(", calm, flat") + 1) + "]"
    assert _cued(f"{huge} Реплика.", "[calm, flat]").startswith(huge)

    # The story's cue rides in on the SAME bracket as the model's lead, and that
    # bracket is read twice: by fish as delivery, by script.speakers() as who
    # says the next quoted line. speakers() takes the text before the first
    # comma, so a comma-joined merge onto a bare [emphasis] hands the next quote
    # to a speaker called "emphasis" and shifts every real speaker's colour
    # after it. The space join is what keeps the two readings apart.
    assert "," not in FISH_BODY_CUE, "a comma in the story cue invents a speaker"
    lead = _cued("[emphasis] Она вернула сорок тысяч. [aunt, proud] «Мои»",
                 FISH_BODY_CUE, " ")
    assert lead.count("[") == 2, f"the merge must not add a bracket: {lead}"
    assert not script.plain(FISH_BODY_CUE), "the story cue must strip clean"
    assert script.speakers(lead)[-1] == "aunt", script.speakers(lead)
    assert "emphasis" not in script.speakers(lead), script.speakers(lead)
    # a story that opens on a real speaker cue keeps that speaker
    named = _cued("[aunt, proud] «Мои деньги»", FISH_BODY_CUE, " ")
    assert script.speakers(named)[-1] == "aunt", script.speakers(named)
    # and one that opens on plain narration gets the cue as its own bracket
    assert _cued("Она вернула всё.", FISH_BODY_CUE, " ").startswith(FISH_BODY_CUE)
    # the comma join is still what the title and the question get
    assert _cued("[doubtful] Вопрос?", "[calm, flat]").startswith("[doubtful, calm, flat]")

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
