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
    # MEASURED, not guessed. TikTok's "Здоровое поведение" is the category that
    # actually bites this channel, and it bites harder than the adult-themes
    # one everybody expects: on 2026-09-14 "Сестра назвала мою жену жирной"
    # took 28 views and "Я нашел у жены тайник с огромными фаллоимитаторами"
    # took 575, both with the same banner, the same length and the same
    # account. Sex is distributed; a word about somebody's weight is not.
    #
    # SOFTEN swaps these in the caption and was not enough - the audio and the
    # burned-in subtitles keep them, and the platform reads both. So the story
    # is refused outright rather than published with a politer description.
    #
    # Adjectival толст\w* is deliberately absent: толстовка, толстый слой and
    # толстый кошелёк are ordinary words, and this list DROPS a story rather
    # than masking a word in it - a false positive here costs a video. The
    # nouns are safe because nobody calls a hoodie a толстуха.
    #
    # "fat chance" is excluded because it is an idiom about probability that
    # says nothing about anybody's body, and the source half of this gate reads
    # English Reddit, where it is common. жирный шрифт and жирное пятно are the
    # same shape of word and are NOT excluded: they are rare in a story about
    # people, and the cost of being wrong about them is one candidate out of
    # hundreds, against a video that publishes to nobody.
    "body_shaming": r"\b(жирн(ая|ый|ое|ые|ую|ого|ой|ым|ых|ому)|жирух\w*|"
                    r"толстух\w*|толстяк\w*|ожирени\w*|целлюлит\w*|"
                    r"свиномат\w*|тройн\w+\s+подбород\w+|"
                    r"fat(?!\s+chance)(so|ty|ass)?|obese|obesity|lardass)\b",
}

# Russian mat, masked on screen. The prompt tells the model to avoid it;
# this only catches what slips through.
PROFANITY = re.compile(
    # "бля" only - "бла" swept up благотворительный, благодарность and every
    # other благо- word. Narrow beats clever: a missed swear is a smaller
    # failure than a masked ordinary noun in the middle of a sentence.
    r"\b(бля\w*|ху[йёея]\w*|пизд\w*|[её]б\w*|муда[кч]\w*|сука|суки|"
    r"fuck\w*|shit\w*|bitch\w*|cunt\w*)", re.IGNORECASE)

# Swapped in the CAPTION and the description only - not in the audio, not on
# the card. The story keeps the words it was told in; what goes to the platform
# as metadata does not have to read as an attack on how somebody looks.
# TikTok's harassment policy names body shaming outright, and a video read that
# way is quietly kept out of the For You feed rather than taken down - the
# failure that looks like nothing at all, which is why this is worth a table.
#
# STEMS rather than whole words, because the ending carries the agreement:
# жирной -> полной, жирная -> полная, жирным -> полным. That trick only holds
# inside one declension class, which is why this list is short and is not a
# thesaurus - "тупой" would want "наивный" and the same stem swap would turn
# "тупик" into "наивник".
SOFTEN = [
    (re.compile(r"\bжирн", re.IGNORECASE), "полн"),
    (re.compile(r"\bтолст", re.IGNORECASE), "полн"),
    (re.compile(r"\bуродлив", re.IGNORECASE), "непривлекательн"),
    # the genre's own word, and a whole one - "Я мудак, что..." is the shape
    # every AmItheAsshole title comes in
    (re.compile(r"\bмудак\b", re.IGNORECASE), "не прав"),
    (re.compile(r"\bfat\b", re.IGNORECASE), "plus-size"),
    (re.compile(r"\bugly\b", re.IGNORECASE), "plain"),
]

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


def soften(text: str) -> str:
    """Shaming wording swapped for something neutral. Captions only - see SOFTEN.

    The capital is carried across, or a swap at the front of a title lowercases
    it: "Жирная соседка" has to come back "Полная соседка", not "полная".
    """
    for pat, into in SOFTEN:
        text = pat.sub(
            lambda m: into.capitalize() if m.group(0)[:1].isupper() else into, text)
    return text


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

    # The category that actually costs this channel its reach - see BANNED.
    # Both halves, because the source is English and the narration is not.
    assert blocked("Сестра назвала мою жену жирной").startswith("body_shaming:")
    assert blocked("ей бы сбросить вес, а не лезть в чужую жизнь "
                   "с её тройным подбородком").startswith("body_shaming:")
    assert blocked("AITA for calling my sister fat").startswith("body_shaming:")

    # narrow on purpose: these must NOT fire
    for ok in ["I was born in 1988", "we had to kill time at the airport",
               "мы убили целый вечер на это", "the movie was a massacre of good taste",
               "she rated it 8 out of 10",
               # body_shaming's neighbours: ordinary words one letter away from
               # it, and the reason толст\w* is not on the list
               "она надела толстовку", "толстый слой пыли", "толстый кошелёк",
               "fat chance of that happening"]:
        assert blocked(ok) is None, f"false positive on {ok!r}"

    # The caption is what the platform reads the video as, and body shaming in
    # it is a harassment hit that never announces itself. Endings survive the
    # stem swap, and so does the capital at the front of a title.
    assert soften("Сестра назвала мою жену жирной") == "Сестра назвала мою жену полной"
    assert soften("Жирная соседка") == "Полная соседка"
    assert soften("Я мудак, что не вступился") == "Я не прав, что не вступился"
    assert soften("AITA for calling her fat") == "AITA for calling her plus-size"
    assert soften("обычный заголовок без шейминга") == "обычный заголовок без шейминга"
    # ...and it leaves the story alone: mask() is what the screen gets, and the
    # audio gets neither
    assert mask("Я мудак") == "Я м****"

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
