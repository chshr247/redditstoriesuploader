@echo off
rem Once a day, fill the channel's queue with stories read aloud on YouTube.
rem
rem This runs on the DESK and not in CI, and that is not a preference. YouTube
rem answers a datacentre IP with "sign in to confirm you are not a bot" before
rem it hands over any stream URL, so a GitHub runner cannot download a video at
rem all - measured 2026-09-06, three out of three refused in under two seconds
rem with the PO token provider built and in place, while the same videos came
rem down from here untouched. Listing a channel is metadata and still works
rem anywhere; it is the media that is blocked.
rem
rem What leaves this machine afterwards is two things, and it needs both:
rem   * the recordings, into the "source-audio" release, so the runner that
rem     renders a story can reach the tape it is cut from (--cache-audio, run
rem     for you at the end of --digest)
rem   * the stories themselves, in seen.db, which is what --push-state does
rem A run that banks stories and pushes neither has done nothing at all.
rem
rem Register as a scheduled task next to "reddit hits". Missing a day is
rem harmless: the queue holds several days of stories and the catalogue is
rem re-read from scratch every time rather than appended to.
cd /d "%~dp0"

rem Latest 60 per channel, titles judged by the model - cheap, no media.
python upvote.py --harvest 60 || exit /b 1

rem The expensive half: download, transcribe, split into stories, and put the
rem recordings in the release. Three videos is UPVOTE_DIGEST_PER_RUN's default
rem and roughly an hour of CPU here, most of it whisper.
python upvote.py --digest 3 || exit /b 1

rem And the half that is easy to forget, which is why it is not optional: the
rem stories live in seen.db, publish.yml commits that file twice an hour, and
rem a plain `git pull --rebase` on a binary database conflicts every time. This
rem takes CI's copy and puts this machine's two harvest tables into it, then
rem pushes - retrying, because losing that race is the ordinary outcome.
python upvote.py --push-state || exit /b 1

python upvote.py --show
