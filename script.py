"""Step 2: raw Reddit post -> title card text + narration, sized for TARGET_SEC.

Output is bare prose with no markup: whatever this returns, edge-tts reads
out loud verbatim. No "**", no emoji, no stage directions.

Two calls, split along one seam: the first writes WORDS, the second writes the
title and adds everything that goes in [square brackets], and may change no word
while it does. One call used to do both, and carrying the title rules, the
markup rules and the prose rules at once is what made it drop single lines out
of the middle of itself. It also meant a fault in the markup bought a rewrite of
the prose - see write_script(), which has the measurements.

The instructions themselves live in prompts.py, one set per channel language,
now two prompts per set - WRITE and POLISH, cut along that same seam.
What stays here is the machinery that checks the answer, and the parts of that
which depend on the language - the shapes a title must not have - are keyed by
language the same way. Every one of those checks exists because a rule in the
prompt alone was not enough (see todo.md, "причина 3").
"""
import logging
import os
import re
import sys

# prompts.py is not in this repo. This one is public and must stay public, so
# the prompt set lives in a private one cloned to .private/ - by CI with a
# deploy key, by hand for local work. Nothing else is over there, and putting
# the directory on the path rather than copying the file out of it keeps one
# copy to edit instead of two to keep in step.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".private"))


def _prompts():
    """(WRITE, POLISH, MULTI, GENRE), imported on use rather than at import time.

    Both this and the OpenAI SDK belong to write_script() and to nothing else,
    and CI checks the private repo out only for a run that is going to call the
    model. Keeping them out of module scope is what lets review.py - which
    imports this file for _title_fault() and the word ceiling - run its title
    check on every tick, ahead of the gate, with nothing installed but dotenv.
    """
    try:
        import prompts
    except ImportError:
        raise SystemExit(
            "prompts.py not found - clone the private repo into .private/ "
            "(git clone https://github.com/chshr247/reddit-prompts.git .private)"
        ) from None
    return prompts.WRITE, prompts.POLISH, prompts.MULTI, prompts.GENRE

import safety
from config import (LLM_BASE_URL, LLM_MAX_TOKENS, LLM_MODEL, LLM_REASONING,
                    OPENAI_API_KEY, OUTPUT_LANG,
                    HORROR_SEC, SUBREDDITS_HORROR, TARGET_SEC, VOICE_SPEEDUP)


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
#
# This is the ENGINE's rate and nothing else - what fish produces, before
# VOICE_SPEEDUP touches it. The budget is counted at _heard_wpm() instead, and
# the distinction is load-bearing: re-measure THIS number with the speed-up off,
# or the factor gets applied twice and the scripts come out a fifth too long.
WPM = {"ru": 150, "en": 177}
TOLERANCE = 0.15
# How far PAST the target a narration may run before it is sent back, and it is
# deliberately not TOLERANCE. The two directions are not the same failure: short
# means the story ran out of material, which is a rewrite, while long means it
# had more than the target holds - and what gets cut first to fit is never an
# event, it is the particular detail inside one, which is the part worth
# keeping. So the ceiling is looser than the floor and only the ceiling moved.
#
# 18% of a 78-second budget is about 92 seconds, which is the number this is
# really set to - 230 words of Russian, 271 of English, one video either way.
# The target itself does not move: the model still aims at TARGET_SEC and this
# only stops a story with something to say from being refused for saying it.
OVER = 0.18
# The closing question is spoken too, so it needs its own slice of the budget.
# Added on top of TARGET_SEC rather than carved out of it: taken from the story
# it would squeeze the payoff, which is the one part that must not be rushed.
CTA_SEC = 3

# The title is narrated at the head of every part, so it always ate part of the
# same budget. Stage one no longer writes it, and holding its share back here is
# what keeps the two stages counting the same seconds: six to twelve words by
# rule, nine in the middle, and TOLERANCE is worth ten times that either way.
TITLE_WORDS = 9

LANG_NAME = {"ru": "Russian", "en": "English"}

log = logging.getLogger(__name__)

# One set of instructions per channel language, kept in prompts.py and read
# through _prompts(): the examples are most of the prompt, and examples in the
# wrong language teach the wrong thing. MULTI is appended to WRITE, so an
# ordinary video keeps hitting the provider's prefix cache on WRITE alone.

# A separator line the model actually produces, and nothing else: the prompt
# bans markup, so a bare rule can only be the one we asked for.
PART_SEP = re.compile(r"^\s*-{3,}\s*$", re.M)

# Source characters worth one video. A 75-second narration is ~195 Russian
# words, and an English source spends fewer characters on the same events than
# the retelling does, so the threshold sits above the narration's own length.
# source.py never fetches past MAX_CHARS = 4000, which is where MAX_PARTS lands.
PART_CHARS = 1800
MAX_PARTS = 3

# How many characters of English source make one word of it. Only the horror
# slot needs the number, and it is derived rather than picked: ~5.7 characters
# to an English word, a faithful retelling comes back at roughly the same word
# count in Russian, and the viewer hears _heard_wpm() of them a minute. So a
# 4000-character post is about five minutes told in full.
SOURCE_CHARS_PER_WORD = 5.7


def part_count(post: dict) -> int:
    """How many videos this post is worth, by length of the source alone.

    A guess, made before spending an LLM call: the model still gets to answer
    with fewer parts if the material is thinner than the character count says.
    """
    # The horror slot is one video whatever the source weighs. Splitting is a
    # device for a 75-second feed - a cliffhanger works because the answer is
    # an hour away and the viewer is still scrolling. A scary story cut in half
    # is a scary story whose ending is in a different video, and target_sec()
    # already gives it the runtime the split was buying.
    if post.get("sub") in SUBREDDITS_HORROR:
        return 1
    return min(len(post["text"]) // PART_CHARS + 1, MAX_PARTS)


def _wpm() -> int:
    """What the ENGINE speaks at, measured on its own output by `python voice.py`."""
    return WPM.get(OUTPUT_LANG, WPM["en"])


def _heard_wpm() -> float:
    """What the VIEWER hears, which is the engine's rate times the speed-up.

    The two were the same thing until VOICE_SPEEDUP existed, and conflating them
    afterwards cost the English channel eleven percent of every video. The budget
    is a duration expressed in words, so it has to be counted at the rate the
    finished file actually plays at: 230 words of engine speech is 78 seconds
    before the shift and 70 after it, so a channel aiming at 75 shipped 67 and
    left the story eight seconds of detail short. Measured on hu9xlv, 2026-08-16.
    """
    return _wpm() * VOICE_SPEEDUP


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


def target_sec(post: dict) -> int:
    """Seconds of narration this story is worth.

    The feed is a flat TARGET_SEC and stays that way: a family row told in
    three minutes is a family row nobody watched to the end of.

    The horror slot is scaled by the length of its source instead, up to
    HORROR_SEC. Both ends of that matter. A scary story squeezed into 75
    seconds loses the detail it lives on - the second set of footprints goes
    first, because it is not plot. And a flat five minutes handed to a
    600-word post is an invitation to pad, which the prompt bans anyway, so
    the short ones stay short and only the long ones run.
    """
    if post.get("sub") not in SUBREDDITS_HORROR:
        return TARGET_SEC
    full = round(len(post.get("text") or "") / SOURCE_CHARS_PER_WORD
                 / _heard_wpm() * 60)
    return max(TARGET_SEC, min(HORROR_SEC, full))


def _target_words(sec: int = 0) -> int:
    """Budget for title plus narration together - both are spoken.

    CTA_SEC rides on top of TARGET_SEC so the closing question is paid for out
    of extra runtime instead of out of the ending.

    Counted at the rate the FINISHED video plays at, not the engine's - see
    _heard_wpm(). On a channel with no speed-up the two are identical and this
    changes nothing; on one with a speed-up it is the difference between
    hitting TARGET_SEC and falling short of it by the whole factor.
    """
    return round(((sec or TARGET_SEC) + CTA_SEC) / 60 * _heard_wpm())


def _fits(total: int, target: int) -> bool:
    return target * (1 - TOLERANCE) <= total <= target * (1 + OVER)


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

# Closing questions that ask nothing about the story they close. Same disease as
# WEAK_TITLE and the same reason for a check: the prompt has said to name what
# actually happened from the start, and it loses often enough to matter - the
# comments this post was picked for were arguing about one particular thing, and
# a question that could sit under any video throws that argument away.
#
# It lost partly to the prompt itself, which handed the model "А как бы вы
# поступили на моём месте?" as the shape to copy while forbidding it two rules
# above. That example is specific now, and this catches the rest.
WEAK_CTA = {
    "ru": re.compile(
        # nearly every one of these opens on "А", which is part of the shape the
        # prompt asks for and no part of what makes the line stock
        r"^(?:а|и|ну|так)?\s*(?:"
        r"как\s+бы\s+вы\s+поступил"
        r"|что\s+бы\s+вы\s+сделал"
        r"|вы\s+бы\s+как\b"
        r"|кто\s+(?:из\s+нас\s+|тут\s+|здесь\s+)*(?:прав|не\s*прав|виноват)"
        r"|как\s+думаете,?\s+что\s+(?:было|будет)\s+дальше"
        r"|что\s+(?:вы\s+)?думаете\s*\??$"
        r"|как\s+вы\s+считаете\s*\??$"
        r"|что\s+скажете\s*\??$"
        r"|на\s+чьей\s+вы\s+стороне\s*\??$"
        r")", re.IGNORECASE),
    "en": re.compile(
        r"^(?:and|so|but)?\s*(?:"
        r"what\s+would\s+you\s+(?:have\s+)?(?:do|done)\b"
        r"|would\s+you\s+have\s+done\s+the\s+same\b"
        r"|who(?:['’]?s|\s+is|\s+was)\s+(?:in\s+the\s+)?(?:right|wrong)\s*\??$"
        r"|whose\s+side\s+are\s+you\s+on\s*\??$"
        r"|what\s+do\s+you\s+think\s*\??$"
        r"|(?:so\s+)?am\s+i\s+the\s+(?:asshole|jerk)\b"
        r"|thoughts\s*\??$"
        r")", re.IGNORECASE),
}

# Words the question is built out of rather than words it is about. Stripped
# before asking whether the question named anything from the story, or every
# question would pass on "вы" alone.
CTA_FRAME = {
    "ru": {"а", "вы", "бы", "как", "что", "это", "так", "тут", "ещё", "или",
           "если", "него", "неё", "них", "его", "её", "их", "меня", "моём",
           "моей", "месте", "думаете", "считаете", "поступили", "сделали",
           "стали", "смогли", "были", "быть", "такое", "таком", "такой"},
    "en": {"what", "would", "you", "have", "the", "and", "for", "with", "that",
           "this", "them", "him", "her", "his", "she", "was", "were", "been",
           "done", "did", "your", "my", "in", "my", "place", "same", "do",
           "think", "about", "it", "if", "on", "at", "to", "of", "a", "an"},
}

# Ten was the number while a title stated one bare fact. It states two now - a
# finished statement, then the turn that makes it absurd - and the second half
# does not fit in what is left after the first.
#
# One number for ordinary videos and split ones alike. The split story's own
# wider ceiling was also twelve, so raising this one collapsed the two into the
# same value; a ceiling that no longer distinguishes anything is a constant
# pretending to be a rule, so it is gone rather than kept at parity.
#
# Two costs, both measured rather than assumed. The card sets at 82px between
# 120px margins, and twelve Russian words take FOUR lines, not the three the
# old comment here claimed - "Я чуть не потеряла сознание в баре, а мой парень
# продолжал танцевать" is the fixture that shows it. Still legible, and it is
# the cover frame, so it is taller rather than broken. And twelve words is
# about 4.8 seconds of narrated card against 3.6 for nine - the card IS the
# hook, so if retention at three seconds moves the wrong way, this is the first
# number to put back.
MAX_TITLE_WORDS = 12

# Numbers spelled out in the title: "восемьсот долларов" where "800 долларов"
# belongs. Full words only - a stem like "пят" would fire on "пятница".
# NOTE: tens and up only. Numerals below twenty share stems with ordinary
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


def _spelled_number(t: str, lang: str) -> str:
    """The number written out in letters, or "" - "140 тысяч" is not one.

    The rule bans a figure the eye cannot catch while scrolling, and it used to
    fire on the scale word alone. That swept up the mixed form as well: "140
    тысяч" HAS its digit, reads faster than "140000", and is what a person
    actually writes - two titles in a row were refused for it. So the complaint
    only stands when nothing in front of the scale word is a digit.

    English gets the same test and keeps its own exception for free: "20k"
    matches with the digit INSIDE the match rather than before it, so it is
    still refused - that one is about the ear, not the eye, since the engine
    reads it as one mumbled syllable.
    """
    for m in SPELLED_NUMBER[lang].finditer(t):
        if not re.search(r"\d[\d\s]*$", t[:m.start()]):
            return m.group()
    return ""


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
# NOTE: a hand-picked list, not a dictionary. These are the ones whose other
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
# NOTE: stems, like tags.py, but two of them are written the long way round
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


# Two or three per two hundred words is what the prompt asks for. Only the
# FLOOR is enforced, and only at zero: a story with no loud line at all is the
# failure we actually measured - 43 sentences and not one mark - while a story
# with four is merely at the top of its budget, and refusing that would cost a
# rewrite for something no listener would notice.
EXCLAIM_PER = 200


def _flat_fault(body: str, lang: str = "") -> str:
    """Empty unless the narration is punctuated entirely on full stops.

    A rule in the prompt was not enough on its own, which is the same thing
    every other check in this file exists to say. The engine takes its contour
    from punctuation and from nothing else, so a narration written on nothing
    but full stops is read on one note however good its words are - and the
    model reverts to exactly that unless it is told, in the answer it gets back,
    that it did.
    """
    t = plain(body)
    n = len(t.split())
    if n < EXCLAIM_PER or "!" in t:
        return ""
    return ("the narration is punctuated entirely on full stops and will be "
            "read on one note - find the line where somebody actually raised "
            "their voice and end it on `!`, two or three times in the whole "
            "text and nowhere else")


def _kin_fault(text: str, lang: str = "") -> str:
    """Empty unless the text names a relative by a word nobody says any more."""
    t = plain(text)
    for pat, fix in ARCHAIC_KIN.get(lang or OUTPUT_LANG, ()):
        m = pat.search(t)
        if m:
            return (f"\"{m.group()}\" is a word a young viewer has to stop and "
                    f"work out - name the person plainly instead, \"{fix}\"")
    return ""


# Measured over the ru channel's first month. Narrations where nobody speaks
# in the first hundred words sat at the base push - median 1378 views, 1.8% of
# them liked; the ones with a line of speech before that ran 2-5k at 4.5%. The
# prompt asks for forty, which is where the breakouts actually landed. This is
# the looser bound on purpose: at forty the check refuses the best video the
# channel has had, which opens on a moment and quotes nobody until word 45.
OPENING_WORDS = 100


def _open_fault(body: str) -> str:
    """Empty unless the story is in front of its first moment, not inside it.

    A quoted line is the proxy: the opening the prompt asks for is a person
    doing or saying something on a particular day, and the cheapest evidence
    that it was written is somebody speaking. Part 1 only - a later part opens
    on its recap line, which is a summary on purpose.
    """
    said = plain(body).find("«")
    if said >= 0 and _words(plain(body)[:said]) < OPENING_WORDS:
        return ""
    return (f"nobody speaks in the first {OPENING_WORDS} words - it opens on a "
            "standing fact instead of a moment. Put the line the conflict "
            "starts with there, in «angle quotes»")


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
    n = _spelled_number(t, lang)
    if n:
        return (f"the TITLE spells the number \"{n}\" out in letters - start it "
                "with a digit, \"20000\" or \"20 тысяч\", never \"двадцать тысяч\""
                if lang == "ru" else
                f"the TITLE writes the number \"{n}\" out in words - start it "
                "with a digit, \"20000\" or \"20 thousand\", never \"twenty "
                "thousand\"")
    c = CURRENCY[lang].search(t)
    if c:
        return (f"the TITLE names a currency in \"{c.group().strip()}\" - drop "
                "the unit and keep the bare figure, "
                + ("\"счёт на 8000\"" if lang == "ru" else "\"a bill for 8000\""))
    h = HOMOGRAPH[lang].search(t)
    if h:
        return (f"\"{h.group()}\" is said two ways and the TITLE is too short "
                "for the engine to guess right - say it with a different word")
    # The title is stated, never exclaimed and never asked. The prompt has said
    # so since the beginning and the model obeyed, because nothing in the
    # narration used those marks either - now the narration budgets them on
    # purpose (see the punctuation rule in prompts.py) and the habit leaks. A
    # title that shouts reads as clickbait on the card and is narrated with the
    # lift that belongs to the story's own loud line.
    # END of the line only: a mark INSIDE a quoted line is how several titles
    # already work - «Не дам [emphasis] деньги на спорткар!» - отказал сыну -
    # and that shape is not what this is about.
    if t.rstrip("»”\"").endswith(("!", "?")):
        return ("the TITLE ends on an exclamation or question mark - it is one "
                "stated line, so end it on the last word with no mark at all")
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


def _cta_weak_fault(cta: str, story: str, lang: str = "") -> str:
    """Empty when the closing question is about THIS story.

    Two ways it can fail to be. It can be one of the stock lines, which WEAK_CTA
    carries. Or it can be shaped right and still name nothing the narration
    mentioned - "А вы бы поменяли график?" closing a story that spent two
    hundred words calling that thing a список. The second is the milder fault
    and much the harder one to see by eye, which is exactly why it is worth a
    check instead of one more line in the prompt.

    Matched on four-character prefixes, which is as much stemming as this needs:
    "график" against "графику" is the case that matters, and a check asking only
    "did the question name ANYTHING from the story" has to err towards letting
    things through - a false refusal costs a whole rewrite.
    """
    lang = lang or OUTPUT_LANG
    q = plain(cta).strip()
    if WEAK_CTA[lang].match(q):
        return ("the closing question is a stock line that would fit any video "
                "- ask it about what actually happened in THIS story")
    words = [w for w in re.findall(r"\w+", q.lower())
             if len(w) >= 5 and w not in CTA_FRAME[lang]]
    if not words:
        return ("the closing question names nothing out of the story - put into "
                "it the person it is about, or the thing that was done")
    seen = {w[:4] for w in re.findall(r"\w+", plain(story).lower())}
    if not any(w[:4] in seen for w in words):
        return ("the closing question is about "
                + ", ".join(f'"{w}"' for w in words[:3])
                + " - and the narration mentions none of them. Ask about "
                "something the story actually named")
    return ""


def _ending_fault(body: str, final: bool = True, markup: bool = True,
                  lang: str = "") -> str:
    """Empty when the narration closes properly.

    `markup` off checks the words alone, which is all stage one writes: the
    question has to be there and has to be its own sentence, but the cue and the
    mark that dress it are not written until the pass after.

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
    story, cta = split_cta(body)
    if not cta:
        return "the closing question must stand as its own final sentence"
    # What the question SAYS is checked in both stages: those are stage one's
    # words, and stage two is forbidden from touching them, so a stock question
    # that reaches stage two is a fault stage one should have been told about.
    if weak := _cta_weak_fault(cta, story, lang):
        return weak
    return _cta_fault(cta) if markup else ""


def _emphasis_fault(body: str) -> str:
    """Empty when the narration is stressed sentence by sentence.

    One [emphasis] per sentence, never two - a rule the prompt has always stated
    and nothing has ever checked. The title's mark is checked, the closing
    question's is checked, and the two hundred words between them were not: a
    narration can arrive with a single mark in the whole text, read flat by the
    voice engine, and pass every other gate on its way to the render. Measured
    on one story told three times over, the count came back 20, 1 and 19, so the
    drift is not a rare miss - it is a coin flip.

    Two thirds rather than all of them. The point is to catch a narration that
    is not marked up at all, not to send one back over a short sentence somebody
    left bare.
    """
    sentences = [s for s in SENTENCE.split(body.strip()) if plain(s).strip()]
    if not sentences:
        return ""
    marked = 0
    for s in sentences:
        n = s.lower().count("[emphasis]")
        if n > 1:
            return (f"one sentence carries {n} [emphasis] - it takes exactly "
                    "one, in front of the word that sentence exists to deliver")
        marked += n
    if marked * 3 < len(sentences) * 2:
        return (f"only {marked} of the {len(sentences)} sentences carry an "
                "[emphasis] - every sentence takes one, in front of the word it "
                "exists to deliver")
    return ""


def _drift_fault(tagged: str, written: str) -> str:
    """Empty when the polish pass added brackets and changed nothing else.

    This is the whole safety of splitting the call in two. Stage one's words
    were written against the source post and checked against rules stage two
    never sees - length, premise, scenes, the ending, the gendered verbs - so a
    stage two that rewrites while it marks up silently voids every one of those
    checks, and it does it in the one call with no way to tell it went wrong.
    Strip the brackets back off and the text must be the text that went in.

    Compared word by word rather than character by character: plain() already
    folds the whitespace the cues leave behind, and a complaint that can name
    the word it stopped at is one the model can act on.
    """
    a, b = plain(tagged).split(), plain(written).split()
    if a == b:
        return ""
    for n, (x, y) in enumerate(zip(a, b), 1):
        if x != y:
            return (f"the narration was changed at word {n}: you wrote \"{x}\" "
                    f"where the text you were given says \"{y}\" - put the words "
                    "back exactly and add only what goes inside square brackets")
    return (f"the narration came back {len(a)} words against the {len(b)} you "
            "were given - put back what you dropped and add only what goes "
            "inside square brackets")


def guess_gender(post: dict) -> str:
    """Fallback when the model forgets the tag: Reddit's own (28F) / (25M) markers."""
    m = re.search(r"\b\d{1,2}\s*([MFmf])\b", f"{post.get('title', '')} {post.get('text', '')}")
    return "female" if m and m.group(1).lower() == "f" else "male"


def _split(raw: str, fallback_gender: str = "male",
           want_gender: bool = True) -> tuple[str, str, str]:
    """Pull NARRATOR: and TITLE: off the front. Returns (gender, title, body).

    `want_gender` off silences the missing-NARRATOR warning: stage two is never
    asked for that line, so its absence there is the shape working, not a miss.
    """
    g = re.search(r"NARRATOR:\s*(male|female)", raw, re.IGNORECASE)
    gender = g.group(1).lower() if g else fallback_gender
    if not g and want_gender:
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
    # The title keeps its cues, exactly as the body does. It is drawn on
    # screen, but nothing draws it raw: card.build() runs plain() over it, the
    # card's word timings come from the aligner, which works on plain text, and
    # the meta file publishing reads is written plain. Stripping here instead
    # cost the title card the only delivery the model could give it.
    return gender, _clean(title).rstrip(" .,:;-"), _clean(body)


def _chunks(raw: str, parts: int) -> tuple[list[str], list[str]]:
    """(one chunk per part, complaints). Capped at `parts`, never past it."""
    chunks = [c for c in PART_SEP.split(raw) if c.strip()] if parts > 1 else [raw]
    faults = []
    # Fewer parts than asked is allowed down to two - the prompt tells the model
    # to write fewer rather than stall, and a thin third part is worse than none.
    if not (2 <= len(chunks) <= parts if parts > 1 else len(chunks) == 1):
        faults.append(f"you wrote {len(chunks)} parts - write between 2 and "
                      f"{parts}, separated by a line of three dashes")
    # counted before this, so writing four parts when asked for three is a
    # complaint rather than a silent truncation
    return chunks[:parts], faults


# A title line, with or without a NARRATOR line above it. Matched off the RAW
# chunk, because _clean() folds every newline into a space and after it there is
# no telling where the title stopped.
STRAY_TITLE = re.compile(r"\s*(?:NARRATOR:[^\n]*\n\s*)?TITLE:[^\n]*")


def _parse_write(raw: str, post: dict, parts: int, target: int,
                 lang: str = "") -> tuple[tuple[str, list[str]], list[str]]:
    """((gender, [body, ...]), complaints) for stage one's plain narration.

    Everything checked here is a property of the WORDS: the length, the ending,
    the words nobody says out loud. Nothing here looks at markup, because there
    is none yet - stage two writes all of it, and its own checks cover it.
    """
    chunks, faults = _chunks(raw, parts)
    gender, out = "", []
    for i, chunk in enumerate(chunks, 1):
        label = f"part {i}: " if parts > 1 else ""
        if i == 1:
            g = re.search(r"NARRATOR:\s*(male|female)", chunk, re.IGNORECASE)
            if g:
                gender, chunk = g.group(1).lower(), chunk[g.end():]
            else:
                log.warning("no NARRATOR: tag, falling back to the post")
        if m := STRAY_TITLE.match(chunk):
            faults.append(label + "it writes a TITLE: line - no title is "
                          "written at this step, only the narration")
            chunk = chunk[m.end():]
        body = _clean(chunk)

        # the model can introduce what the source did not have, so re-check
        if hit := safety.blocked(body):
            raise Unsuitable(f"generated text tripped the blocklist ({hit})")

        # A cue here is not harmless. Stage two adds its own, and the two sets
        # collide: an [emphasis] already in place makes the sentence look done,
        # so the mark that belongs on the right word never arrives.
        if TAG.search(body):
            faults.append(label + "it carries cues in square brackets - the "
                          "narration is written in plain words, and every "
                          "bracket in it is added by the pass after this one")

        faults += [label + f for f in
                   (_kin_fault(body, lang),
                    _flat_fault(body, lang),
                    _open_fault(body) if i == 1 else "",
                    _ending_fault(body, final=i == len(chunks), markup=False,
                                  lang=lang))
                   if f]
        n = _words(body)
        if not _fits(n + TITLE_WORDS, target):
            faults.append(f"{label}it is {n} words, rewrite to about "
                          f"{target - TITLE_WORDS} - "
                          + ("cut it down" if n + TITLE_WORDS > target
                             else "expand it"))
        out.append(body)
    return (gender or guess_gender(post), out), faults


def _parse_polish(raw: str, written: list[str], parts: int, post: dict,
                  lang: str = "") -> tuple[tuple[str, list[str]], list[str]]:
    """((title, [tagged, ...]), complaints) for stage two's marked-up answer.

    A split story has ONE title, written once at the top, and every part is
    returned carrying it. That is what the viewer gets: the title is narrated
    and lit on the card at the head of each part, so a story that renamed itself
    halfway looked like three unrelated videos. The "Часть N" line under it is
    the renderer's, added to the card and never to this text.
    """
    chunks, faults = _chunks(raw, parts)
    if len(chunks) != len(written):
        faults.append(f"you returned {len(chunks)} parts where {len(written)} "
                      "were given - every one of them comes back, in the same "
                      "order, separated by the same lines of three dashes")
    title, out = "", []
    for i, chunk in enumerate(chunks, 1):
        label = f"part {i}: " if parts > 1 else ""
        if i == 1:
            _, title, body = _split(chunk, want_gender=False)
        else:
            # Only the first chunk carries the title. A stray TITLE: on a later
            # part is narrated as that part's opening line, so strip it and
            # complain, and the rewrite drops it.
            body = chunk
            if m := STRAY_TITLE.match(body):
                faults.append(f"part {i}: it writes its own TITLE: line - the "
                              "title is written ONCE, above part 1, and is the "
                              "title of every part")
                body = body[m.end():]
            body = _clean(body)

        if i <= len(written):
            faults += [label + f for f in [_drift_fault(body, written[i - 1])] if f]
        faults += [label + f for f in
                   (_ending_fault(body, final=i == len(chunks), lang=lang),
                    _open_fault(body) if i == 1 else "",
                    _emphasis_fault(body))
                   if f]
        out.append(body)

    # Once, not per part: one title, one complaint, and a rewrite that is not
    # told the same thing three times over.
    if hit := safety.blocked(title):
        raise Unsuitable(f"generated title tripped the blocklist ({hit})")
    faults += [f for f in (_title_fault(title, lang),
                          _kin_fault(title, lang)) if f]
    return (title, out), faults


def _ask(client, system: str, user: str, check, keep: str,
         skippable: bool = False):
    """One model call, checked, with one rewrite if the answer has faults.

    `check(raw)` returns (result, complaints). The last answer is returned even
    when complaints remain: a narration four percent short beats no narration,
    and that is the call this has always made.
    """
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    result, faults = None, []
    # Three, not two, since LLM_REASONING turned a rewrite from thirty thousand
    # tokens into four hundred. Measured on one story written four ways: the
    # markup stage is the one that needs the extra go - without thinking it
    # under-marks, coming back with [emphasis] on 8 of 20 sentences where the
    # rule is every sentence, and the second attempt is not always enough. An
    # attempt now costs less than a hundredth of what buying the thinking back
    # would, so this is where that money goes.
    for attempt in range(3):
        resp = client.chat.completions.create(
            model=LLM_MODEL, messages=msgs, max_tokens=LLM_MAX_TOKENS,
            reasoning_effort=LLM_REASONING)
        # DeepSeek caches the constant prefix automatically - the system prompt
        # stays first so it hits every call. These counters are how you confirm
        # it, and after the split there are two prefixes to watch instead of one.
        u = resp.usage
        # `out` is the whole bill, and thinking is counted in it - print the two
        # apart so a reasoning setting that is quietly back on is visible here.
        d = getattr(u, "completion_tokens_details", None)
        log.info("tokens: %d in (%d cached), %d out (%d thinking)",
                 u.prompt_tokens, getattr(u, "prompt_cache_hit_tokens", 0),
                 u.completion_tokens, getattr(d, "reasoning_tokens", 0) or 0)

        raw = resp.choices[0].message.content
        if skippable and (m := re.match(r"\s*SKIP:\s*(.*)", raw)):
            raise Unsuitable(m.group(1).strip()[:120] or "no reason given")

        result, faults = check(raw)
        if not faults:
            return result
        log.warning("attempt %d rejected: %s", attempt + 1, "; ".join(faults))
        msgs += [{"role": "assistant", "content": raw},
                 {"role": "user", "content":
                  "Rewrite it. Problems: " + "; ".join(faults) + ". " + keep}]

    log.warning("accepting as is (%s)", "; ".join(faults))
    return result


def write_script(post: dict, parts: int = 1) -> tuple[str, list[tuple[str, str]]]:
    """(gender, [(title, narration), ...]) - one entry per video, each TARGET_SEC.

    Two calls, not one. The first writes words and only words; the second writes
    the title and adds the delivery markup to those words without touching them.
    The seam is exactly that: stage one owns everything that is text, stage two
    owns everything inside square brackets, and _drift_fault() holds the line.

    It was one call for a long time, and the reason to split it was measured
    twice. A prompt carrying the title rules, the markup rules and the prose
    rules at once drops single lines out of the middle of itself - see the
    counts in _emphasis_fault(), which came back 20, 1 and 19 on one story told
    three times. And a fault in the markup used to cost a rewrite of the whole
    answer, so a story could arrive correctly marked up and worse written than
    the draft it replaced. Now a markup fault retries the markup.

    The title is written last, from the finished narration, which kills a whole
    class of fault on its own: a title cannot promise what the narration does
    not deliver when the narration is the only thing it was written from.

    parts > 1 splits the post across that many videos in a SINGLE stage-one
    call: the model needs the whole plot in front of it to choose where the cuts
    fall and to end each part on purpose rather than wherever the budget ran out.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty - fill in .env")

    from openai import OpenAI

    WRITE, POLISH, MULTI, GENRE = _prompts()
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    sec = target_sec(post)
    target = _target_words(sec)
    lang, name = OUTPUT_LANG, LANG_NAME[OUTPUT_LANG]
    # Which slot this story came from is written nowhere but its sub, and both
    # prompts need to know: the horror slot leads on a different thing, ends on
    # a different line and builds its title's second beat out of something else.
    # Everything else in either prompt is the same text - see prompts.GENRE. A
    # language with no horror block falls back to drama, which is what the
    # English channel does, having no horror subs to reach this with anyway.
    kind = "horror" if post.get("sub") in SUBREDDITS_HORROR else "drama"
    genre = GENRE[lang].get(kind) or GENRE[lang]["drama"]

    system = WRITE[lang].format(lang=name, **genre)
    if parts > 1:
        system += MULTI[lang].format(n=parts)
    ask = (f"{'Each part' if parts > 1 else 'The'} narration must be about "
           f"{target - TITLE_WORDS} words. With the title read over it that is "
           f"{sec + CTA_SEC} seconds of speech, the last {CTA_SEC} of "
           "them the closing question"
           + (", which only the final part has.\n\n" if parts > 1 else ".\n\n")
           + f"Title: {post['title']}\n\nBody:\n{post['text']}")
    gender, written = _ask(
        client, system, ask,
        lambda raw: _parse_write(raw, post, parts, target),
        "Keep the plot and keep the NARRATOR: line.", skippable=True)

    if not written:
        raise Unsuitable("the model returned nothing usable")

    # The parts go over as one text, separated the way they came back. Stage two
    # writes ONE title for the whole story, so it has to see the whole story.
    ask = ("Here is the finished narration. Write its title, and add the "
           "delivery markup to it without changing a single word.\n\n"
           + "\n---\n".join(written))
    title, tagged = _ask(
        client, POLISH[lang].format(lang=name, **genre), ask,
        lambda raw: _parse_polish(raw, written, parts, post),
        "Keep the narration exactly as it was given to you.")

    # A stage two that came back short would otherwise drop a part on the floor.
    # Its own words, unmarked, are worth more than a video that is not there.
    if len(tagged) < len(written):
        log.warning("polish returned %d of %d parts, the rest go out unmarked",
                    len(tagged), len(written))
        tagged += written[len(tagged):]

    log.info("%d words across %d part(s)",
             sum(_words(title) + _words(b) for b in tagged), len(tagged))
    return gender, [(title, b) for b in tagged]


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

    # One [emphasis] a sentence in the narration - the rule the prompt states
    # and nothing checked until a real answer came back with one mark in 189
    # words and passed every other gate on its way to the render.
    _one = "Я [emphasis] пришла домой. Он [emphasis] молчал. Свет [emphasis] горел."
    assert _emphasis_fault(_one) == "", _emphasis_fault(_one)
    assert _emphasis_fault("") == ""
    _bare = "Я пришла домой. Он молчал. Свет [emphasis] горел. Дверь была открыта."
    assert "1 of the 4" in _emphasis_fault(_bare), _emphasis_fault(_bare)
    # one bare sentence in a marked-up text is not worth a rewrite
    assert _emphasis_fault(
        "Я [emphasis] пришла. Он [emphasis] молчал. Свет горел.") == ""
    assert "carries 2" in _emphasis_fault("Я [emphasis] пришла [emphasis] домой.")
    # a cue in front of a sentence must not hide it from the count
    _cued = ("[nervous] Я [emphasis] открыла дверь. [me, calm] «Ты [emphasis] дома?» "
             "[sighing] Он [emphasis] не ответил.")
    assert _emphasis_fault(_cued) == "", _emphasis_fault(_cued)

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
    # The horror slot is scaled by the length of its source and stopped at the
    # ceiling, and it is never split - target_sec() is what buys the runtime
    # the split used to.
    _real_horror = SUBREDDITS_HORROR
    globals()["SUBREDDITS_HORROR"] = ["_selftest_horror"]
    _thin = {"sub": "_selftest_horror", "text": "x" * 600}
    _mid = {"sub": "_selftest_horror", "text": "x" * 4000}
    _fat = {"sub": "_selftest_horror", "text": "x" * 20000}
    _feed = {"sub": "AmItheAsshole", "text": "x" * 4000}
    assert target_sec(_thin) == TARGET_SEC, "a thin source is not padded out"
    assert target_sec(_fat) == HORROR_SEC, "and a long one stops at the ceiling"
    assert TARGET_SEC < target_sec(_mid) < HORROR_SEC, target_sec(_mid)
    assert target_sec(_feed) == TARGET_SEC, "the feed does not move"
    assert part_count(_fat) == 1, "the horror slot is one video, whatever it weighs"
    assert part_count(_feed) > 1, "...and the feed still splits what is long"
    # The horror slot has to be worth its own prompt, its own voice and four
    # times the render: a ceiling that is not far above the feed's target is a
    # slot that buys nothing. Was 3x while the feed sat at 87 seconds; the feed
    # moved to 130 and this tracks the relation, not the old multiple.
    assert _target_words(HORROR_SEC) > 2 * _target_words()
    globals()["SUBREDDITS_HORROR"] = _real_horror

    tw = _target_words()
    assert _fits(tw, tw) and not _fits(tw * 2, tw) and not _fits(3, tw)
    # The band is asymmetric on purpose: a story with more to say than the
    # target holds is let through, a story that ran out of material is not.
    assert OVER > TOLERANCE, "only the ceiling was meant to move"
    assert _fits(round(tw * (1 + TOLERANCE)) + 1, tw), "the old ceiling is inside"
    # floor, not round: the ceiling is tw * (1 + OVER) exactly, and rounding
    # a .5 upwards lands one word PAST it - which is a self-test that fails
    # for some targets and not others rather than a budget that is wrong.
    ceiling = int(tw * (1 + OVER))
    assert _fits(ceiling, tw), "and the new one is the ceiling"
    assert not _fits(ceiling + 2, tw), "but it is still a ceiling"
    assert not _fits(round(tw * (1 - TOLERANCE)) - 2, tw), "the floor did not move"
    assert tw > round(TARGET_SEC / 60 * _heard_wpm()), "the CTA needs its own words"
    # The budget is a duration in disguise, so it has to be counted at the rate
    # the finished file plays at. Off by the speed-up factor it aims short by
    # exactly that much - eleven percent of every English video, story included.
    assert abs(tw / _heard_wpm() * 60 - (TARGET_SEC + CTA_SEC)) < 1, \
        f"{tw} words is not {TARGET_SEC + CTA_SEC}s at {_heard_wpm():.0f} wpm"
    assert (_heard_wpm() == _wpm()) == (VOICE_SPEEDUP == 1.0), \
        "the speed-up must move the heard rate and nothing else"

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

    # The narration budgets exclamation marks on purpose now, and the habit
    # leaks upward - a title is stated, never exclaimed and never asked.
    assert _title_fault("Соседка прислала мне [emphasis] счёт за свой потоп!", "ru"), \
        "an exclaimed title"
    assert _title_fault("А вы бы [emphasis] оплатили соседке её потоп?", "ru"), \
        "a title that asks is the closing question, not a title"
    # ...but a mark INSIDE a quoted line is a shape several titles already use
    _quoted = "«Оплати мой [emphasis] потоп!» - написала соседка сверху"
    assert not _title_fault(_quoted, "ru"), _title_fault(_quoted, "ru")

    # A narration on nothing but full stops is read on one note. Only the floor
    # is checked, and only at zero - see _flat_fault.
    _flat = "Она положила чек на стол и ушла. " * 30
    assert _flat_fault(_flat, "ru"), "an all-full-stops narration must be caught"
    assert not _flat_fault(_flat + "Я не поверил своим глазам!", "ru"), \
        "one mark is inside the budget"
    assert not _flat_fault("Она положила чек и ушла.", "ru"), \
        "a chunk too short to judge is left alone"
    # the mark counts wherever it is, including inside a quoted line
    assert not _flat_fault(_flat + "[мать] «Это не твои деньги!»", "ru")

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
    # ...but the MIXED form keeps its digit, so it is not "spelled out" at all.
    # Refusing it cost two titles in a row before the rule was narrowed.
    assert not ru("Я сэкономил 140 тысяч на технике из-за ошибки")
    assert not ru("Фирма потеряла 100 тысяч из-за одного запрета")
    assert ru("Брат занял пять тысяч и пропал перед свадьбой"), "no digit, no pass"
    assert _spelled_number("140 тысяч", "ru") == ""
    assert _spelled_number("сто сорок тысяч", "ru") == "сто"
    # ordinary words that merely start like a numeral must not trip it
    assert not ru("В четверг похороны, а в пятницу она спросила про деньги")
    assert not ru("Он оставил пятно на платье, а виноватой стала я")
    assert not SPELLED_NUMBER["ru"].search("Ремонт стоит дороже"), '"сто" must not fire on "стоит"'

    # Part 1 opens inside a moment, and a quoted line is what proves it.
    assert not _open_fault("Мы только сели ужинать, когда она положила руки на "
                           "стол. «Или кот, или я».")
    assert _open_fault("Мы с парнем вместе десять месяцев. Он живёт с "
                       "родителями и каждый вечер проводит у меня.")
    assert _open_fault("слово " * (OPENING_WORDS + 5) + "«поздно».")
    assert not _open_fault("слово " * (OPENING_WORDS - 5) + "«вовремя».")

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
    assert en("Neighbor billed me 20k for her own flood"), \
        "the digit is INSIDE the match, so the narrowing must not save 20k"
    assert not en("Neighbor billed me 80000 for her own flood")
    assert not en("Neighbor billed me 80 thousand for her own flood")
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
                             "[doubtful] Would you have [emphasis] left him "
                             "dinner?", lang="en")
    assert not _ending_fault('...And I froze. [angry] “Get out of my house.”',
                             final=False), "a part may end on a line of dialogue"
    # English says the stock question with its own words, so it needs its own list
    assert _cta_weak_fault("Would you have done the same in my place?",
                           "He left.", "en")
    assert _cta_weak_fault("So am I the asshole here?", "He left.", "en")
    assert _cta_weak_fault("What would you have done?", "He left.", "en")
    assert not _cta_weak_fault("Would you have left him dinner?",
                               "He left. Dinner was on the table.", "en")

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
    # Not "А как бы вы поступили на моём месте?", which is what this fixture was
    # until _cta_weak_fault() started refusing it - and that same line was what
    # the prompt handed the model as the shape to copy, two rules below the one
    # forbidding it. The template, the fixture and the output were one sentence.
    GOOD_CTA = "[doubtful] А вы бы [emphasis] простили брата за такое?"
    assert not _ending_fault(f"Брат занял денег и пропал. {GOOD_CTA}")

    # a question that would sit under any video at all
    assert _cta_weak_fault("А как бы вы поступили на моём месте?", "Я ушёл.")
    assert _cta_weak_fault("Кто из нас прав, как думаете?", "Я ушёл.")
    assert _cta_weak_fault("А что бы вы сделали?", "Я ушёл.")
    assert _cta_weak_fault("На чьей вы стороне?", "Я ушёл.")
    # shaped right, and still naming something the story never had - the
    # narration called that thing a список from beginning to end
    assert "график" in _cta_weak_fault(
        "А вы бы поменяли график?",
        "Мы составили список обязанностей. Менять список я отказался.")
    # the same question under the story that does name it
    assert not _cta_weak_fault(
        "А вы бы поменяли график?",
        "Мы повесили график на холодильник. Я показываю на график.")
    # a case ending is not a different word, or every question would be refused
    assert not _cta_weak_fault("А вы бы простили брата за такое?",
                               "Брат занял денег и пропал.")
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

    # STAGE ONE. A two-part answer must come apart cleanly: ONE NARRATOR at the
    # top holding for every part, a cliffhanger in the half that is not the
    # last, and not one square bracket anywhere - the markup is the next pass.
    RAW2 = ("NARRATOR: female\n\n"
            "Соседка забрала мои ключи. «Отдам, когда захочу». И тут я "
            "услышала её шаги на лестнице.\n"
            "---\n"
            "Ключи она вернула через неделю. А вы бы простили соседке эти ключи?")
    POST = {"title": "", "text": ""}
    (g4, p4), f4 = _parse_write(RAW2, POST, 2, 30, "ru")
    assert g4 == "female" and len(p4) == 2, (g4, p4)
    assert p4[1].startswith("Ключи она вернула"), p4[1]
    # the fixture is deliberately far off thirty words; nothing else may be
    # wrong, and the closing question names the keys the story is about
    assert all("words" in f for f in f4), f4
    assert all(f.startswith("part ") for f in f4), "faults must name their part"
    # stage one writes words. A title line is the next step's job done here, and
    # narrated as the part's opening sentence if it survives - strip it, and
    # complain, so the rewrite drops it.
    (_, p10), f10 = _parse_write(
        RAW2.replace("NARRATOR: female\n", "NARRATOR: female\nTITLE: Заголовок\n"),
        POST, 2, 30, "ru")
    assert p10[0].startswith("Соседка забрала"), p10[0]
    assert any("TITLE" in f for f in f10), f10
    # a cue written here is worse than useless: stage two reads a marked
    # sentence as one already done and never puts the mark where it belongs
    (_, _), f11 = _parse_write(RAW2.replace("Соседка забрала", "[sad] Соседка забрала"),
                               POST, 2, 30, "ru")
    assert any("square brackets" in f for f in f11), f11
    # only the last part may address the viewer, and only it must
    RAW_BAD = RAW2.replace("И тут я услышала её шаги на лестнице.",
                           "А вы бы простили соседке эти ключи?")
    (_, _), f7 = _parse_write(RAW_BAD, POST, 2, 30, "ru")
    assert any(f.startswith("part 1") and "not the last" in f for f in f7), f7
    RAW_BAD2 = RAW2.replace("А вы бы простили соседке эти ключи?", "И она ушла.")
    (_, _), f8 = _parse_write(RAW_BAD2, POST, 2, 30, "ru")
    assert any(f.startswith("part 2") and "question" in f for f in f8), f8
    # a stock closing question is stage one's fault, since those are its words
    RAW_WEAK = RAW2.replace("А вы бы простили соседке эти ключи?",
                            "А как бы вы поступили на моём месте?")
    (_, _), f12 = _parse_write(RAW_WEAK, POST, 2, 30, "ru")
    assert any("stock line" in f for f in f12), f12
    # one part where two were asked for is a fault, not a silent single video
    (_, p5), f5 = _parse_write(RAW2.split("---")[0], POST, 2, 30, "ru")
    assert len(p5) == 1 and any("parts" in f for f in f5), f5

    # STAGE TWO takes those words back and may add nothing but brackets.
    WRITTEN = ["Соседка забрала мои ключи. «Отдам, когда захочу». И тут я "
               "услышала её шаги на лестнице.",
               "Ключи она вернула через неделю. А вы бы простили соседке эти ключи?"]
    RAW_P = ("TITLE: Соседка [emphasis] забрала мои ключи и вернула их через "
             "неделю\n\n"
             "Соседка [emphasis] забрала мои ключи. [neighbour, angry] "
             "«Отдам, когда [emphasis] захочу». И тут я услышала её "
             "[emphasis] шаги на лестнице.\n"
             "---\n"
             "Ключи она [emphasis] вернула через неделю. [doubtful] А вы бы "
             "[emphasis] простили соседке эти ключи?")
    (t4, q4), fp = _parse_polish(RAW_P, WRITTEN, 2, POST, "ru")
    assert not fp, fp
    # plain(), because a part keeps its markup all the way to voice.speak_parts
    # - the cues are what the engine reads - and only meta.json gets it
    # stripped. Comparing the raw title against unmarked words could never pass.
    assert plain(t4).startswith("Соседка забрала"), t4
    assert len(q4) == 2 and plain(q4[1]).startswith("Ключи она вернула"), q4
    # The one thing this pass may never do, and the reason the split is safe at
    # all: those words passed checks stage two is not even shown.
    (_, _), fd = _parse_polish(
        RAW_P.replace("Соседка [emphasis] забрала", "Соседка [emphasis] тихо забрала"),
        WRITTEN, 2, POST, "ru")
    assert any("changed at word" in f for f in fd), fd
    assert not _drift_fault("Он [emphasis] ушёл.", "Он ушёл."), "brackets are free"
    assert "word 2" in _drift_fault("Он тихо ушёл.", "Он ушёл.")
    assert "3 words against the 2" in _drift_fault("Он ушёл. Совсем.", "Он ушёл.")
    # a later part writing its own TITLE: is the model reverting to the shape
    # this had when one call did both jobs
    RAW_TITLED = RAW_P.replace(
        "Ключи она [emphasis] вернула",
        "TITLE: Соседка пришла с полицией\n\nКлючи она [emphasis] вернула")
    (_, q9), f9 = _parse_polish(RAW_TITLED, WRITTEN, 2, POST, "ru")
    assert plain(q9[1]).startswith("Ключи она вернула"), q9[1]
    assert any(f.startswith("part 2") and "TITLE" in f for f in f9), f9
    # ...and the shared title is checked ONCE, however many parts there are
    assert sum("the TITLE is" in f for f in f9) <= 1, f9
    # every part that went in comes back, or a video quietly loses its second half
    (_, _), fm = _parse_polish(RAW_P.split("---")[0], WRITTEN, 2, POST, "ru")
    assert any("were given" in f for f in fm), fm
    # One ceiling, and a split story's shared title answers to the same one -
    # it used to have its own, wider, until raising this number made the two
    # equal. Asserted on the length complaint alone: this fixture trips the
    # markup rules too, and it is the ceiling being tested.
    over = " ".join(["слово"] * (MAX_TITLE_WORDS + 1))
    assert f"keep it under {MAX_TITLE_WORDS}" in _title_fault(over, "ru")
    assert "keep it under" not in _title_fault(
        " ".join(["слово"] * MAX_TITLE_WORDS), "ru")
    # and the single-video path must not start splitting on a stray dash line
    (_, q6), _ = _parse_polish(
        "TITLE: Заголовок\n\n---\n\nКлючи она вернула. [doubtful] А вы бы "
        "[emphasis] простили соседке эти ключи?",
        ["Ключи она вернула. А вы бы простили соседке эти ключи?"], 1, POST, "ru")
    assert len(q6) == 1, q6

    # the prompt set has to exist for the channel this process is, or the run
    # dies deep inside write_script() with a KeyError instead of here
    assert OUTPUT_LANG in _prompts()[0] and OUTPUT_LANG in WEAK_TITLE, OUTPUT_LANG

    print(f"logic ok: {OUTPUT_LANG}, engine {_wpm()} wpm"
          + (f" x{VOICE_SPEEDUP} = {_heard_wpm():.0f} heard"
             if VOICE_SPEEDUP != 1.0 else "")
          + f", target {tw} words for {TARGET_SEC}+{CTA_SEC} sec")

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
                  f"[{w} words -> ~{w / _heard_wpm() * 60:.0f} sec of video]")
    else:
        print("OPENAI_API_KEY not set - live run skipped")
