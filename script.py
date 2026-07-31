"""Step 2: raw Reddit post -> title card text + narration, sized for TARGET_SEC.

Output is bare prose with no markup: whatever this returns, edge-tts reads
out loud verbatim. No "**", no emoji, no stage directions.

Title and body come from one call. The source is English and the narration
usually is not, so the title needs translating too - and a model that writes
both at once keeps them in one voice, for one request instead of two.
"""
import logging
import re

from openai import OpenAI

import safety
from config import (LLM_BASE_URL, LLM_MODEL, OPENAI_API_KEY, OUTPUT_LANG,
                    TARGET_SEC)


class Unsuitable(Exception):
    """The post cannot become a video. Raised instead of returning junk."""

# Measured with `python voice.py`. Russian runs far slower than English on the
# same voice, so this MUST be re-measured whenever TTS_VOICE or OUTPUT_LANG
# changes - the whole word budget hangs off it.
WPM = {"ru": 150, "en": 191}
TOLERANCE = 0.15

LANG_NAME = {"ru": "Russian", "en": "English"}

log = logging.getLogger(__name__)

SYSTEM = """You turn Reddit stories into narration for vertical short videos (TikTok/Shorts).

Write everything in {lang}. The source post is in English - translate and adapt it,
do not transliterate. Use natural spoken {lang}, not a literal word-for-word rendering.

First decide whether the post works at all. Answer with exactly

SKIP: <short reason>

and nothing else if any of these is true:
- it is not a personal story: news, politics, activism, a call to action, a question, a meta post about Reddit itself
- nothing actually happens, or the payoff is a shrug
- it only works if you already know the subreddit, a meme, or Reddit slang
- it continues an earlier post the viewer never saw
- it needs specialist knowledge to land: a game, a fandom, professional jargon
- it cannot be told safely on TikTok: it centres on nazism or hate symbols, sexual violence, harm to children, suicide or self-harm, graphic violence, or hard drugs

The bar for keeping it: a stranger scrolling past, who has never heard of Reddit,
recognises the situation from their own life - family, money, work, neighbours,
dating, petty injustice - and wants to know how it ended. Everything else is a SKIP.
Be strict. Passing on a mediocre story costs nothing; a boring video costs a viewer.

Softening a story is not your job. If it needs sanitising to be postable, SKIP it.

Otherwise answer in exactly this shape:

NARRATOR: male or female
TITLE: <one line, max 12 words - the hook that goes on the title card>
<blank line>
<the narration, one paragraph>

Rules:
- NARRATOR is the gender of the person telling the story, taken from the post: markers like (28F), "my wife", "my husband", "as a girl". If the post never says, answer male.
- {lang} marks gender on past-tense verbs and adjectives. Every one of them must agree with NARRATOR - "я сделал" for male, "я сделала" for female - and stay consistent from the first word to the last. This is the single easiest way to make the narration sound wrong, so check it.
- Keep it TikTok-safe: no profanity, no slurs, no graphic detail. Say "умер", not how.
- Imagine a stranger scrolling who knows nothing about Reddit. Everything they need is in your text.
- Write for the ear, not the eye: short sentences, plain words, natural spoken rhythm. Punctuate where a person would actually pause - the voice engine reads commas and full stops as breath.
- Mark delivery with cues in square brackets, in English, placed immediately before the words they change: "[nervous] Я открыл письмо. [shocked] Она знала всё это время." Free-form descriptions work too: [voice dropping], [barely holding it together].
- Use between three and six cues in the whole narration, and only where the story actually turns - the reveal, the punchline, the moment it goes wrong. A cue on every sentence sounds like a bad audiobook. Never put one in the TITLE line.
- The title states what happened without giving away the ending. It is read aloud, so no abbreviations, no brackets, no "(20F)" - write ages and genders as words if they matter at all.
- The narration opens on the first beat of the story. Never repeat or rephrase the title.
- First person, past tense, short plain sentences.
- Cut everything that does not move the plot: greetings, "edit:", "TL;DR", thanks, award mentions.
- Write numbers as words.
- End on the payoff or twist. No calls to subscribe, no addressing the viewer.
- No headings, markup, quotes, emoji or commentary anywhere."""


def _wpm() -> int:
    return WPM.get(OUTPUT_LANG, WPM["en"])


TAG = re.compile(r"\[[^\]\n]{1,60}\]")


def strip_tags(s: str) -> str:
    """Drop Fish delivery cues. Anything the viewer reads goes through this."""
    return re.sub(r"\s+", " ", TAG.sub(" ", s)).strip()


def _clean(s: str) -> str:
    """Strip markup the model smuggles in anyway. Keeps [tags]."""
    s = re.sub(r"[*_#`>]", "", s)
    s = re.sub(r"\s+", " ", s).strip().strip('"')
    return s


def _words(s: str) -> int:
    return len(strip_tags(s).split())


def _target_words() -> int:
    """Budget for title plus narration together - both are spoken."""
    return round(TARGET_SEC / 60 * _wpm())


def _fits(total: int, target: int) -> bool:
    return target * (1 - TOLERANCE) <= total <= target * (1 + TOLERANCE)


def guess_gender(post: dict) -> str:
    """Fallback when the model forgets the tag: Reddit's own (28F) / (25M) markers."""
    m = re.search(r"\b\d{1,2}\s*([MFmf])\b", f"{post.get('title', '')} {post.get('text', '')}")
    return "female" if m and m.group(1).lower() == "f" else "male"


def _split(raw: str, fallback_gender: str = "male") -> tuple[str, str, str]:
    """Pull NARRATOR: and TITLE: off the front. Returns (gender, title, body)."""
    g = re.search(r"NARRATOR:\s*(male|female)", raw, re.IGNORECASE)
    gender = g.group(1).lower() if g else fallback_gender
    if not g:
        log.warning("no NARRATOR: tag, falling back to %s", fallback_gender)

    m = re.search(r"TITLE:\s*(.+)", raw)
    if m:
        title = m.group(1).splitlines()[0]
        body = raw[m.end(1):]
    else:
        # drop the NARRATOR line if it is there, then take the first line as title
        rest = raw[g.end():] if g else raw
        head, _, body = rest.strip().partition("\n")
        title = head
        log.warning("no TITLE: tag, taking the first line")
    # the title is burned on screen, so it must never carry a cue; the body
    # keeps them and gets stripped later, once, for subtitles and alignment
    return gender, strip_tags(_clean(title)).rstrip(" .,:;-"), _clean(body)


def write_script(post: dict) -> tuple[str, str, str]:
    """(title, narration, gender) filling TARGET_SEC. One retry to fix the length."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty - fill in .env")

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    target = _target_words()
    msgs = [
        {"role": "system", "content": SYSTEM.format(lang=LANG_NAME.get(OUTPUT_LANG, "English"))},
        {"role": "user", "content":
            f"Title and narration together must total about {target} words "
            f"(that is {TARGET_SEC} seconds of speech).\n\n"
            f"Title: {post['title']}\n\nBody:\n{post['text']}"},
    ]

    for attempt in range(2):
        resp = client.chat.completions.create(model=LLM_MODEL, messages=msgs)
        # DeepSeek caches the constant prefix automatically - SYSTEM stays first
        # so it hits every call. These counters are how you confirm it.
        u = resp.usage
        log.info("tokens: %d in (%d cached), %d out", u.prompt_tokens,
                 getattr(u, "prompt_cache_hit_tokens", 0), u.completion_tokens)

        raw = resp.choices[0].message.content
        skip = re.match(r"\s*SKIP:\s*(.*)", raw)
        if skip:
            raise Unsuitable(skip.group(1).strip()[:120] or "no reason given")

        gender, title, body = _split(raw, guess_gender(post))
        # the model can introduce what the source did not have, so re-check
        hit = safety.blocked(title, body)
        if hit:
            raise Unsuitable(f"generated text tripped the blocklist ({hit})")

        total = _words(title) + _words(body)
        if title and body and _fits(total, target):
            return title, body, gender

        log.warning("attempt %d: %d words instead of %d", attempt + 1, total, target)
        msgs += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
                f"That is {total} words. Rewrite to about {target}, "
                f"{'cut it down' if total > target else 'expand it'} - keep the plot "
                f"and keep the NARRATOR: and TITLE: lines."},
        ]

    log.warning("accepting as is: %d words, video will run ~%.0f sec",
                total, total / _wpm() * 60)
    return title, body, gender


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    assert _clean('**Well**, I  *did*\n\nit.') == "Well, I did it."
    assert _clean('"in quotes"') == "in quotes"
    g, t, b = _split("NARRATOR: female\nTITLE: Как я всё испортила\n\nЯ думала, это хорошая идея.")
    assert (g, t) == ("female", "Как я всё испортила"), (g, t)
    assert b == "Я думала, это хорошая идея.", b
    g2, t2, b2 = _split("TITLE: Как я всё испортил\n\nЯ думал, это идея.")
    assert g2 == "male" and t2 == "Как я всё испортил"
    g3, t3, b3 = _split("NARRATOR: female\nПросто первая строка\nи тело.")
    assert (g3, t3, b3) == ("female", "Просто первая строка", "и тело."), (g3, t3, b3)
    assert guess_gender({"title": "TIFU by ignoring my (28F) sister"}) == "female"
    assert guess_gender({"title": "TIFU by losing my keys"}) == "male"

    # cues must survive in the body and vanish from the title and the word count
    assert strip_tags("[nervous] Я открыл. [shocked] Она знала.") == "Я открыл. Она знала."
    assert strip_tags("[voice dropping, almost a whisper] Всё.") == "Всё."
    assert strip_tags("без тегов") == "без тегов"
    assert _words("[nervous] раз два три") == 3, "cues must not eat the word budget"
    gt, tt, bt = _split("NARRATOR: male\nTITLE: [sad] Мой заголовок\n\n[calm] Тело истории.")
    assert tt == "Мой заголовок", tt
    assert "[calm]" in bt, "body must keep its cues"
    tw = _target_words()
    assert _fits(tw, tw) and not _fits(tw * 2, tw) and not _fits(3, tw)
    print(f"logic ok: {OUTPUT_LANG}, {_wpm()} wpm, target {tw} words for {TARGET_SEC} sec")

    if OPENAI_API_KEY:
        import source
        post = source.fetch(1)[0]
        title, body, gender = write_script(post)
        print(f"\n--- r/{post['sub']} [{post['score']}] {post['title']}\n")
        print(f"NARRATOR: {gender}\nTITLE: {title}")
        print("\n" + body)
        n = _words(title) + _words(body)
        print(f"\n{n} words -> ~{n / _wpm() * 60:.0f} sec")
    else:
        print("OPENAI_API_KEY not set - live run skipped")
