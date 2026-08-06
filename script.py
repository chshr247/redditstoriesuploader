"""Step 2: raw Reddit post -> title card text + narration, sized for TARGET_SEC.

Output is bare prose with no markup: whatever this returns, edge-tts reads
out loud verbatim. No "**", no emoji, no stage directions.

Title and body come from one call. The source is English and the narration
usually is not, so the title needs translating too - and a model that writes
both at once keeps them in one voice, for one request instead of two.

The instructions themselves live in prompts.py, one set per channel language.
What stays here is the machinery that checks the answer, and the parts of that
which depend on the language - the shapes a title must not have - are keyed by
language the same way. Every one of those checks exists because a rule in the
prompt alone was not enough (see todo.md, "причина 3").
"""
import logging
import os
import re
import sys

from openai import OpenAI

# prompts.py is not in this repo. This one is public and must stay public, so
# the prompt set lives in a private one cloned to .private/ - by CI with a
# deploy key, by hand for local work. Nothing else is over there, and putting
# the directory on the path rather than copying the file out of it keeps one
# copy to edit instead of two to keep in step.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".private"))
try:
    import prompts
except ImportError:
    raise SystemExit(
        "prompts.py not found - clone the private repo into .private/ "
        "(git clone https://github.com/chshr247/reddit-prompts.git .private)") from None

import safety
from config import (LLM_BASE_URL, LLM_MODEL, OPENAI_API_KEY, OUTPUT_LANG,
                    TARGET_SEC)


class Unsuitable(Exception):
    """The post cannot become a video. Raised instead of returning junk."""

# Measured with `python voice.py`. Russian runs far slower than English on the
# same voice, so this MUST be re-measured whenever TTS_VOICE or OUTPUT_LANG
# changes - the whole word budget hangs off it.
#
# en measured 2026-08-02 on the four voices the English channel actually uses,
# 134-word sample each: 165, 194, 199 and 156 wpm. 191 before that was a guess
# and would have written 253-word scripts - a hundred seconds of video on the
# slowest of them. The number here is the HARMONIC mean of the four, which is
# the one that makes the average DURATION come out at TARGET_SEC + CTA_SEC;
# the arithmetic mean would aim short. Range at 230 words: 69 to 88 seconds,
# all of it clear of the MIN_SEC floor, so no video gets re-voiced slower.
# The spread between voices is still wider than the word tolerance, which is
# the open problem noted in todo.md item 3 and is not specific to a language.
WPM = {"ru": 150, "en": 177}
TOLERANCE = 0.15
# The closing question is spoken too, so it needs its own slice of the budget.
# Added on top of TARGET_SEC rather than carved out of it: taken from the story
# it would squeeze the payoff, which is the one part that must not be rushed.
CTA_SEC = 3

LANG_NAME = {"ru": "Russian", "en": "English"}

log = logging.getLogger(__name__)

# One set of instructions per channel language, kept in prompts.py: the
# examples are most of the prompt, and examples in the wrong language teach
# the wrong thing. MULTI is still appended to SYSTEM, so an ordinary video
# keeps hitting the provider's prefix cache on SYSTEM alone.
SYSTEM, MULTI = prompts.SYSTEM, prompts.MULTI

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


# How much may sit inside one bracket. A cue past this ceiling is not a cue:
# it survives plain(), lands in the word count and gets burned into the
# subtitles. 60 was enough while only the closing question merged a cue with a
# delivery constraint - 32 chars of constraint left room for any mood. The
# title merges too now, and its constraint is 52, so a mood as ordinary as
# "surprised" came to 63 and the merge was dropped instead, silently costing
# the hook the delivery that was tuned for it. 80 clears the longest
# documented tag against the longest constraint with room to spare.
CUE_MAX = 80
TAG = re.compile(rf"\[[^\]\n]{{1,{CUE_MAX}}}\]")
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

    Only a cue with a COMMA names anyone, which is the shape the prompt asks
    for: label, comma, delivery. A bare cue is delivery alone and leaves the
    pending label untouched - "[husband, angry] [emphasis] «Ужин»" is the
    husband, not a speaker called "emphasis". That mattered once [emphasis]
    started appearing on a key word in every sentence: a bare cue read as a
    label would take a colour of its own and shift every real speaker's colour
    after it, see render._styles().
    """
    out: list[str | None] = []
    pending = None          # speaker named by the most recent cue
    current = None          # speaker of the quote we are inside of
    for token in re.split(rf"(\[[^\]\n]{{1,{CUE_MAX}}}\])", s):
        if not token:
            continue
        if token.startswith("[") and token.endswith("]"):
            if "," in token:
                label = token[1:-1].split(",", 1)[0].strip().lower()
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
#
# Both closing quotes are terminal: » ends a Russian line of speech and ” an
# English one, and American style puts the full stop INSIDE the quote, so the
# last character of the sentence is the quote itself. Without ” here an English
# story that ends a scene on dialogue has no boundary before its closing
# question - split_cta() returns nothing, _ending_fault() says the question is
# missing, and every such story costs a rewrite.
TERMINAL = ".!?…»”"
SENTENCE = re.compile(
    rf"(?<=[{re.escape(TERMINAL)}])\s+(?=(?:\[[^\]\n]{{1,{CUE_MAX}}}\]\s*)*[«„“A-ZА-ЯЁ])")


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
#
# Everything below is per channel language, and it has to be: a rule in the
# prompt that no check backs up is a rule that quietly stops applying. The
# Russian numeral list run over an English title matches nothing and refuses
# nothing, which looks exactly like a title with no faults in it.
WEAK_TITLE = {
    "ru": re.compile(
        r"^(?:"
        r"не\s+\w+(?:те|йте)\b"          # imperative plea: "Не используйте меня..."
        r"|никогда\s+не\s+\w+(?:те|йте)\b"
        r"|истори[яю]\s+о\s+том\b"
        r"|о\s+том,?\s+как\b"
        r"|мой\s+опыт\b"
        r")", re.IGNORECASE),
    # English says the same shapes with its own words. "PSA" and "a reminder
    # that" have no Russian counterpart in the list because Russian Reddit
    # translations do not produce them; they are the native form of the plea
    # here and the single most common way an English title says nothing.
    "en": re.compile(
        r"^(?:"
        # apostrophes are written both ways - the model reaches for the curly
        # one as often as the straight one, and a rule that only catches half
        # of them is the same as no rule
        r"(?:please\s+)?(?:don['’]?t|do\s+not|never|always|stop|quit)\s+\w+"
        r"|psa\b|ps\s?a:"
        r"|(?:a|just\s+a)\s+reminder\b"
        r"|(?:the\s+)?story\s+of\s+(?:how|when|the)\b"
        r"|my\s+experience\b"
        r"|(?:here['’]?s\s+)?why\s+you\s+(?:should|shouldn['’]?t|need)\b"
        r"|i['’]?m\s+(?:so\s+)?(?:done|tired|sick|fed\s+up)\s+(?:of|with)\b"
        r")", re.IGNORECASE),
}

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
SPELLED_NUMBER = {
    "ru": re.compile(
        r"\b(?:двадцат[иь]|тридцат[иь]|сорока?|пятьдесят|пятидесяти|шестьдесят"
        r"|шестидесяти|семьдесят|семидесяти|восемьдесят|восьмидесяти|девяносто"
        r"|девяноста|ст[оа]|двести|двухсот|тр[ие]ста|тр[ёе]хсот|четыреста"
        r"|четыр[ёе]хсот|пятьсот|пятисот|шестьсот|шестисот|семьсот|семисот"
        r"|восемьсот|восьмисот|девятьсот|девятисот|тысяч\w*|миллион\w*"
        r"|миллиард\w*)\b", re.IGNORECASE),
    # Same rule, and "20k" added because English writes it that way and it is
    # read aloud as one mumbled syllable.
    "en": re.compile(
        r"\b(?:twenty|thirty|fou?rty|fifty|sixty|seventy|eighty|ninety"
        r"|hundreds?|thousands?|millions?|billions?|grand)\b|\b\d+\s?k\b",
        re.IGNORECASE),
}

# The currency word after a figure. It buys nothing - the sum is the hook, not
# the unit - and costs three syllables of the ~40 characters the feed shows.
CURRENCY = {
    "ru": re.compile(r"\d[\d\s]{0,8}(?:рубл|руб\b|долл|евро|₽)|[$₽]\s*\d",
                     re.IGNORECASE),
    "en": re.compile(r"\d[\d\s]{0,8}(?:dollars?|bucks|euros?|pounds?|usd)\b"
                     r"|[$€£]\s*\d", re.IGNORECASE),
}

# Words the voice engine has to guess at, and guesses from context the title
# does not have: a six-word line gives it nothing to go on, so a wrong guess is
# heard as a different word in the first three seconds of the video.
# ponytail: a hand-picked list, not a dictionary. These are the ones whose other
# reading is a plausible word in a story title; the pairs that only collide in
# rare grammar (руки, окна, дома / "present", "record") are left out because
# they would fire on everything. Add to it when a video actually comes back
# mispronounced.
HOMOGRAPH = {
    "ru": re.compile(
        r"\b(?:замок|замка|мука|духи|полки|белки|пропасть|хлопок|село"
        r"|дорога|стоит|плачу|лечу|ношу|острота)\b", re.IGNORECASE),
    # English calls them heteronyms and has far fewer that matter here. "read"
    # is the one that actually bites: past and present are spelled alike and a
    # story title is almost always past.
    "en": re.compile(
        r"\b(?:read|lead|tear|tears|wound|bow|wind|refuse|desert|resume"
        r"|invalid|row|sow)\b", re.IGNORECASE),
}

# In-law words the language is quietly retiring. Not wrong - unknown: a viewer
# under thirty has to stop and work out which one золовка is, and the sentence
# after it is gone by the time they have. Свекровь, тёща and зять are missing
# from this list on purpose, and the prompt keeps them for the same reason:
# those three never left everyday speech.
#
# The one check here that reads the NARRATION as well as the title, because
# that is where these words actually turn up - the title has six words and
# spends them on the event, the narration is where a family gets named.
#
# Russian only, and the dict has no "en" key to say so. Every other table above
# carries both languages because the rule exists in both prompts; English
# in-law words are all current, so SYSTEM_EN has no such rule, and an English
# half here would be a regex that can never fire pretending to be a check.
#
# ponytail: stems, like tags.py, but two of them are written the long way round
# because a short one would eat a word that is allowed. "свекр" matches
# свекровь, hence the lookahead; "кум" matches кумир and кумовство, hence the
# spelled-out endings.
ARCHAIC_KIN = {
    "ru": [
        (re.compile(r"\bзоловк\w*", re.IGNORECASE), "сестра мужа"),
        (re.compile(r"\bдевер\w*", re.IGNORECASE), "брат мужа"),
        (re.compile(r"\bшур(?:ин|ья|ьё|ье)\w*", re.IGNORECASE), "брат жены"),
        (re.compile(r"\bсвояк\w*", re.IGNORECASE), "муж сестры жены"),
        (re.compile(r"\bсвоячениц\w*", re.IGNORECASE), "сестра жены"),
        (re.compile(r"\bснох[аиоуе]\w*", re.IGNORECASE), "жена сына"),
        (re.compile(r"\bневестк\w*", re.IGNORECASE), "жена сына"),
        (re.compile(r"\b(?:св[её]кор\w*|св[её]кр(?!ов)\w*)", re.IGNORECASE),
         "отец мужа"),
        (re.compile(r"\bкум(?:а|ы|е|у|ой|ом|ам|ами)?\b", re.IGNORECASE),
         "крёстный моего сына"),
    ],
}


def _kin_fault(text: str, lang: str = "") -> str:
    """Empty unless the text names a relative by a word nobody says any more."""
    t = plain(text)
    for pat, fix in ARCHAIC_KIN.get(lang or OUTPUT_LANG, ()):
        m = pat.search(t)
        if m:
            return (f"\"{m.group()}\" is a word a young viewer has to stop and "
                    f"work out - name the person plainly instead, \"{fix}\"")
    return ""


# How the model is told to keep a title to one sentence, which is the one
# complaint that has to name a conjunction to be actionable.
_TURN_WORDS = {"ru": '"а" or "но"', "en": '"and" or "but"'}


def _title_fault(title: str, lang: str = "") -> str:
    """Empty when the title is usable, otherwise what to tell the model."""
    lang = lang or OUTPUT_LANG
    t = plain(title).strip()
    if not t:
        return "the TITLE line is missing"
    if len(t.split()) > MAX_TITLE_WORDS:
        return (f"the TITLE is {len(t.split())} words, keep it under "
                f"{MAX_TITLE_WORDS}")
    if WEAK_TITLE[lang].match(t):
        return ("the TITLE states a position or gives advice instead of showing "
                "a moment - rewrite it as the single sharpest thing that "
                "happened, in concrete words")
    n = SPELLED_NUMBER[lang].search(t)
    if n:
        return (f"the TITLE spells the number \"{n.group()}\" out in letters - "
                "write the whole figure in digits, \"20000\" not \"20 тысяч\""
                if lang == "ru" else
                f"the TITLE writes the number \"{n.group()}\" out in words - "
                "write the whole figure in digits, \"20000\" not \"20 thousand\"")
    c = CURRENCY[lang].search(t)
    if c:
        return (f"the TITLE names a currency in \"{c.group().strip()}\" - drop "
                "the unit and keep the bare figure, "
                + ("\"счёт на 8000\"" if lang == "ru" else "\"a bill for 8000\""))
    h = HOMOGRAPH[lang].search(t)
    if h:
        return (f"\"{h.group()}\" is said two ways and the TITLE is too short "
                "for the engine to guess right - say it with a different word")
    # One sentence, always. The same boundary split_cta() uses, so "two
    # sentences" means here exactly what it means everywhere else in the file.
    if len(SENTENCE.split(t)) > 1:
        return ("the TITLE is two sentences - it must be exactly one, with the "
                f"turn made on a comma with {_TURN_WORDS[lang]} instead of a "
                "full stop")
    return _title_cue_fault(title, t)


def _title_cue_fault(title: str, plain_title: str) -> str:
    """Empty when the title's markup is right. `plain_title` is plain(title).

    An optional mood cue in front, one [emphasis] inside, nothing else. The
    mark is what the rule is really about: the card is on screen for three
    seconds against a thumb already moving, and a title read flat gives that
    thumb nothing to catch on.

    Where it sits is half the point. Not the last word - the title is narrated
    under FISH_TITLE_CUE, whose job is to keep the engine off the final word,
    and the two would be pulling against each other. Not the second half
    either: by then the scroll has been decided, so a peak there is spent on
    people who already stayed. And not the first word, which is stressed by
    being first: marking it buys no peak and drags the line's weight forward,
    away from the word the title actually turns on. The prompt has said all
    three from the start; only two of them were ever checked.
    """
    marked = [t for t in TAG.findall(title) if t.lower().startswith("[emphasis")]
    lead = LEAD_CUE.match(title.strip())
    others = [t for t in TAG.findall(title[lead.end():] if lead else title)
              if not t.lower().startswith("[emphasis")]
    if others:
        return ("the TITLE carries a cue inside the line - only the mood cue in "
                "front of it and one [emphasis] belong there")
    if len(marked) != 1:
        return ("the TITLE needs exactly one [emphasis], in front of the word "
                "it turns on")
    after = title[title.lower().index("[emphasis"):]
    left = len(plain(after).split())
    if left < 2:
        return ("the TITLE marks its LAST word - the narration holds that word "
                "flat, so move the [emphasis] onto the word the title turns on")
    words = len(plain_title.split())
    # Nothing of the title stands in front of the mark, so the mark is on word
    # one. Checked before the halves below, which would let this through: the
    # first word is as far into the first half as it is possible to be.
    if left == words:
        return ("the TITLE marks its FIRST word - that one is stressed by "
                "being first, so move the [emphasis] onto the word the title "
                "turns on")
    if left <= words / 2:
        return ("the TITLE marks a word in its second half - move the "
                "[emphasis] into the first half, where it is heard before the "
                "viewer has decided to scroll")
    return ""


# The mood cue in front of the question. [emphasis] is not a mood, so a
# question that opens with one still counts as having no opening cue.
LEAD_CUE = re.compile(rf"^\[(?!emphasis\b)[^\]\n]{{1,{CUE_MAX}}}\]", re.IGNORECASE)


def _cta_fault(cta: str) -> str:
    """Empty when the closing question is marked up for delivery.

    A leading mood cue, then exactly one [emphasis] and nothing else. The mark
    was kept out of the question for a while: measured on its own it came back
    no longer, and sometimes shorter, than the same word untagged. That was a
    duration test, and duration is a poor proxy - the question now carries it
    like every other sentence.

    Not on the LAST word, though. The question is synthesized under
    FISH_CTA_CUE, whose whole job is to keep the engine off the final word, and
    a mark there is asking for one thing and its opposite in the same take.
    """
    cta = cta.strip()
    m = LEAD_CUE.match(cta)
    if not m:
        return ("the closing question must open with a mood cue before its "
                "first word, like \"[doubtful] А как бы...\"")
    rest = cta[m.end():]
    marks = TAG.findall(rest)
    if [t for t in marks if not t.lower().startswith("[emphasis")]:
        return ("the closing question carries a cue inside the line - the "
                "opening mood cue and one [emphasis] are all it may have")
    if len(marks) != 1:
        return ("the closing question needs exactly one [emphasis], in front of "
                "the word it turns on")
    after = rest[rest.lower().index("[emphasis") :]
    if len(strip_tags(after).split()) < 2:
        return ("the closing question marks its LAST word - move the [emphasis] "
                "onto the word the question turns on, further back in the line")
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
        # everything terminal except the question mark: a middle part must not
        # end on a question at all, which the check above has just refused
        if t[-1] not in TERMINAL.replace("?", ""):
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
    # The title keeps its cues, exactly as the body does. It is burned on
    # screen, but nothing burns it raw: build_ass() runs plain() over it, the
    # card's word timings come from the aligner, which works on plain text, and
    # the meta file publishing reads is written plain. Stripping here instead
    # cost the title card the only delivery the model could give it.
    return gender, _clean(title).rstrip(" .,:;-"), _clean(body)


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
                   (_title_fault(title),
                    # title and narration together: one complaint either way,
                    # and the fix is the same wherever the word turned up
                    _kin_fault(f"{title} {body}"),
                    _ending_fault(body, final=i == len(chunks)))
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
    lang = OUTPUT_LANG
    system = SYSTEM[lang].format(lang=LANG_NAME[lang])
    if parts > 1:
        system += MULTI[lang].format(n=parts)
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
    gt, tt, bt = _split("NARRATOR: male\nTITLE: [sad] Мой [emphasis] заголовок\n\n[calm] Тело истории.")
    assert tt == "[sad] Мой [emphasis] заголовок", tt
    assert plain(tt) == "Мой заголовок", "the card sees none of it"
    assert "[calm]" in bt, "body must keep its cues"

    # only "label, delivery" names a speaker. A bare cue is delivery, and with
    # [emphasis] now landing in almost every sentence one WILL sit in front of a
    # quote sooner or later - it must not become a speaker of its own.
    def _who(s):
        return list(zip(plain(s).split(), speakers(s)))
    assert _who("Он спустился. [husband, angry] «Ужин»")[-1][1] == "husband"
    assert _who("Он спустился. [husband, angry] [emphasis] «Ужин»")[-1][1] == "husband", \
        "a bare cue must not steal the label"
    assert _who("Он спустился. [emphasis] «Ужин»")[-1][1] == "other", \
        "an unnamed quote is other, never the cue's own word"
    # emphasis inside the narration colours nothing at all
    assert set(speakers("Она вернула [emphasis] сорок тысяч.")) == {None}

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

    # Both languages are checked on every run, whichever channel this process
    # is. A rule that only holds for the channel you happen to be testing is
    # how the English side would ship with its numeral check switched off.
    def _marked(title: str) -> str:
        """Give a fixture the [emphasis] every title now carries.

        These cases are about what the WORDS of a title may be, and none of
        them should have to restate the markup rule to say it. Second word:
        inside the first half, and neither the opening word nor the last one.
        """
        w = title.split()
        if "[emphasis]" in title or len(w) < 3:
            return title
        # A leading mood cue is not a word. Counting it as one puts the mark in
        # front of word ONE, which is the thing the docstring above promises
        # not to do - latent until the first-word check went in and every
        # cued fixture started failing on its markup instead of its words.
        at = 2 if w[0].startswith("[") else 1
        return " ".join(w[:at] + ["[emphasis]"] + w[at:])

    def ru(title: str) -> str:
        return _title_fault(_marked(title), "ru")

    def en(title: str) -> str:
        return _title_fault(_marked(title), "en")

    # the markup rule itself, on a title whose words are beyond reproach
    GOOD_TITLE = "Соседка [emphasis] прислала мне счёт на 80000 за потоп"
    assert not _title_fault(GOOD_TITLE, "ru"), _title_fault(GOOD_TITLE, "ru")
    assert not _title_fault(f"[angry] {GOOD_TITLE}", "ru")
    assert _title_fault(GOOD_TITLE.replace("[emphasis] ", ""), "ru"), "no mark"
    assert _title_fault("Соседка прислала мне счёт за свой [emphasis] потоп", "ru"), \
        "the last word is what FISH_TITLE_CUE holds flat"
    assert _title_fault("[emphasis] Соседка прислала мне счёт на 80000", "ru"), \
        "the first word is stressed by being first, so the mark buys nothing"
    assert _title_fault("[angry] [emphasis] Соседка прислала мне счёт на 80000", "ru"), \
        "the mood cue in front does not make the next word any less the first"
    assert _title_fault("Соседка прислала мне счёт на [emphasis] 80000 за потоп", "ru"), \
        "second half - the scroll has already been decided"
    assert _title_fault("[angry] Соседка [surprised] прислала [emphasis] счёт на 80000", "ru"), \
        "a cue inside the line"

    # a title has to show a moment, not describe a stance
    assert ru("Не используйте меня для воспитания детей"), "the plea shape must be caught"
    assert ru("Никогда не занимайте денег родне")
    assert ru("История о том, как я съехал")
    assert ru("Мой опыт с ипотекой")
    assert ru("")
    assert ru(" ".join(["слово"] * (MAX_TITLE_WORDS + 1)))
    # one sentence, always - these all read well and are all rejected anyway
    assert ru("Соседка орала на моих детей. Наказала я своих")
    assert ru("В четверг были похороны. В пятницу она спросила про деньги")
    assert ru("Отец ткнул в меня пальцем. «Будешь плохо есть»")
    assert not ru("Соседка орала на моих детей, а наказала я своих")
    assert not ru("В четверг похороны, а в пятницу она спросила про деньги")
    # digits are the point of the title, not a stray token to trip over
    assert not ru("Свекровь платит 8000 за комнату в моей квартире")
    assert ru("Попросила свекровь платить восемьсот за комнату")
    assert ru("Отец требует тридцать процентов моей зарплаты")
    assert ru("Брат занял пять тысяч и пропал перед свадьбой")
    # the figure is the hook, the unit is filler - and it eats the 40-char cut
    assert ru("Соседка прислала счёт на 80000 рублей за свой потоп")
    assert ru("Свекровь потребовала 800 долларов за комнату")
    assert ru("Свекровь потребовала $800 за комнату")
    assert not ru("Соседка прислала счёт на 80000 за свой потоп")
    # ordinary words that merely start like a numeral must not trip it
    assert not ru("В четверг похороны, а в пятницу она спросила про деньги")
    assert not ru("Он оставил пятно на платье, а виноватой стала я")
    assert not SPELLED_NUMBER["ru"].search("Ремонт стоит дороже"), '"сто" must not fire on "стоит"'

    # in-law words nobody says any more, in the title and in the narration
    assert _kin_fault("Золовка забрала кольцо бабушки", "ru")
    assert _kin_fault("Я отдала ключи золовке и пожалела", "ru")
    assert _kin_fault("Деверь занял денег и пропал", "ru")
    assert _kin_fault("Шурин переехал к нам на месяц", "ru")
    assert _kin_fault("Свояк потребовал долю в квартире", "ru")
    assert _kin_fault("Свояченица требует свою долю", "ru")
    assert _kin_fault("Сноха выставила меня из кухни", "ru")
    assert _kin_fault("Невестка не пустила меня к внуку", "ru")
    assert _kin_fault("Свёкор продал машину без спроса", "ru")
    assert _kin_fault("Свекра я больше не пускаю в дом", "ru")
    assert _kin_fault("Кум пришёл на крестины пьяным", "ru")
    # the cue is stripped first, so an English speaker label is not a match
    assert not _kin_fault("[husband, angry] «Отдай ключи»", "ru")
    # the three that stayed, and the words a short stem would have eaten
    assert not _kin_fault("Свекровь сменила замок в нашей квартире", "ru")
    assert not _kin_fault("Свекрови я больше ничего не должна", "ru")
    assert not _kin_fault("Тёща въехала к нам, а зять молчал", "ru")
    assert not _kin_fault("Он был её кумиром, пока не занял денег", "ru")
    assert not _kin_fault("Невеста опоздала на собственную свадьбу", "ru")
    # English has no such rule, so the check has nothing to say about it
    assert not _kin_fault("My sister-in-law took the ring", "en")

    # a homograph in a short title is read as a coin flip, so it never ships
    assert ru("Тёща сменила замок в нашей квартире"), "за́мок vs замо́к"
    assert ru("Ремонт стоит дороже, а платить велели мне"), "стóит vs стои́т"
    assert not ru("Тёща въехала в нашу квартиру, пока мы были в отпуске")
    # a trailing stop is stripped before the card is drawn, so it is not a split
    assert not ru("Тест сказал другое.")
    # "не" plus a normal verb is a fact, not an instruction - it must pass
    assert not ru("Он не пришёл на собственную свадьбу")
    assert not ru(f"[sad] Он сжё{ACCENT}г мои письма за одну ночь"), "cues and marks are not words"

    # The same list in English. Same order, so the two blocks can be read side
    # by side and a rule missing from one of them is visible.
    assert en("Don't let your family borrow money"), "the plea shape must be caught"
    assert en("Never lend your car to a friend")
    assert en("PSA: check your bank statements every month")
    assert en("A reminder that family money is never free")
    assert en("The story of how I moved out")
    assert en("My experience with an entitled landlord")
    assert en("Why you should never cosign for family")
    assert en("I'm done with my neighbor's kids")
    assert en("")
    assert en(" ".join(["word"] * (MAX_TITLE_WORDS + 1)))
    # one sentence, always
    assert en("The neighbor screamed at my kids. I grounded mine")
    assert not en("The neighbor screamed at my kids, but I grounded mine")
    # digits stay digits, and the currency word is filler that eats the cut
    assert not en("My sister-in-law pays 8000 for one room")
    assert en("My sister-in-law pays eight hundred for one room")
    assert en("Dad wants thirty percent of my paycheck")
    assert en("My brother borrowed 5000 dollars and vanished")
    assert en("My brother borrowed $5000 and vanished before the wedding")
    assert en("Neighbor billed me 20k for her own flood")
    assert not en("Neighbor billed me 80000 for her own flood")
    # ordinary words must not trip the numeral check
    assert not en("Mom took my car keys on my wedding day")
    assert not en("He left a stain on the dress and blamed me")
    # heteronyms: the engine picks the reading from context a title has none of
    assert en("I read my sister's messages and told everyone"), "read vs read"
    assert en("She had tears in her eyes at the register"), "tears vs tears"
    assert not en("My mother-in-law moved in while we were away")
    assert not en("Dad cut me from the will after one dinner.")
    assert not en(f"[sad] He burned my letters in one night"), "cues are not words"
    # the checks must not leak across languages: each one refuses its own
    # language's mistakes and stays quiet on the other's ordinary words
    assert not en("Соседка прислала счёт на 80000 за свой потоп")
    assert not ru("Dad cut me from the will after one dinner")

    # An English scene ends on the quote mark, with the stop inside it. If that
    # is not a sentence boundary, the closing question after it is invisible.
    s, c = split_cta("He yelled. [angry] “Are you serious?” Would you have stayed?")
    assert c == "Would you have stayed?", c
    assert s.endswith("“Are you serious?”"), s
    assert not _ending_fault('He left. [me, cold] “Dinner is on the table.” '
                             "[doubtful] Would you have [emphasis] said that to him?")
    assert not _ending_fault('...And I froze. [angry] “Get out of my house.”',
                             final=False), "a part may end on a line of dialogue"

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
    GOOD_CTA = "[doubtful] А как бы вы [emphasis] поступили на моём месте?"
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

    # a leading mood cue, one [emphasis], and nothing else on the line
    assert not _cta_fault(GOOD_CTA)
    assert not _cta_fault("[thoughtful] А вы бы [emphasis] простили его?")
    assert _cta_fault("А вы бы [emphasis] простили его?"), "no mood cue"
    assert _cta_fault("[emphasis] А вы бы простили его?"), "emphasis is not a mood"
    assert _cta_fault("[doubtful] А как бы вы поступили?"), "no emphasis at all"
    assert _cta_fault("[doubtful] А вы бы [emphasis] простили [emphasis] его?"), \
        "one mark, not two"
    assert _cta_fault("[doubtful] А вы бы простили [quietly] его?"), "second cue"
    assert _cta_fault("[doubtful] А вы бы его [emphasis] простили?"), \
        "the last word is what FISH_CTA_CUE holds flat"

    # how many videos a post is worth, by source length alone
    assert part_count({"text": "x" * 500}) == 1
    assert part_count({"text": "x" * (PART_CHARS + 10)}) == 2
    assert part_count({"text": "x" * 4000}) == MAX_PARTS, "MAX_CHARS must not exceed it"

    # a two-part answer must come apart cleanly: one NARRATOR at the top, a
    # title and a cliffhanger question in each half
    RAW2 = ("NARRATOR: female\n"
            "TITLE: Свекровь [emphasis] потребовала ключи, а я сменила замки\n\n"
            "Тело первой части. И тут я услышала её шаги на лестнице.\n"
            "---\n"
            "TITLE: Свекровь [emphasis] пришла с полицией, а ключи были другие\n\n"
            "Тело второй части. [doubtful] А вы бы её [emphasis] пустили в дом?")
    g4, p4, f4 = _parse_parts(RAW2, {"title": "", "text": ""}, 2, 6)
    assert g4 == "female" and len(p4) == 2, (g4, p4)
    # plain(), because a part keeps its markup all the way to voice.speak_parts
    # - the cues are what the engine reads - and only meta.json gets it
    # stripped. Comparing the raw title against unmarked words could never pass.
    assert plain(p4[0][0]).startswith("Свекровь потребовала"), p4[0]
    assert plain(p4[1][0]).startswith("Свекровь пришла"), p4[1]
    assert p4[1][1].startswith("Тело второй"), p4[1]
    # the fixture is deliberately far off six words; nothing else may be wrong
    assert all("words" in f for f in f4), f4
    assert all(f.startswith("part ") for f in f4), "faults must name their part"
    # only the last part may address the viewer, and only it must
    RAW_BAD = RAW2.replace("И тут я услышала её шаги на лестнице.",
                           "[curious] Как думаете, что было дальше?")
    _, _, f7 = _parse_parts(RAW_BAD, {"title": "", "text": ""}, 2, 6)
    assert any(f.startswith("part 1") and "not the last" in f for f in f7), f7
    RAW_BAD2 = RAW2.replace("[doubtful] А вы бы её [emphasis] пустили в дом?", "И она ушла.")
    _, _, f8 = _parse_parts(RAW_BAD2, {"title": "", "text": ""}, 2, 6)
    assert any(f.startswith("part 2") and "question" in f for f in f8), f8
    # one part where two were asked for is a fault, not a silent single video
    _, p5, f5 = _parse_parts(RAW2.split("---")[0], {"title": "", "text": ""}, 2, 6)
    assert len(p5) == 1 and any("parts" in f for f in f5), f5
    # and the single-video path must not start splitting on a stray dash line
    _, p6, _ = _parse_parts("TITLE: Заголовок\n\n---\n\nТело. А вы бы смогли?",
                            {"title": "", "text": ""}, 1, 6)
    assert len(p6) == 1, p6

    # the prompt set has to exist for the channel this process is, or the run
    # dies deep inside write_script() with a KeyError instead of here
    assert OUTPUT_LANG in SYSTEM and OUTPUT_LANG in WEAK_TITLE, OUTPUT_LANG

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
