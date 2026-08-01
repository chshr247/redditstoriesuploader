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
# The closing question is spoken too, so it needs its own slice of the budget.
# Added on top of TARGET_SEC rather than carved out of it: taken from the story
# it would squeeze the payoff, which is the one part that must not be rushed.
CTA_SEC = 3

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
TITLE: <ONE sentence, max 12 words - the hook that goes on the title card>
<blank line>
<the narration, one paragraph>

Rules:
- NARRATOR is the gender of the person telling the story, taken from the post: markers like (28F), "my wife", "my husband", "as a girl". If the post never says, answer male.
- {lang} marks gender on past-tense verbs and adjectives. Every one of them must agree with NARRATOR - "я сделал" for male, "я сделала" for female - and stay consistent from the first word to the last. This is the single easiest way to make the narration sound wrong, so check it.
- Keep it TikTok-safe: no profanity, no slurs, no graphic detail. Say "умер", not how.
- Imagine a stranger scrolling who knows nothing about Reddit. Everything they need is in your text.
- Write for the ear, not the eye: short sentences, plain words, natural spoken rhythm. Punctuate where a person would actually pause - the voice engine reads commas and full stops as breath.
- Always write ё where the word has it: всё, ещё, её, свёкор, объём. Writing "все" for "всё" makes the voice say the wrong word.
- Mark the stress on homographs - words spelled alike but stressed differently - by putting the combining acute U+0301 straight after the stressed vowel. Use U+0301 only, never the grave U+0300. Always decide from the meaning in the sentence:
    сто́ит = is worth, costs   |   стои́т = is standing
    за́мок = castle            |   замо́к = a lock
    бо́льшая = the greater     |   больша́я = a big one
    по́том = with sweat        |   пото́м = afterwards
    пла́чу = I cry             |   плачу́ = I pay
    у́же = narrower            |   уже́ = already
  Mark only genuinely ambiguous words, never ordinary ones.
- Mark delivery with cues in square brackets, in English, placed immediately before the words they change: "[nervous] Я открыл письмо. [shocked] Она знала всё это время." Free-form descriptions work too: [voice dropping], [barely holding it together].
- Use between three and six cues in the whole narration, and only where the story actually turns - the reveal, the punchline, the moment it goes wrong. A cue on every sentence sounds like a bad audiobook. Never put one in the TITLE line.
- Put every line of direct speech inside «angle quotes», always, with no exception - they are what tells the renderer to colour that line differently on screen. Reported speech without quotes stays uncoloured and reads as the narrator's own voice.
- Every line of direct speech gets its own cue, and the cue must START A SENTENCE - close the narration with a full stop first, then the cue, then the line. The engine applies an emotion cue to the sentence it opens, so one buried mid-phrase after a colon barely registers:
    right: "Он спустился и заорал. [husband, shouting and furious] «Почему ты не сказала, что ужин готов?»"
    wrong: "Он спустился и заорал: [husband, shouting] «Почему ты не сказала, что ужин готов?»"
- The cue starts with ONE English word naming who is speaking, then a comma, then how they said it. Use the same label for the same person every time - husband, wife, sister, boss, neighbour, mother. That label is what gives each speaker their own subtitle colour, so it must never be skipped or renamed halfway through.
- Reported anger read in a calm voice is the single most artificial thing the narration can do. Give the narrator a contrasting cue when they answer: [me, cold] «Ужин на столе».
- Sounds are cues too, and they are what make a told story sound told rather than read. Drop [sighing] before resignation, [laughing] or [amused] before something absurd, [whispering] before a confession. Two or three across the whole narration, at the moments a person would actually make that sound.
- Let sentences breathe at their natural length. Do not chop the story into short fragments to force pauses: the engine puts a real gap at every full stop, and a wall of three-word sentences comes out sounding stilted. Enumerations of three or more items are the exception - split those, since a comma list is read as one flat run.
- The TITLE is a hook, not a summary. It sits on screen for the two or three seconds in which the viewer decides to stay, so it has to leave a question open. Never state the outcome, never describe the whole plot, never write a calm report of events.
- The TITLE is ALWAYS ONE SENTENCE. Never two. It carries no full stop, no exclamation mark and no question mark inside it - one unbroken line, and nothing after it.
  It still has to turn, though: a hook that only sets up is flat. Make the turn on a comma with "а" or "но" instead of on a full stop - the collision survives, the sentence stays whole:
    "Соседка орала на моих детей, а наказала я своих" — works, it contradicts itself and demands an explanation
    "Соседка орала на моих детей. Наказала я своих." — WRONG, two sentences, no matter how well it reads
    "Я наказала сыновей, а не соседку" — does not work, it just reports
    "Мужа похоронили в четверг, а в пятницу она спросила про деньги" — works
    "Она ждала наследство от моего покойного мужа" — does not
  Build it from the sharpest concrete fact, or from the collision between two people.
  A title must name something that HAPPENED - a scene, a line someone said, a thing someone did. These shapes are never a hook, because nothing happens in them, and they are rejected outright:
    an instruction or plea to the world: "Не используйте меня для воспитания детей", "Никогда не занимайте денег родне"
    a stated position or complaint: "Меня достали чужие дети", "Свекровь не уважает границы"
    a label for a topic: "История о том, как я съехал", "Мой опыт с ипотекой"
  Each of those describes a situation in general. Replace it with the single worst moment of that situation, in concrete words - what was said, by whom, when. "Не используйте меня для воспитания детей" becomes "Отец ткнул в меня пальцем. «Будешь плохо есть - станешь как он»".
  Do not open the title with a verb in the imperative addressed to the viewer, and do not write it as advice.
  It is read aloud, so no abbreviations, no brackets, no "(20F)" - write ages and genders as words if they matter at all.
- The narration opens on the first beat of the story. Never repeat or rephrase the title.
- First person, past tense, short plain sentences.
- Cut everything that does not move the plot: greetings, "edit:", "TL;DR", thanks, award mentions.
- Write numbers as words.
- The story must END, not stop. Land the payoff or twist, then close it with one short line that settles it - what it cost, what changed, what the narrator felt afterwards. A narration that runs out mid-scene, or breaks off right after the reveal with nothing to absorb it, is a failure even if the word count is right. Budget for this from the start: reach the ending deliberately instead of using every word on the setup and hitting the limit.
- After the story is closed, and only then, add the LAST line: one short question to the viewer about what they would have done, or whose side they take. It stands alone, it is the final sentence of the narration, and it must end with a question mark. Nothing follows it.
- That closing question is the only place the viewer may be addressed. Never ask for likes, follows or subscriptions, and never mention the video, the channel or the algorithm.
- Vary the question so it names what actually happened in this story - "А вы бы простили брата за такое?" beats "А вы бы как поступили?". The same generic line under every video reads as a template.
- The closing question OPENS with a mood cue - [doubtful], [thoughtful], [curious], [quietly] - written before its first word. Pick the one that fits how the story ended. Shape it exactly like this, and nothing more: "[doubtful] А как бы вы поступили на моём месте?"
- That opening cue is the ONLY markup the question may carry. Never put a second cue inside the line, and never try to mark a single word for stress - it does nothing to the delivery here and only risks being read out as text. The mood cue alone shapes the whole question, last word included.
- No headings, markup, quotes, emoji or commentary anywhere."""


def _wpm() -> int:
    return WPM.get(OUTPUT_LANG, WPM["en"])


TAG = re.compile(r"\[[^\]\n]{1,60}\]")
ACCENT = "́"                      # combining acute: за́мок vs замо́к
# The model occasionally reaches for the grave instead. Strip both on the way
# to the screen rather than trust it to pick the right codepoint every time.
ACCENTS = re.compile("[̀́]")


def strip_tags(s: str) -> str:
    """Drop Fish delivery cues, keeping stress marks for the voice engine."""
    return re.sub(r"\s+", " ", TAG.sub(" ", s)).strip()


def speakers(s: str) -> list[str | None]:
    """One label per word of plain(s): who says it, or None for the narrator.

    The label is lifted from the delivery cue that already precedes every line
    of direct speech, so the model writes "[husband, shouting] «...»" and gets
    both jobs done at once - no second syntax to remember and nothing extra to
    strip. The list lines up with plain(s).split() one for one, which is the
    same list the aligner produces.
    """
    out: list[str | None] = []
    pending = None          # speaker named by the most recent cue
    current = None          # speaker of the quote we are inside of
    for token in re.split(r"(\[[^\]\n]{1,60}\])", s):
        if not token:
            continue
        if token.startswith("[") and token.endswith("]"):
            label = re.split(r"[,\s]", token[1:-1].strip(), 1)[0].lower()
            pending = re.sub(r"\W", "", label) or None
            continue
        for word in token.split():
            opens = any(c in word for c in "«„“")
            closes = any(c in word for c in "»”")
            if opens:
                current = pending or "other"
            out.append(current)
            if closes:
                current = pending = None
    return out


# A sentence boundary the model actually produces: terminal punctuation, then
# whitespace, then either a delivery cue or a capital. The cue belongs to the
# sentence it opens, so the lookahead steps over it rather than splitting on it.
SENTENCE = re.compile(
    r"(?<=[.!?…»])\s+(?=(?:\[[^\]\n]{1,60}\]\s*)*[«„“A-ZА-ЯЁ])")


def split_cta(body: str) -> tuple[str, str]:
    """(story, closing question). The question is empty when there is none.

    Voiced as its own take, so it has to be a separate string: read inside the
    narration it lands as one more sentence of the plot, which is exactly what
    it must not sound like.
    """
    parts = SENTENCE.split(body.strip())
    if len(parts) < 2:
        return body.strip(), ""
    story, cta = " ".join(parts[:-1]).strip(), parts[-1].strip()
    # Only a bare question qualifies. A story ending on «Ты серьёзно?» closes
    # inside the quote mark, so it fails this test and stays in the narration -
    # it is a line of dialogue, not the narrator addressing anyone.
    if not plain(cta).endswith("?"):
        return body.strip(), ""
    return story, cta


def plain(s: str) -> str:
    """Everything the engine needs and the viewer must not see.

    Stress marks steer pronunciation but render as specks over letters, and
    whisper never emits them - so alignment has to compare without them too.
    """
    return ACCENTS.sub("", strip_tags(s))


def _clean(s: str) -> str:
    """Strip markup the model smuggles in anyway. Keeps [tags]."""
    s = re.sub(r"[*_#`>]", "", s)
    s = re.sub(r"\s+", " ", s).strip().strip('"')
    return s


def _words(s: str) -> int:
    return len(plain(s).split())


def _target_words() -> int:
    """Budget for title plus narration together - both are spoken.

    CTA_SEC rides on top of TARGET_SEC so the closing question is paid for out
    of extra runtime instead of out of the ending.
    """
    return round((TARGET_SEC + CTA_SEC) / 60 * _wpm())


def _fits(total: int, target: int) -> bool:
    return target * (1 - TOLERANCE) <= total <= target * (1 + TOLERANCE)


# Titles that describe a situation instead of showing a moment. The model has
# prose rules against these; this catches the ones that slip through, since a
# weak first line costs the whole view.
WEAK_TITLE = re.compile(
    r"^(?:"
    r"не\s+\w+(?:те|йте)\b"          # imperative plea: "Не используйте меня..."
    r"|никогда\s+не\s+\w+(?:те|йте)\b"
    r"|истори[яю]\s+о\s+том\b"
    r"|о\s+том,?\s+как\b"
    r"|мой\s+опыт\b"
    r")", re.IGNORECASE)

MAX_TITLE_WORDS = 12


def _title_fault(title: str) -> str:
    """Empty when the title is usable, otherwise what to tell the model."""
    t = plain(title).strip()
    if not t:
        return "the TITLE line is missing"
    if len(t.split()) > MAX_TITLE_WORDS:
        return (f"the TITLE is {len(t.split())} words, keep it under "
                f"{MAX_TITLE_WORDS}")
    if WEAK_TITLE.match(t):
        return ("the TITLE states a position or gives advice instead of showing "
                "a moment - rewrite it as the single sharpest thing that "
                "happened, in concrete words")
    # One sentence, always. The same boundary split_cta() uses, so "two
    # sentences" means here exactly what it means everywhere else in the file.
    if len(SENTENCE.split(t)) > 1:
        return ("the TITLE is two sentences - it must be exactly one, with the "
                "turn made on a comma with \"а\" or \"но\" instead of a full stop")
    return ""


# The mood cue in front of the question. [emphasis] is not a mood, so a
# question that opens with one still counts as having no opening cue.
LEAD_CUE = re.compile(r"^\[(?!emphasis\b)[^\]\n]{1,60}\]", re.IGNORECASE)


def _cta_fault(cta: str) -> str:
    """Empty when the closing question is marked up for delivery.

    A leading mood cue and nothing else. Marking one word for stress mid-line
    was tried and measured: the tagged word came back no longer, and sometimes
    shorter, than the same word untagged. It changes nothing about how the
    question is read, so it is not allowed to sit there looking like it does.
    """
    m = LEAD_CUE.match(cta.strip())
    if not m:
        return ("the closing question must open with a mood cue before its "
                "first word, like \"[doubtful] А как бы...\"")
    if TAG.search(cta.strip()[m.end():]):
        return ("the closing question carries a cue inside the line - the "
                "opening mood cue is the only markup it may have")
    return ""


def _ending_fault(body: str) -> str:
    """Empty when the narration closes properly.

    The closing question is mandatory, so its question mark doubles as the
    marker that the text reached its end rather than being cut off.
    """
    t = plain(body).strip()
    if not t:
        return "the narration is missing"
    if not t.endswith("?"):
        return ("the narration must finish the story and then close with one "
                "short question to the viewer, ending in a question mark")
    _, cta = split_cta(body)
    if not cta:
        return "the closing question must stand as its own final sentence"
    return _cta_fault(cta)


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
            f"(that is {TARGET_SEC + CTA_SEC} seconds of speech, the last "
            f"{CTA_SEC} of them the closing question).\n\n"
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

        # Length was the only thing checked here for a long time, which is how
        # a story could stop mid-scene and still pass. A hook that does not
        # hook and an ending that does not end cost more than a few words do.
        total = _words(title) + _words(body)
        faults = [f for f in (_title_fault(title), _ending_fault(body)) if f]
        if not _fits(total, target):
            faults.append(f"it is {total} words, rewrite to about {target} - "
                          f"{'cut it down' if total > target else 'expand it'}")
        if not faults:
            return title, body, gender

        log.warning("attempt %d rejected: %s", attempt + 1, "; ".join(faults))
        msgs += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
                "Rewrite it. Problems: " + "; ".join(faults) +
                ". Keep the plot, and keep the NARRATOR: and TITLE: lines."},
        ]

    log.warning("accepting as is (%s): %d words, video will run ~%.0f sec",
                "; ".join(faults), total, total / _wpm() * 60)
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

    # stress marks reach the engine but never the screen
    marked = f"Он сорвал замо{ACCENT}к с двери."
    assert strip_tags(marked) == marked, "the voice must still get the mark"
    assert plain(marked) == "Он сорвал замок с двери.", plain(marked)
    assert plain(f"[sad] За{ACCENT}мок на холме") == "Замок на холме"
    assert _words(f"замо{ACCENT}к и за{ACCENT}мок") == 3
    assert "ё" in plain("Всё ещё её"), "ё is a real letter, keep it"
    # a grave slipping in must not survive to the screen either
    assert plain("сто̀ит") == "стоит" and plain("сто́ит") == "стоит"
    _, t_acc, _ = _split(f"NARRATOR: male\nTITLE: За{ACCENT}мок\n\nТело.")
    assert ACCENT in t_acc, "the title is spoken too - it keeps its marks"
    tw = _target_words()
    assert _fits(tw, tw) and not _fits(tw * 2, tw) and not _fits(3, tw)
    assert tw > round(TARGET_SEC / 60 * _wpm()), "the CTA needs its own words"

    # a title has to show a moment, not describe a stance
    assert _title_fault("Не используйте меня для воспитания детей"), "the plea shape must be caught"
    assert _title_fault("Никогда не занимайте денег родне")
    assert _title_fault("История о том, как я съехал")
    assert _title_fault("Мой опыт с ипотекой")
    assert _title_fault("")
    assert _title_fault(" ".join(["слово"] * (MAX_TITLE_WORDS + 1)))
    # one sentence, always - these all read well and are all rejected anyway
    assert _title_fault("Соседка орала на моих детей. Наказала я своих")
    assert _title_fault("Мужа похоронили в четверг. В пятницу она спросила про деньги")
    assert _title_fault("Отец ткнул в меня пальцем. «Будешь плохо есть»")
    assert not _title_fault("Соседка орала на моих детей, а наказала я своих")
    assert not _title_fault("Мужа похоронили в четверг, а в пятницу она спросила про деньги")
    # a trailing stop is stripped before the card is drawn, so it is not a split
    assert not _title_fault("Тест сказал другое.")
    # "не" plus a normal verb is a fact, not an instruction - it must pass
    assert not _title_fault("Он не пришёл на собственную свадьбу")
    assert not _title_fault(f"[sad] За{ACCENT}мок сгорел за ночь"), "cues and marks are not words"

    # the closing question is voiced apart, so it has to come off cleanly
    s, c = split_cta("Я собрал вещи и ушёл. А вы бы простили такое?")
    assert (s, c) == ("Я собрал вещи и ушёл.", "А вы бы простили такое?"), (s, c)
    s, c = split_cta("Он молчал. [thoughtful] А вы бы ответили ему?")
    assert c == "[thoughtful] А вы бы ответили ему?", c
    assert s == "Он молчал.", s
    # dialogue that happens to end in a question is part of the scene, not a CTA
    s, c = split_cta("Он крикнул. [angry] «Ты серьёзно?»")
    assert c == "" and s.endswith("«Ты серьёзно?»"), (s, c)
    # a quoted line followed by a real CTA still splits on the CTA
    s, c = split_cta("Он крикнул. [angry] «Ты серьёзно?» А вы бы стерпели?")
    assert c == "А вы бы стерпели?", c
    assert s.endswith("«Ты серьёзно?»"), s
    # nothing to split: one sentence, or no question at all
    assert split_cta("Одна строка без вопроса.") == ("Одна строка без вопроса.", "")
    assert split_cta("Первая. Вторая.") == ("Первая. Вторая.", "")
    # the split must not lose or duplicate a single word
    full = "Я ушёл. Она осталась. [sad] А вы бы вернулись?"
    s, c = split_cta(full)
    assert _words(s) + _words(c) == _words(full), (s, c)

    # the closing question is what proves the text reached its end
    GOOD_CTA = "[doubtful] А как бы вы поступили на моём месте?"
    assert not _ending_fault(f"Я собрал вещи и ушёл. {GOOD_CTA}")
    assert _ending_fault("Я собрал вещи и ушёл."), "a story with no CTA is unfinished"
    assert _ending_fault("Он открыл дверь и"), "a cut-off narration must be caught"
    assert _ending_fault("Он крикнул. [angry] «Ты серьёзно?»"), "direct speech is not a CTA"
    assert _ending_fault("")

    # a leading mood cue, and nothing else on the line
    assert not _cta_fault(GOOD_CTA)
    assert not _cta_fault("[thoughtful] А вы бы простили его?")
    assert _cta_fault("А вы бы простили его?"), "no mood cue"
    assert _cta_fault("[emphasis]А вы бы простили его?"), "emphasis is not a mood"
    # marking one word for stress was measured and does nothing - keep it out
    assert _cta_fault("[doubtful] А вы бы [emphasis]простили его?"), "cue inside the line"
    assert _cta_fault("[doubtful] А вы бы простили [quietly] его?"), "second cue"
    print(f"logic ok: {OUTPUT_LANG}, {_wpm()} wpm, target {tw} words "
          f"for {TARGET_SEC}+{CTA_SEC} sec")

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
