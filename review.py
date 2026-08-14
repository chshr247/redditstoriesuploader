"""Step 2.5: the title goes past a human before anything is rendered.

script.py writes the story AND the title in one call, and everything below -
the voice, the card, the split, the caption - reads that one string. This parks
the written story on a GitHub issue and stops the run. A later run reads the
answer off the issue and carries on from exactly where make_video() used to.

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
import json
import logging
import subprocess
import time

import script
import source
from config import OUTPUT_LANG

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
         "answered", "title")


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
               "title TEXT DEFAULT '', PRIMARY KEY(post_id, lang))")
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
        f"`+` — пойдёт название модели.\n"
        f"`-` — история снимается совсем, рендера не будет.\n"
        f"Любой другой комментарий станет названием. Поставь `[emphasis]` перед "
        f"словом, на котором строка поворачивает: не первое и не последнее, "
        f"до {script.MAX_TITLE_WORDS} слов, одно предложение.\n"
        f"Молчание {HOURS} ч — уходит вариант модели.")


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

def _choose(comments: list[dict], owner: str, model_title: str,
            answered: int) -> tuple[str, str, int]:
    """(title, complaint, comment_id) for the newest comment the owner wrote.

    Only one of title/complaint is ever set, and `title` has three states rather
    than two: a string is the title to render, "" is nothing decided yet, and
    None is the story thrown away - a title nobody wants to write is usually a
    story nobody wants to watch, and that verdict has to be sayable.

    `answered` is the id of the last comment already judged, so a title that was
    refused is refused once and not once every half hour for six hours.
    """
    mine = [c for c in comments if c["user"]["login"].lower() == owner.lower()]
    if not mine or mine[-1]["id"] <= answered:
        return "", "", 0
    last = mine[-1]
    text = " ".join(last["body"].split())
    verdict = text.strip("`.! ").lower()
    if verdict in REJECT:
        return None, "", last["id"]
    if verdict in ACCEPT:
        return model_title, "", last["id"]
    # The user's line goes through the same gate the model's does. It is the
    # only check there is on a hand-written title, and the one it fails most is
    # the [emphasis] marker - which is not decoration: it drives the Fish cue
    # and the card's word-by-word accent run.
    fault = script._title_fault(text, OUTPUT_LANG)
    return ("", fault, last["id"]) if fault else (text, "", last["id"])


def _comments(issue: int) -> list[dict]:
    return json.loads(_gh("api", f"repos/{{owner}}/{{repo}}/issues/{issue}/comments",
                          "--jq", "[.[]|{id,body,user:{login:.user.login}}]") or "[]")


def waiting() -> bool:
    """True while a story of this channel is out for a title."""
    with _db() as db:
        return bool(db.execute("SELECT 1 FROM review WHERE lang=?",
                               (OUTPUT_LANG,)).fetchone())


def _poll(timeout: bool) -> dict | None:
    """Read the issue and act on it. Returns the story when it is ready to go.

    `timeout` is the only difference between the two callers. check() runs on
    every tick, ahead of the gate, and answers the user: it accepts a title, it
    refuses a bad one, it drops a dropped story. ready() runs in a gated run,
    the one that can actually render, and it alone may decide that HOURS have
    passed with no answer - a fallback that fires hours before anything could
    render on it would start the clock in the wrong place.
    """
    with _db() as db:
        row = db.execute(f"SELECT {','.join(_COLS)} FROM review WHERE lang=? "
                         "ORDER BY ts LIMIT 1", (OUTPUT_LANG,)).fetchone()
    if not row:
        return None
    r = dict(zip(_COLS, row))
    r["written"] = json.loads(r["written"])
    model_title = r["written"][0][0]
    if r["title"]:
        return r        # chosen already; a previous run just failed to render it

    title, fault, cid = _choose(_comments(r["issue"]), _owner(),
                                model_title, r["answered"])
    if title is None:
        # Thrown away, and it does not come back: the post was marked used when
        # it was written, which is exactly the record needed here.
        log.info("%s: dropped by the user, not rendering", r["post_id"])
        close(r["post_id"], "Снято, рендера не будет.")
        return None
    if cid:
        with _db() as db:
            db.execute("UPDATE review SET answered=?, title=? WHERE post_id=? "
                       "AND lang=?", (cid, title, r["post_id"], OUTPUT_LANG))
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

    r["title"] = title
    return r


def ready() -> dict | None:
    """The parked story with its final title, or None while it is still out."""
    return _poll(timeout=True)


def check() -> str:
    """Answer the user without rendering anything. Safe outside the gate.

    The render has to happen in a run that can publish - out/ dies with the
    runner - so it waits for a slot, and slots open every few hours. Judging a
    title does not: this runs on every tick, so a misplaced [emphasis] comes
    back in half an hour instead of three, and by the time a slot opens the
    answer is already on the row.
    """
    r = _poll(timeout=False)
    return r["title"] if r else ""


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

    # The gate does not guard this and must not: it decides whether a video can
    # be made, and reading a comment is not making one.
    if "--check" in sys.argv:
        if not waiting():
            print("nothing is out for review")
        elif t := check():
            print(f"title settled: {t}")
        else:
            print("still waiting on a title")
        sys.exit(0)

    OWNER, MODEL = "chshr247", "Я [emphasis] закрыла камеру сестре мужа ладонью, хотя она обещала"

    def c(i, login, body):
        return {"id": i, "user": {"login": login}, "body": body}

    # nobody has said anything
    assert _choose([], OWNER, MODEL, 0) == ("", "", 0)
    # a stranger cannot title a video on this channel, whatever they write
    assert _choose([c(1, "randomguy", "Мой [emphasis] заголовок про кота")],
                   OWNER, MODEL, 0) == ("", "", 0)
    # ...not even when the owner has already answered underneath them
    strangers = [c(1, "randomguy", "[emphasis] чужой заголовок сюда"), c(2, OWNER, "+")]
    assert _choose(strangers, OWNER, MODEL, 0) == (MODEL, "", 2)
    # accepting the model, in the shapes a phone actually types
    for word in ("+", "да", "ОК", "`+`", "да!"):
        assert _choose([c(3, OWNER, word)], OWNER, MODEL, 0)[0] == MODEL, word
    # throwing the story away - None, and never "" which means "not decided"
    for word in ("-", "нет", "СКИП", "`-`", "хуйня"):
        got = _choose([c(3, OWNER, word)], OWNER, MODEL, 0)
        assert got == (None, "", 3), (word, got)
    # a stranger cannot drop this channel's story either
    assert _choose([c(1, "randomguy", "-")], OWNER, MODEL, 0) == ("", "", 0)
    # a title of one's own, and the newest one wins
    mine = "Я [emphasis] заказал взрослое меню детям, а сестра жены наггетсы"
    got, fault, cid = _choose([c(4, OWNER, "+"), c(5, OWNER, mine)], OWNER, MODEL, 0)
    assert (got, fault, cid) == (mine, "", 5), (got, fault, cid)
    # the same gate the model answers to - here, a missing [emphasis]
    got, fault, cid = _choose([c(6, OWNER, "Просто заголовок без метки")],
                              OWNER, MODEL, 0)
    assert not got and "emphasis" in fault and cid == 6, (got, fault)
    # ...and a refusal is delivered once, not on every run for six hours
    assert _choose([c(6, OWNER, "Просто заголовок без метки")], OWNER, MODEL, 6) \
        == ("", "", 0)
    # a multi-line comment is one line by the time it is a title
    got, _, _ = _choose([c(7, OWNER, f"{mine}\n\nвот так")], OWNER, MODEL, 0)
    assert "\n" not in got and got.startswith("Я [emphasis] заказал"), got

    print("review ok")
