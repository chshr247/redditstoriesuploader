"""Stories found by listening to a channel that already read them out loud.

This is a SOURCE, in the sense source.py is one: it ends with rows the ordinary
pipeline can pick up, and it writes no story and narrates nothing. What it adds
over source.py is the picking - a video with 400k views is a story an audience
has already voted on, which is a stronger signal than contested() can get from
a post's comment count.

    python upvote.py --harvest       list the channels' videos, download none
    python upvote.py --digest 2      transcribe two, split them into stories
    python upvote.py --show          what is queued

Three steps, and the first two are cheap in the way that matters: --harvest
downloads no media at all, and --digest is the only thing that pulls audio, one
video at a time, so a run costs what one video costs.

THE RECORDING IS THE NARRATION. A story is cut out of the source audio and
handed to the render as it was read, over the operator's own background, under
the ordinary card and the ordinary captions. That rests on the channels'
permission to reuse the RECORDINGS and not only the stories - given
2026-09-05, which is the day this half of the file was written; without it a
harvested story would be text only and voice.py would read it aloud instead.

Nothing downstream knows the difference. The story is parked on an issue like
any other, the user answers for its title like any other, and the one seam is
body_mp3 - the clip is handed to voice.speak_parts() at exactly the point a
take picked off the issue is handed to it, and is aligned and subtitled by the
same code. The text is VERBATIM for that reason: it is what the tape says, so
nothing rewrites it. A narration rewritten by hand on the issue drops the
recording (see confirm()) rather than saying one thing under subtitles that
say another.

A long story goes out in parts, and the cuts are the model's - it is asked in
the same call that finds the stories where this one turns, so a part ends
where the viewer wants the next one rather than at the halfway mark.

WHAT THE DOWNLOAD NEEDS, because none of it is obvious from an error message
and all of it was learned the hard way on 2026-09-06. YouTube signs its media
URLs with a challenge, so a plain yt-dlp answers 403 to every full download
while --flat-playlist metadata keeps working perfectly - which is the
confusing part. Three things together fix it, and any two of them do not:

  * a JS runtime (node), passed as --js-runtimes; see _JS
  * yt-dlp-ejs, and a PO token provider. This desk uses the bgutil one in its
    script mode: pip install bgutil-ytdlp-pot-provider, then build the
    generator from the matching tag into the path the plugin looks in by
    default -
      git clone --branch 1.3.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider ~/bgutil-ytdlp-pot-provider
      cd ~/bgutil-ytdlp-pot-provider/server && npm install && npx tsc
    which puts generate_once.js where it is found without any extra flag.
  * a RECENT yt-dlp. 2026.07.04 answered 403 with a valid token in hand;
    2026.08.19 downloads. See the floor in requirements.txt.
"""
import argparse
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import script
from config import (DB_PATH, LLM_BASE_URL, MIN_SEC, OPENAI_API_KEY,
                    OUT_DIR, OUTPUT_LANG, PART_SEC, SUBREDDITS,
                    SUBREDDITS_HORROR, VOICE_SPEEDUP, chan_file)

log = logging.getLogger("upvote")

# handle:language, comma separated. The language is not decoration: a story
# harvested here is told in the language it was RECORDED in, so a channel that
# does not match OUTPUT_LANG has nothing this channel can use and is skipped
# rather than translated. Translating it would make it an ordinary reddit story
# with extra steps, and source.py already does that better.
CHANNELS = os.getenv("UPVOTE_CHANNELS", "upvotefamily:ru,tuchniyzhab:ru")
# NOT config.WHISPER_SIZE, and the difference is the whole reason this exists.
# voice._align() uses whisper for TIMES and throws its text away, so `base` is
# plenty there. Here the text IS the product - it becomes the story - and base
# mishears enough proper nouns and numbers to be worth the slower model.
WHISPER = os.getenv("UPVOTE_WHISPER", "small")
# A transcript segment is a few seconds of speech. Stories run minutes, so the
# model is handed segments rather than words: numbered lines it can reason
# about, instead of undifferentiated text.
#
# High enough for the whole video, deliberately. At 400 this cut every
# transcript at about twenty minutes, which is most of an upvotefamily video
# and the first THIRD of a tuchniyzhab one - so two thirds of the stories on
# the longest videos were unreachable, and the model was being asked to find
# the end of a story whose end it had not been shown.
#
# The number is measured rather than guessed, and the guess was wrong twice.
# Whisper cuts a segment about every 2.4 seconds on these channels - 1600 of
# them reached 64.5 minutes of a 74-minute video, leaving ten minutes and the
# last story unread (wEgnl93S-bw, 2026-09-06). 2200 is that rate with room:
# 88 minutes, past the longest video either channel has posted.
#
# What it costs, so the next person changing it knows: whisper reads the whole
# file instead of stopping early (the slow half - a 74-minute video is a
# quarter of an hour on this CPU), and the split prompt grows with it, near
# 30k tokens at the ceiling. Both are cheap next to the download. The risk
# that is NOT cheap is the model's: it under-enumerated a 400-line transcript
# until the prompt was fixed, and a 1600-line one asks more of it - if long
# videos come back with two stories in them, look here first.
MAX_SEGMENTS = int(os.getenv("UPVOTE_MAX_SEGMENTS", 2200))

# How long a harvested story may run, and in how many videos - its own numbers
# rather than config.PART_SEC and script.MAX_PARTS, because the two paths are
# not the same shape. A WRITTEN story that wants five parts is told in three:
# the model is asked for fewer words and writes a shorter story. A RECORDING
# that wants five parts is however many minutes of somebody talking, and told
# in three it is three videos of whatever that divides into. So the length is
# the story's, not the channel's, and these say how much of it the channel
# will take.
#
# Five parts is HEADROOM and it is load-bearing. A post and its updates are
# ONE story with cuts on the update markers (see the split prompt), because
# an update told as its own video opens by thanking an audience for advice
# that video never showed. That is the right call for the viewer and it is
# what makes half an hour reachable: the wife-affair chain on wEgnl93S-bw is
# 29 minutes. The ceiling above it is main._park_one, which drops - loudly -
# anything needing more parts than a day of videos can hold.
#
# The real ceiling is the smaller of this and the DAY's allowance, and it is
# applied in main._park_one: a split story publishes inside one day, so a
# sixth part is a part that cannot go out. Raising PARTS without raising
# config.TIKTOK_PER_DAY buys nothing at all.
#
# The length itself was 480 until 2026-09-07, chosen against the platform's
# ten-minute limit rather than against anybody watching. An eight-minute part
# is a duration a viewer reads off the scrubber in the first seconds and
# leaves on, and at 480 most of the queue went out as ONE video - eighteen of
# twenty-four unused stories, the longest of them ten minutes. 270 puts the
# longest part in that queue at 4:24 instead of 8, costs four extra renders
# across the whole queue, and drops nothing: the queue's two longest stories
# (14.9 min) come to four parts, which is what a day holds.
#
# What it DOES narrow is the far end. The most a story may run is this times
# main._park_one's `most`, which is 4 on the workflow's TIKTOK_PER_DAY - so
# the effective ceiling is 18 minutes, not the 30 STORY_MAX still allows, and
# a chain between the two is now dropped where 480 would have taken it.
# Nothing in the queue is there yet. If the log starts naming such stories,
# this is the number that let them in, not STORY_MAX.
PART_MAX = int(os.getenv("UPVOTE_PART_SEC", 270))
# ...and the ceiling on a WHOLE story, past which it is not published at all.
#
# It exists because a post and its updates are one story now (see the split
# prompt), and a chain can run to the length of the video: wEgnl93S-bw is a
# man, his wife's affair, and updates 2 through 11 - 73 minutes end to end,
# ten parts, which is two days of the channel spent on one account. Half an
# hour is the most a single story may take, and a longer one is skipped
# whole rather than published in a size nobody asked for or hacked back into
# pieces that each open on "thanks for the advice".
#
# Enforced in code and deliberately NOT told to the model: asked to keep a
# story under a length, it would go back to answering one long chain as
# several stories, which is the thing the prompt just stopped it doing.
STORY_MAX = int(os.getenv("UPVOTE_STORY_SEC", 1800))
PARTS = int(os.getenv("UPVOTE_MAX_PARTS", 5))
# TikTok's own limit on one video, and the last word whatever the two above
# say. Parts are cut on the model's turns rather than on a stopwatch, so an
# uneven set of turns can hand one part more than its share - and a part past
# this is a part the platform refuses.
PART_CEILING = 600

# The channel's subs, as a CHOICE offered to the model rather than a string
# match made after it. Free-form, the same story came back r/PettyRevenge on
# one run and r/ProRevenge on the next (N09wqiI_O4w, 2026-09-06) - one of
# which is on this channel's list and one of which is not, so the filter kept
# or dropped a story on a coin toss. Asked to pick from the list, the model
# answers the question the filter is actually asking: is this the kind of
# story this channel publishes.
# Both of the channel's lists, because both are its own. config.py keeps them
# disjoint so the two Reddit pools cannot rank against each other or take each
# other's slot - but that separation is about WHERE a story is scraped from,
# and a recording was not scraped from anywhere. Offering only the ordinary
# list made the model answer `none` to a story this channel does publish, and
# the filter below then dropped it: r/Glitch_in_the_Matrix, ggwWdlCb33U #3,
# 2026-09-07. The sub travels with the story either way, so voice.py still
# pins the horror narrator and _pick_music still reaches for the horror bed.
_DIGEST_SUBS = list(SUBREDDITS) + list(SUBREDDITS_HORROR)

_SUBS_BLOCK = ("The list:\n" + "\n".join(f"  {x}" for x in _DIGEST_SUBS)
               if _DIGEST_SUBS else
               "This channel publishes any subreddit, so name the one it "
               "reads like and never answer none.")

SPLIT_SYSTEM = """\
You are given a transcript of one video in which a narrator reads several
unrelated stories aloud, one after another. Each story was originally a post on
Reddit. The transcript is numbered by segment, each line "N [start-end] text".

Find where each story begins and ends. Ignore the channel's own intro, outro,
sponsor reads, and any "like and subscribe" talk - those belong to no story.
The video opens by naming the show and the episode number ("<show name>, выпуск
номер 27"), and hands over to every story after the first one the same way -
"Следующая история", "И переходим к следующей истории", "И последняя на сегодня
история". That line is the CHANNEL speaking, not the story: it belongs to no
story, and the story starts on the words after it.

A video of this length holds SEVERAL stories - commonly five to ten, one every
two to four minutes. List every one of them. Stopping after the first story or
two, and sweeping the rest of the video into one enormous range, is the single
most common way to answer this wrongly.

Answer with JSON only, a list of objects:

  [{"first": 12, "last": 88, "sub": "AmItheAsshole", "title": "...",
    "teller": "f", "starts": "", "cuts": [40, 63]}]

  first        - the segment where the story's OWN words begin. The channel
                announces the show and the episode number in the same breath as
                the story's first line ("<show name>, выпуск номер 27. На
                работе..."), and one segment holds both - so pick the segment
                the story starts in even when the announcement shares it, and
                quote the story's own first words in `starts` so the
                announcement can be cut off in front of them. Do not move to
                the next segment to avoid them: that throws away the story's
                opening line, which is the one line the video cannot afford to
                lose.
  starts      - the first three or four words of the story, copied exactly
                from `first`'s segment. They are how the announcement is told
                from the story when one segment holds both, and MOST stories
                need them: the channel hands over to the next story in the
                same breath as its first line. Leave it "" only when the
                segment really does open on the story's own words.
                Those words are usually the story's title read aloud - the
                title IS its opening line, so quote from the START of it.
                Quoting from somewhere after it cuts the hook off the story,
                and that is worse than leaving the announcement in.
  first, last - segment numbers, inclusive. They must not overlap between
                stories, and must run in increasing order. `last` may never be
                larger than the highest segment number in the transcript - if a
                story runs to the end of what you were given, use that number.
                Do not guess at how long the video is: you are given all of it
                that matters, and a number past the end is an invented one.
  sub         - which of THIS CHANNEL'S subreddits the story reads like it
                came from, spelled exactly as it appears in the list below.
                Answer "none" when it belongs to none of them: a round-up of
                mysteries, a quiz, a page of one-line answers, a story from a
                sub this channel does not publish. "none" is a real answer and
                a common one - do not stretch a story into the nearest name on
                the list to avoid saying it.

%s
  teller      - "f" if the person telling the story is a woman, "m" if a man,
                "" if the story never says. Decide it from what the story
                itself says - who they are, who they live with, how others
                address them - and not from how the narrator's voice sounds:
                one reader reads every story on the channel.
  title       - a short title for the story, in the transcript's own language.
                It is the STORY's title and never the show's - no show name, no
                episode number, nothing that would only make sense to somebody
                who already watches that channel.
  cuts        - segment numbers where this story could be broken across two
                videos. A cut is the FIRST segment of what would be the next
                video, and it goes where the story TURNS: the moment somebody
                answers back, or the thing the narrator has been building to
                is about to land. Never mid-sentence, never inside a scene,
                and never in the last quarter of the story - a part that ends
                after the point has been made is a part nobody comes back for.
                [] for a story short enough to be one video.

                ENOUGH OF THEM TO GO ROUND. No stretch between the start of
                the story, its cuts, and its end may run longer than %d
                minutes - a stretch longer than that is a video nobody can
                publish, and the whole story is then thrown away rather than
                published in pieces of the wrong size. Count the minutes as
                you go, from the [start-end] on each line: every time a
                stretch is getting close, find the best turn inside it and cut
                there. A long opening needs cuts inside it just as much as the
                rest does - do not simply mark the joins between the obvious
                sections and leave a twenty-minute first act.

                And do NOT answer a long story as several stories to satisfy
                this. A new story starts ONLY where a DIFFERENT person's
                account begins - a plain change of who is talking and about
                what. An update by the same narrator is NOT that, however
                it is announced - "Update 4", "Обновление", "Апдейт второй,
                через три дня", or a fresh greeting to the readers.
                All of it is the same person still telling the same story, so
                it is a CUT - and the best kind there is, because it is
                exactly where the audience came back for more. Told as its
                own story it is broken: a video that opens "thank you all for
                the advice" for viewers who were never shown what the advice
                was about is a video nobody can follow. So everything from
                the first word to the last update is ONE story with cuts in
                it, however many minutes it runs. If you find yourself ending
                a story where the same narrator simply carried on talking,
                that is a cut, not a story.

A story shorter than %d seconds is not a story, it is a fragment - leave it
out. If the video contains no complete story at all, answer exactly: []
# The order the substitutions appear in the text above: the sub list, then
# the minutes a part may run, then the floor under a story.
""" % (_SUBS_BLOCK, PART_MAX // 60, MIN_SEC)


PICK_SYSTEM = """\
You are given the titles of videos from a channel that reads stories aloud,
one per line as "N. title".

Answer with the numbers of the videos that are COMPLETE STORIES told start to
finish - the kind where a narrator reads out a post somebody wrote about
something that happened to them. One story or several, either is fine.

Leave out everything else, and there is plenty of it:

  * round-ups of short answers to a question - dozens of one-line replies,
    where nothing runs long enough to be a video on its own
  * mysteries, history, facts, true crime, lists, quizzes, news, reactions
  * anything addressed to the audience as a question rather than told as an
    account of what happened to one person

The test is what the title promises. A title that says who did what and how it
turned out is the kind to keep. A title that asks the audience something is
usually the kind to leave out - but not always: a question can be the first
line of one person's own story ("You want me online at all hours? No problem,
but I am a programmer"), and that one stays.

The channel's own labelling says nothing either way. A rubric in brackets, a
series name, a "#15" on the end - that is how the channel files its videos,
not what is in them. Judge the rest of the title and ignore the label.

WHEN YOU ARE NOT SURE, KEEP IT. The two mistakes do not cost the same. A video
kept wrongly costs one reading of one video, and the story it turns out not to
have is caught further down the line anyway. A video dropped wrongly is a story
that nobody will ever see, that nothing downstream can recover, and that no
one will know was lost. Leave out what is plainly not a reading, and keep
everything else.

Answer with JSON only, and answer for the videos you are LEAVING OUT - the
ones you keep need no line. Each one is an object naming it twice, by number
and by its own first words:

  [{"n": 4, "t": "Какой секрет разрушит"},
   {"n": 11, "t": "What Simple Job Would"}]

  n - the number on the line.
  t - the first three or four words of that same line, copied exactly. They
      are what identifies the video: numbers are easy to lose count of down a
      long list, and a verdict landing on the wrong line throws away somebody
      else's story.

An empty list is a valid answer - it means every video here is a reading.
"""

# One call reads this many titles. A channel's whole harvest fits in two or
# three of them - the titles are one line each, so this is thousands of tokens
# where the transcript split is tens of thousands.
PICK_BATCH = int(os.getenv("UPVOTE_PICK_BATCH", 60))


def _db():
    db = sqlite3.connect(DB_PATH)
    # What the channel has published, as metadata only. `done` is set once the
    # video has been transcribed, so --digest can be run repeatedly and picks
    # up where it stopped instead of paying for the same audio twice.
    # `keep` is the title filter's verdict: 1 a reading, 0 something else,
    # NULL not judged yet. NULL is treated as 1 downstream - a judgement that
    # never happened must not empty the queue.
    db.execute("CREATE TABLE IF NOT EXISTS yt("
               "id TEXT PRIMARY KEY, chan TEXT, title TEXT, sec REAL, "
               "views INT, ts REAL, done INT DEFAULT 0, keep INT)")
    try:
        db.execute("ALTER TABLE yt ADD COLUMN keep INT")   # rows that predate it
    except sqlite3.OperationalError:
        pass
    # One row per story found inside one video. `start`/`end` are seconds into
    # the source recording: a transcript is split on a time axis because that
    # is the only axis whisper gives, and they are kept so a story can be
    # re-read against the audio it came from without transcribing again.
    #
    # `segs` and `cuts` are what the story can still be cut on after the
    # transcription is over: the segments it is made of, and the segment
    # numbers the model marked as turns in it. `parts` is the answer once the
    # story has actually been parked - one entry per video, with the stretch
    # of the recording it is read from - and it is the only thing narration()
    # needs at the render, hours and one process later.
    db.execute("CREATE TABLE IF NOT EXISTS yt_story("
               "vid TEXT, n INT, sub TEXT, title TEXT, body TEXT, "
               "start REAL, end REAL, views INT, ts REAL, used INT DEFAULT 0, "
               "segs TEXT, cuts TEXT, parts TEXT, "
               "PRIMARY KEY(vid, n))")
    return db


def _channels() -> list[str]:
    """The handles worth reading for THIS channel's language."""
    out = []
    for pair in CHANNELS.split(","):
        handle, _, lang = pair.strip().partition(":")
        if not handle:
            continue
        if lang and lang != OUTPUT_LANG:
            log.info("%s reads in %s, this channel is %s - skipping",
                     handle, lang, OUTPUT_LANG)
            continue
        out.append(handle.lstrip("@"))
    return out


# YouTube signs its media URLs with a script the extractor has to RUN, so the
# DOWNLOAD needs a JS engine even though the metadata listing does not: without
# one every media request comes back 403, and yt-dlp says so only in a line of
# its own deprecation notice. Measured here 2026-09-06. Node is what this desk
# and GitHub's runners both have; the flag is passed only when the runtime is
# really there, so a machine without it gets yt-dlp's own complaint rather than
# one about a runtime nobody installed.
_JS = ["--js-runtimes", "node"] if shutil.which("node") else []

# A signed-in session, for the runs that need one. A PO token answers the URL
# challenge; it does NOT answer "Sign in to confirm you are not a bot", which
# is what YouTube shows a datacentre IP - measured on this repo's runner
# 2026-09-06, three downloads out of three refused in under two seconds while
# the same videos resolved from the desk without a cookie in sight.
#
# Netscape cookies.txt, the only format yt-dlp reads. Empty or missing means
# no cookie is passed at all, which is the right default: an anonymous
# download from a residential IP works, and a cookie file is an ACCOUNT - if
# YouTube decides the traffic is abusive, the account wearing it is the one
# that answers for it. Treat whatever fills this like a password: it is a
# live session, it outlives a password change, and only signing out of all
# devices revokes it.
_COOKIES = os.getenv("UPVOTE_COOKIES", "")
_COOKIE_ARGS = (["--cookies", _COOKIES]
                if _COOKIES and Path(_COOKIES).is_file() else [])


def _ytdlp(*args: str) -> str:
    """yt-dlp, or a clear word about why it is not here.

    Run as a MODULE, not as the `yt-dlp` command: pip installs the console
    script into a Scripts/ directory that is not always on PATH - it was not
    on this desk - while `python -m yt_dlp` works wherever the package was
    installed for the interpreter that is running this.
    """
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", *_JS, *_COOKIE_ARGS,
                            *args], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if "No module named" in (e.stderr or ""):
            raise RuntimeError("yt-dlp is not installed - pip install yt-dlp")
        raise RuntimeError(f"yt-dlp failed: {(e.stderr or '').strip()[-300:]}")
    return r.stdout


def harvest(limit: int = 60) -> int:
    """Metadata for each channel's latest videos. Downloads no media."""
    added = 0
    with _db() as db:
        for handle in _channels():
            raw = _ytdlp("--flat-playlist", "-J", "--playlist-end", str(limit),
                         f"https://www.youtube.com/@{handle}/videos")
            entries = (json.loads(raw) or {}).get("entries") or []
            for e in entries:
                if not e.get("id"):
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO yt(id, chan, title, sec, views, ts) "
                    "VALUES (?,?,?,?,?,?)",
                    (e["id"], handle, e.get("title") or "",
                     e.get("duration") or 0, e.get("view_count") or 0, time.time()))
                added += cur.rowcount
                # Views move; the row does not. A video harvested last month is
                # ranked on what it has NOW, which is the whole point of ranking
                # on views rather than on recency.
                db.execute("UPDATE yt SET views=? WHERE id=?",
                           (e.get("view_count") or 0, e["id"]))
            log.info("%s: %d videos listed", handle, len(entries))
    # Judged here and not at the digest: the point of the filter is to spend
    # nothing on a video that was never a reading, and by the digest the
    # download has already happened.
    judge()
    return added


def _parse_pick(raw: str, titles: list[str]) -> tuple:
    """Model answer -> the video numbers to LEAVE OUT, plus what is wrong.

    Every verdict has to name its video in words as well as in numbers, and
    the words are what counts. The model judges titles well and loses count
    badly: the same three titles it got right in a batch of three came back
    with the good story dropped inside a batch of sixty (2026-09-06). A
    verdict whose words match no title is discarded rather than applied to
    whatever line its number points at - discarding it keeps the video, which
    is the safe way to be wrong here.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return set(), ["answer is not a JSON list"]
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return set(), [f"JSON will not parse: {e}"]
    if not isinstance(items, list):
        return set(), ["answer is not a JSON list"]

    heads = [[_plain(w) for w in t.split() if _plain(w)] for t in titles]
    drop, faults = set(), []
    for x in items:
        if not isinstance(x, dict):
            faults.append(f"{x!r} is not an object with n and t")
            continue
        want = [_plain(w) for w in str(x.get("t") or "").split() if _plain(w)]
        if not want:
            faults.append(f"video {x.get('n')} was named by number only")
            continue
        hit = [i for i, h in enumerate(heads) if h[:len(want)] == want]
        if len(hit) == 1:
            drop.add(hit[0])
        else:
            faults.append(
                f"{' '.join(want)!r} matches "
                f"{'no title' if not hit else f'{len(hit)} titles'} in the list")
    return drop, faults


def _pick(titles: list[str]) -> set:
    """One model call: which of these titles are NOT readings, by index."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty - fill in .env")
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    return script._ask(
        client, PICK_SYSTEM,
        "\n".join(f"{i}. {t}" for i, t in enumerate(titles)),
        lambda raw: _parse_pick(raw, titles),
        keep="Keep the verdicts you already had right.",
        temperature=0)


def judge() -> tuple:
    """Mark which un-judged videos are readings. (kept, dropped).

    Runs at the HARVEST, where a verdict costs a line of text rather than a
    video. Both of these channels mix formats - a round-up of one-line answers
    reads like a story channel's video and is not one - and until this existed
    the only thing that noticed was the sub filter, which sits at the far end
    of a download, a transcription and a split. One of those cost 24 minutes
    of audio and three model calls to find out the video was about the Dyatlov
    Pass (gQvJPc7eFgY, 2026-09-06).

    A batch that cannot be judged is LEFT un-judged rather than dropped: NULL
    reads as "worth digesting" downstream, so a model that is unreachable
    slows nothing down and loses nothing.
    """
    kept = dropped = 0
    with _db() as db:
        rows = db.execute("SELECT id, title FROM yt WHERE keep IS NULL "
                          "ORDER BY views DESC").fetchall()
    if not rows:
        return 0, 0
    for i in range(0, len(rows), PICK_BATCH):
        chunk = rows[i:i + PICK_BATCH]
        try:
            out = _pick([t for _, t in chunk])
        except Exception:
            log.exception("could not judge %d titles - leaving them unjudged",
                          len(chunk))
            continue
        with _db() as db:
            db.executemany("UPDATE yt SET keep=? WHERE id=?",
                           [(0 if k in out else 1, vid)
                            for k, (vid, _) in enumerate(chunk)])
        for k in sorted(out):
            log.info("not a reading: %s", chunk[k][1][:70])
        kept, dropped = kept + len(chunk) - len(out), dropped + len(out)
    log.info("titles judged: %d readings, %d something else", kept, dropped)
    return kept, dropped


# Where a source recording waits between the machine that CAN download it and
# the machine that renders from it. They are not the same machine any more:
# YouTube answers a datacentre IP with "sign in to confirm you are not a bot"
# before it hands over any stream URL, so a GitHub runner cannot fetch a video
# at all - measured 2026-09-06, three downloads out of three refused in under
# two seconds with the PO token provider built and in place, while the same
# videos came down from the desk untouched.
#
# So the desk digests and puts the audio in a release; the runner renders and
# takes it out again. A release rather than the Actions cache because the desk
# cannot write to that cache, and rather than the repo because these are 20 MB
# of mp3 per video that no diff should ever carry.
#
# ponytail: nothing prunes the release. It grows by one mp3 per digested
# video, roughly 60 MB a day at three a day. The rule is not the hard part -
# cache_audio() already works out which recordings are still wanted, and
# everything else could go - it is that deletion would run against WHOEVER's
# seen.db, and this file travels between a desk and a runner that are
# routinely hours apart. A prune off a stale copy deletes the recording of a
# story the other machine is about to render. Prune by hand, or teach it to
# refuse when the db is behind origin.
AUDIO_RELEASE = os.getenv("UPVOTE_AUDIO_RELEASE", "source-audio")


def _gh_release(*args: str) -> subprocess.CompletedProcess:
    """gh release, never raising: every caller here has a fallback."""
    return subprocess.run(["gh", "release", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _release_get(out: Path) -> bool:
    """Fetch one source mp3 out of the release. False if it is not there.

    False is an ordinary answer and not an error: the release is a shortcut,
    and a machine that can download the video does not need it.
    """
    r = _gh_release("download", AUDIO_RELEASE, "-p", out.name,
                    "-D", str(out.parent))
    if r.returncode == 0 and out.exists():
        log.info("%s: source audio came from the %s release", out.name,
                 AUDIO_RELEASE)
        return True
    return False


def _release_put(out: Path) -> None:
    """Put one source mp3 in the release, creating the release on first use.

    Never fatal. The audio is on this machine either way, and a digest that
    banked its stories must not be thrown away over an upload - the next
    --cache-audio picks up whatever did not make it.
    """
    r = _gh_release("upload", AUDIO_RELEASE, str(out), "--clobber")
    if r.returncode:
        _gh_release("create", AUDIO_RELEASE, "--title", "source audio",
                    "--notes", "Recordings the harvested stories are cut from."
                    " Uploaded by upvote.py --cache-audio; not part of a"
                    " release in the ordinary sense.")
        r = _gh_release("upload", AUDIO_RELEASE, str(out), "--clobber")
    if r.returncode:
        log.warning("%s: could not be put in the %s release - %s", out.name,
                    AUDIO_RELEASE, r.stderr.strip()[-200:])
    else:
        log.info("%s: in the %s release", out.name, AUDIO_RELEASE)


def _release_names() -> set:
    """What the release already holds, so a run uploads only what it must."""
    r = _gh_release("view", AUDIO_RELEASE, "--json", "assets",
                    "-q", ".assets[].name")
    return set(r.stdout.split()) if r.returncode == 0 else set()


def _audio(vid: str) -> Path:
    """The video's audio, local. The only step that pulls media."""
    out = OUT_DIR / f"yt_{vid}.mp3"
    # The release first, and yt-dlp only if it is not there: the release is
    # the one source a runner can actually reach.
    if not out.exists() and not _release_get(out):
        _ytdlp("-x", "--audio-format", "mp3", "--audio-quality", "5",
               "-o", str(OUT_DIR / f"yt_{vid}.%(ext)s"),
               f"https://www.youtube.com/watch?v={vid}")
    if not out.exists():
        raise RuntimeError(f"yt-dlp produced no mp3 for {vid}")
    return out


def _transcribe(mp3: Path) -> list[dict]:
    """[{i, start, end, text, words}] - whisper's segments, as it heard them.

    Word times are asked for because of one segment per video: the one where
    the channel's own announcement and the story's first line are spoken in
    the same breath. The story has to start on a WORD there, and a segment is
    three seconds of "close enough" that lands mid-phrase. Everywhere else the
    words are ignored - see _parse_split, which drops them once the head of
    the first segment has been trimmed.
    """
    import voice
    segments, _ = voice.model(WHISPER).transcribe(str(mp3), language=OUTPUT_LANG,
                                                  word_timestamps=True)
    out = []
    for i, s in enumerate(segments):
        text = (s.text or "").strip()
        if text:
            out.append({"i": len(out), "start": round(s.start, 2),
                        "end": round(s.end, 2), "text": text,
                        "words": [{"w": w.word.strip(),
                                   "start": round(float(w.start), 2),
                                   "end": round(float(w.end), 2)}
                                  for w in (s.words or [])]})
        if len(out) >= MAX_SEGMENTS:
            log.warning("%s: stopping at %d segments", mp3.name, MAX_SEGMENTS)
            break
    return out


_morph = None


def _past(word: str):
    """pymorphy's reading of `word` when it is a past-tense singular verb.

    None for everything else, which is most words - so the caller can walk a
    sentence and touch only what it means to touch.
    """
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    for p in _morph.parse(word):
        if p.tag.POS in ("VERB", "PRTS", "PRTF") and p.tag.tense == "past" \
                and p.tag.number == "sing":
            return p
    return None


def _same_case(was: str, now: str) -> str:
    return now.capitalize() if was[:1].isupper() else now


def agreed(text: str, teller: str) -> str:
    """`text` with the storyteller's own verbs put into the right gender.

    Whisper cannot hear this and the model can read it. "решил" and "решила"
    differ by one unstressed vowel at the end of a word, and which one comes
    back is chance: the same model on the same audio answered "решил" reading
    the video from the top and "решила" reading the story on its own, because
    a three-second shift moves every 30-second window (N09wqiI_O4w,
    2026-09-06). A bigger model does not fix it - `medium` got the gender
    right and returned the whole transcript without punctuation or capitals,
    which subtitles cannot use.

    So the gender is decided ONCE, by the model that reads the story and can
    see who is telling it, and applied here by pymorphy3 - which is already a
    dependency, for numerals in voice.py. Only the verb that follows "я"
    within a couple of words is touched, only its ending changes, and the
    number of words is the same afterwards: the audio still says what the
    subtitles say, to the vowel.
    """
    if teller not in ("m", "f"):
        return text
    want = "masc" if teller == "m" else "femn"
    toks = list(re.finditer(r"\w+", text, re.UNICODE))
    edits = []
    for i, t in enumerate(toks):
        if t.group().lower() != "я":
            continue
        for nxt in toks[i + 1:i + 4]:
            if (p := _past(nxt.group())) is None:
                continue
            if p.tag.gender != want and (form := p.inflect({want})):
                edits.append((nxt.start(), nxt.end(),
                              _same_case(nxt.group(), form.word)))
            break        # the first verb after "я" is the one it governs
    for a, b, w in reversed(edits):
        text = text[:a] + w + text[b:]
    return text


def _plain(word: str) -> str:
    """A word stripped to what two spellings of it have in common."""
    return re.sub(r"[^\w]", "", word).lower()


# The channel handing over between stories - "Следующая история", "И переходим
# к следующей истории", "И последняя на сегодня история". The model is told to
# quote past it in `starts` and does not: of 85 stories harvested up to
# 2026-09-07 it left five announcements whole and cut two of them off one word
# in ("к следующей истории." at the head of the story). The phrases are a
# closed set, so they are cut here rather than asked for again.
_INTRO = re.compile(
    r"^\W*(?:(?:и|а|ну|итак)\W+)*(?:(?:мы\s+)?переход\w*\s+)?(?:к\s+)?"
    r"(?:следующ|последн|очередн|перв|нов)\w*(?:\s+(?:на\s+)?сегодня)?"
    r"\s+истори\w*(?:\s+(?:на\s+)?сегодня)?\W*", re.I)


def _intro_words(words: list[dict]) -> int:
    """How many words at the head of `words` are the channel's hand-over."""
    m = _INTRO.match(" ".join(w["w"] for w in words))
    return min(len(m.group(0).split()), len(words)) if m else 0


def _numbered(segs: list[dict]) -> str:
    """The transcript as the model reads it, and how far it runs.

    The header is not decoration: without it the model answers with segment
    numbers past the end of what it was given - 111-795 on a 400-segment
    transcript, three attempts running (N09wqiI_O4w, 2026-09-06). It is
    guessing at the length of the video, and this is the answer.
    """
    head = (f"The transcript has {len(segs)} segments, numbered 0 to "
            f"{len(segs) - 1}. Every number you answer with must be in "
            f"that range.\n\n")
    return head + "\n".join(
        f"{s['i']} [{s['start']:.0f}-{s['end']:.0f}] {s['text']}" for s in segs)


def _parse_split(raw: str, segs: list[dict]) -> tuple[list[dict], list[str]]:
    """Model answer -> stories, plus what is wrong with it.

    Shape is checked here rather than trusted, because every field is an index
    into someone else's list: a `last` past the end or a pair that runs
    backwards would slice silently and produce a story made of nothing.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return [], ["answer is not a JSON list"]
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return [], [f"JSON will not parse: {e}"]
    if not isinstance(items, list):
        return [], ["answer is not a JSON list"]

    out, faults, last_end = [], [], -1
    for k, it in enumerate(items):
        if not isinstance(it, dict):
            faults.append(f"story {k} is not an object")
            continue
        try:
            a, b = int(it["first"]), int(it["last"])
        except (KeyError, TypeError, ValueError):
            faults.append(f"story {k} has no usable first/last")
            continue
        if 0 <= a < len(segs) <= b:
            # "it runs to the end", said with a number the model invented. The
            # clamp is what it meant; faulting it bought three identical
            # answers and 25k tokens (N09wqiI_O4w, 2026-09-06). A `first` that
            # is itself out of range still faults below - that one is not a
            # guess at the length, it is an index into nothing.
            log.info("story %d ends at %d, past the %d segments there are - "
                     "clamping it to the last one", k, b, len(segs))
            b = len(segs) - 1
        if not (0 <= a <= b < len(segs)):
            faults.append(f"story {k}: segments {a}-{b} are out of range")
            continue
        if a <= last_end:
            faults.append(f"story {k} starts at {a}, inside the one before it")
            continue
        last_end = b
        sec = segs[b]["end"] - segs[a]["start"]   # before any trim: a few
        #                                              words never cross it
        if sec < MIN_SEC:
            # Dropped, NOT faulted. The prompt tells the model to leave
            # fragments out and it keeps offering them anyway - these videos
            # are full of one-line answers between the stories - so a fault
            # here buys two rewrites of the whole answer and ends where it
            # started. Measured on gQvJPc7eFgY 2026-09-06: three attempts,
            # 25k tokens, a different 48-second fragment each time, and the
            # same story list once this line had dropped it.
            log.info("story %d is %.0fs, under the %ds floor - dropping it",
                     k, sec, MIN_SEC)
            continue
        if sec > STORY_MAX:
            # Dropped for the same reason and in the same way: too long to
            # publish is as unusable as too short to, and a fault would only
            # ask the model to lie about where the story ends.
            log.info("story %d is %.0f min, past the %d-minute ceiling - "
                     "dropping it", k, sec / 60, STORY_MAX // 60)
            continue
        # Cuts are held RELATIVE to the story, because that is the only
        # numbering that survives: the story is stored on its own and its
        # segments are renumbered from zero when it is. A cut outside the
        # story, or on its first segment, is not a cut - it is a bad index,
        # and dropping it quietly leaves one fewer place to break rather than
        # a part of nothing.
        cuts = sorted({int(c) - a for c in it.get("cuts") or []
                       if isinstance(c, (int, float)) and a < int(c) <= b})
        # The channel's announcement, trimmed off the head of the first
        # segment - on a WORD, because that segment holds the end of the
        # announcement and the start of the story in one breath.
        #
        # The model QUOTES the story's first words and this finds them, rather
        # than the model counting the announcement's words and this trusting
        # the count. Counting is the thing it is worst at: asked for a count
        # it answered 5 on one run and 4 on the next, and the 4 left "27" -
        # the tail of "выпуск номер 27" - at the head of the video
        # (N09wqiI_O4w, 2026-09-06). Quoting it got right both times.
        head, cut, words, at = dict(segs[a]), "", segs[a].get("words") or [], 0
        title = str(it.get("title") or "").strip()
        if (starts := str(it.get("starts") or "").strip()) and words:
            want = [_plain(w) for w in starts.split() if _plain(w)]
            said = [_plain(w["w"]) for w in words]
            at = next((i for i in range(1, len(said) - len(want) + 1)
                       if said[i:i + len(want)] == want), 0) if want else 0
            if at and _plain("".join(w["w"] for w in words[:at])) in _plain(title):
                # Not the channel talking: the model quoted from inside the
                # story's own title, which is the line it opens on. Trimming
                # to there takes the hook off the front of the story - four
                # times in 85 stories, "Я мудак, потому что" ahead of "не
                # накормил младшего брата" (q81nGUspiYY, 2026-09-07).
                log.info("story %d: %r is the story's own title, not an "
                         "intro - not trimming", k, starts[:40])
                at = 0
            elif not at:
                # Found at the head already, or not found at all - either way
                # there is nothing to cut on the model's word, and cutting on
                # a guess would take the story's own opening words with it.
                log.info("story %d: nothing to trim before %r", k, starts[:40])
        # Whatever the model said, the hand-over is cut on its own shape - it
        # is what the model misses most often, and it reads the same every time.
        # Four times in five it is a segment of its own, so the whole segment
        # goes; on a single-segment story there is nothing to fall through to.
        at += _intro_words(words[at:]) if a < b else 0
        if at:
            cut = " ".join(w["w"] for w in words[:at])
            head["text"] = " ".join(w["w"] for w in words[at:])
            # An emptied segment is left in place, not dropped: `cuts` are
            # indices into this list and removing one would move every cut in
            # the story. It is given no width and the story starts on the next.
            head["start"] = (words[at]["start"] if at < len(words)
                             else segs[a + 1]["start"])
            if not head["text"]:
                head["end"] = head["start"]
            log.info("story %d: %d word(s) of channel intro trimmed, "
                     "it starts %.1fs in", k, at, head["start"])
        # Applied per SEGMENT and not to the joined text: the segments are
        # what is stored, and a part's body is built back out of them.
        teller = str(it.get("teller") or "").strip().lower()[:1]
        story = [{**x, "text": agreed(x["text"], teller)}
                 for x in (head, *segs[a + 1:b + 1])]
        body = " ".join(x["text"] for x in story if x["text"])

        # The show's name, handed back as the story's TITLE. The model leaks
        # it on the very story it has just told us where to cut the
        # announcement off - twice on N09wqiI_O4w, 2026-09-06, on a prompt
        # that says in as many words that the title is the story's and never
        # the show's. So it is caught here instead of asked for again: the
        # words that were cut are known exactly, and a title made of nothing
        # else is known exactly too. The opening sentence takes its place,
        # which is what the runs that got it right answered anyway - and the
        # user retitles it on the issue either way.
        if cut and _plain(title) and _plain(title) in _plain(cut):
            title = re.split(r"(?<=[.!?])\s", body.strip())[0][:90]
            log.info("story %d was titled %r, which is the channel's own show "
                     "- using its opening line instead", k,
                     str(it.get("title") or "")[:40])

        out.append({"first": a, "last": b,
                    # removeprefix, NOT lstrip: lstrip takes a set of
                    # characters, so "relationship_advice".lstrip("r/") comes
                    # back "elationship_advice" and matches nothing in
                    # SUBREDDITS. Invisible until a sub happened to start
                    # with an r (wEgnl93S-bw, 2026-09-06).
                    "sub": str(it.get("sub") or "").strip().removeprefix("r/"),
                    "title": title,
                    "start": head["start"], "end": segs[b]["end"],
                    "cuts": cuts,
                    # the words have done their job; what is stored is the
                    # time axis the clip is cut on and the text it says
                    "segs": [{"start": x["start"], "end": x["end"],
                              "text": x["text"]} for x in story],
                    "body": body})
    # An empty answer is a legitimate one - a video can be all intro and
    # sponsor read - so it is a result, not a fault. Faults are for answers
    # that claim a story and describe it wrongly.
    return out, faults


def _split(segs: list[dict]) -> list[dict]:
    """One model call: where does each story start and stop."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty - fill in .env")
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=LLM_BASE_URL or None)
    return script._ask(
        client, SPLIT_SYSTEM, _numbered(segs),
        lambda raw: _parse_split(raw, segs),
        keep="Keep the segment numbers you already had right.",
        temperature=0)


def digest(count: int = 1) -> int:
    """Transcribe up to `count` un-read videos and bank the stories in them.

    Most-watched first, which is the reason this module exists: the channel's
    audience has already sat through these, and that is a harder number than
    anything the text can be scored on before it is told.
    """
    stored = 0
    with _db() as db:
        rows = db.execute(
            "SELECT id, chan, title, views FROM yt WHERE done=0 "
            "AND COALESCE(keep, 1)=1 ORDER BY views DESC LIMIT ?",
            (count,)).fetchall()
    if not rows:
        log.info("nothing left to digest - run --harvest")
        return 0

    for vid, chan, vtitle, views in rows:
        log.info("%s (%s, %s views): %s", vid, chan, views, vtitle[:60])
        try:
            segs = _transcribe(_audio(vid))
        except Exception:
            log.exception("%s: could not be read", vid)
            continue
        if not segs:
            log.warning("%s: whisper heard nothing", vid)
            with _db() as db:
                db.execute("UPDATE yt SET done=1 WHERE id=?", (vid,))
            continue

        try:
            stories = _split(segs)
        except script.Unsuitable as e:
            log.info("%s: skipped (%s)", vid, e)
            stories = []
        except Exception:
            log.exception("%s: split failed, leaving it for next run", vid)
            continue

        kept = 0
        with _db() as db:
            for n, s in enumerate(stories):
                # NO safety.blocked() here, by decision 2026-09-06: the
                # channel that recorded this story is moderated by YouTube and
                # has already survived it, so the gate was rejecting stories
                # that a platform has passed.
                #
                # Worth knowing what that trades away, because it is not
                # nothing: safety.py is a TIKTOK gate, and this video ships to
                # TikTok. The categories it exists for - suicid\w* above all -
                # are exactly the ones YouTube tolerates in a narrated story
                # and TikTok actions. If a video is ever taken down for its
                # content, this is the line that was removed.
                # The topic filter, and it is the sub list rather than a second
                # opinion: whatever this channel already publishes is what its
                # audience turned up for. An empty SUBREDDITS means no filter.
                if _DIGEST_SUBS and s["sub"].lower() not in {
                        x.lower() for x in _DIGEST_SUBS}:
                    log.info("%s #%d is r/%s, not in this channel's subs",
                             vid, n, s["sub"] or "?")
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO yt_story(vid, n, sub, title, body,"
                    " start, end, views, ts, segs, cuts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (vid, n, s["sub"], s["title"], s["body"],
                     s["start"], s["end"], views, time.time(),
                     json.dumps(s["segs"], ensure_ascii=False),
                     json.dumps(s["cuts"])))
                kept += 1
            db.execute("UPDATE yt SET done=1 WHERE id=?", (vid,))
        log.info("%s: %d stor%s kept of %d found", vid, kept,
                 "y" if kept == 1 else "ies", len(stories))
        stored += kept
    return stored


def next_story(skip: "set[str] | tuple" = ()) -> dict | None:
    """The best unused story, in the shape the pipeline reads a post in.

    `id` is deliberately the same shape source.py hands over - one string that
    identifies the story for `seen` - so nothing downstream has to know this
    one arrived by ear rather than out of the archive.

    `skip` is for a story the caller cannot take RIGHT NOW but which is not
    spent - a multi-parter on a day with no room for its parts. It stays
    unused and comes back on a day that has the room; passing it here is how
    the caller reaches the story behind it instead of giving up on the whole
    queue over the one at its head.
    """
    with _db() as db:
        rows = db.execute(
            "SELECT vid, n, sub, title, body, start, end, views FROM yt_story "
            "WHERE used=0 ORDER BY views DESC, vid, n").fetchall()
    for vid, n, sub, title, body, start, end, views in rows:
        if (key := f"yt_{vid}_{n}") in skip:
            continue
        return {"id": key, "vid": vid, "n": n, "subreddit": sub,
                "title": title, "selftext": body, "start": start, "end": end,
                "score": views, "num_comments": 0, "source": "upvote"}
    return None


# ------------------------------------------------------------- the recording
#
# Everything above ends at text. This is the half that gives the text its
# voice back: the story is a stretch of somebody's recording, and the render
# gets that stretch instead of a synthesized reading of it.

# yt_<video>_<story>, and _p<part> once the story is queued as several videos.
# The id is the only thing main.py carries into the render - the parts table
# downstream stores text and nothing else - so the clip has to be findable
# from it alone. Non-greedy, and it backtracks: a video id that itself ends in
# _<digits> would otherwise take the story number for its own.
_KEY = re.compile(r"yt_(.+?)_(\d+)(?:_p(\d+))?")

# Whisper's segment edges land on speech, and speech does not stop dead - a
# final consonant and the breath after it fall just outside them. The pad is
# audible as a natural end where a hard cut is audible as a clipped word.
#
# THE TAIL ONLY. It used to lead the head as well, by the same amount, and
# what fell inside those 250 milliseconds was whatever came BEFORE the story:
# on the first story of a video that is the channel's own announcement, so a
# clip whose intro had been carefully trimmed off opened with the tail of it
# anyway - "...двадцать семь" audible before the first word (N09wqiI_O4w,
# 2026-09-06). Whisper puts a word's start at or a shade before its onset, so
# starting exactly on it costs nothing; starting a quarter second early costs
# somebody else's words.
# ponytail: one number, set by ear on ru; widen it for a channel whose
# narrator trails off more than this.
PAD = float(os.getenv("UPVOTE_PAD", 0.25))


def heard(story_id: str) -> bool:
    """True for a story that arrived by ear, i.e. one this module harvested."""
    return bool(_KEY.fullmatch(story_id or ""))


def source_url(story_id: str) -> str:
    """The recording this story was cut from, at the second it starts. "" if none.

    For the issue's header, which otherwise offers a redd.it link built from an
    id that was never a reddit post - "https://redd.it/yt_wEgnl93S-bw_0", a
    dead link on every harvested story since the first one (2026-09-06). The
    timestamp is the point: the answer to "is this really one story" is thirty
    seconds of listening, and this is what makes those thirty seconds one click.
    """
    if not (m := _KEY.fullmatch(story_id or "")):
        return ""
    with _db() as db:
        row = db.execute("SELECT start FROM yt_story WHERE vid=? AND n=?",
                         (m.group(1), int(m.group(2)))).fetchone()
    return f"https://youtu.be/{m.group(1)}?t={int(row[0])}" if row else ""


def _clip(vid: str, start: float, end: float, dest: Path) -> Path:
    """One stretch of a source recording, as its own mp3.

    The digest and the render are different runs and, on CI, different
    machines - so the audio is not assumed to be lying around from the
    transcription. It is fetched again if it is gone, which costs one download
    and is the difference between a story that renders and one that cannot.
    """
    if dest.exists():
        return dest
    src = OUT_DIR / f"yt_{vid}.mp3"
    if not src.exists():
        log.info("%s: the source audio is gone, fetching it again", vid)
        src = _audio(vid)
    # -ss AFTER -i: seeking on the input lands on a frame boundary, which is a
    # syllable of somebody else's sentence at the head of ours. Decoding a few
    # minutes of mp3 to cut it exactly costs nothing.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ss", f"{max(0.0, start):.2f}", "-to", f"{end + PAD:.2f}",
         str(dest)], check=True)
    return dest


def _row(story_id: str):
    """(vid, n, row) for a harvested story, or None if it is not one."""
    m = _KEY.fullmatch(story_id or "")
    if not m:
        return None
    vid, n = m.group(1), int(m.group(2))
    with _db() as db:
        row = db.execute(
            "SELECT title, segs, cuts, parts FROM yt_story WHERE vid=? AND n=?",
            (vid, n)).fetchone()
    return (vid, n, row) if row else None


def want_parts(story: dict, cap: int = 0) -> int:
    """Videos this story is worth, by how long its recording PLAYS for.

    `cap` overrides PART_MAX, and there is exactly one caller for it:
    main._park_one asking whether a story it cannot split today would fit in
    ONE video at PART_CEILING - the platform's limit, where PART_MAX is only
    this channel's preference.

    Plays, not runs: the track is sped up on its way out (config.VOICE_SPEEDUP,
    1.25 on the English channel), so a six-minute recording is a five-minute
    video and splitting it against the raw length buys a part nobody needed.

    NOT capped at PARTS, and that is the point of it. A written story that
    wants six parts is told in five, because the model writes it shorter; a
    RECORDING that wants six parts is as long as it is, whatever anybody
    wants, and told in five it is five videos past PART_MAX. So the true count
    comes back and the caller refuses the story instead of squeezing it.
    """
    played = (story["end"] - story["start"]) / VOICE_SPEEDUP
    return max(1, math.ceil(played / (cap or PART_MAX)))


def _bounds(segs: list[dict], cuts: list[int], n: int) -> "list[tuple] | None":
    """`n` segment ranges split on the model's cuts, or None if they will not.

    The cuts are where the story turns; which of them to use is arithmetic -
    the one nearest each even division, so three parts of one length beats two
    of four minutes and one of forty seconds. None when the result would put a
    part under the floor, and the caller then asks for one part fewer: a story
    with its turns bunched at the front is a story told in two videos, not
    three, and that beats a third video eight seconds long.

    EVEN IN TIME, not in segment count, and the difference is not academic.
    Whisper cuts a segment on speech, so their lengths run from a word to a
    sentence, and a story's segments are nowhere near uniform: on
    N09wqiI_O4w story 1 the cut at segment 403 of 866 - 47% of the way
    through the list - is 620 seconds into 1008, 62% of the way through the
    tape. Divided by index that story came out 620s + 287s + 100s, which is
    exactly the "two of four minutes and one of forty seconds" this paragraph
    promises not to do; its first part ran 10.3 minutes, past the ten a video
    may be, and main.park_heard burned the whole story over it (2026-09-06).
    """
    if n <= 1:
        return [(0, len(segs) - 1)]
    span = segs[-1]["end"] - segs[0]["start"]
    picked: list[int] = []
    for k in range(1, n):
        free = [c for c in cuts if c not in picked]
        if not free:
            return None
        want = segs[0]["start"] + k * span / n
        picked.append(min(free, key=lambda c: abs(segs[c]["start"] - want)))
    edges = [0, *sorted(picked), len(segs)]
    out = [(a, b - 1) for a, b in zip(edges, edges[1:])]
    if any(a > b or segs[b]["end"] - segs[a]["start"] < MIN_SEC
           for a, b in out):
        return None
    return out


# What the end of a sentence looks like at the end of a whisper segment. The
# closing quote and bracket are there because Russian punctuation puts them
# AFTER the full stop, and a segment ending «...сказала она.» is a sentence end
# by every reading except a naive endswith(".").
_SENTENCE_END = re.compile(r'[.!?…]["»)\]]*\s*$')


def _sentence_cuts(segs: list[dict]) -> list[int]:
    """Every segment index that begins a new SENTENCE, as fallback cut points.

    The model's turns are the good cuts and these are the safe ones: a story
    whose turns are all bunched at the front used to be told in one video of
    everything, or dropped for running past PART_CEILING. Cutting mid-scene is
    worse than cutting on a turn and better than not publishing - and _bounds()
    picks the one nearest the even division either way, so the fallback lands
    on the sentence closest to the middle rather than just anywhere.

    Index `c` is where a part STARTS, so it is the segment after the one that
    ended the sentence - which is what _bounds() means by a cut.
    """
    return [i for i in range(1, len(segs))
            if _SENTENCE_END.search(segs[i - 1]["text"] or "")]


def split_parts(story_id: str, n: int = 1) -> list:
    """The story as up to `n` videos: [(title, body), ...], verbatim.

    The audio range of each part is written onto the row here, and that is the
    load-bearing half - narration() reads it back at the render, in another
    run, with nothing to go on but the part's id. One title for every part,
    which is what the rest of the pipeline already assumes: it is the cover
    drawn at the head of each one, not a line of the narration.
    """
    got = _row(story_id)
    if not got:
        raise ValueError(f"not a harvested story: {story_id!r}")
    vid, k, (title, segs, cuts, _) = got
    segs, cuts = json.loads(segs or "[]"), json.loads(cuts or "[]")
    if not segs:
        raise ValueError(f"{story_id} has no segments to cut on")

    for want in range(max(1, n), 0, -1):
        if bounds := _bounds(segs, cuts, want):
            break
        # No turn of the model's leaves parts of a usable length at this
        # count. Fall back to sentence ends before giving up a part: a cut
        # mid-scene costs less than the video that is never made.
        if bounds := _bounds(segs, _sentence_cuts(segs), want):
            log.info("%s: no usable turn for %d parts - cutting on the "
                     "sentence nearest each division instead", story_id, want)
            break
    if want < n:
        log.info("%s: asked for %d parts, the story breaks into %d",
                 story_id, n, want)

    parts = [{"start": segs[a]["start"], "end": segs[b]["end"],
              "body": " ".join(x["text"] for x in segs[a:b + 1] if x["text"])}
             for a, b in bounds]
    with _db() as db:
        db.execute("UPDATE yt_story SET parts=? WHERE vid=? AND n=?",
                   (json.dumps(parts, ensure_ascii=False), vid, k))
    return [(title, p["body"]) for p in parts]


def too_long(story_id: str) -> str:
    """Empty when every recorded part fits in a video, otherwise which does not.

    Parts are cut where the story turns and not where a stopwatch says, so an
    uneven set of turns can hand one of them far more than its share even when
    the arithmetic said the story would fit. This measures what was actually
    recorded rather than the count that was asked for.
    """
    got = _row(story_id)
    if not got or not got[2][3]:
        return ""
    for i, p in enumerate(json.loads(got[2][3]), 1):
        if (sec := (p["end"] - p["start"]) / VOICE_SPEEDUP) > PART_CEILING:
            return (f"part {i} runs {sec / 60:.1f} min, past the "
                    f"{PART_CEILING / 60:.0f} a video may be")
    return ""


def narration(key: str) -> "Path | None":
    """The recording behind this render, cut to length. None if there is none.

    `key` is what main.py names the file after - the story id, or the story id
    with _p<n> on the end for one part of it. None for every ordinary story,
    which is what lets the render ask on every video without knowing where the
    story came from.
    """
    m = _KEY.fullmatch(key or "")
    got = _row(key) if m else None
    if not got:
        return None
    if not got[2][3]:
        # Loud, because the fallback is otherwise silent: the video renders in
        # a TTS voice under subtitles taken off the tape, and nothing in the
        # log says the recording went unused. confirm() drops the ranges on
        # purpose and says so on its way out; anything else that lands here is
        # a story parked without split_parts(), and this is where it surfaces.
        # 48 of them did on 2026-09-07, one already published in Fish's voice.
        log.warning("%s: harvested, but the row carries no recorded split - "
                    "reading it aloud instead of using the tape", key)
        return None
    parts = json.loads(got[2][3])
    n = int(m.group(3) or 1)
    if not 1 <= n <= len(parts):
        # Reachable only if the story was re-split after it was parked, which
        # confirm() is there to prevent. Reading the wrong part aloud is worse
        # than reading it fresh, so this refuses rather than guesses.
        log.warning("%s: part %d of a story recorded in %d - narrating fresh",
                    key, n, len(parts))
        return None
    p = parts[n - 1]
    return _clip(got[0], p["start"], p["end"],
                 OUT_DIR / f"{chan_file(key)}_heard.mp3")


def confirm(story_id: str, written: list) -> None:
    """Keep the recording only while the text still says what the tape says.

    A narration rewritten by hand on the issue - or split into a different
    number of parts there - is a different set of words, and the clip would
    then say one thing under subtitles saying another. Dropping the ranges
    sends the story down the ordinary path instead: voice.py reads the rewrite
    aloud, which is a worse video than the tape and a far better one than a
    video whose audio and captions have parted company.
    """
    got = _row(story_id)
    if not got or not got[2][3]:
        return
    recorded = [p["body"] for p in json.loads(got[2][3])]
    if [b for _, b in written] == recorded:
        return
    log.warning("%s: the narration was rewritten - the recording no longer "
                "matches it, so the story is read aloud instead", story_id)
    with _db() as db:
        db.execute("UPDATE yt_story SET parts=NULL WHERE vid=? AND n=?",
                   (got[0], got[1]))


def mark_used(story_id: str) -> None:
    """Call only after a successful render, same contract as source.mark_used."""
    m = re.fullmatch(r"yt_(.+)_(\d+)", story_id)
    if not m:
        raise ValueError(f"not an upvote story id: {story_id!r}")
    with _db() as db:
        db.execute("UPDATE yt_story SET used=1 WHERE vid=? AND n=?",
                   (m.group(1), int(m.group(2))))


def show() -> None:
    with _db() as db:
        vids, done = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(done),0) FROM yt").fetchone()
        skip, unjudged = db.execute(
            "SELECT SUM(keep=0), SUM(keep IS NULL) FROM yt").fetchone()
        left, used = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(used),0) FROM yt_story").fetchone()
        print(f"videos: {vids} listed, {done} read, {skip or 0} not readings"
              + (f", {unjudged} unjudged" if unjudged else ""))
        print(f"stories: {left} found, {used} used, {left - used} waiting")
        for r in db.execute(
                "SELECT title, sub, views, ROUND(end-start) FROM yt_story "
                "WHERE used=0 ORDER BY views DESC LIMIT 10"):
            print(f"  {r[2]:>9} r/{r[1]:<20} {r[3]:>4.0f}s  {r[0][:50]}")


def cache_audio() -> int:
    """Every queued story's recording, local AND in the release.

    Run it wherever the download works. The renderer runs somewhere else and
    only ever reads the release, so a story whose audio never got here is a
    story that cannot be made - which is exactly what happened to the 13
    banked on 2026-09-06, digested on the desk and unreachable from CI.
    """
    # NOT just the queued ones. A story is marked used the moment it is
    # PARKED, and its recording is wanted hours later at the render - so
    # `used=0` alone skips exactly the story being made right now. What is
    # still needed is: queued, plus anything sitting in `review`, plus any
    # part not yet published. Lang is deliberately ignored - a recording
    # wanted by any channel is a recording that stays.
    with _db() as db:
        vids = [r[0] for r in db.execute(
            "SELECT DISTINCT vid FROM yt_story WHERE used=0 "
            "UNION SELECT vid FROM yt_story WHERE 'yt_'||vid||'_'||n IN ("
            "  SELECT post_id FROM review"
            "  UNION SELECT post_id FROM parts WHERE done=0) ORDER BY vid")]
    have = _release_names()
    for vid in vids:
        got = _audio(vid)
        if got.name not in have:
            _release_put(got)
    return len(vids)


# ------------------------------------------------------------- shared state
#
# The stories live in seen.db and the recording lives in the release, and only
# the second one travels on its own. A desk that digests and does not push the
# database has banked stories CI will never see - which is why this is here
# rather than in the .cmd: the push is part of the harvest, not a courtesy
# after it.
#
# The merge is mechanical because the tables have one writer each. `yt` and
# `yt_story` are written by the harvest and by nothing else; `review`,
# `parts`, `tiktok` and the rest are written by the publishing side and by
# nothing else. So the safe move is never "mine or theirs" over the whole
# file - it is: take THEIR file, put MY two tables into it. A binary rebase
# cannot do that, which is why an ordinary `git pull --rebase` on this file
# conflicts every time (four times over on 2026-09-06, by hand).


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _desk_rows() -> dict:
    """Everything this machine knows that CI cannot work out for itself.

    The harvest, and - since 2026-09-07 - what the desk has PUBLISHED. Those
    two belong together because they fail the same way: a row written here and
    never carried over is a job CI does again. The harvest half only wasted the
    transcription; the publish half wasted eleven renders of one video in four
    hours, because finish_part() marks a part on whichever machine sent it, and
    on this setup that is never the machine that built it. See the parts note
    in _apply_desk().

    Keyed rather than positional: this grew from two tables to four, and the
    same reasoning the tiktok INSERT uses applies here - a fifth entry must not
    silently land where the fourth was read.

    CLOSED explicitly, and that is the point of the try/finally rather than a
    bare `with`: sqlite3's context manager ends the TRANSACTION and leaves the
    handle open, and on Windows an open handle is enough to make the caller's
    `git checkout` of this very file fail with "unable to unlink old". See
    push_state() for what that silently cost.
    """
    cols = lambda t: [c[1] for c in db.execute(f"pragma table_info({t})")]
    db = _db()
    try:
        with db:
            return {
                "yt": (cols("yt"), db.execute("SELECT * FROM yt").fetchall()),
                "yt_story": (cols("yt_story"),
                             db.execute("SELECT * FROM yt_story").fetchall()),
                # done only. A part this desk has NOT sent says nothing about
                # one CI may have sent since, and clearing it here on the
                # strength of a stale copy would lose the story's middle.
                "parts_done": db.execute(
                    "SELECT post_id, n, lang FROM parts WHERE done=1").fetchall(),
                "tiktok": (cols("tiktok"),
                           db.execute("SELECT * FROM tiktok").fetchall()),
            }
    finally:
        db.close()


def _apply_desk(state: dict) -> int:
    """Put those rows into whatever seen.db is on disk now. New rows only.

    `used` is deliberately not overwritten on a story that is already there:
    the publishing side owns it, and this machine's copy of it is older than
    whatever CI has just done with the story.

    The parts and tiktok halves are one-way in the same spirit. A part goes
    done and never comes back, and a tiktok row is inserted only where CI has
    none - both machines write that table and neither's copy is a truth about
    the other's sends.

    CLOSED explicitly, for _desk_rows()'s reason and one more: push_state()
    retries, and the second try opens with `git checkout -- seen.db` - which
    on Windows cannot replace a file this function still holds open. Caught by
    the self-test below, having been missed here when _desk_rows() was fixed.
    """
    yt_cols, yt_rows = state["yt"]
    st_cols, st_rows = state["yt_story"]
    db = _db()
    try:
        with db:
            have_yt = {r[0] for r in db.execute("SELECT id FROM yt")}
            fresh = [r for r in yt_rows if r[0] not in have_yt]
            db.executemany(f"INSERT INTO yt({','.join(yt_cols)}) VALUES "
                           f"({','.join('?' * len(yt_cols))})", fresh)
            # a video this machine has judged or digested, judged or digested here
            db.executemany("UPDATE yt SET done=max(done, ?), keep=coalesce(?, keep)"
                           " WHERE id=?",
                           [(r[yt_cols.index("done")], r[yt_cols.index("keep")], r[0])
                            for r in yt_rows if r[0] in have_yt])
            have_st = {(r[0], r[1])
                       for r in db.execute("SELECT vid, n FROM yt_story")}
            new = [r for r in st_rows if (r[0], r[1]) not in have_st]
            db.executemany(f"INSERT INTO yt_story({','.join(st_cols)}) VALUES "
                           f"({','.join('?' * len(st_cols))})", new)

            # A part this desk has sent. Without this line CI keeps handing the
            # same part to next_part() every tick, rebuilds it, uploads it as
            # another artifact, and nothing ever clears it - measured
            # 2026-09-07, eleven identical renders of yt_N09wqiI_O4w_1_p1
            # between 22:54 and 02:46, for a video published at 16:15 the day
            # before. max() rather than a plain 1, so a part CI cleared first
            # is not un-cleared by this desk's staler row.
            db.executemany("UPDATE parts SET done=max(done, 1) "
                           "WHERE post_id=? AND n=? AND lang=?",
                           state["parts_done"])
            # OR IGNORE, not OR REPLACE: both machines write this table and
            # CI's row for a file is the one its own send made.
            tk_cols, tk_rows = state["tiktok"]
            db.executemany(f"INSERT OR IGNORE INTO tiktok({','.join(tk_cols)}) "
                           f"VALUES ({','.join('?' * len(tk_cols))})", tk_rows)
    finally:
        db.close()
    return len(new)


def push_state(tries: int = 3) -> bool:
    """Commit what this machine did onto whatever CI has committed since.

    The harvest AND the sends - see _desk_rows() for why the two travel
    together. publish.py calls this after every send for the second half; the
    daily harvest calls it for the first.

    Retried, because losing the race is the ORDINARY outcome and not a fault:
    publish.yml runs twice an hour and commits seen.db most times it does, and
    a digest takes twenty minutes. Each try re-reads their file and puts the
    same rows into it, so a try costs nothing but the round trip.
    """
    for attempt in range(1, tries + 1):
        state = _desk_rows()
        # this desk's drift is a cache - CHECKED, because when this fails the
        # run does the opposite of its job. git cannot replace a file another
        # process holds open, and on Windows it says so with "unable to unlink
        # old" and exit 255; unchecked, the desk then pushed its whole stale
        # seen.db - its `review`, `parts`, `tiktok` and `uploaded` over CI's
        # newer ones - and _apply_desk reported 0 new rows because it was
        # looking at our own file. Silent on 2026-09-07, harmless only because
        # CI happened to have committed nothing in the hour.
        if (r := _git("checkout", "--", str(DB_PATH))).returncode:
            log.error("cannot take CI's %s back, something holds it open: %s",
                      DB_PATH.name, r.stderr.strip()[:200])
            return False
        if (r := _git("fetch", "-q", "origin")).returncode:
            log.error("fetch failed: %s", r.stderr.strip()[:200])
            return False
        if (r := _git("merge", "--ff-only", "origin/main")).returncode:
            log.error("cannot fast-forward to origin/main - this checkout has "
                      "commits of its own: %s", r.stderr.strip()[:200])
            return False
        n = _apply_desk(state)
        _git("add", str(DB_PATH))
        if _git("diff", "--cached", "--quiet", str(DB_PATH)).returncode == 0:
            log.info("nothing new to push")
            return True
        _git("commit", "-q", "-m",
             f"state: harvest YouTube stories ({OUTPUT_LANG}) [skip ci]")
        if _git("push").returncode == 0:
            log.info("pushed %d new story row(s) and this desk's sends on try %d",
                     n, attempt)
            return True
        log.info("push refused - CI committed first, rebuilding onto theirs")
        # --mixed and never --hard: this runs unattended in a working copy
        # that may well have edits in it, and undoing OUR commit is no reason
        # to throw those away. seen.db is put back by the checkout at the top
        # of the next try, and the rows go in again from `state`.
        _git("reset", "-q", "--mixed", "HEAD~1")
    log.error("still unpushed after %d tries - run it again or merge by hand",
              tries)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harvest", nargs="?", type=int, const=60, default=None,
                    help="list each channel's latest N videos")
    ap.add_argument("--digest", nargs="?", type=int, const=1, default=None,
                    help="transcribe N videos and split them into stories")
    ap.add_argument("--judge", action="store_true",
                    help="judge the titles harvested but not yet judged")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--push-state", action="store_true",
                    help="commit this machine's harvest onto origin and push")
    ap.add_argument("--cache-audio", action="store_true",
                    help="download missing MP3s for queued stories")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        # The parser is the only non-trivial logic here: every field it reads
        # is an index into a list it did not build, and a wrong one slices
        # silently. So it is checked, and the network is not.
        segs = [{"i": i, "start": i * 10.0, "end": i * 10.0 + 10.0,
                 "text": f"line {i}"} for i in range(20)]
        ok, faults = _parse_split(
            '[{"first":0,"last":9,"sub":"r/tifu","title":"T"},'
            ' {"first":10,"last":19,"sub":"AmItheAsshole","title":"U"}]', segs)
        assert not faults, faults
        assert len(ok) == 2, ok
        assert ok[0]["sub"] == "tifu", ok[0]           # the r/ comes off
        assert ok[0]["start"] == 0.0 and ok[0]["end"] == 100.0, ok[0]
        assert ok[0]["body"].startswith("line 0 line 1"), ok[0]["body"]

        # overlap: the second story starts inside the first
        _, f = _parse_split('[{"first":0,"last":9},{"first":5,"last":19}]', segs)
        assert any("inside the one before" in x for x in f), f
        # An ending past the transcript is the model guessing at how long the
        # video is - it means "to the end", and is clamped there rather than
        # faulted, because faulting it returned the same invented number three
        # times over.
        ok3, f = _parse_split('[{"first":0,"last":99}]', segs)
        assert not f and len(ok3) == 1, (ok3, f)
        assert ok3[0]["end"] == 200.0, ok3[0]        # the last segment's end
        # A `first` past the end is not that: nothing was meant by it, and
        # there is no story to clamp.
        _, f = _parse_split('[{"first":99,"last":120}]', segs)
        assert any("out of range" in x for x in f), f
        # backwards
        _, f = _parse_split('[{"first":9,"last":2}]', segs)
        assert any("out of range" in x for x in f), f
        # under the floor - two segments is 20s, MIN_SEC is above that. A
        # fragment is dropped and is NOT a fault: faulting it rewrites the
        # whole answer for a line the model was always going to include.
        ok2, f = _parse_split('[{"first":0,"last":1}]', segs)
        assert ok2 == [] and not f, (ok2, f)
        # an empty answer is a result, not a fault
        # ...and the ceiling above it drops a story the same way. 20 segments
        # of 10s is 200s, so the fixture is measured against a low ceiling.
        _real_max = STORY_MAX
        try:
            globals()["STORY_MAX"] = 100
            okc, f = _parse_split('[{"first":0,"last":19,"sub":"x","title":"T"}]',
                                  segs)
            assert okc == [] and not f, (okc, f)
            globals()["STORY_MAX"] = 300
            okc, f = _parse_split('[{"first":0,"last":19,"sub":"x","title":"T"}]',
                                  segs)
            assert len(okc) == 1 and not f, (okc, f)
        finally:
            globals()["STORY_MAX"] = _real_max
        assert _parse_split("[]", segs) == ([], [])
        assert _parse_split("not json at all", segs)[1], "garbage must fault"

        # The title filter names what it drops in words, and the words are
        # what is believed: a number that has drifted down a long list would
        # otherwise throw away a story nobody knows was there.
        _titles = ["Какой секрет разрушит вашу жизнь?",
                   "Училка-тиран терроризировала школу (Ядерная месть)",
                   "What Simple Job Would Humble Most People?"]
        assert _parse_pick('[{"n":0,"t":"Какой секрет разрушит"}]',
                           _titles) == ({0}, [])
        assert _parse_pick("[]", _titles) == (set(), [])
        # the number is wrong and the words are right - the words win
        assert _parse_pick('[{"n":2,"t":"Какой секрет"}]',
                           _titles)[0] == {0}
        # words that match nothing drop nothing, and say so
        out, f = _parse_pick('[{"n":1,"t":"это другое видео"}]', _titles)
        assert out == set() and f, (out, f)
        out, f = _parse_pick('[{"n":1}]', _titles)
        assert out == set() and any("by number only" in x for x in f), f
        assert _parse_pick("nothing here", _titles)[1], "garbage must fault"

        # ids survive the round trip, which is what mark_used depends on
        assert re.fullmatch(r"yt_(.+)_(\d+)", "yt_dQw4w9WgXcQ_3").group(2) == "3"

        # The channel's announcement shares a segment with the story's first
        # line, and the story has to start on the WORD after it - dropping the
        # whole segment costs the opening line, which is the one line the
        # video cannot afford to lose.
        spoken = [{"i": 0, "start": 0.0, "end": 8.0,
                   "text": "Шоу выпуск 27 На работе закрыли глаза",
                   "words": [{"w": w, "start": i * 1.0, "end": i * 1.0 + 1.0}
                             for i, w in enumerate(
                                 "Шоу выпуск 27 На работе закрыли глаза".split())]},
                  *[{"i": i, "start": i * 10.0, "end": i * 10.0 + 10.0,
                     "text": f"line {i}", "words": []} for i in range(1, 20)]]
        ok4, f = _parse_split(
            '[{"first":0,"last":19,"starts":"На работе закрыли",'
            ' "sub":"x","title":"T"}]', spoken)
        assert not f, f
        assert ok4[0]["start"] == 3.0, ok4[0]["start"]      # the 4th word's start
        assert ok4[0]["body"].startswith("На работе закрыли глаза line 1"), \
            ok4[0]["body"][:60]
        assert "выпуск" not in ok4[0]["body"], ok4[0]["body"][:60]
        assert "words" not in ok4[0]["segs"][0], "the words are not stored"
        # a title made of the words that were just cut is the show's name,
        # not the story's - the opening line replaces it
        ok6, f = _parse_split(
            '[{"first":0,"last":19,"starts":"На работе закрыли",'
            ' "sub":"x","title":"Шоу выпуск"}]', spoken)
        assert not f and ok6[0]["title"].startswith("На работе закрыли"), ok6[0]
        # ...and an ordinary title is left alone
        ok7, _ = _parse_split(
            '[{"first":0,"last":19,"starts":"На работе закрыли",'
            ' "sub":"x","title":"Обычный заголовок"}]', spoken)
        assert ok7[0]["title"] == "Обычный заголовок", ok7[0]
        # words that are not in the segment keep it whole rather than eating
        # the story on a guess
        ok5, f = _parse_split(
            '[{"first":0,"last":19,"starts":"это сказано не здесь",'
            ' "sub":"x","title":"T"}]', spoken)
        assert not f and ok5[0]["start"] == 0.0, ok5[0]
        assert ok5[0]["body"].startswith("Шоу выпуск"), ok5[0]["body"][:40]

        # The hand-over between stories, which the model leaves in as often as
        # not - cut on its own shape, with the story's start moved off it so
        # the tape does not say it either.
        def _spoken(first: str) -> list:
            return [{"i": 0, "start": 0.0, "end": float(len(first.split())),
                     "text": first,
                     "words": [{"w": w, "start": i * 1.0, "end": i * 1.0 + 1.0}
                               for i, w in enumerate(first.split())]},
                    *[{"i": i, "start": 100.0 + i, "end": 101.0 + i,
                       "text": f"line {i}", "words": []} for i in range(1, 20)]]

        for said, kept, at in [
                ("И переходим к следующей истории. Жена захотела", "Жена", 5),
                ("Следующая история, решил выяснять", "решил", 2),
                ("И последняя история на сегодня. Девушка шутила", "Девушка", 5),
                ("И последняя на сегодня история. Хотите знать", "Хотите", 5),
                ("к следующей истории. Жена захотела", "Жена", 3)]:
            okh, f = _parse_split(
                '[{"first":0,"last":19,"sub":"x","title":"T"}]', _spoken(said))
            assert not f, (said, f)
            assert okh[0]["body"].startswith(kept), (said, okh[0]["body"][:40])
            assert okh[0]["start"] == float(at), (said, okh[0]["start"])
        # Four hand-overs in five are a whole segment of their own. The
        # segment stays in the list, empty, so the cuts still point where they
        # did - and the story starts on the one after it.
        alone = _spoken("И переходим к следующей истории.")
        ok9, f = _parse_split(
            '[{"first":0,"last":19,"sub":"x","title":"T"}]', alone)
        assert not f, f
        assert ok9[0]["body"].startswith("line 1"), ok9[0]["body"][:40]
        assert ok9[0]["start"] == 101.0, ok9[0]["start"]     # segment 1's start
        assert ok9[0]["segs"][0]["text"] == "", ok9[0]["segs"][0]
        assert len(ok9[0]["segs"]) == 20, len(ok9[0]["segs"])
        # ...and a story that opens on a word from that vocabulary is not one
        first = _spoken("Первая работа и моя итальянская забастовка")
        okh, _ = _parse_split('[{"first":0,"last":19,"sub":"x","title":"T"}]',
                              first)
        assert okh[0]["body"].startswith("Первая работа"), okh[0]["body"][:40]
        assert okh[0]["start"] == 0.0, okh[0]

        # The other way the head is lost: the model quotes `starts` from
        # inside the story's own title, and the trim takes the hook with it.
        aita = _spoken("Я мудак потому что не накормил младшего брата Мама")
        ok8, f = _parse_split(
            '[{"first":0,"last":19,"sub":"x","starts":"не накормил младшего",'
            ' "title":"Я мудак, потому что не накормил младшего брата"}]', aita)
        assert not f and ok8[0]["start"] == 0.0, ok8[0]
        assert ok8[0]["body"].startswith("Я мудак потому что"), ok8[0]["body"][:40]

        # the storyteller's own verbs, put into the gender the model read off
        # the story - whisper hears the ending as a coin toss
        assert agreed("Тогда я решил уйти.", "f") == "Тогда я решила уйти."
        assert agreed("Тогда я решил уйти.", "m") == "Тогда я решил уйти."
        assert agreed("Тогда я решил уйти.", "") == "Тогда я решил уйти."
        # a capital survives, and so does everything that is not that verb
        assert agreed("Я пошел домой, он пошел следом.", "f") \
            == "Я пошла домой, он пошел следом.", agreed("Я пошел домой, он пошел следом.", "f")
        # the word count never changes - the audio still says what is written
        for t in ("Я знал, что она ушла.", "Мне пришлось ждать.", "Я работаю."):
            assert len(agreed(t, "f").split()) == len(t.split()), t

        # cuts are held relative to the story, and only the ones inside it
        ok, f = _parse_split(
            '[{"first":10,"last":19,"cuts":[10,14,19,40],"sub":"x","title":"T"}]',
            segs)
        assert not f and ok[0]["cuts"] == [4, 9], ok
        assert len(ok[0]["segs"]) == 10, ok[0]["segs"]

        # ...and the parts are cut ON them, nearest to an even division
        assert _bounds(segs, [10], 2) == [(0, 9), (10, 19)]
        assert _bounds(segs, [4, 10, 16], 2) == [(0, 9), (10, 19)]
        # a part under the floor is not a part - and neither is one with
        # nowhere to cut it. Both answer None, and split_parts() then asks
        # for one part fewer rather than shipping forty seconds of video.
        assert _bounds(segs, [3], 2) is None
        assert _bounds(segs, [], 2) is None
        assert _bounds(segs, [4, 10, 16], 3) is None
        # Even in TIME. These segments are not uniform - five of 100s then
        # five of 20s - so the halfway SEGMENT (5) and the halfway SECOND
        # (300s, segment 3) are different cuts, and only one of them splits
        # the story in two. Dividing by index picks 500s + 100s.
        _uneven = [{"i": i, "start": s0, "end": s0 + d, "text": f"line {i}"}
                   for i, (s0, d) in enumerate(
                       [(i * 100.0, 100.0) for i in range(5)]
                       + [(500.0 + i * 20.0, 20.0) for i in range(5)])]
        assert _bounds(_uneven, [3, 5], 2) == [(0, 2), (3, 9)]
        assert _uneven[2]["end"] - _uneven[0]["start"] == 300.0
        assert _uneven[9]["end"] - _uneven[3]["start"] == 300.0
        # ...and where the model marked no usable turn, a sentence end serves.
        # Every third segment closes one, so the cuts sit at 1, 4, 7...
        _talk = [{**x, "text": x["text"] + ("." if x["i"] % 3 == 0 else "")}
                 for x in segs]
        assert _sentence_cuts(_talk) == [1, 4, 7, 10, 13, 16, 19]
        assert _bounds(_talk, _sentence_cuts(_talk), 2) == [(0, 9), (10, 19)]
        # the closing quote comes AFTER the full stop in Russian
        assert _sentence_cuts([{"text": '- Уходи, - сказала она."'},
                               {"text": "x"}]) == [1]
        assert _sentence_cuts([{"text": "и тогда"}, {"text": "x"}]) == []

        # The half that has to survive a process boundary: which stretch of
        # which recording a part is, found again from the part's key alone.
        import tempfile
        globals()["DB_PATH"] = Path(tempfile.mkdtemp()) / "t.db"
        globals()["_clip"] = lambda vid, a, b, dest: (vid, a, b)
        with _db() as db:
            db.execute(
                "INSERT INTO yt_story(vid, n, sub, title, body, start, end,"
                " views, ts, segs, cuts) VALUES ('v1',0,'tifu','T','b',"
                "0,200,1,0,?,?)", (json.dumps(segs), json.dumps([10])))
        w = split_parts("yt_v1_0", 2)
        assert [t for t, _ in w] == ["T", "T"], w        # one title, both parts
        assert w[0][1].startswith("line 0") and w[1][1].startswith("line 10"), w
        assert narration("yt_v1_0_p1") == ("v1", 0.0, 100.0)
        assert narration("yt_v1_0_p2") == ("v1", 100.0, 200.0)
        assert narration("1n0abcd") is None, "an ordinary story has no tape"
        assert narration("yt_v1_0_p9") is None, "a part that was never recorded"
        assert heard("yt_v1_0") and not heard("1n0abcd_p2")
        # a video id that itself ends in _<digits> keeps its own number
        assert _KEY.fullmatch("yt_abcdefgh_12_3").group(1) == "abcdefgh_12"

        # the tape is kept while the text is the tape's, and dropped otherwise
        confirm("yt_v1_0", w)
        assert narration("yt_v1_0_p1"), "an unchanged narration keeps its clip"
        confirm("yt_v1_0", [("T", "something somebody else said")])
        assert narration("yt_v1_0_p1") is None, "a rewrite drops the clip"

        # one part, one range, and the story told whole
        assert split_parts("yt_v1_0", 1) == [("T", " ".join(
            f"line {i}" for i in range(20)))]
        assert narration("yt_v1_0") == ("v1", 0.0, 200.0)
        # the issue's header links the tape, not a reddit post that never was
        assert source_url("yt_v1_0") == "https://youtu.be/v1?t=0"
        assert source_url("1n0abcd") == "", "an ordinary story has no tape"
        assert source_url("yt_nosuch_0") == "", "nor has one that is not banked"

        # A story the caller has to defer must not hide the queue behind it -
        # main._park_one skips a multi-parter on a day with no room for it and
        # asks again, so a `skip` that was ignored would hand back the same row
        # for ever and the loop would never reach the story that DOES fit.
        with _db() as db:
            db.execute("INSERT INTO yt_story(vid, n, sub, title, body, start,"
                       " end, views, ts) VALUES ('v2',0,'x','T2','b',0,60,9,0)")
        assert next_story()["id"] == "yt_v2_0", "most views leads"
        assert next_story({"yt_v2_0"})["id"] == "yt_v1_0", "the next one behind it"
        assert next_story({"yt_v2_0", "yt_v1_0"}) is None, "and then nothing"

        # ...and the other half of that branch: PART_MAX is the length this
        # channel PREFERS, PART_CEILING is the one the platform enforces, so a
        # recording between them is one long video rather than nothing.
        _mid = {"start": 0.0, "end": (PART_MAX + PART_CEILING) / 2 * VOICE_SPEEDUP}
        assert want_parts(_mid) == 2, "over PART_MAX it wants splitting"
        assert want_parts(_mid, PART_CEILING) == 1, "and it still fits one video"
        _long = {"start": 0.0, "end": (PART_CEILING + 60) * VOICE_SPEEDUP}
        assert want_parts(_long, PART_CEILING) == 2, "past the ceiling it does not"

        # The whole fallback through split_parts: a story the model marked
        # nowhere to cut still comes back as the two videos that were asked
        # for, rather than as one long one.
        with _db() as db:
            db.execute(
                "INSERT INTO yt_story(vid, n, sub, title, body, start, end,"
                " views, ts, segs, cuts) VALUES ('v3',0,'x','T3','b',0,200,1,"
                "0,?,'[]')", (json.dumps(_talk),))
        _w = split_parts("yt_v3_0", 2)
        assert len(_w) == 2, _w
        assert _w[1][1].startswith("line 10"), _w[1][1][:20]

        # _apply_desk carries the SENDS, not only the harvest. The check is
        # what the re-render loop cost: a part this desk finished must come
        # back done in CI's copy, a part CI already finished must not be
        # un-done by our staler row, and a tiktok row CI has must win.
        _theirs = Path(DB_PATH).with_name("_selftest_theirs.db")
        _theirs.unlink(missing_ok=True)
        _c = sqlite3.connect(_theirs)
        _c.executescript(
            "CREATE TABLE parts(post_id TEXT, n INT, done INT DEFAULT 0,"
            " lang TEXT, PRIMARY KEY(post_id, n, lang));"
            "CREATE TABLE tiktok(file TEXT PRIMARY KEY, publish_id TEXT,"
            " ts REAL, channel TEXT, backend TEXT);"
            "INSERT INTO parts VALUES ('p',1,0,'ru'),('p',2,1,'ru');"
            "INSERT INTO tiktok VALUES ('a.mp4','theirs',1,'ru','api');")
        _c.commit(); _c.close()
        _state = {"yt": (["id"], []), "yt_story": (["vid", "n"], []),
                  # ours: part 1 sent here, part 2 stale at 0
                  "parts_done": [("p", 1, "ru")],
                  "tiktok": (["file", "publish_id", "ts", "channel", "backend"],
                             [("a.mp4", "ours", 2, "ru", "tau"),
                              ("b.mp4", "ours", 3, "ru", "tau")])}
        _real_path, globals()["DB_PATH"] = DB_PATH, _theirs
        try:
            _apply_desk(_state)
        finally:
            globals()["DB_PATH"] = _real_path
        _c = sqlite3.connect(_theirs)
        assert _c.execute("SELECT done FROM parts WHERE n=1").fetchone()[0] == 1, \
            "a part this desk sent did not reach CI's copy"
        assert _c.execute("SELECT done FROM parts WHERE n=2").fetchone()[0] == 1, \
            "a part CI had already cleared was un-cleared"
        assert _c.execute("SELECT publish_id FROM tiktok WHERE file='a.mp4'"
                          ).fetchone()[0] == "theirs", "CI's tiktok row was overwritten"
        assert _c.execute("SELECT publish_id FROM tiktok WHERE file='b.mp4'"
                          ).fetchone()[0] == "ours", "this desk's send never arrived"
        _c.close(); _theirs.unlink(missing_ok=True)

        print("upvote ok")
    elif a.harvest is not None:
        print(f"{harvest(a.harvest)} new videos")
    elif a.digest is not None:
        # cache_audio() and not a second command to remember: a digest whose
        # recordings never reached the release banks stories the renderer
        # cannot use, and the workflow already runs the two back to back.
        print(f"{digest(a.digest)} new stories")
        print(f"{cache_audio()} source audio files ready")
    elif a.judge:
        kept, dropped = judge()
        print(f"{kept} readings, {dropped} something else")
    elif a.push_state:
        sys.exit(0 if push_state() else 1)
    elif a.show:
        show()
    elif a.cache_audio:
        print(f"{cache_audio()} source audio files ready")
    else:
        ap.print_help()
