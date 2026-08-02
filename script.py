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
TITLE: <ONE sentence, six to nine words - the biggest fact of the story, for the title card>
<blank line>
<the narration, one paragraph>

Rules:
- NARRATOR is the gender of the person telling the story, taken from the post: markers like (28F), "my wife", "my husband", "as a girl". If the post never says, answer male.
- {lang} marks gender on past-tense verbs and adjectives. Every one of them must agree with NARRATOR - "я сделал" for male, "я сделала" for female - and stay consistent from the first word to the last. This is the single easiest way to make the narration sound wrong, so check it.
- Keep it TikTok-safe: no profanity, no slurs, no graphic detail. Say "умер", not how.
- Imagine a stranger scrolling who knows nothing about Reddit. Everything they need is in your text.
- Write for the ear, not the eye: short sentences, plain words, natural spoken rhythm. Punctuate where a person would actually pause - the voice engine reads commas and full stops as breath.
- Always write ё where the word has it: всё, ещё, её, свёкор, объём. Writing "все" for "всё" makes the voice say the wrong word.
- {lang} has homographs: same letters, different stress, different meaning. There are hundreds and no list covers them, so learn the SHAPES they come in rather than memorising words. These are examples of each shape, not an inventory:
    two unrelated words that collided: замок, мука, духи, полки, белки, вести, пропасть, хлопок, вина, село
    genitive singular against nominative plural - the largest group by far: стены, руки, ноги, горы, окна, слова, дома, стороны, деньгами
    forms of one verb: плачу, лечу, ношу, сушу, кошу, солю, целую
    aspect pairs differing in nothing but stress: насыпать, отрезать, разрезать, ссыпать
    a short adjective against a comparative or another adjective: большая, дорога, острота
  The voice engine guesses the stress from the surrounding words and there is no way to correct it: a wrong guess is heard as a different word, and no markup fixes it - accent marks are ignored outright, so never write one anywhere.
  So before you commit any word of this kind, read your own sentence back with the OTHER stress. If it still makes sense that way, the sentence is broken and you must rewrite it. Two ways out, in this order:
    let the context decide, which is enough most of the time: "во дворе стоит машина" leaves no room for "costs", "это стоит слишком дорого" leaves none for "stands"
    or take a different word: "он запер дверь" instead of "он повесил замок", "дороже" instead of "больше стоит", "позже" instead of "потом"
  Short sentences are where this bites, because a three-word sentence gives the engine nothing to go on. That is the one place worth spending an extra word.
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
- The TITLE names ONE fact: the single biggest thing that happens anywhere in the story, stated in full, in the bluntest words the story allows. What it withholds is the CAUSE and the outcome - how that came about and how it ended is what the video is for. Never withhold the event itself. A title that hides what happened reads as nothing having happened, and a stranger scrolls past it.
- Keep it SHORT: six to nine words. The feed cuts the title off after roughly forty characters and the fact has to survive that cut whole, so who did what sits at the front and nothing load-bearing waits until the end.
- The TITLE is ALWAYS ONE SENTENCE. Never two. It carries no full stop, no exclamation mark and no question mark inside it - one unbroken line, and nothing after it.
- It must rest on at least ONE of these three. Without one the fact stays abstract and the title is dead on the screen:
    a digit, and the more absurd its size the better: "Брат продал кольцо покойной бабушки за 5000"
    a line someone actually said, in «angle quotes»: "Свекровь сказала при гостях «этот ребёнок не наш»"
    the named stake instead of its category: "кольцо покойной бабушки", never "украшение"
  Working titles, for shape:
    "Соседка прислала мне счёт на 80000 за свой потоп"
    "Тёща въехала в нашу квартиру, пока мы были в отпуске"
    "Отец вычеркнул меня из завещания после одного ужина"
    "Мать сняла с моей карты 40000 на футбол брата"
  The same stories written wrong, because the event is hidden:
    "Тёща жила в нашей квартире" — nothing happened, so there is nothing to watch
    "У нас были сложности с соседкой" — a topic, not a fact
    "Соседка орала на моих детей. Наказала я своих." — WRONG, two sentences, no matter how well it reads
- The verb must be what the post actually says. Escalating "не отдала ключи" into "украла ключи" buys the click and loses the viewer at the eighth second, when the narration turns out to be smaller than the title promised.
  A title must name something that HAPPENED - a scene, a line someone said, a thing someone did. These shapes are never a hook, because nothing happens in them, and they are rejected outright:
    an instruction or plea to the world: "Не используйте меня для воспитания детей", "Никогда не занимайте денег родне"
    a stated position or complaint: "Меня достали чужие дети", "Свекровь не уважает границы"
    a label for a topic: "История о том, как я съехал", "Мой опыт с ипотекой"
  Each of those describes a situation in general. Replace it with the single worst moment of that situation, in concrete words - what was said, by whom, when, and all of it inside one sentence. "Не используйте меня для воспитания детей" becomes "Отец ткнул в меня пальцем и сказал «будешь как он»".
  Do not open the title with a verb in the imperative addressed to the viewer, and do not write it as advice.
  It is read aloud, so no abbreviations, no brackets, no "(20F)" - write genders as words if they matter at all.
  Numbers in the TITLE are DIGITS, always, and they carry NO currency: "счёт на 8000", never "8000 рублей" and never "800 долларов". A digit is the one thing the eye catches while scrolling; "восемьсот" is read as just more text, and the currency word is three syllables spent pushing the fact past the cut. Write the figure out in full - "20000", never "20 тысяч". Do not convert a sum into another currency either: drop the unit and keep the number the source gives.
- The narration ANSWERS the title inside its first two sentences - how the thing in the title came about, or what it turned out to be. The title is a promise and the viewer is sitting on it; everything else can wait, that answer cannot. Do not repeat the title word for word, answer it.
- First person, past tense, short plain sentences.
- Cut everything that does not move the plot: greetings, "edit:", "TL;DR", thanks, award mentions.
- In the narration write numbers as words. Only the TITLE uses digits.
- The story must END, not stop. Land the payoff or twist, then close it with one short line that settles it - what it cost, what changed, what the narrator felt afterwards. A narration that runs out mid-scene, or breaks off right after the reveal with nothing to absorb it, is a failure even if the word count is right. Budget for this from the start: reach the ending deliberately instead of using every word on the setup and hitting the limit.
- After the story is closed, and only then, add the LAST line: one short question to the viewer about what they would have done, or whose side they take. It stands alone, it is the final sentence of the narration, and it must end with a question mark. Nothing follows it.
- That closing question is the only place the viewer may be addressed. Never ask for likes, follows or subscriptions, and never mention the video, the channel or the algorithm.
- Vary the question so it names what actually happened in this story - "А вы бы простили брата за такое?" beats "А вы бы как поступили?". The same generic line under every video reads as a template.
- The closing question OPENS with a mood cue - [doubtful], [thoughtful], [curious], [quietly] - written before its first word. Pick the one that fits how the story ended. Shape it exactly like this, and nothing more: "[doubtful] А как бы вы поступили на моём месте?"
- That opening cue is the ONLY markup the question may carry. Never put a second cue inside the line. The mood cue alone shapes the whole question, last word included.
- No headings, markup, quotes, emoji or commentary anywhere."""

# Appended to SYSTEM when one post is worth more than one video. Kept apart so
# the single-video prompt - which is what most runs use - stays byte-identical
# and keeps hitting the provider's prefix cache.
MULTI = """

This post carries more story than one video holds. Write it as EXACTLY {n} parts,
all of them in one answer, separated by a line containing only three dashes:

NARRATOR: male or female
TITLE: <part 1 title>
<blank line>
<part 1 narration>
---
TITLE: <part 2 title>
<blank line>
<part 2 narration>

NARRATOR is written once, at the very top, and holds for every part.
Every rule above applies to EACH part on its own - its own title, its own cues,
its own closing question, its own length. On top of them:
- Cut where the story turns, never where the words run out. Each part covers a
  whole stretch of events; one that stops in the middle of a scene is a failure.
- Never write "Часть 1" or any part number anywhere in your text. The marker is
  added outside it.
- Part 1 gives a stranger everything they need. Each later part opens with ONE
  short line saying where things stopped, then carries on - whoever is watching
  may never have seen the part before it.
- Each part's TITLE names the biggest fact of THAT part and never how it turns
  out. This is the same rule as above and it is the one most easily lost on the
  parts after the first, where the obvious title is the payoff:
    "Официантка вынесла клиенту весы прямо в зал" — works
    "Весы показали полный килограмм, и клиент ушёл ни с чем" — WRONG, the
    whole reason to watch is gone before the video starts
  Most of the people who open a later part came from the one before it. Giving
  them the answer in the title is telling them not to bother.
- Every part except the last ends on a cliffhanger: stop one beat BEFORE the
  answer, at the moment the outcome is about to land, on a finished sentence.
  The stop IS the cliffhanger. So these parts carry NO closing question and no
  line addressed to the viewer at all. This OVERRIDES the rule above that every
  narration ends with a question to the viewer - that rule is for the last part.
    right:  "...Мусоровоз медленно подъезжал к дому соседа. И тут я замерла."
    wrong:  "...И тут я замерла. [curious] Как думаете, что было дальше?"
  Do not swap the question for a rhetorical one of the narrator's either
  ("Что мне оставалось делать?"). End on what happened, and stop there.
- The LAST part resolves everything and closes the way described above: the
  payoff, one short settling line, then the question to the viewer.
- Do not stall to fill the count. Every part needs events of its own, and two
  parts of real story beat three parts of waiting. If the post cannot carry {n}
  full parts, write fewer - but never fewer than two."""

# A separator line the model actually produces, and nothing else: the prompt
# bans markup, so a bare rule can only be the one we asked for.
PART_SEP = re.compile(r"^\s*-{3,}\s*$", re.M)

# Source characters worth one video. A 75-second narration is ~195 Russian
# words, and an English source spends fewer characters on the same events than
# the retelling does, so the threshold sits above the narration's own length.
# source.py never fetches past MAX_CHARS = 4000, which is where MAX_PARTS lands.
PART_CHARS = 1800
MAX_PARTS = 3


def part_count(post: dict) -> int:
    """How many videos this post is worth, by length of the source alone.

    A guess, made before spending an LLM call: the model still gets to answer
    with fewer parts if the material is thinner than the character count says.
    """
    return min(len(post["text"]) // PART_CHARS + 1, MAX_PARTS)


def _wpm() -> int:
    return WPM.get(OUTPUT_LANG, WPM["en"])


TAG = re.compile(r"\[[^\]\n]{1,60}\]")
ACCENT = "́"                      # combining acute: за́мок vs замо́к
# Measured against Fish on 2026-08-02: the engine ignores the mark completely -
# "за́мок" and "замо́к" synthesize identically. The prompt no longer asks for
# them, so this is a net, not a feature: a mark the model writes out of habit
# would otherwise reach the screen as a speck over the letter. Grave included,
# since the model reached for the wrong codepoint often enough to matter.
ACCENTS = re.compile("[̀́]")


def strip_tags(s: str) -> str:
    """Drop Fish delivery cues. Accents, if any, are left to plain()."""
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

    Accents steer nothing - the engine ignores them - but they render as specks
    over letters, and whisper never emits them, so alignment has to compare
    without them too.
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

# Nine is what the prompt asks for; ten is the slack. A title states one fact
# now, and a fact that needs eleven words is carrying its own explanation - which
# is the story's job, not the card's. Past this the title also stops fitting the
# feed's ~40-character cut, which is the whole reason it got short.
MAX_TITLE_WORDS = 10

# Numbers spelled out in the title: "восемьсот долларов" where "800 долларов"
# belongs. Full words only - a stem like "пят" would fire on "пятница".
# ponytail: tens and up only. Numerals below twenty share stems with ordinary
# words and are short enough to read fine either way, so they are left alone;
# extend the list if "пять тысяч" style titles start slipping through as prose.
SPELLED_NUMBER = re.compile(
    r"\b(?:двадцат[иь]|тридцат[иь]|сорока?|пятьдесят|пятидесяти|шестьдесят"
    r"|шестидесяти|семьдесят|семидесяти|восемьдесят|восьмидесяти|девяносто"
    r"|девяноста|ст[оа]|двести|двухсот|тр[ие]ста|тр[ёе]хсот|четыреста"
    r"|четыр[ёе]хсот|пятьсот|пятисот|шестьсот|шестисот|семьсот|семисот"
    r"|восемьсот|восьмисот|девятьсот|девятисот|тысяч\w*|миллион\w*"
    r"|миллиард\w*)\b", re.IGNORECASE)

# The currency word after a figure. It buys nothing - the sum is the hook, not
# the unit - and costs three syllables of the ~40 characters the feed shows.
CURRENCY = re.compile(r"\d[\d\s]{0,8}(?:рубл|руб\b|долл|евро|₽)|[$₽]\s*\d", re.IGNORECASE)

# Words whose stress the voice engine has to guess, and guesses from context the
# title does not have: a six-word line gives it nothing to go on, so a wrong
# guess is heard as a different word in the first three seconds of the video.
# ponytail: a hand-picked list, not a dictionary. These are the ones whose other
# reading is a plausible word in a story title; the pairs that only collide in
# rare grammar (руки, окна, дома) are left out because they would fire on
# everything. Add to it when a video actually comes back mispronounced.
HOMOGRAPH = re.compile(
    r"\b(?:замок|замка|мука|духи|полки|белки|пропасть|хлопок|село"
    r"|дорога|стоит|плачу|лечу|ношу|острота)\b", re.IGNORECASE)


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
    n = SPELLED_NUMBER.search(t)
    if n:
        return (f"the TITLE spells the number \"{n.group()}\" out in letters - "
                "write the whole figure in digits, \"20000\" not \"20 тысяч\"")
    c = CURRENCY.search(t)
    if c:
        return (f"the TITLE names a currency in \"{c.group().strip()}\" - drop "
                "the unit and keep the bare figure, \"счёт на 8000\"")
    h = HOMOGRAPH.search(t)
    if h:
        return (f"\"{h.group()}\" is stressed two ways and the TITLE is too short "
                "for the engine to guess right - say it with a different word")
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


def _ending_fault(body: str, final: bool = True) -> str:
    """Empty when the narration closes properly.

    For an ordinary video, and for the LAST part of a split story, the closing
    question is mandatory - so its question mark doubles as the marker that the
    text reached its end instead of being cut off mid-scene.

    A part that is not the last one has no question: it stops one beat before
    the answer, and that stop is the cliffhanger. Adding a question on top only
    restates it, and in practice it came out as "Как думаете, что было дальше?"
    - the generic shape the rules reject everywhere else. That costs the
    question mark as an end marker, so the weaker test stands in: terminal
    punctuation, which still catches the failure it was put there for ("Он
    открыл дверь и"). What it cannot catch is a part that stops because the
    words ran out rather than on purpose - for a middle part those two look
    alike from the outside, and only the prompt can tell them apart.
    """
    t = plain(body).strip()
    if not t:
        return "the narration is missing"

    if not final:
        if split_cta(body)[1]:
            return ("this part is not the last one, so it must NOT end with a "
                    "question to the viewer - stop one beat before the answer "
                    "and let the stop be the cliffhanger")
        if t[-1] not in ".!…»":
            return ("the part breaks off in the middle of a sentence - stop "
                    "before the answer, but finish the sentence you are on")
        return ""

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


def _parse_parts(raw: str, post: dict, parts: int,
                 target: int) -> tuple[str, list[tuple[str, str]], list[str]]:
    """(gender, [(title, body), ...], complaints) for one model answer.

    Every part is checked on its own and the complaints are labelled, so a
    rewrite request names the part that is wrong instead of the whole answer.
    """
    chunks = [c for c in PART_SEP.split(raw) if c.strip()] if parts > 1 else [raw]
    faults = []
    # Fewer parts than asked is allowed down to two - the prompt tells the model
    # to write fewer rather than stall, and a thin third part is worse than none.
    if not (2 <= len(chunks) <= parts if parts > 1 else len(chunks) == 1):
        faults.append(f"you wrote {len(chunks)} parts - write between 2 and "
                      f"{parts}, separated by a line of three dashes")
    # counted before this, so writing four parts when asked for three is a
    # complaint rather than a silent truncation
    chunks = chunks[:parts]

    gender, out = "", []
    for i, chunk in enumerate(chunks, 1):
        g, title, body = _split(chunk, gender or guess_gender(post))
        gender = gender or g
        # the model can introduce what the source did not have, so re-check
        hit = safety.blocked(title, body)
        if hit:
            raise Unsuitable(f"generated text tripped the blocklist ({hit})")

        # Length was the only thing checked here for a long time, which is how
        # a story could stop mid-scene and still pass. A hook that does not
        # hook and an ending that does not end cost more than a few words do.
        # Only the last part closes with a question to the viewer; the ones
        # before it end on the cliffhanger itself.
        label = f"part {i}: " if parts > 1 else ""
        faults += [label + f for f in
                   (_title_fault(title), _ending_fault(body, final=i == len(chunks)))
                   if f]
        total = _words(title) + _words(body)
        if not _fits(total, target):
            faults.append(f"{label}it is {total} words, rewrite to about {target} - "
                          f"{'cut it down' if total > target else 'expand it'}")
        out.append((title, body))
    return gender or guess_gender(post), out, faults


def write_script(post: dict, parts: int = 1) -> tuple[str, list[tuple[str, str]]]:
    """(gender, [(title, narration), ...]) - one entry per video, each TARGET_SEC.

    parts > 1 splits the post across that many videos in a SINGLE call: the
    model needs the whole plot in front of it to choose where the cuts fall and
    to end each part on purpose rather than wherever the budget ran out.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty - fill in .env")

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    target = _target_words()
    system = SYSTEM.format(lang=LANG_NAME.get(OUTPUT_LANG, "English"))
    if parts > 1:
        system += MULTI.format(n=parts)
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"{'Each part' if parts > 1 else 'The'} title and narration together "
            f"must total about {target} words, which is "
            f"{TARGET_SEC + CTA_SEC} seconds of speech - the last {CTA_SEC} of "
            f"them the closing question"
            + (", which only the final part has.\n\n" if parts > 1 else ".\n\n")
            + f"Title: {post['title']}\n\nBody:\n{post['text']}"},
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

        gender, written, faults = _parse_parts(raw, post, parts, target)
        if not faults:
            return gender, written

        log.warning("attempt %d rejected: %s", attempt + 1, "; ".join(faults))
        msgs += [
            {"role": "assistant", "content": raw},
            {"role": "user", "content":
                "Rewrite it. Problems: " + "; ".join(faults) +
                ". Keep the plot, and keep the NARRATOR: and TITLE: lines."},
        ]

    if not written:
        raise Unsuitable("the model returned nothing usable")
    log.warning("accepting as is (%s): %d words across %d part(s)",
                "; ".join(faults),
                sum(_words(t) + _words(b) for t, b in written), len(written))
    return gender, written


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

    # an accent the model writes anyway must never reach the screen
    marked = f"Он сорвал замо{ACCENT}к с двери."
    assert plain(marked) == "Он сорвал замок с двери.", plain(marked)
    assert plain(f"[sad] За{ACCENT}мок на холме") == "Замок на холме"
    assert _words(f"замо{ACCENT}к и за{ACCENT}мок") == 3
    assert "ё" in plain("Всё ещё её"), "ё is a real letter, keep it"
    # a grave slipping in must not survive to the screen either
    assert plain("сто̀ит") == "стоит" and plain("сто́ит") == "стоит"
    # _split keeps an accent; every screen path runs plain() over it anyway
    _, t_acc, _ = _split(f"NARRATOR: male\nTITLE: За{ACCENT}мок\n\nТело.")
    assert plain(t_acc) == "Замок", plain(t_acc)
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
    assert _title_fault("В четверг были похороны. В пятницу она спросила про деньги")
    assert _title_fault("Отец ткнул в меня пальцем. «Будешь плохо есть»")
    assert not _title_fault("Соседка орала на моих детей, а наказала я своих")
    assert not _title_fault("В четверг похороны, а в пятницу она спросила про деньги")
    # digits are the point of the title, not a stray token to trip over
    assert not _title_fault("Золовка платит 8000 за комнату в моей квартире")
    assert _title_fault("Попросила золовку платить восемьсот за комнату")
    assert _title_fault("Отец требует тридцать процентов моей зарплаты")
    assert _title_fault("Брат занял пять тысяч и пропал перед свадьбой")
    # the figure is the hook, the unit is filler - and it eats the 40-char cut
    assert _title_fault("Соседка прислала счёт на 80000 рублей за свой потоп")
    assert _title_fault("Свекровь потребовала 800 долларов за комнату")
    assert _title_fault("Свекровь потребовала $800 за комнату")
    assert not _title_fault("Соседка прислала счёт на 80000 за свой потоп")
    # ordinary words that merely start like a numeral must not trip it
    assert not _title_fault("В четверг похороны, а в пятницу она спросила про деньги")
    assert not _title_fault("Он оставил пятно на платье, а виноватой стала я")
    assert not SPELLED_NUMBER.search("Ремонт стоит дороже"), '"сто" must not fire on "стоит"'

    # a homograph in a short title is read as a coin flip, so it never ships
    assert _title_fault("Тёща сменила замок в нашей квартире"), "за́мок vs замо́к"
    assert _title_fault("Ремонт стоит дороже, а платить велели мне"), "стóит vs стои́т"
    assert not _title_fault("Тёща въехала в нашу квартиру, пока мы были в отпуске")
    # a trailing stop is stripped before the card is drawn, so it is not a split
    assert not _title_fault("Тест сказал другое.")
    # "не" plus a normal verb is a fact, not an instruction - it must pass
    assert not _title_fault("Он не пришёл на собственную свадьбу")
    assert not _title_fault(f"[sad] Он сжё{ACCENT}г мои письма за одну ночь"), "cues and marks are not words"

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

    # a part that is not the last one carries no question at all - the stop is
    # the cliffhanger, and terminal punctuation is what proves it was deliberate
    assert not _ending_fault("Мусоровоз подъезжал к дому. И тут я замерла.", final=False)
    assert not _ending_fault("Он крикнул. [angry] «Ты серьёзно?»", final=False), \
        "a scene may end on a line of dialogue"
    assert _ending_fault("Он открыл дверь и", final=False), "a cut-off part must be caught"
    assert _ending_fault(f"И тут я замерла. {GOOD_CTA}", final=False), \
        "a middle part must not address the viewer"
    assert _ending_fault("И тут я замерла. Что мне было делать?", final=False), \
        "a rhetorical question is the same shape and is out too"
    assert _ending_fault("", final=False)

    # a leading mood cue, and nothing else on the line
    assert not _cta_fault(GOOD_CTA)
    assert not _cta_fault("[thoughtful] А вы бы простили его?")
    assert _cta_fault("А вы бы простили его?"), "no mood cue"
    assert _cta_fault("[emphasis]А вы бы простили его?"), "emphasis is not a mood"
    # marking one word for stress was measured and does nothing - keep it out
    assert _cta_fault("[doubtful] А вы бы [emphasis]простили его?"), "cue inside the line"
    assert _cta_fault("[doubtful] А вы бы простили [quietly] его?"), "second cue"

    # how many videos a post is worth, by source length alone
    assert part_count({"text": "x" * 500}) == 1
    assert part_count({"text": "x" * (PART_CHARS + 10)}) == 2
    assert part_count({"text": "x" * 4000}) == MAX_PARTS, "MAX_CHARS must not exceed it"

    # a two-part answer must come apart cleanly: one NARRATOR at the top, a
    # title and a cliffhanger question in each half
    RAW2 = ("NARRATOR: female\n"
            "TITLE: Свекровь потребовала ключи, а я сменила замки\n\n"
            "Тело первой части. И тут я услышала её шаги на лестнице.\n"
            "---\n"
            "TITLE: Свекровь пришла с полицией, а ключи были уже другие\n\n"
            "Тело второй части. [doubtful] А вы бы её пустили?")
    g4, p4, f4 = _parse_parts(RAW2, {"title": "", "text": ""}, 2, 6)
    assert g4 == "female" and len(p4) == 2, (g4, p4)
    assert p4[0][0].startswith("Свекровь потребовала"), p4[0]
    assert p4[1][0].startswith("Свекровь пришла"), p4[1]
    assert p4[1][1].startswith("Тело второй"), p4[1]
    # the fixture is deliberately far off six words; nothing else may be wrong
    assert all("words" in f for f in f4), f4
    assert all(f.startswith("part ") for f in f4), "faults must name their part"
    # only the last part may address the viewer, and only it must
    RAW_BAD = RAW2.replace("И тут я услышала её шаги на лестнице.",
                           "[curious] Как думаете, что было дальше?")
    _, _, f7 = _parse_parts(RAW_BAD, {"title": "", "text": ""}, 2, 6)
    assert any(f.startswith("part 1") and "not the last" in f for f in f7), f7
    RAW_BAD2 = RAW2.replace("[doubtful] А вы бы её пустили?", "И она ушла.")
    _, _, f8 = _parse_parts(RAW_BAD2, {"title": "", "text": ""}, 2, 6)
    assert any(f.startswith("part 2") and "question" in f for f in f8), f8
    # one part where two were asked for is a fault, not a silent single video
    _, p5, f5 = _parse_parts(RAW2.split("---")[0], {"title": "", "text": ""}, 2, 6)
    assert len(p5) == 1 and any("parts" in f for f in f5), f5
    # and the single-video path must not start splitting on a stray dash line
    _, p6, _ = _parse_parts("TITLE: Заголовок\n\n---\n\nТело. А вы бы смогли?",
                            {"title": "", "text": ""}, 1, 6)
    assert len(p6) == 1, p6

    print(f"logic ok: {OUTPUT_LANG}, {_wpm()} wpm, target {tw} words "
          f"for {TARGET_SEC}+{CTA_SEC} sec")

    if OPENAI_API_KEY:
        import source
        post = source.fetch(1)[0]
        n = part_count(post)
        gender, written = write_script(post, parts=n)
        print(f"\n--- r/{post['sub']} [{post['score']}] {len(post['text'])}ch "
              f"-> {len(written)} part(s)\n{post['title']}\n")
        print(f"NARRATOR: {gender}")
        for i, (title, body) in enumerate(written, 1):
            w = _words(title) + _words(body)
            print(f"\nTITLE {i}/{len(written)}: {title}\n\n{body}\n"
                  f"[{w} words -> ~{w / _wpm() * 60:.0f} sec]")
    else:
        print("OPENAI_API_KEY not set - live run skipped")
