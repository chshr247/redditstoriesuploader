"""Step 2.5: the title goes past a human before anything is rendered.

script.py writes the story AND the title in one call, and everything below -
the voice, the card, the split, the caption - reads that one string. This parks
the written story on a GitHub issue. A later run reads the answer off the issue
and carries on from exactly where make_video() used to.

A DAY'S stories are parked at once, config.REVIEW_BATCH of them, and that is
the whole shape of this file. One at a time meant a question every three hours,
each arriving minutes before its own video was due, and an unanswered one held
the pipeline: nothing else could be written until it was answered. A batch is
read in one sitting instead. A story turned down frees its slot on the spot and
the replacement is written within the minute (review.yml), and a story accepted
is answered with the time it actually publishes - which is a real answer only
because the queue position is in it, three of four accepted together being
hours apart. See main.top_up() for the writing half.

Why an issue rather than a file or a chat bot. The workflow already holds a
token that can open one, so there is no second secret. An issue comment stays
where it was written, so any run can read it - a Telegram cursor is consumed by
whichever run polls first, and this repo puts two runs in the same minute on
purpose (see the cron in publish.yml). And the answers pile up next to what the
model proposed, which is the corpus the title rules were derived from in the
first place.

The repository is PUBLIC. A comment is an answer only if the person who wrote
it owns the repo: anyone can comment, and an accepted comment is narrated and
published under this channel's name. _choose() checks the login and trusts
nothing else about the text beyond what _title_fault() already checks.
"""
import datetime
import json
import logging
import re
import subprocess
import time

import script
import source
from config import OUTPUT_LANG, REVIEW_BATCH, REVIEW_TZ_H

log = logging.getLogger(__name__)

# Unanswered for this long and the model's own title is used. The channel
# publishing on its own beats the channel going quiet: a slot missed is gone,
# and a title chosen by the model is the title every video had until now.
HOURS = 6

# What a comment says to accept the model's title as written, and what it says
# to throw the whole story away. Anything else in the comment IS the new title,
# so both lists are deliberately tiny - a word that could plausibly open a title
# must never appear in either.
ACCEPT = {"+", "да", "ок", "ok", "ага", "yes"}
REJECT = {"-", "нет", "скип", "skip", "no", "мимо", "хуйня"}

_COLS = ("post_id", "issue", "ts", "gender", "sub", "score", "written",
         "answered", "title", "body")


def _db():
    db = source._db()
    # One row per story per channel, alive only between the write and the
    # render. `written` is the whole script as JSON - [[title, body], ...] -
    # because a split story is written in one call and gets ONE title for all
    # of its parts, chosen here before any of them is queued.
    # `title` is the chosen one, written down the moment it is accepted. The
    # render happens after, and can fail - re-reading the issue then would find
    # a comment already marked answered and quietly fall through to the six
    # hour timeout, throwing away an answer the user had actually given.
    db.execute("CREATE TABLE IF NOT EXISTS review("
               "post_id TEXT, lang TEXT, issue INT, ts REAL, gender TEXT, "
               "sub TEXT, score INT, written TEXT, answered INT DEFAULT 0, "
               "title TEXT DEFAULT '', body TEXT DEFAULT '', "
               "PRIMARY KEY(post_id, lang))")
    # `body` arrived after `title`; a row written before it keeps '' and the
    # story is narrated as the model wrote it, which is what '' means anyway.
    if "body" not in {c[1] for c in db.execute("PRAGMA table_info(review)")}:
        db.execute("ALTER TABLE review ADD COLUMN body TEXT DEFAULT ''")
    return db


def _gh(*args: str, stdin: str = "") -> str:
    """gh, decoded as utf-8 whatever the console codepage says."""
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


_owner_cache = ""


def _owner() -> str:
    global _owner_cache
    if not _owner_cache:
        _owner_cache = _gh("repo", "view", "--json", "owner", "-q", ".owner.login")
    return _owner_cache


def ok() -> str:
    """Empty when GitHub is reachable, otherwise why not.

    Asked BEFORE a script is written, never after. A story parked with no issue
    to park it on is a story whose title nobody can answer, and the run would
    pay the LLM for it again on every tick until someone noticed.
    """
    try:
        return "" if _owner() else "gh returned no repo owner"
    except (RuntimeError, FileNotFoundError, OSError) as e:
        return str(e)[:200]


# --------------------------------------------------------------------- parking

def _body(post: dict, written: list) -> str:
    title = written[0][0]
    parts = "\n\n".join(
        (f"**Часть {i}.** " if len(written) > 1 else "") + body
        for i, (_, body) in enumerate(written, 1))
    return (
        f"r/{post['sub']} · {post['score']} · https://redd.it/{post['id']}\n\n"
        f"**Название от модели:**\n\n`{title}`\n\n"
        f"{parts}\n\n---\n"
        f"**Первая строка комментария:**\n"
        f"`+` — пойдёт название модели.\n"
        f"`-` — история снимается совсем, рендера не будет — "
        f"вместо неё сразу придёт следующая.\n"
        f"Что угодно ещё — станет названием. Поставь `[emphasis]` перед словом, "
        f"на котором строка поворачивает: не первое и не последнее, "
        f"до {script.MAX_TITLE_WORDS} слов, одно предложение.\n\n"
        f"**Через пустую строку — новый текст истории**, если хочешь его "
        f"переписать. Если после пустой строки ничего нет, текст модели идёт "
        f"как есть. **Один вопрос и больше ничего** — заменится только "
        f"закрывающий вопрос, текст останется моделевский. "
        f"Части до последней обрываются на самом интересном, без "
        f"вопроса. Последняя закрывается вопросом зрителю с меткой настроения и "
        f"одним `[emphasis]` — `[doubtful] А вы бы [emphasis] сделали так же?`; "
        f"а если просто оборвёшь текст без вопроса, подставится вопрос модели. "
        f"Каждая часть — около {script._target_words()} слов вместе с названием, "
        f"оно читается в начале каждой.\n\n"
        + (f"Если будешь переписывать текст — частей {len(written)}, раздели их "
           f"строкой из трёх дефисов; можно прислать меньше, но не меньше двух. "
           f"Одно название встаёт на все части в любом случае. И оставь "
           f"**пустую строку перед дефисами**: без неё markdown превращает "
           f"предыдущий абзац в заголовок. На разбор не влияет, но выглядит "
           f"сломанным.\n\n"
           if len(written) > 1 else "")
        + f"Молчание {HOURS} ч — уходит вариант модели.")


def park(post: dict, gender: str, written: list[tuple[str, str]]) -> int:
    """Open the issue, store the script, return the issue number."""
    # Assigned, not just opened: a notification for one's own repository depends
    # on the watch setting, an assignment does not - the same reason publish.py's
    # draft reminder assigns itself.
    url = _gh("issue", "create",
              "--title", f"[{OUTPUT_LANG}] {script.plain(written[0][0])[:70]}",
              "--assignee", _owner(),
              "--body-file", "-", stdin=_body(post, written))
    issue = int(url.rstrip("/").rsplit("/", 1)[1])
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO review(post_id, lang, issue, ts, "
                   "gender, sub, score, written, answered) VALUES (?,?,?,?,?,?,?,?,0)",
                   (post["id"], OUTPUT_LANG, issue, time.time(), gender,
                    post["sub"], post["score"], json.dumps(written, ensure_ascii=False)))
    log.info("%s: title is with the user on issue #%d", post["id"], issue)
    return issue


# -------------------------------------------------------------------- the reply

def _choose(comments: list[dict], owner: str, written: list, answered: int
            ) -> tuple[str, list, str, int]:
    """(title, bodies, complaint, comment_id) for the owner's newest comment.

    The comment is read in the shape the model answers in: a first line, then a
    blank line, then the narration. The first line is the title, `+` to keep the
    model's, or `-` to throw the story away. Everything after the blank line
    replaces the narration - one entry per part, split on a line of three
    dashes exactly as the model writes them. Nothing after the blank line means
    only the title was touched, and `bodies` comes back empty.

    `title` has three states rather than two: a string is the title to render,
    "" is nothing decided yet, and None is the story dropped - a title nobody
    wants to write is usually a story nobody wants to watch, and that verdict
    has to be sayable. `complaint` excludes the other two.

    `answered` is the id of the last comment already judged, so a title that was
    refused is refused once and not once every half hour for six hours.

    `written` is what the model wrote - [[title, body], ...] - and is read for
    three things: the title `+` accepts, how many parts the story has, and the
    closing question to fall back on when a rewrite arrives without one.
    """
    mine = [c for c in comments if c["user"]["login"].lower() == owner.lower()]
    if not mine or mine[-1]["id"] <= answered:
        return "", [], "", 0
    last = mine[-1]
    split = re.split(r"\n[ \t]*\n", last["body"].strip(), maxsplit=1)
    head, tail = split[0], (split[1] if len(split) > 1 else "")
    text = " ".join(head.split())
    verdict = text.strip("`.! ").lower()
    if verdict in REJECT:
        return None, [], "", last["id"]

    title = written[0][0] if verdict in ACCEPT else text
    if verdict not in ACCEPT:
        # The user's line goes through the same gate the model's does. It is the
        # only check there is on a hand-written title, and the one it fails most
        # is the [emphasis] marker - which is not decoration: it drives the Fish
        # cue and the card's word-by-word accent run.
        if fault := script._title_fault(text, OUTPUT_LANG):
            return "", [], fault, last["id"]

    # Split on the separator the model itself writes, so a story rewritten by
    # hand is punctuated exactly like the answer it replaces.
    bodies = [script._clean(c) for c in script.PART_SEP.split(tail) if c.strip()]
    # One question and nothing else swaps ONLY the closing question, and the
    # narration stays the model's. Without this, changing the last sentence
    # means retyping the whole story - twice over on a split, where the question
    # lives at the end of the last part and four hundred words stand in front of
    # it. Checked before the borrow below, whose condition is the opposite one.
    if len(bodies) == 1 and _only_question(bodies[0]):
        if fault := script._cta_fault(bodies[0]):
            return "", [], fault, last["id"]
        question = bodies[0]
        bodies = [b for _, b in written]
        bodies[-1] = f"{script.split_cta(bodies[-1])[0]} {question}".strip()
    # A closing question is required and is a nuisance to type: a mood cue, one
    # [emphasis], and not on the last word. So a rewrite that simply ends -
    # ends, no question mark at all - keeps the one the model already wrote for
    # this story. Only when there is none: a question that IS there and is
    # marked up wrong is the author's to fix, not ours to replace.
    if bodies and not script.plain(bodies[-1]).rstrip().endswith("?"):
        if cta := script.split_cta(written[-1][1])[1]:
            bodies[-1] = f"{bodies[-1].rstrip()} {cta}"
    if fault := _body_fault(bodies, title, len(written)):
        return "", [], fault, last["id"]
    return title, bodies, "", last["id"]


def _only_question(body: str) -> bool:
    """One sentence, and it is a question - the shape that means "just the CTA".

    A narration is never one sentence, so there is nothing this can be confused
    with. It has to be checked before the borrow in _choose(), which fires on
    the opposite condition: a text that does NOT end on a question.
    """
    sentences = [s for s in script.SENTENCE.split(body.strip())
                 if script.plain(s).strip()]
    return len(sentences) == 1 and script.plain(body).rstrip().endswith("?")


def _body_fault(bodies: list[str], title: str, parts: int) -> str:
    """Empty when a hand-written narration is usable, otherwise why not.

    The same things the model's own text has to satisfy, and no more: how many
    parts there are, the kin words a young viewer stumbles over, the ending -
    only the LAST part closes on the question to the viewer, the ones before it
    stop on the cliffhanger - and the length, which is a duration in disguise.
    Nothing downstream bounds a video's length, so a narration typed twice as
    long is simply a video twice as long.

    Length is per part and counts the title, because the title is narrated at
    the head of every part even though it is written once.
    """
    if not bodies:
        return ""
    if parts == 1 and len(bodies) > 1:
        return ("это история на одно видео — убери разделители из трёх дефисов, "
                "текст идёт одним куском")
    if parts > 1 and not 2 <= len(bodies) <= parts:
        return (f"история написана в {parts} частях, а в комментарии "
                f"{len(bodies)} — раздели текст строкой из трёх дефисов, "
                f"от 2 до {parts} частей")
    target = script._target_words()
    for i, body in enumerate(bodies, 1):
        label = f"часть {i}: " if len(bodies) > 1 else ""
        if fault := (script._kin_fault(body)
                     or script._ending_fault(body, final=i == len(bodies))):
            return label + fault
        total = script._words(title) + script._words(body)
        if not script._fits(total, target):
            return (f"{label}в названии и тексте вместе {total} слов, надо около "
                    f"{target} — {'сократи' if total > target else 'дополни'}")
    return ""


def _comments(issue: int) -> list[dict]:
    return json.loads(_gh("api", f"repos/{{owner}}/{{repo}}/issues/{issue}/comments",
                          "--jq", "[.[]|{id,body,user:{login:.user.login}}]") or "[]")


def parked() -> int:
    """How many of this channel's stories are sitting on issues right now.

    What decides whether another story is written: main.py tops this up to
    config.REVIEW_BATCH so the day's questions arrive together and are answered
    in one sitting, instead of one landing minutes before each video was due.
    """
    with _db() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM review WHERE lang=?",
                          (OUTPUT_LANG,)).fetchone()
    return n


def queued() -> int:
    """Videos the parked batch will produce, which is not how many issues it is.

    A split story is one question and two or three sends. The batch is capped
    in videos (config.REVIEW_BATCH) because the ceiling it exists to respect -
    the day's allowance - counts sends, and a question whose video has no slot
    left today is a question asked for nothing.

    Read off `written`, so it is what the model actually returned rather than
    the part count that was asked for.
    """
    with _db() as db:
        rows = db.execute("SELECT written FROM review WHERE lang=?",
                          (OUTPUT_LANG,)).fetchall()
    return sum(len(json.loads(w)) for (w,) in rows)


def split_parked() -> bool:
    """True when one of the parked stories is already a multi-parter.

    source.multipart_today() cannot answer this: it reads the `parts` table,
    which is written at the RENDER, and a batch is parked hours before any of
    it renders. Without this the whole batch would be sized for splitting on
    the same day and the one-split-a-day rule would mean nothing.
    """
    with _db() as db:
        rows = db.execute("SELECT written FROM review WHERE lang=?",
                          (OUTPUT_LANG,)).fetchall()
    return any(len(json.loads(w)) > 1 for (w,) in rows)


def _rows() -> list[dict]:
    """Every story of this channel out for a title, oldest first."""
    out = []
    with _db() as db:
        rows = db.execute(f"SELECT {','.join(_COLS)} FROM review WHERE lang=? "
                          "ORDER BY ts", (OUTPUT_LANG,)).fetchall()
    for row in rows:
        r = dict(zip(_COLS, row))
        r["written"] = json.loads(r["written"])
        out.append(r)
    return out


def _poll(timeout: bool) -> dict | None:
    """Judge every parked issue, return the first story ready to be rendered.

    Every row on every tick, not just the oldest: the day's stories are parked
    together and read in whatever order the user opens them, so an answer on
    the fourth issue has to be picked up while the first is still untouched.
    One gh call per parked story - four on a full batch, twice an hour.

    Rows come oldest-first, so the story handed back is the one that has waited
    longest among those actually settled. That is the order main.py renders in
    and the order publish.eta() counts, which is what makes the time quoted
    back to the user the time it really goes out.
    """
    first = None
    for r in _rows():
        if (got := _judge(r, timeout)) and first is None:
            first = got
    return first


def _judge(r: dict, timeout: bool) -> dict | None:
    """One parked story: read its issue, act on the answer, hand it back if ready.

    `timeout` is the only difference between the two callers. check() runs on
    every tick, ahead of the gate, and answers the user: it accepts a title, it
    refuses a bad one, it drops a dropped story. ready() runs in a gated run,
    the one that can actually render, and it alone may decide that HOURS have
    passed with no answer - a fallback that fires hours before anything could
    render on it would start the clock in the wrong place.
    """
    model_title = r["written"][0][0]
    if r["title"]:
        # Chosen already; a previous run just failed to render it.
        return _final(r, json.loads(r["body"]) if r["body"] else [])

    title, bodies, fault, cid = _choose(_comments(r["issue"]), _owner(),
                                        r["written"], r["answered"])
    if title is None:
        # Thrown away, and it does not come back: the post was marked used when
        # it was written, which is exactly the record needed here.
        log.info("%s: dropped by the user, not rendering", r["post_id"])
        close(r["post_id"], "Снято, рендера не будет.")
        return None
    settled = bool(cid and title)
    if cid:
        r["title"] = title
        with _db() as db:
            db.execute("UPDATE review SET answered=?, title=?, body=? WHERE "
                       "post_id=? AND lang=?",
                       (cid, title, json.dumps(bodies, ensure_ascii=False),
                        r["post_id"], OUTPUT_LANG))
    if fault:
        _gh("issue", "comment", str(r["issue"]), "--body",
            f"Не приму: {fault}.\n\nНапиши ещё раз, или `+` — возьму вариант модели.")
        log.info("%s: title refused (%s)", r["post_id"], fault)
        return None
    if not title:
        if not timeout or time.time() - r["ts"] < HOURS * 3600:
            return None
        _gh("issue", "comment", str(r["issue"]),
            "--body", f"{HOURS} ч без ответа — ушло название модели.")
        log.info("%s: no answer in %dh, using the model's title", r["post_id"], HOURS)
        title = model_title
        # written down for the same reason an accepted one is: the timeout is
        # announced on the issue exactly once, not again on the next run.
        with _db() as db:
            db.execute("UPDATE review SET title=? WHERE post_id=? AND lang=?",
                       (title, r["post_id"], OUTPUT_LANG))
        settled = True

    r["title"] = title
    # Said once, in the run that settled it. "Accepted" on its own reads as
    # "published", and with a day's stories accepted in one sitting three of
    # them are hours away - so the answer to `+` is a time, not an
    # acknowledgement.
    if settled:
        _say_when(r["issue"], r["ts"])
    return _final(r, bodies)


def _local(ts: float) -> str:
    """A unix time in the clock the answer is read on, which is a phone."""
    tz = datetime.timezone(datetime.timedelta(hours=REVIEW_TZ_H))
    d = datetime.datetime.fromtimestamp(ts, tz)
    days = (d.date() - datetime.datetime.now(tz).date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days, f"{d:%d.%m}")
    return f"{day} в {d:%H:%M}"


def _say_when(issue: int, ts: float) -> None:
    """Comment the time this story actually publishes, queue position included.

    The position is the point. Four stories accepted in one sitting go out over
    the whole day, spaced by the gap between sends, and the only thing that
    tells them apart is how many settled stories were parked BEFORE this one -
    which is exactly the order _poll() hands them to main.py in. Anything
    already rendered and sitting in out/ counts too; on CI that is nothing,
    because the runner takes out/ with it.

    Never fatal, and never retried: the video goes out whether or not GitHub
    took the comment, and a second attempt next tick would post it twice.
    """
    try:
        import publish

        with _db() as db:
            rows = db.execute(
                "SELECT written FROM review WHERE lang=? AND title!='' AND ts<?",
                (OUTPUT_LANG, ts)).fetchall()
        # Videos ahead, not stories ahead: a settled three-parter in front of
        # this one is three sends before it, not one.
        ahead = sum(len(json.loads(w)) for (w,) in rows) + len(publish.pending())
        when = publish.eta(ahead)
        note = ("Принято." if not when else
                f"Принято, публикация {_local(when)}"
                + (f" — в очереди впереди ещё {ahead}." if ahead else "."))
        _gh("issue", "comment", str(issue), "--body", note)
    except Exception:
        log.exception("could not say when #%d publishes", issue)


def _final(r: dict, bodies: list[str]) -> dict:
    """Fold the chosen title and any rewritten narration into `written`.

    Callers get one shape whoever wrote what: [(title, body), ...], the title
    the same on every entry because a split story carries one. A rewrite may
    come back with fewer parts than the model wrote - _body_fault() allows two
    up to what was written, exactly the band script._chunks() allows the model -
    so the rewritten list replaces `written` rather than being zipped into it.
    """
    bodies = bodies or [b for _, b in r["written"]]
    r["written"] = [(r["title"], b) for b in bodies]
    return r


def ready() -> dict | None:
    """The parked story with its final title, or None while it is still out."""
    return _poll(timeout=True)


def check() -> list[str]:
    """Answer the user without rendering anything. Safe outside the gate.

    The render has to happen in a run that can publish - out/ dies with the
    runner - so it waits for a slot, and slots open every few hours. Judging a
    title does not: this runs on every tick, so a misplaced [emphasis] comes
    back in half an hour instead of three, and by the time a slot opens the
    answer is already on the row.

    Returns every settled title, not the next one: a batch is answered in one
    sitting, and the caller's line about what happened should say so.
    """
    _poll(timeout=False)
    return [r["title"] for r in _rows() if r["title"]]


def rendered(post_id: str) -> None:
    """The story is on its way out - the row is spent, the issue is NOT.

    One issue is one case, from the title being asked for to the caption of the
    last video landing in it, so what closes it is publishing and not this. The
    row goes now regardless: it exists to hold the pipeline still while a title
    is undecided, and that is over.
    """
    with _db() as db:
        db.execute("DELETE FROM review WHERE post_id=? AND lang=?",
                   (post_id, OUTPUT_LANG))


def close(post_id: str, note: str) -> None:
    """Dropped - there will be no video, so nothing else will ever close it."""
    with _db() as db:
        row = db.execute("SELECT issue FROM review WHERE post_id=? AND lang=?",
                         (post_id, OUTPUT_LANG)).fetchone()
        db.execute("DELETE FROM review WHERE post_id=? AND lang=?",
                   (post_id, OUTPUT_LANG))
    if row:
        _gh("issue", "close", str(row[0]), "--comment", note)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Which channel owns an issue, asked by number and answered from the table
    # rather than from the "[ru]" in its title. A comment arriving on GitHub has
    # to reach the right channel and only that one, and the title of an issue in
    # a public repo is whatever anyone typed - the row is the fact.
    if "--lang-of" in sys.argv:
        n = int(sys.argv[sys.argv.index("--lang-of") + 1])
        with _db() as db:
            row = db.execute("SELECT lang FROM review WHERE issue=?", (n,)).fetchone()
        print(row[0] if row else "")
        sys.exit(0)

    # Is there room in the batch for another story? Exit code only, and read by
    # review.yml before it installs anything: writing a replacement needs the
    # dependencies and the private prompt set, and a comment that was a title
    # rather than a `-` should not pay for either.
    if "--room" in sys.argv:
        n = queued()
        print(f"{n}/{REVIEW_BATCH} video(s) parked over {parked()} issue(s)")
        sys.exit(0 if n < REVIEW_BATCH else 1)

    # The gate does not guard this and must not: it decides whether a video can
    # be made, and reading a comment is not making one.
    if "--check" in sys.argv:
        if not parked():
            print("nothing is out for review")
        elif titles := check():
            print(f"{len(titles)} of {parked()} settled: " + " | ".join(titles))
        else:
            print(f"still waiting on {parked()} title(s)")
        print(f"{queued()}/{REVIEW_BATCH} video(s) in the batch")
        sys.exit(0)

    OWNER = "chshr247"
    MODEL = "Я [emphasis] закрыла камеру сестре мужа ладонью, хотя она обещала"
    MODEL_CTA = "[curious] А вы бы [emphasis] открыли эту коробку?"
    # Every fixture narration is counted off the LIVE target, never written out
    # at a fixed length: _target_words() moves with TARGET_SEC, and a hand-
    # counted body silently drops under the budget the next time it does - which
    # is what left these at 195 and 185 words against a target of 332 when the
    # Russian narration went to 130 seconds.
    def _filler(line: str, parts: int = 1) -> str:
        return line * (script._target_words() // len(line.split()) // parts + 2)

    ONE = [[MODEL, _filler("Модель нашла коробку. ") + MODEL_CTA]]
    FILLER = _filler("Модель нашла эту коробку сама. ")
    THREE = [[MODEL, FILLER + "И тут всё оборвалось."],
             [MODEL, FILLER + "И тут оборвалось снова."],
             [MODEL, FILLER + MODEL_CTA]]

    def c(i, login, body):
        return {"id": i, "user": {"login": login}, "body": body}

    # nobody has said anything
    assert _choose([], OWNER, ONE, 0) == ("", [], "", 0)
    # a stranger cannot title a video on this channel, whatever they write
    assert _choose([c(1, "randomguy", "Мой [emphasis] заголовок про кота")],
                   OWNER, ONE, 0) == ("", [], "", 0)
    # ...not even when the owner has already answered underneath them
    strangers = [c(1, "randomguy", "[emphasis] чужой заголовок сюда"), c(2, OWNER, "+")]
    assert _choose(strangers, OWNER, ONE, 0) == (MODEL, [], "", 2)
    # accepting the model, in the shapes a phone actually types
    for word in ("+", "да", "ОК", "`+`", "да!"):
        assert _choose([c(3, OWNER, word)], OWNER, ONE, 0)[0] == MODEL, word
    # throwing the story away - None, and never "" which means "not decided"
    for word in ("-", "нет", "СКИП", "`-`", "хуйня"):
        got = _choose([c(3, OWNER, word)], OWNER, ONE, 0)
        assert got == (None, [], "", 3), (word, got)
    # a stranger cannot drop this channel's story either
    assert _choose([c(1, "randomguy", "-")], OWNER, ONE, 0) == ("", [], "", 0)
    # a title of one's own, and the newest one wins
    mine = "Я [emphasis] заказал взрослое меню детям, а сестра жены наггетсы"
    got, body, fault, cid = _choose([c(4, OWNER, "+"), c(5, OWNER, mine)],
                                    OWNER, ONE, 0)
    assert (got, body, fault, cid) == (mine, [], "", 5), (got, body, fault, cid)
    # the same gate the model answers to - here, a missing [emphasis]
    got, _, fault, cid = _choose([c(6, OWNER, "Просто заголовок без метки")],
                                 OWNER, ONE, 0)
    assert not got and "emphasis" in fault and cid == 6, (got, fault)
    # ...and a refusal is delivered once, not on every run for six hours
    assert _choose([c(6, OWNER, "Просто заголовок без метки")], OWNER, ONE, 6) \
        == ("", [], "", 0)

    # A narration of one's own, under the blank line. Long enough to pass the
    # budget and closed with a question, which is what the format is built on.
    story = (_filler("Я работал в ночную смену и однажды нашёл в подсобке коробку. ")
             + "[doubtful] А вы бы [emphasis] открыли эту коробку?")
    got, bodies, fault, cid = _choose([c(8, OWNER, f"{mine}\n\n{story}")],
                                      OWNER, ONE, 0)
    assert (got, fault) == (mine, ""), (got, fault)
    assert len(bodies) == 1, bodies
    assert bodies[0].startswith("Я работал") and bodies[0].endswith("коробку?")
    # ...and it works under a bare `+` too: the model's title, my text
    got, bodies, fault, _ = _choose([c(9, OWNER, f"+\n\n{story}")], OWNER, ONE, 0)
    assert (got, fault) == (MODEL, "") and bodies, (got, fault, bodies)
    # no blank line means the title alone was touched
    assert _choose([c(10, OWNER, mine)], OWNER, ONE, 0)[1] == []
    # A rewrite that just ends, with no question at all, borrows the model's -
    # the markup it needs is a nuisance to type and the model already typed it.
    plain_story = _filler("Я работал в ночную смену и нашёл в подсобке коробку. ")
    got, bodies, fault, _ = _choose([c(11, OWNER, f"{mine}\n\n{plain_story}")],
                                    OWNER, ONE, 0)
    assert (got, fault) == (mine, ""), (got, fault)
    assert bodies[0].endswith(MODEL_CTA), bodies[0][-80:]
    # ...but a question that IS there and is marked up wrong stays the author's
    # to fix. Silently replacing it would throw away what they meant to ask.
    _, _, fault, _ = _choose([c(12, OWNER, f"{mine}\n\n{plain_story}Ну а вы бы вернули эту коробку?")],
                             OWNER, ONE, 0)
    assert "mood cue" in fault, fault
    # on a split it is the LAST part that borrows it, and only that one
    got, bodies, fault, _ = _choose(
        [c(13, OWNER, f"+\n\n{plain_story}\n---\n{plain_story}")], OWNER, THREE, 0)
    assert fault == "" and len(bodies) == 2, (fault, len(bodies))
    assert bodies[1].endswith(MODEL_CTA) and not bodies[0].endswith(MODEL_CTA)
    # ...nor is one that would make a video of the wrong length
    short = "Коротко про коробку. [doubtful] А вы бы [emphasis] вернули эту коробку?"
    _, _, fault, _ = _choose([c(12, OWNER, f"{mine}\n\n{short}")], OWNER, ONE, 0)
    assert "слов" in fault, fault
    # One question and nothing else swaps only the closing question: the model's
    # narration survives, with its old question cut off the end of the last part.
    NEWQ = "[curious] А вы бы [emphasis] вернули эту коробку?"
    got, bodies, fault, _ = _choose([c(19, OWNER, f"{mine}\n\n{NEWQ}")],
                                    OWNER, ONE, 0)
    assert (got, fault) == (mine, ""), (got, fault)
    assert len(bodies) == 1 and bodies[0].endswith(NEWQ), bodies[0][-90:]
    assert MODEL_CTA not in bodies[0], "the old question was left in place"
    assert bodies[0].startswith("Модель нашла коробку."), bodies[0][:40]
    # ...and on a split it lands at the end of the LAST part and nowhere else
    got, bodies, fault, _ = _choose([c(20, OWNER, f"+\n\n{NEWQ}")], OWNER, THREE, 0)
    assert fault == "" and len(bodies) == 3, (fault, len(bodies))
    assert bodies[-1].endswith(NEWQ) and not bodies[0].endswith(NEWQ), bodies
    assert bodies[0] == THREE[0][1], bodies[0][:50]
    # a question with no mood cue is refused like any other
    _, _, fault, _ = _choose([c(21, OWNER, f"{mine}\n\nА вы бы вернули эту коробку?")],
                             OWNER, ONE, 0)
    assert "mood cue" in fault, fault

    # A split story rewritten by hand: parts split on the model's own separator,
    # and only the last one closes on the question to the viewer.
    mid = _filler("Я работал в ночную смену и однажды нашёл в подсобке коробку. ")
    three = f"{mid}\n---\n{mid}\n---\n{story}"
    got, bodies, fault, _ = _choose([c(13, OWNER, f"{mine}\n\n{three}")],
                                    OWNER, THREE, 0)
    assert (got, fault) == (mine, ""), (got, fault)
    assert len(bodies) == 3 and bodies[-1].endswith("коробку?"), len(bodies)
    assert "---" not in bodies[0] and bodies[0].startswith("Я работал")
    # merging three into two is allowed - the same band the model may answer in
    assert _choose([c(14, OWNER, f"+\n\n{mid}\n---\n{story}")],
                   OWNER, THREE, 0)[2] == ""
    # ...but not into one, and not into four
    for bad in (story, f"{mid}\n---\n{mid}\n---\n{mid}\n---\n{story}"):
        _, _, fault, _ = _choose([c(15, OWNER, f"+\n\n{bad}")], OWNER, THREE, 0)
        assert "частях" in fault, fault
    # a middle part that closes on the question is not a middle part
    _, _, fault, _ = _choose([c(16, OWNER, f"+\n\n{story}\n---\n{story}")],
                             OWNER, THREE, 0)
    assert fault.startswith("часть 1:"), fault
    # and a one-part story must not arrive in pieces
    _, _, fault, _ = _choose([c(17, OWNER, f"+\n\n{mid}\n---\n{story}")],
                             OWNER, ONE, 0)
    assert "одно видео" in fault, fault
    # the title alone still works on a split, as it always did
    assert _choose([c(18, OWNER, mine)], OWNER, THREE, 0)[:2] == (mine, [])

    # The batch bookkeeping, on a scratch table rather than this channel's rows.
    # split_parked() is the one with teeth: source.multipart_today() reads the
    # `parts` table, which is empty until a render, so without this every story
    # in a morning batch would be sized for splitting on the same day.
    _real_db, _rows_of = _db, [
        ("a", 1, 100.0, "", json.dumps(ONE)),          # unanswered, one part
        ("b", 2, 200.0, "T", json.dumps(ONE)),         # settled, publishes first
        ("c", 3, 300.0, "", json.dumps(THREE)),        # unanswered, a split
    ]
    import sqlite3

    _mem = sqlite3.connect(":memory:")
    _mem.execute("CREATE TABLE review(post_id TEXT, lang TEXT, issue INT, "
                 "ts REAL, gender TEXT, sub TEXT, score INT, written TEXT, "
                 "answered INT DEFAULT 0, title TEXT DEFAULT '', "
                 "body TEXT DEFAULT '')")
    _mem.executemany("INSERT INTO review(post_id, lang, issue, ts, gender, sub, "
                     "score, written, title) VALUES (?,?,?,?,'m','s',1,?,?)",
                     [(pid, OUTPUT_LANG, iss, ts, w, t)
                      for pid, iss, ts, t, w in _rows_of])
    _db = lambda: _mem                                  # noqa: E731
    try:
        assert parked() == 3, parked()
        # three issues, but FIVE videos - the three-parter is the whole point
        # of counting the batch in sends rather than in questions
        assert queued() == 5, queued()
        assert split_parked(), "a parked three-parter is a split in flight"
        # oldest first, because that is the order they render and the order the
        # times quoted on the issues were counted in
        assert [r["post_id"] for r in _rows()] == ["a", "b", "c"]
        assert _rows()[2]["written"] == THREE, "written comes back parsed"
        _mem.execute("DELETE FROM review WHERE post_id='c'")
        assert not split_parked() and parked() == 2 and queued() == 2
    finally:
        _db = _real_db

    # The time quoted back on the issue, in the reader's own clock.
    _now = time.time()
    assert _local(_now).startswith("сегодня в ")
    assert _local(_now + 86400).startswith("завтра в ")
    assert _local(_now + 5 * 86400)[:2].isdigit(), _local(_now + 5 * 86400)

    print("review ok")
