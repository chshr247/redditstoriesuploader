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
the replacement is written within the minute (review.yml). See main.top_up()
for the writing half.

A story that has been fully decided - the title, and the reading where readings
were offered - is answered with the time it publishes, and that time is a claim
rather than an estimate: it is written onto the row, no other story may take
it, and _poll() holds the story back until the clock reaches it. It was an
estimate until 2026-08-27 and was recomputed on every tick from whatever had
settled so far, which is how #140 and #141 were both promised 22:07.

The repository is PUBLIC and this file runs in two places - as github-actions on
a runner, and under the operator's own login on their desk. Every comment it
writes carries a mark and it reads no comment that has one; without that its own
verdicts came back to it as answers, and its own record of what it had already
said (seen.db, which crosses between the two by git and is stale in between)
was not enough to stop it saying everything twice. See MARK and _said().

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
from config import (OUTPUT_LANG, REVIEW_BATCH, REVIEW_TAKES, REVIEW_TZ_H,
                    chan_file)

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
         "answered", "title", "body", "takes", "take", "pub_at", "voice")


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
               "takes TEXT DEFAULT '', take INT DEFAULT -1, "
               "pub_at REAL DEFAULT 0, voice TEXT DEFAULT '', "
               "PRIMARY KEY(post_id, lang))")
    # `body` arrived after `title`; a row written before it keeps '' and the
    # story is narrated as the model wrote it, which is what '' means anyway.
    have = {c[1] for c in db.execute("PRAGMA table_info(review)")}
    if "body" not in have:
        db.execute("ALTER TABLE review ADD COLUMN body TEXT DEFAULT ''")
    # `takes` is the offer that was posted - the release asset names, and the
    # id of the comment carrying them - and `take` is which one was picked, -1
    # while it is still out. A row written before these existed reads as
    # takes='' ("nothing offered yet"), and REVIEW_TAKES <= 1 short-circuits
    # the whole stage, so upgrading mid-flight cannot strand a row waiting for
    # an answer to a question that was never asked.
    if "takes" not in have:
        db.execute("ALTER TABLE review ADD COLUMN takes TEXT DEFAULT ''")
        db.execute("ALTER TABLE review ADD COLUMN take INT DEFAULT -1")
    # `pub_at` is the time this story was PROMISED on its issue, and the row is
    # held until it. 0 means nothing has been promised yet, which is what every
    # row written before this reads as - it gets a time on the next tick.
    if "pub_at" not in have:
        db.execute("ALTER TABLE review ADD COLUMN pub_at REAL DEFAULT 0")
    # `voice` is the narrator the story was READ ALOUD in, written down when
    # the takes were made so the render does not draw a second one.
    # voice.pick_voice() picks at random out of the channel's pool, and the
    # take the user chooses is the body of the video only - the title and the
    # closing question are synthesized at the render. Two draws there means one
    # video in two voices. Empty is what every row written before this reads
    # as, and empty means "draw one", which is what the render always did.
    if "voice" not in have:
        db.execute("ALTER TABLE review ADD COLUMN voice TEXT DEFAULT ''")
    return db


def _gh(*args: str, stdin: str = "") -> str:
    """gh, decoded as utf-8 whatever the console codepage says."""
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


# Every comment this file writes carries it, and nothing this file READS may
# have it. The pipeline runs in two places - as github-actions on a runner, and
# through the operator's own `gh` login on their desk - and on the desk the bot
# IS the owner, so each verdict it posted became the newest "answer" on the
# issue and the next poll read it back as a hand-written title. Issue #136,
# 2026-08-26: the desk re-posted the six-hour timeout and a second, contradictory
# publish time four hours after CI had said both, on a story already out.
#
# An HTML comment, because GitHub renders it as nothing at all. The tag after it
# says WHICH verdict, which is the other half - see _said().
MARK = "<!-- reddit-bot"


def _say(issue: int, body: str, tag: str = "") -> str:
    """Comment on the issue, marked as ours. Returns the comment URL."""
    return _gh("issue", "comment", str(issue), "--body",
               f"""{body}

{MARK}:{tag} -->""")


def _mine(comments: list[dict], owner: str) -> list[dict]:
    """The owner's own comments - ours dropped, whoever's login posted them."""
    return [c for c in comments
            if c["user"]["login"].lower() == owner.lower()
            and MARK not in (c["body"] or "")]


# The runner's login. No person can post under it, so a comment carrying it is
# ours whether or not it carries a mark - which is the only reason the lines
# below are safe. The desk posts as the OWNER, and an owner's comment is a
# title whatever it happens to say, so nothing here may look at those.
_BOT = "github-actions[bot]"

# The openings this file wrote before MARK existed (2026-08-27). They are still
# the newest word on issues that are still open, and _said() read them as never
# said: #140 and #141 were promised a publish time in the morning, the mark went
# in at midday, and both were promised a SECOND, different time in the afternoon.
# Every tag whose verdict must be announced exactly once is listed.
_LEGACY = {"when": "Принято, публикация",
           "timeout": "ч без ответа — ушло название",
           "take": "Принято, дубль",
           "caption": "Черновик ушёл в инбокс"}


def _said(comments: list[dict], tag: str) -> bool:
    """Has this verdict already been announced on this issue?

    The only record of one that BOTH machines can read. seen.db is the other
    one, and it crosses between them by git: a row CI answered at 16:04 is
    still sitting unanswered in the desk's copy at 21:18, and the desk then
    answers it again. The issue itself has one copy.
    """
    if any(f"{MARK}:{tag} -->" in (c["body"] or "") for c in comments):
        return True
    lead = _LEGACY.get(tag, "")
    return bool(lead) and any(
        c["user"]["login"].lower() == _BOT and lead in (c["body"] or "")
        for c in comments)


_owner_cache = ""


def _owner() -> str:
    global _owner_cache
    if not _owner_cache:
        _owner_cache = _gh("repo", "view", "--json", "owner", "-q", ".owner.login")
    return _owner_cache


_slug_cache = ""


def _slug() -> str:
    """owner/repo, for building a release download URL."""
    global _slug_cache
    if not _slug_cache:
        _slug_cache = _gh("repo", "view", "--json", "nameWithOwner",
                          "-q", ".nameWithOwner")
    return _slug_cache


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
    mine = _mine(comments, owner)
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
                     or (script._open_fault(body) if i == 1 else "")
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
    """Every story of this channel out for a title, in the order they go out.

    Which is the order they were PROMISED, and only falls back on the order
    they were parked in for a story with no promise yet. The two part company
    as soon as one question is answered before an older one: #141 was answered
    the minute it was asked and #140 took three tries, so #140 was parked first
    and publishes second.
    """
    out = []
    with _db() as db:
        rows = db.execute(f"SELECT {','.join(_COLS)} FROM review WHERE lang=? "
                          "ORDER BY CASE WHEN pub_at>0 THEN pub_at ELSE ts END",
                          (OUTPUT_LANG,)).fetchall()
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

    Rows come in the order they were promised, so the story handed back is the
    one whose slot came round first - which is the order main.py renders in and
    the order the times on the issues were counted in.

    Nothing is handed back before the time its issue was promised. That promise
    used to be an estimate that moved: publish.eta() was asked again from
    scratch every time another story settled in front of this one, so the time
    written on the issue and the time the video actually went out were two
    different times. _claim() writes one down and this holds the row until it.
    """
    first = None
    now = time.time()
    for r in _rows():
        got = _judge(r, timeout)
        if not got:
            continue
        # Every row, not only the first ready one: a take picked on the fourth
        # issue has to be read while the first is still waiting out its clock,
        # and _stage() is what reads it.
        if (stage := _stage(got, timeout)) == "wait":
            continue
        if stage == "render" and not got["pub_at"]:
            _claim(got)
        # The caller renders, or makes the takes and renders on a later tick.
        # Takes are not a publication and answer to no promise - they are the
        # last question left before one can be made.
        if first is None and (stage == "offer" or now >= got["pub_at"]):
            got["needs_takes"] = stage == "offer"
            first = got
    return first


def _stage(r: dict, timeout: bool) -> str:
    """What a title-settled row still owes: "render", "offer" or "wait".

    The takes stage sits BETWEEN the title being settled and the render, and
    only for a story that ships as one video: three takes of each of three
    parts is nine links, and nobody picks from nine. Turned off - REVIEW_TAKES
    of 0 or 1 - every row goes straight to "render", which is what every row
    did before this existed.
    """
    if REVIEW_TAKES <= 1 or len(r["written"]) > 1:
        return "render"
    if not r["takes"]:
        return "offer"
    if r["take"] >= 0:
        return "render"
    return "render" if _judge_take(r, timeout) else "wait"


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
    # Read once and carried on the row: _stage() needs the same comments to
    # find the take, and two fetches of one issue in one tick is one too many.
    r["comments"] = comments = _comments(r["issue"])

    # ALREADY OUT, and the strongest thing either machine can know. publish.yml
    # comments the caption into the issue the title was chosen on as each video
    # is sent, so a caption there means the video is in the TikTok inbox - and
    # a row still pointing at it is a row that would render and SEND that video
    # a second time. Checked ahead of everything else, including a row that has
    # its own title and publish time written on it: those say what this machine
    # decided, and the caption says what actually happened.
    if _said(comments, "caption"):
        log.info("%s: #%d is already in the inbox - dropping the spent row",
                 r["post_id"], r["issue"])
        rendered(r["post_id"])
        return None

    # Both machines write this table and it reaches the other one by git, so a
    # row here can be HOURS behind the issue it points at. The issue is the one
    # copy they share: a story whose publish time has already been announced on
    # it is settled, whatever this file still thinks. Drop the row rather than
    # decide it a second time - deciding it twice is what put two contradictory
    # times and two timeout notices on #136 (2026-08-26), four hours after the
    # video had already gone out.
    if not r["pub_at"] and _said(comments, "when"):
        log.info("%s: #%d was settled elsewhere - dropping the stale row",
                 r["post_id"], r["issue"])
        rendered(r["post_id"])
        return None

    if r["title"]:
        # Chosen already; a previous run just failed to render it.
        return _final(r, json.loads(r["body"]) if r["body"] else [])

    title, bodies, fault, cid = _choose(comments, _owner(),
                                        r["written"], r["answered"])
    if title is None:
        # Thrown away, and it does not come back: the post was marked used when
        # it was written, which is exactly the record needed here.
        log.info("%s: dropped by the user, not rendering", r["post_id"])
        close(r["post_id"], "Снято, рендера не будет.")
        return None
    if cid:
        r["title"] = title
        with _db() as db:
            db.execute("UPDATE review SET answered=?, title=?, body=? WHERE "
                       "post_id=? AND lang=?",
                       (cid, title, json.dumps(bodies, ensure_ascii=False),
                        r["post_id"], OUTPUT_LANG))
    if fault:
        _say(r["issue"],
             f"Не приму: {fault}.\n\nНапиши ещё раз, или `+` — возьму вариант модели.",
             "fault")
        log.info("%s: title refused (%s)", r["post_id"], fault)
        return None
    if not title:
        if not timeout or time.time() - r["ts"] < HOURS * 3600:
            return None
        if not _said(comments, "timeout"):
            _say(r["issue"], f"{HOURS} ч без ответа — ушло название модели.",
                 "timeout")
        log.info("%s: no answer in %dh, using the model's title", r["post_id"], HOURS)
        title = model_title
        # written down for the same reason an accepted one is: the timeout is
        # announced on the issue exactly once, not again on the next run.
        with _db() as db:
            db.execute("UPDATE review SET title=? WHERE post_id=? AND lang=?",
                       (title, r["post_id"], OUTPUT_LANG))

    r["title"] = title
    # No time is quoted here any more: the story is not finished being decided.
    # _claim() says it once the LAST question about it has been answered - see
    # the note there.
    return _final(r, bodies)


# ------------------------------------------------------------------- the takes

# Where the offered mp3s live between the run that makes them and the run that
# renders one. NOT an issue attachment: GitHub's API has no endpoint for those
# at all - the web drag-drop posts to a private one - and <audio> is stripped
# out of comment markdown, so an inline player was never on the table either.
# A release asset is the only durable, API-writable store here that costs no
# second secret and leaves nothing in git history. One release holds every
# story's takes; the assets are deleted as each story renders.
TAKES_TAG = "voice-review"
TAKES_HOURS = 6


def _asset(post_id: str, i: int) -> str:
    """What the take is CALLED on the release, which is its own file name.

    `gh release upload file#label` sets a display label and nothing else: the
    asset name is the basename of the file uploaded, and the download URL is
    built from the name. So this has to spell the mp3 exactly as voice.takes()
    wrote it, which is chan_file(post_id) - the bare post id on the default
    channel, and post_id_en on the other one.

    It used to splice in `_{lang}` unconditionally. On `en` that happens to be
    what chan_file() says, so the stage worked there; on `ru` every link posted
    was a 404, the picked take could not be fetched at the render and the story
    was quietly narrated a fourth time, and drop_takes() deleted nothing.
    #140 and #141, 2026-08-27.
    """
    return f"{chan_file(post_id)}_take{i + 1}.mp3"


def _release() -> None:
    """Make sure the holding release exists. Idempotent and cheap."""
    try:
        _gh("release", "view", TAKES_TAG, "--json", "tagName")
        return
    except RuntimeError:
        pass          # not there yet - gh exits non-zero, which _gh raises on
    _gh("release", "create", TAKES_TAG, "--notes",
        "Черновые дубли озвучки, которые ждут выбора в issue. "
        "Файлы удаляются, как только история отрендерена.",
        "--title", "voice review", "--prerelease")


def offer_takes(r: dict, mp3s: list, fish_voice: str = "") -> None:
    """Upload the takes and ask which one goes out.

    Called from review.yml on the comment that settles the title, so the two
    questions arrive on the same fast path; the render slot keeps the same call
    as its fallback. The story does not render on this tick - it renders on the
    one after the answer.

    `fish_voice` is the narrator these takes were read in, and it is written
    onto the row rather than left to be drawn again. The take that wins is the
    BODY of the video and nothing else - the title and the closing question are
    synthesized at the render - so a second draw there puts two narrators in
    one video. See the `voice` column in _db().
    """
    _release()
    urls = []
    for i, mp3 in enumerate(mp3s):
        name = _asset(r["post_id"], i)
        if mp3.name != name:      # cannot happen; renames the asset if it does
            log.error("take %s is named %s, the links say %s", i + 1, mp3.name, name)
        # No `#label`: it renames nothing and reads as if it did. clobber, so
        # a retry after a half-finished offer does not collide with the assets
        # the failed attempt already pushed.
        _gh("release", "upload", TAKES_TAG, str(mp3), "--clobber")
        urls.append(f"https://github.com/{_slug()}/releases/"
                    f"download/{TAKES_TAG}/{mp3.name}")

    numbers = ", ".join(f"`{i + 1}`" for i in range(len(urls)))
    links = "\n".join(f"{i + 1}. {u}" for i, u in enumerate(urls))
    out = _say(r["issue"], f"""\
**Озвучка готова, дублей {len(urls)} — выбери.**

{links}

Движок пересеивается на каждом вызове, так что это один и тот же текст,
прочитанный по-разному.

**Первой строкой — номер:** {numbers}.
Молчание {TAKES_HOURS} ч — уйдёт первый.""", "takes")
    cid = int(out.rstrip("/").rsplit("-", 1)[-1]) if "-" in out else 0
    r["voice"] = fish_voice
    with _db() as db:
        db.execute("UPDATE review SET takes=?, voice=? WHERE post_id=? AND lang=?",
                   (json.dumps({"n": len(urls), "cid": cid, "ts": time.time()}),
                    fish_voice, r["post_id"], OUTPUT_LANG))
    log.info("%s: %d takes offered on issue #%d, read by %s", r["post_id"],
             len(urls), r["issue"], fish_voice[:8] or "the engine's default")


def _pick(comments: list[dict], owner: str, after: int, n: int) -> int:
    """Which take the user asked for, or -1 while nothing valid has been said.

    Only the owner, and only comments AFTER the one that carried the links -
    the issue is public and already full of the title conversation, and a `1`
    written up there was about something else.
    """
    for c in _mine(comments, owner):
        if c["id"] <= after:
            continue
        first = (c["body"] or "").strip().splitlines()
        if first and (d := first[0].strip().strip(".")).isdigit() and 1 <= int(d) <= n:
            return int(d) - 1
    return -1


def _judge_take(r: dict, timeout: bool) -> bool:
    """True once the row has a take to render. Mirrors _judge()'s contract."""
    offer = json.loads(r["takes"])
    got = _pick(r["comments"], _owner(), offer["cid"], offer["n"])
    # Answered on the issue already - by the other machine, off a row that has
    # not reached this one yet. The pick itself is re-read above and needs no
    # help; what must not happen twice is saying so out loud.
    said = _said(r["comments"], "take")
    if got < 0:
        if not said and (not timeout
                         or time.time() - offer["ts"] < TAKES_HOURS * 3600):
            return False
        if not said:
            _say(r["issue"], f"{TAKES_HOURS} ч без ответа — ушёл первый дубль.",
                 "take")
            log.info("%s: no take picked in %dh, using the first",
                     r["post_id"], TAKES_HOURS)
        got = 0
    elif not said:
        _say(r["issue"], f"Принято, дубль {got + 1}.", "take")
    r["take"] = got
    with _db() as db:
        db.execute("UPDATE review SET take=? WHERE post_id=? AND lang=?",
                   (got, r["post_id"], OUTPUT_LANG))
    return True


def take_url(r: dict) -> str:
    """Where the chosen take lives, for the render to fetch."""
    return (f"https://github.com/{_slug()}/releases/download/"
            f"{TAKES_TAG}/{_asset(r['post_id'], r['take'])}")


def drop_takes(post_id: str, n: int) -> None:
    """Delete a story's assets once it has rendered. Best effort.

    A leftover asset costs nothing but clutter, so a failure here must never
    take the render down with it - by this point the video already exists.
    """
    for i in range(n):
        try:
            _gh("release", "delete-asset", TAKES_TAG, _asset(post_id, i), "--yes")
        except Exception:
            log.info("could not delete %s, leaving it", _asset(post_id, i))


def _local(ts: float) -> str:
    """A unix time in the clock the answer is read on, which is a phone."""
    tz = datetime.timezone(datetime.timedelta(hours=REVIEW_TZ_H))
    d = datetime.datetime.fromtimestamp(ts, tz)
    days = (d.date() - datetime.datetime.now(tz).date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days, f"{d:%d.%m}")
    return f"{day} в {d:%H:%M}"


def _claim(r: dict) -> None:
    """Promise the time this story publishes, and hold the row until then.

    Said once the story is FULLY decided - the title chosen and, where readings
    were offered, the take picked - and not a moment earlier. It used to be said
    the instant a title was accepted, which was a time for a video that then
    spent six more hours waiting for a voice to be chosen: #136 was quoted 19:07
    while its readings had not been made yet.

    publish.eta() answers the EARLIEST slot the gap between sends, the daily
    count, the midnight-UTC reset and the cron grid all allow. What was wrong
    was leaving it an estimate and counting the wrong thing into it: every
    story counted the stories PARKED before it that had settled, so a story
    answered late did not see the one answered early behind it. #140 and #141,
    2026-08-27: #141 was answered on sight and #140 took three tries, and both
    were told 22:07.

    A claim is written down instead, and what counts as ahead is holding a
    slot rather than being older. Every row still in this table is a video
    still to come - rendered() is what removes one - so this takes the first
    slot none of them has, and keeps it.

    Anything already rendered and sitting in out/ counts too; on CI that is
    nothing, because the runner takes out/ with it.

    Never fatal: the promise is worth more than the run, and a story with no
    time on it publishes on the next tick exactly as it did before this existed.
    """
    # A promise is made once and never moved. _poll() only asks on a row with
    # no time on it, and this says so here as well: a second call on a claimed
    # row would recount a queue that has changed underneath it and hand back a
    # different minute for a story the issue has already been told about.
    if r.get("pub_at"):
        return

    at = time.time()
    ahead = 0
    try:
        import publish

        with _db() as db:
            rows = db.execute("SELECT post_id, written, pub_at FROM review "
                              "WHERE lang=? AND pub_at>0", (OUTPUT_LANG,)).fetchall()
        # Videos ahead, not stories ahead: a claimed three-parter in front of
        # this one is three sends before it, not one.
        ahead = sum(len(json.loads(w)) for pid, w, _ in rows if pid != r["post_id"])
        ahead += len(publish.pending())
        at = publish.eta(ahead) or at
        # ...and no two stories are promised the same minute. eta() answers the
        # earliest slot with `ahead` videos in front, which is a count and not a
        # reading of who holds what: a row that rendered since the story in
        # front of it was promised takes its count out of the queue and hands
        # the next story a minute already spoken for. Bounded by the rows that
        # exist, so a publish.eta() that has stopped moving ends the walk.
        held = {p for pid, _, p in rows if pid != r["post_id"]}
        for _ in range(len(held)):
            if at not in held:
                break
            ahead += 1
            at = publish.eta(ahead) or at
    except Exception:
        log.exception("could not work out when %s publishes", r["post_id"])

    r["pub_at"] = at
    with _db() as db:
        db.execute("UPDATE review SET pub_at=? WHERE post_id=? AND lang=?",
                   (at, r["post_id"], OUTPUT_LANG))
    # Once per issue, and the mark is what makes that true across both machines
    # - see _said(). A second copy of this line is what #136 ended up with.
    if _said(r["comments"], "when"):
        return
    try:
        _say(r["issue"], f"Принято, публикация {_local(at)}"
             + (f" — в очереди впереди ещё {ahead}." if ahead else "."), "when")
    except Exception:
        log.exception("could not say when #%d publishes", r["issue"])


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


def owed_takes() -> list[dict]:
    """Settled stories that still owe the reader a choice of readings.

    The rows only. Narrating them is the caller's half and the expensive one -
    it needs the TTS key, ffmpeg and the whole of requirements.txt - so this
    answers with nothing but sqlite, and review.yml asks it before installing
    any of that. A comment that settled no title pays for none of it.

    Offering the takes used to wait for a render slot, on the grounds that the
    slot is where the TTS key and the minutes already were. What that cost is
    the wait: #141's title was accepted at 09:57 and its readings arrived at
    13:41, because a slot opens once every TIKTOK_MIN_GAP_HOURS. The question
    is asked off the comment now, and the slot keeps the fallback - a row that
    reaches one still owing takes is offered them there exactly as before.

    _final() is applied for the reason _judge() applies it: a take is read off
    `written`, and a story rewritten by hand in the comment has to be narrated
    as it was rewritten rather than as the model first wrote it.
    """
    out = []
    for r in _rows():
        # An empty `takes` is what keeps _stage() away from _judge_take(),
        # which reads the issue's comments - a gh call per row, on a gate whose
        # whole point is to be cheap.
        if r["title"] and not r["takes"] and _stage(r, False) == "offer":
            out.append(_final(r, json.loads(r["body"]) if r["body"] else []))
    return out


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
        _gh("issue", "close", str(row[0]),
            "--comment", f"{note}\n\n{MARK}:closed -->")


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

    # Does anything owe a choice of readings? The same bargain as --room and
    # read the same way: review.yml asks before it installs ffmpeg and the TTS
    # half of requirements.txt, and a comment that settled no title pays for
    # neither. Sqlite only - no gh call, no network.
    if "--owes-takes" in sys.argv:
        owed = owed_takes()
        print(f"{len(owed)} settled stor{'y' if len(owed) == 1 else 'ies'} "
              f"still to be read aloud")
        sys.exit(0 if owed else 1)

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

    ONE = [[MODEL, _filler("Модель нашла коробку. "
                            "«Не трогай её», сказала сменщица. ") + MODEL_CTA]]
    FILLER = _filler("Модель нашла эту коробку сама. "
                     "«Не трогай её», сказала сменщица. ")
    THREE = [[MODEL, FILLER + "И тут всё оборвалось."],
             [MODEL, FILLER + "И тут оборвалось снова."],
             [MODEL, FILLER + MODEL_CTA]]

    def c(i, login, body):
        return {"id": i, "user": {"login": login}, "body": body}

    # The bot posts under the OWNER'S login whenever the pipeline runs on a
    # desk rather than on a runner, so its own verdicts came back to it as the
    # newest "answer" - a title, on a story that was already published. What
    # tells them apart is the mark, and nothing else can.
    def b(i, body, tag=""):
        return c(i, OWNER, f"{body}\n\n{MARK}:{tag} -->")

    assert _mine([b(1, "Принято, публикация сегодня в 17:07", "when")], OWNER) == []
    assert _said([b(1, "6 ч без ответа", "timeout")], "timeout")
    assert not _said([b(1, "6 ч без ответа", "timeout")], "when")
    assert not _said([c(1, OWNER, "6 ч без ответа — ушло название модели.")],
                     "timeout"), "the text is not the record, the mark is"
    # ...unless the RUNNER wrote it, which no person can. Every issue still open
    # carries verdicts from before the mark existed, and reading those as never
    # said is what put a second, different publish time on #140 and #141.
    assert _said([c(1, _BOT, "Принято, публикация сегодня в 22:07 — ...")], "when")
    assert _said([c(1, _BOT, "Черновик ушёл в инбокс TikTok. Подпись:")], "caption")
    assert not _said([c(1, _BOT, "Принято, публикация сегодня в 22:07")], "take")
    # the take's own timeout is not the title's, and they open the same way
    assert not _said([c(1, _BOT, "6 ч без ответа — ушёл первый дубль.")], "timeout")

    # What the take is called on the release IS the file voice.takes() wrote:
    # `gh release upload file#label` labels, it does not rename, and the URL is
    # built from the name. Spelled any other way every link is a 404 - see
    # _asset(). chan_file() is the one place that decides, for both of them.
    assert _asset("abc", 1) == f"{chan_file('abc')}_take2.mp3"

    # nobody has said anything
    assert _choose([], OWNER, ONE, 0) == ("", [], "", 0)
    # ...and the bot talking to itself is still nobody
    assert _choose([b(1, "Не приму: нет метки [emphasis]", "fault")],
                   OWNER, ONE, 0) == ("", [], "", 0)
    assert _pick([b(31, "2", "take")], OWNER, 29, 3) == -1, "not even a bare number"
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
    story = (_filler("Я работал в ночную смену и однажды нашёл в подсобке коробку. "
                     "«Не открывай её», сказал сменщик. ")
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
    plain_story = _filler("Я работал в ночную смену и нашёл в подсобке коробку. "
                          "«Не открывай её», сказал сменщик. ")
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
    short = "Коротко про коробку. «Верни её», сказал сменщик. [doubtful] А вы бы [emphasis] вернули эту коробку?"
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
    mid = _filler("Я работал в ночную смену и однажды нашёл в подсобке коробку. "
                  "«Не открывай её», сказал сменщик. ")
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
                 "body TEXT DEFAULT '', takes TEXT DEFAULT '', "
                 "take INT DEFAULT -1, pub_at REAL DEFAULT 0, "
                 "voice TEXT DEFAULT '')")
    _mem.executemany("INSERT INTO review(post_id, lang, issue, ts, gender, sub, "
                     "score, written, title) VALUES (?,?,?,?,'m','s',1,?,?)",
                     [(pid, OUTPUT_LANG, iss, ts, w, t)
                      for pid, iss, ts, t, w in _rows_of])
    _db = lambda: _mem                                  # noqa: E731

    # Which take the user asked for. The issue is PUBLIC and already carries
    # the whole title conversation, so a bare "1" is an answer only from the
    # owner and only below the comment that posted the links.
    _cs = [c(30, "stranger", "2"), c(31, OWNER, "1"), c(32, OWNER, "3")]
    assert _pick(_cs, OWNER, after=31, n=3) == 2, "must read the LAST word, not the first"
    assert _pick(_cs, OWNER, after=29, n=3) == 0, "the owner's first pick wins"
    assert _pick([c(31, "stranger", "2")], OWNER, 29, 3) == -1, "a stranger decides nothing"
    assert _pick([c(31, OWNER, "9")], OWNER, 29, 3) == -1, "out of range is not a pick"
    assert _pick([c(31, OWNER, "")], OWNER, 29, 3) == -1, "an empty comment is not a pick"
    # a number has to LEAD the comment, or every title containing one is a vote
    assert _pick([c(31, OWNER, "мне нравится 2")], OWNER, 29, 3) == -1
    assert _pick([c(31, OWNER, "2.\nвторой живее")], OWNER, 29, 3) == 1, "trailing stop ok"

    # The stage a title-settled row is in. Off, or split across several videos,
    # and it goes straight to the render exactly as it did before takes existed.
    _one = {"written": [["t", "b"]], "takes": "", "take": -1, "issue": 1,
            "post_id": "z"}
    assert _stage({**_one, "written": [["t", "b"], ["t", "c"]]}, False) == "render",         "a split story is never offered takes"
    assert _stage({**_one, "takes": "{}", "take": 2}, False) == "render"
    _real_takes = REVIEW_TAKES
    globals()["REVIEW_TAKES"] = 1
    assert _stage(_one, False) == "render", "REVIEW_TAKES=1 turns the stage off"
    globals()["REVIEW_TAKES"] = 3
    assert _stage(_one, False) == "offer", "nothing offered yet"
    globals()["REVIEW_TAKES"] = _real_takes

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

        # Which settled stories still owe the reader a choice of readings, and
        # answered off the table alone - review.yml asks this before it installs
        # ffmpeg and the TTS half of requirements.txt. `b` has its title and has
        # not been read; `a` is still out for one.
        if REVIEW_TAKES > 1:
            assert [r["post_id"] for r in owed_takes()] == ["b"], owed_takes()
            assert owed_takes()[0]["written"] == [("T", ONE[0][1])], "title folded in"
            _mem.execute("UPDATE review SET takes='{}' WHERE post_id='b'")
            assert owed_takes() == [], "asked once is asked"
            _mem.execute("UPDATE review SET takes='', written=? WHERE post_id='b'",
                         (json.dumps(THREE),))
            assert owed_takes() == [], "a split story is never read aloud"
            _mem.execute("UPDATE review SET written=? WHERE post_id='b'",
                         (json.dumps(ONE),))
            # The narrator the takes were read in rides the row from here to
            # the render, which is the only thing that stops main.py drawing a
            # second one for the title and the closing question.
            _mem.execute("UPDATE review SET voice='heard' WHERE post_id='b'")
            assert owed_takes()[0]["voice"] == "heard", owed_takes()
            _mem.execute("UPDATE review SET voice='' WHERE post_id='b'")

        # Two stories decided in one sitting must not be promised the SAME
        # slot. #140 and #141 were - both "сегодня в 22:07" - because each
        # counted only the stories parked before it, and #141 was answered on
        # sight while #140 took three tries. A claim counts every row already
        # holding one, whatever order they were parked in.
        _real_say, _real_publish = _say, sys.modules.get("publish")
        try:
            _say = lambda *a, **k: ""                          # noqa: E731
            _fake = type(sys)("publish")
            _fake.pending = lambda: []
            _fake.eta = lambda ahead=0: 1000.0 + ahead * 3600.0
            sys.modules["publish"] = _fake
            for _pid in ("b", "a"):        # answered out of the order parked in
                _claim({"post_id": _pid, "issue": 1, "comments": [],
                        "written": ONE})
            assert sorted(row[0] for row in _mem.execute(
                "SELECT pub_at FROM review WHERE lang=?", (OUTPUT_LANG,))) \
                == [1000.0, 4600.0], "one slot each, and never the same one"
            # ...and the render order follows the promise, not the parking
            assert [r["post_id"] for r in _rows()] == ["b", "a"]
            # Asked a second time it says nothing and moves nothing. The minute
            # written on the issue is the minute, not the current best guess -
            # #141 was told 22:07 in the morning and 11:07 the next day in the
            # afternoon, off the same story settling in two stages.
            _fake.eta = lambda ahead=0: 9999.0                    # noqa: E731
            _claim({"post_id": "b", "issue": 1, "comments": [], "written": ONE,
                    "pub_at": 1000.0})
            assert sorted(row[0] for row in _mem.execute(
                "SELECT pub_at FROM review WHERE lang=?",
                (OUTPUT_LANG,))) == [1000.0, 4600.0], "a promise is not re-derived"
            # A slot another story already holds is stepped over. eta() counts
            # videos in front rather than reading who holds what, so a story
            # that rendered since the one ahead of it was promised takes its
            # count out of the queue and frees a minute already spoken for.
            _fake.eta = lambda ahead=0: 1000.0 + ahead * 3600.0   # noqa: E731
            _mem.execute("DELETE FROM review WHERE post_id='b'")  # b has gone out
            _mem.execute("INSERT INTO review(post_id, lang, issue, ts, gender, "
                         "sub, score, written) VALUES ('d',?,4,400.0,'m','s',1,?)",
                         (OUTPUT_LANG, json.dumps(ONE)))
            _claim({"post_id": "d", "issue": 4, "comments": [], "written": ONE})
            assert _mem.execute("SELECT pub_at FROM review WHERE post_id='d'"
                                ).fetchone()[0] == 8200.0, "4600 was taken"
            _mem.execute("DELETE FROM review WHERE post_id='d'")
            _mem.execute("INSERT INTO review(post_id, lang, issue, ts, gender, "
                         "sub, score, written, title) "
                         "VALUES ('b',?,2,200.0,'m','s',1,?,'T')",
                         (OUTPUT_LANG, json.dumps(ONE)))
        finally:
            _say = _real_say
            sys.modules.pop("publish", None)
            if _real_publish is not None:
                sys.modules["publish"] = _real_publish
            _mem.execute("UPDATE review SET pub_at=0")

        # A story whose video is already in the TikTok inbox is never decided
        # again, whatever this table still holds. The desk's copy of it is
        # hours behind CI's - it crosses by git - so #136 was re-decided at
        # 21:18 on a video CI had sent at 16:17, and the only thing standing
        # between that and a second copy of the same video in the inbox is the
        # caption publish.yml leaves on the issue.
        _real_comments = _comments
        try:
            _mem.execute("INSERT INTO review(post_id, lang, issue, ts, gender, "
                         "sub, score, written, title, pub_at) VALUES "
                         "('d',?,4,400.0,'m','s',1,?,'T',1.0)",
                         (OUTPUT_LANG, json.dumps(ONE)))
            _row_d = [r for r in _rows() if r["post_id"] == "d"][0]
            # unmarked, exactly as the workflow wrote it before 2026-08-27
            _comments = lambda n: [c(1, _BOT, "Черновик ушёл в инбокс TikTok. "
                                              "Подпись: вот она")]  # noqa: E731
            assert _judge(dict(_row_d), False) is None, "it is already out"
            assert not [r for r in _rows() if r["post_id"] == "d"], (
                "the spent row goes, so nothing renders it a second time")
        finally:
            _comments = _real_comments

        # A settled story is handed to the renderer at the time its issue was
        # promised and at no other. Before this it went the moment it settled,
        # so the time on the issue was a guess the pipeline never read back.
        _real_poll_bits = _judge, _stage, _claim
        try:
            _t = time.time()
            _judge = lambda r, t: r                              # noqa: E731
            _stage = lambda r, t: "render"                       # noqa: E731
            _claim = lambda r: r.__setitem__("pub_at", _t + 3600)  # noqa: E731
            assert _poll(False) is None, "a story waits for the time promised"
            _mem.execute("UPDATE review SET pub_at=? WHERE post_id='b'", (_t - 60,))
            assert _poll(False)["post_id"] == "b", "and goes when it arrives"
            # ...and a story still owed a take is not waiting on a clock at
            # all: the takes are the last question, asked before any promise.
            _stage = lambda r, t: "offer"                        # noqa: E731
            assert _poll(False)["needs_takes"], "takes answer to no promise"
        finally:
            _judge, _stage, _claim = _real_poll_bits
    finally:
        _db = _real_db

    # The time quoted back on the issue, in the reader's own clock.
    _now = time.time()
    assert _local(_now).startswith("сегодня в ")
    assert _local(_now + 86400).startswith("завтра в ")
    assert _local(_now + 5 * 86400)[:2].isdigit(), _local(_now + 5 * 86400)

    print("review ok")
