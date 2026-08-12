"""Content gate for TikTok.

Two jobs, deliberately separate:

  blocked()  - hard reject. The story is dropped before it costs an LLM call
               and never becomes a video. Applied to the English source AND
               to the generated narration, because the model can introduce
               things the source did not have.
  mask()     - soften profanity in ON-SCREEN text only. TikTok reads burned-in
               text far more reliably than audio, so this is where masking pays.

Patterns are deliberately narrow. A blocklist that fires on "kill time" or the
year 1988 starves the pipeline of stories, which is a worse failure than an
occasional miss - the LLM prompt is the second line of defence.
"""
import re

# category -> pattern. Both languages: the source is English, the output is not.
BANNED = {
    # no word boundaries: the code hides inside usernames like "Soundman1488"
    "hate_code": r"1488",
    # `hitler` on its own, not only inside "heil hitler". The Russian half has
    # always had the bare гитлер\w*, and the English half listing him only in a
    # compound was an oversight rather than a decision - a fact block came back
    # with "Adolf Hitler was nominated for the Nobel Peace Prize" and walked
    # straight through. Same for суицид below.
    "hate": r"\b(нацис\w*|нацизм\w*|гитлер\w*|свастик\w*|зиг\s*хайль|хайль\s*гитлер|"
            r"nazi\w*|swastika|hitler|heil\s+hitler|white\s+power|ku\s*klux|kkk)\b",
    "sexual_minor": r"\b(педофил\w*|малолетк\w*|pedophil\w*|child\s+porn|underage\s+(sex|girl|boy))\b",
    "sexual_violence": r"\b(изнасилов\w*|изнасил\w*|rape[drs]?|raping|molest\w*|sexual\s+assault)\b",
    # suicid\w* costs the odd "career suicide" and it is worth it: this is the
    # one word TikTok actions most reliably, and the Russian half has blocked
    # самоубийств\w* unconditionally from the start - карьерное самоубийство
    # included. The trade was already taken on one language and simply missed on
    # the other.
    "self_harm": r"\b(суицид\w*|самоубийств\w*|self[\s-]?harm|suicid\w*|"
                 r"kill(ed|ing)?\s+(my|him|her)self|повесил\w*\s+себя|вскрыл\w*\s+вены)\b",
    "extreme_violence": r"\b(теракт\w*|расстрел\w*|terrorist\s+attack|school\s+shooting|"
                        r"mass\s+shooting|beheaded|расчлен\w*)\b",
    # Reads as genocide out of context. Caught a Formula-1 pun ("which race to
    # exterminate") that was harmless as a story but unpostable as a title card.
    "genocide": r"\b(истреб\w*|геноцид\w*|genocide|exterminat\w*|ethnic\s+cleansing)\b",
    "drugs": r"\b(героин\w*|кокаин\w*|метамфетамин\w*|heroin|cocaine|meth(amphetamine)?|fentanyl)\b",
}

# Russian mat, masked on screen. The prompt tells the model to avoid it;
# this only catches what slips through.
PROFANITY = re.compile(
    # "бля" only - "бла" swept up благотворительный, благодарность and every
    # other благо- word. Narrow beats clever: a missed swear is a smaller
    # failure than a masked ordinary noun in the middle of a sentence.
    r"\b(бля\w*|ху[йёея]\w*|пизд\w*|[её]б\w*|муда[кч]\w*|сука|суки|"
    r"fuck\w*|shit\w*|bitch\w*|cunt\w*)", re.IGNORECASE)

_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in BANNED.items()}


def blocked(*texts: str) -> str | None:
    """Category of the first banned term found, or None if the text is clean."""
    for text in texts:
        if not text:
            continue
        for name, pat in _COMPILED.items():
            m = pat.search(text)
            if m:
                return f"{name}:{m.group(0)[:30]}"
    return None


def mask(word: str) -> str:
    """First letter, then stars. Applied per subtitle card, not to the audio."""
    def _star(m):
        w = m.group(0)
        return w[0] + "*" * (len(w) - 1)
    return PROFANITY.sub(_star, word)


if __name__ == "__main__":
    assert blocked("my username was Soundman1488 for years").startswith("hate_code:")
    assert blocked("Он оказался нацистом").startswith("hate:")
    assert blocked("TIFU by telling my boss the truth") is None
    assert blocked("", None or "") is None
    assert blocked("clean title", "но текст про свастику").startswith("hate:")
    # the pun that slipped through: innocent story, unpostable title card
    assert blocked("TIFU by asking Reddit which ethnic group to exterminate"
                   ).startswith("genocide:")
    assert blocked("какую расу истребить").startswith("genocide:")

    # Every category has to fire on BOTH languages. The source is English and
    # the caption's fact block is English until the model touches it, so a term
    # listed in Russian only is a term this gate does not have. Both of these
    # were exactly that, and both reached a caption.
    assert blocked("my brother died by suicide").startswith("self_harm:")
    assert blocked("he was suicidal for months").startswith("self_harm:")
    assert blocked("Adolf Hitler was nominated for a Nobel Peace Prize"
                   ).startswith("hate:")

    # narrow on purpose: these must NOT fire
    for ok in ["I was born in 1988", "we had to kill time at the airport",
               "мы убили целый вечер на это", "the movie was a massacre of good taste",
               "she rated it 8 out of 10"]:
        assert blocked(ok) is None, f"false positive on {ok!r}"

    assert mask("бляха") == "б****"
    assert mask("Что за хуйня") == "Что за х****"
    assert mask("обычное слово") == "обычное слово"

    # ordinary words the mask must leave alone - every one of these was, or
    # nearly was, mangled by a pattern that reached one letter too far
    for ok in ["благотворительный фонд", "благодарность", "благо", "бланк",
               "мудрость", "художник", "хуже некуда", "сукно", "обед",
               "требую", "суконный"]:
        assert mask(ok) == ok, f"false positive: {ok!r} -> {mask(ok)!r}"
    print(f"safety ok: {len(BANNED)} categories")
