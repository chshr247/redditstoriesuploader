"""Three facts at the top of the TikTok caption, to buy the video a read.

The block is not information, it is watch time. TikTok does not rank on "see
more" taps - it ranks on how long the video played, and a viewer reading four
hundred characters is a viewer whose video is still playing. So the shape is
fixed by what it has to do: the first line promises a payoff and withholds it
("the third one is the best"), the facts are numbered so the promise has
something to point at, and the last line turns the read into a comment.

The facts come from SOURCES below - free, no key, one fact per request, all of
them English, so the Russian channel pays one translation call per video. That
is cheaper than hand-writing a pool of a thousand facts, and it never runs out.

Nothing in here may break an upload. Every failure - the API down, no LLM key,
a translation that came back the wrong length - returns an empty block and the
caption falls back to exactly what it was before this file existed.
"""
import hashlib
import json
import logging
import random
import re
import sqlite3
import time
import urllib.request

from config import (DB_PATH, LLM_BASE_URL, LLM_MODEL, OPENAI_API_KEY,
                    OUTPUT_LANG)

# In order, and the order is a preference, not a rotation: the first one is
# general trivia, which is what the block is supposed to be, and the animal
# ones are what it falls back to rather than skipping the block entirely. A run
# that reaches them is a run where uselessfacts was down.
#
# Three hosts and not three endpoints on one, which is the whole point of
# having more than one. Everything without a key and answering general trivia
# turned out to BE uselessfacts - numbersapi.com times out, api-ninjas wants a
# key - so the redundancy here is animal facts, which read fine in this format
# and are at least reliably up.
#
# The third element is the path to the text inside the response, walked by
# _dig(): the three payloads disagree about where they put it and nothing else.
SOURCES = [
    ("uselessfacts", "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en",
     ("text",)),
    ("catfact", "https://catfact.ninja/fact", ("fact",)),
    ("dogapi", "https://dogapi.dog/api/v2/facts?limit=1",
     ("data", 0, "attributes", "body")),
]
# Not a browser and not curl. Two of the three hosts 403 an unknown agent and
# every one of them takes this.
UA = "reddit-story-bot/1.0 (+https://github.com/)"
WANT = 3
# Long enough to be worth the tap, short enough that three of them still read
# as a list. Under 60 characters the endpoint's "fact" is usually a fragment
# with no verb; over 280 it is a paragraph, and the block has to be finished
# inside the story or it buys nothing.
#
# The floor is 60 and not higher because of what the endpoint actually holds:
# sampled over 40 draws its median fact is 71 characters, and 70% clear 60
# while only 15% clear 100. A floor at 100 would be six requests per fact and
# a block that fails to build on a slow day.
MIN_LEN, MAX_LEN = 60, 280
# Reading time is the thing being bought, so length is worth a few extra
# requests: collect up to WANT + SURPLUS candidates and keep the longest WANT.
# Against a median of 71 that is most of the difference between a block read in
# eight seconds and one read in fifteen, for no extra model call.
SURPLUS = 3
# The endpoint is random, so a request can come back with a fact already spent
# or one outside the length band. At a 70% hit rate the surplus above is
# reached in about nine, and a cap at all is what keeps an API that has started
# answering junk from spinning here.
TRIES = (WANT + SURPLUS) * 3
DIGITS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣",
          "5️⃣")

# Rotated per upload for the same reason the hashtags are: thirty videos on one
# account carrying a byte-identical first line is the account's own signature,
# and the promise stops working the second time a viewer reads it. Each of them
# names a number and refuses to say what is in it - that withholding IS the
# mechanism, so a variant that summarises the facts is not a variant.
HOOK = {
    "ru": [
        "Пока слушаешь — рубрика интересных фактов. Последний самый жёсткий \U0001f60a",
        "Пока идёт история — три факта на подумать. Третий добивает \U0001f60a",
        "Пока слушаешь — лови три факта. Последний лучше всех \U0001f643",
        "Три факта, пока играет история. От третьего до сих пор не отошёл \U0001f605",
    ],
    "en": [
        "While you listen — three facts. The last one is the worst \U0001f60a",
        "Three facts while the story plays. Number three hits hardest \U0001f60a",
        "While this plays — facts corner. The last one broke me \U0001f643",
        "Three facts to read while you listen. The third one wins \U0001f605",
    ],
}

# The exit. A number is the cheapest comment there is - no opinion to defend,
# no spelling - which is the entire point: the comment is what TikTok counts,
# not the read that produced it.
CTA = {
    "ru": [
        "На каком сказал «да ладно»? Жду ответ в комментариях \U0001f447",
        "Какой первым полетишь проверять? Жду в комментариях \U0001f447",
        "Один из трёх ты точно не знал. Пиши какой \U0001f447",
        "Какой факт заберёшь себе? Номер в комментариях \U0001f447",
    ],
    "en": [
        "Which one made you say no way? Waiting in the comments \U0001f447",
        "Which one are you googling first? Drop the number \U0001f447",
        "One of these three you did not know. Which \U0001f447",
        "Which fact are you stealing? Number in the comments \U0001f447",
    ],
}

SYSTEM = {
    "ru": (
        "Переведи каждый факт на русский. Сохрани нумерацию и порядок: "
        "один факт — одна строка вида «1. текст». "
        "Ничего не добавляй, не убирай и не выдумывай — только перевод. "
        "Пиши живым разговорным языком, как подпись под коротким видео. "
        "Меры переводи в метрические (мили в километры, фунты в килограммы). "
        "Без markdown, без кавычек вокруг факта."
    ),
}

log = logging.getLogger(__name__)

_URL = re.compile(r"https?://|www\.")
_NUMBERED = re.compile(r"^\s*\d+\s*[.)]\s*(.+?)\s*$", re.M)


def _db():
    db = sqlite3.connect(DB_PATH)
    # Keyed by (id, lang) and not by id alone: the two channels are unrelated
    # accounts, and a fact spent on one has not been seen by the other's
    # viewers. The row holds the TRANSLATED text, so the table doubles as the
    # cache for the LLM call - a fact is translated once, ever.
    db.execute("CREATE TABLE IF NOT EXISTS facts("
               "id TEXT, lang TEXT, text TEXT, ts REAL, PRIMARY KEY(id, lang))")
    return db


def _dig(data, path):
    for step in path:
        data = data[step]
    return data


def _fetch(alive: list) -> str:
    """One fact from the first source in `alive` that answers, or "".

    A source that fails is dropped from `alive` for the rest of the run rather
    than retried: the failures worth surviving here are a host being down or an
    API having changed shape, and both of those are true for every request that
    follows. The caller makes a dozen of these per video, so retrying a dead
    host a dozen times is most of a minute spent learning the same thing.
    """
    while alive:
        name, url, path = alive[0]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                # The endpoints put newlines inside the text often enough to
                # matter - they would break the one-fact-per-line shape.
                return " ".join(str(_dig(json.load(r), path)).split())
        except Exception as e:                  # noqa: BLE001 - see module docstring
            log.warning("facts: %s is out (%s), falling through", name, e)
            alive.pop(0)
    return ""


def _usable(text: str) -> bool:
    # A fact carrying a URL is a citation that got into the text field, and it
    # reads as spam in a caption even when it is genuine.
    return MIN_LEN <= len(text) <= MAX_LEN and not _URL.search(text)


def _translate(texts: list[str], lang: str) -> list[str]:
    """The same facts in `lang`, or [] if the model gave back a different set.

    A partial translation is the one outcome worth refusing: three lines where
    one is still English is worse on a Russian account than no block at all,
    and it is the failure a length check catches for free.
    """
    if lang not in SYSTEM:
        return texts
    if not OPENAI_API_KEY:
        log.warning("facts: OPENAI_API_KEY is empty, no block")
        return []
    from openai import OpenAI      # only the translating channel pays the import

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    resp = client.chat.completions.create(model=LLM_MODEL, messages=[
        {"role": "system", "content": SYSTEM[lang]},
        {"role": "user", "content":
            "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))}])
    out = _NUMBERED.findall(resp.choices[0].message.content or "")
    if len(out) != len(texts):
        log.warning("facts: asked for %d lines, got %d", len(texts), len(out))
        return []
    return out


def take(n: int = WANT, lang: str = "") -> list[str]:
    """`n` facts nobody on this channel has seen, marked spent. [] on any miss.

    All or nothing: two facts under a line that promised three is a worse
    caption than no facts, and the caller has a working fallback either way.
    """
    lang = lang or OUTPUT_LANG
    db = _db()
    seen = {r[0] for r in db.execute("SELECT id FROM facts WHERE lang=?", (lang,))}

    alive = list(SOURCES)
    fresh: dict[str, str] = {}
    for _ in range(TRIES):
        if len(fresh) >= n + SURPLUS:
            break
        text = _fetch(alive)
        # Keyed by the text and not by whatever id the source hands out: only
        # one of the three has ids at all, and the thing that must not repeat
        # is what a viewer reads, which is the text. It also means the same
        # fact reached through two sources is still one fact.
        fid = hashlib.sha1(text.encode()).hexdigest()[:16]
        if text and fid not in seen and fid not in fresh and _usable(text):
            fresh[fid] = text

    if len(fresh) < n:
        log.warning("facts: got %d of %d, skipping the block", len(fresh), n)
        return []

    # Longest first, then back into fetch order: the block is nicer to read
    # when it does not climb from a one-liner to a paragraph every time.
    keep = set(sorted(fresh, key=lambda i: len(fresh[i]), reverse=True)[:n])
    ids = [i for i in fresh if i in keep]
    out = _translate([fresh[i] for i in ids], lang)
    if len(out) != n:
        return []

    # Written only once the translation is in hand: a fact burned on a caption
    # that was never built is a fact nobody ever sees.
    now = time.time()
    with db:
        db.executemany("INSERT OR REPLACE INTO facts(id, lang, text, ts) "
                       "VALUES (?,?,?,?)",
                       [(i, lang, t, now) for i, t in zip(ids, out)])
    return out


def block(lang: str = "", lead: str = "") -> str:
    """The finished caption block, or "" if it could not be built.

    `lead` goes in front of the hook on the same line - it is where the part
    marker ends up, and it belongs on the first line because that is the one
    the feed shows. On its own line it would spend half the visible caption
    saying "Часть 2/3" and nothing else.
    """
    lang = lang or OUTPUT_LANG
    try:
        picked = take(WANT, lang)
    except Exception as e:                      # noqa: BLE001 - see module docstring
        log.warning("facts: %s", e)
        return ""
    if not picked:
        return ""
    # The three facts are one block with no blank lines inside it, and the hook
    # and the CTA are held off by blank ones. That grouping is the layout doing
    # its job: the numbers read as a list to be gone through, while the line
    # that promises them and the line that asks for a comment each have to be
    # noticed on their own.
    numbered = "\n".join(f"{DIGITS[i]} {t}" for i, t in enumerate(picked))
    return (f"{lead}{random.choice(HOOK[lang])}\n\n"
            f"{numbered}\n\n{random.choice(CTA[lang])}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Nothing below touches the network or the model: this file's self-test
    # runs inside publish.py's, which runs before every CLI command in CI.
    # Distinct text per fact, and that is the whole identity now - take() keys
    # on a hash of it, so two facts reading the same are one fact.
    # Bigger than any one test needs: the table below is never reset, so every
    # take() eats into it for good, and a pool sized to the first test would
    # have the last one failing for want of facts rather than for a bug.
    _pool = [f"Fact number {i}, " + "long enough " * 5 for i in range(60)]
    _served = iter(_pool)
    _real_fetch = _fetch                    # kept for the fallthrough test below
    _fetch = lambda alive: next(_served, "")                    # noqa: E731
    _translate = lambda texts, lang: [f"ru:{t}" for t in texts]  # noqa: E731

    _mem = sqlite3.connect(":memory:")
    _db = lambda: _mem                                          # noqa: E731
    _mem.execute("CREATE TABLE facts(id TEXT, lang TEXT, text TEXT, ts REAL,"
                 " PRIMARY KEY(id, lang))")

    assert _usable("x" * 100) and not _usable("short")
    assert not _usable("x" * 100 + " https://a.b"), "a citation is not a fact"
    assert not _usable("x" * 999)
    assert _dig({"data": [{"a": {"b": "found"}}]}, ("data", 0, "a", "b")) == "found"
    # Every source has to name a path that _dig can walk, or a live fallback is
    # a KeyError discovered on the day the primary goes down.
    assert all(name and url.startswith("https://") and path
               for name, url, path in SOURCES), SOURCES
    assert len({url.split("/")[2] for _, url, _ in SOURCES}) == len(SOURCES), \
        "two sources on one host are one source"

    # The fallthrough, which is the reason there is more than one source: the
    # first host being down must cost a fact from the second and nothing else.
    import io
    _real_urlopen = urllib.request.urlopen

    def _only(host: str, payload: bytes):
        def fake(req, timeout=0):
            if req.full_url.split("/")[2] != host:
                raise OSError("down")
            return io.BytesIO(payload)
        return fake

    urllib.request.urlopen = _only("catfact.ninja", b'{"fact": "a cat fact"}')
    _alive = list(SOURCES)
    assert _real_fetch(_alive) == "a cat fact", "the live source was not reached"
    assert [n for n, _, _ in _alive] == ["catfact", "dogapi"], _alive
    # the nested path is walked, not assumed to be a top-level key
    urllib.request.urlopen = _only(
        "dogapi.dog", b'{"data":[{"attributes":{"body":"a dog\\n fact"}}]}')
    assert _real_fetch(list(SOURCES)) == "a dog fact", "newlines survived _dig"
    # every host down is "", never an exception reaching the upload
    urllib.request.urlopen = _only("nothing.invalid", b"")
    _alive = list(SOURCES)
    assert _real_fetch(_alive) == "" and _alive == []
    urllib.request.urlopen = _real_urlopen

    first = take(3, "ru")
    assert len(first) == 3 and all(t.startswith("ru:") for t in first), first
    # ...and the same three never come back: the table is the memory
    second = take(3, "ru")
    assert not (set(first) & set(second)), "a spent fact came round again"
    # the other channel has its own memory, so it may spend them itself
    _served = iter(_pool)
    assert len(take(3, "en")) == 3, "one channel's spend must not bind the other"

    # An API that stops answering is a caption without a block, never a crash
    # and never a block with a hole in it.
    _served = iter([])
    assert take(3, "ru") == [] and block("ru") == ""
    # Two fresh facts where three were asked for: still nothing. Fresh ones on
    # purpose - reusing spent ones would pass this for the other reason.
    _served = iter(["one, " + "a " * 40, "two, " + "b " * 40])
    assert take(3, "ru") == [], "two facts under a line promising three"

    # The surplus is a preference, not a requirement: exactly n candidates and
    # the block still builds. The longest of the batch are the ones kept.
    _served = iter(["short one, " + "x " * 26,
                    "the long one, " + "y " * 60,
                    "the middle one, " + "z " * 40,
                    "another short, " + "w " * 24])
    _picked = take(2, "ru")
    assert len(_picked) == 2, _picked
    assert "y" in _picked[0] and "z" in _picked[1], f"not the two longest: {_picked}"

    # A translation that came back the wrong length is refused whole
    _served = iter(_pool)
    _translate = lambda texts, lang: ["only one line"]           # noqa: E731
    assert take(3, "ru") == [], "a partial translation must not reach a caption"

    _translate = lambda texts, lang: [f"ru:{t}" for t in texts]  # noqa: E731
    _served = iter(_pool)
    got = block("ru")
    assert got.startswith(tuple(HOOK["ru"])), got[:60]
    # hook, facts, cta - three paragraphs, and the facts are one of them
    _paras = got.split("\n\n")
    assert len(_paras) == 3, _paras
    assert _paras[1].count("\n") == WANT - 1, "the facts must not be spaced apart"
    assert all(d in got for d in DIGITS[:WANT]) and got.rstrip().endswith("\U0001f447")

    # the lead rides on the hook's line, not above it
    _served = iter(_pool)
    _led = block("ru", lead="Часть 2/3. ")
    assert _led.startswith("Часть 2/3. "), _led[:40]
    assert _led.splitlines()[0].removeprefix("Часть 2/3. ") in HOOK["ru"], _led[:80]

    # Both hook sets have to be there: the English channel reads its own, and a
    # missing key is a KeyError inside an upload rather than a missing block.
    assert set(HOOK) == set(CTA) == {"ru", "en"}
    print("facts: fetch, dedupe, translation and formatting ok")
