"""Listen to several fish voices read the same thing, side by side.

Not part of the pipeline and imported by nothing - a bench tool. Voice choice
is the one thing in this repo with no test that can decide it, and until now
there was no way to compare two candidates except to render two whole videos.

    python audition.py                      # the ids already configured
    python audition.py <id> <id> ...        # candidates from fish.audio/m/<id>

Writes out/audition_<id>.mp3, one per voice, same text and same cue as a real
story - a voice auditioned on a bare sentence is a different voice from the one
that has to carry a cue and a line of dialogue.
"""
import logging
import sys

import script
import voice
from config import FISH_BODY_CUE, FISH_VOICES_FEMALE, FISH_VOICES_MALE, OUT_DIR

# Long enough to hear the drift a single sentence hides, and built to need
# every trick the real narration needs: a peak, a turn, a quoted line and a
# question. A flat voice gives itself away on the quote.
SAMPLE = {
    "ru": ("Свекровь позвонила в семь утра и сказала, что заедет на минуту. "
           "[emphasis] Через час она стояла в коридоре с двумя чемоданами. "
           "Я спросила, надолго ли это. [свекровь, весело] «Пока не привыкнете» "
           "Муж молчал и смотрел в пол. Тогда я поняла, что он знал заранее."),
    "en": ("My mother-in-law called at seven and said she would drop by for a "
           "minute. [emphasis] An hour later she was in the hall with two "
           "suitcases. I asked how long this was for. [mother-in-law, cheerful] "
           "“Until you get used to it” My husband said nothing and looked at "
           "the floor. That was when I knew he had known all along."),
}


def main(ids: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ids = ids or FISH_VOICES_MALE + FISH_VOICES_FEMALE
    if not ids:
        sys.exit("no voice ids - pass some, or fill FISH_VOICES_* in .env")

    from config import OUTPUT_LANG
    text = voice._cued(SAMPLE.get(OUTPUT_LANG, SAMPLE["ru"]), FISH_BODY_CUE, " ")
    print(f"{len(ids)} voice(s), {len(script.plain(text))} plain chars\n")
    for vid in ids:
        out = OUT_DIR / f"audition_{vid[:8]}.mp3"
        # _fish_synth, not speak(): the timings are what whisper is for, and
        # nothing here is going on screen. Auditioning ten voices through the
        # full path would run whisper ten times for output nobody reads.
        voice._fish_synth(voice.spell(text), out, 1.0, vid)
        print(f"  {out.name}  {voice.duration(out):5.1f}s   {vid}")
    print(f"\nlisten in {OUT_DIR}, then put the winners in FISH_VOICES_MALE")


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if a.strip()])
