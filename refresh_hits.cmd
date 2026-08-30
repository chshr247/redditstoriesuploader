@echo off
rem Weekly refresh of what the critic is judged against.
rem
rem Three steps and they only make sense together: pull this channel's current
rem view counts out of TikTok, rank the titles into the prompt set's hits.md,
rem and push that file - CI reads it out of the private repo, so a rebuild that
rem is never pushed changes nothing about the stories that get written.
rem
rem Registered as the scheduled task "reddit hits" (weekly, Monday). Missing a
rem week is harmless: the file that is there stays valid, it just ages.
cd /d "%~dp0"

python publish.py --stats > tiktok.csv || exit /b 1
python publish.py --hits || exit /b 1

rem Nothing to commit is the ordinary Monday when no video went out, and `git
rem commit` says so with exit code 1 - which is why this line is not chained
rem onto the push with &&.
git -C .private add hits.md hits_en.md 2>nul
git -C .private diff --cached --quiet && echo hits unchanged && exit /b 0
git -C .private commit -q -m "hits: weekly refresh" || exit /b 1
git -C .private push || exit /b 1
