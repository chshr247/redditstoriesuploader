"""Offline checks for downloader retries and incomplete digests."""

from contextlib import closing
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import upvote


class DownloadTest(unittest.TestCase):
    timeout = "ERROR: Command ['node', 'generate_once.js', '--version'] timed out after 15.0 seconds"

    def test_version_timeout_retries_once(self):
        error = subprocess.CalledProcessError(1, "yt-dlp", stderr=self.timeout)
        success = subprocess.CompletedProcess("yt-dlp", 0, stdout="audio ready")
        with patch.object(upvote.subprocess, "run", side_effect=[error, success]) as run:
            with self.assertLogs("upvote", level="WARNING"):
                self.assertEqual(upvote._ytdlp("video-url"), "audio ready")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0], run.call_args_list[1])

    def test_repeated_timeout_keeps_full_stderr(self):
        stderr = "important diagnostic\n" + "x" * 400 + self.timeout
        error = subprocess.CalledProcessError(1, "yt-dlp", stderr=stderr)
        with patch.object(upvote.subprocess, "run", side_effect=error) as run:
            with self.assertLogs("upvote", level="WARNING"):
                with self.assertRaises(RuntimeError) as raised:
                    upvote._ytdlp("video-url")
            self.assertIn(stderr, str(raised.exception))
            self.assertEqual(run.call_count, 2)

    def test_other_errors_do_not_retry(self):
        for stderr in ("HTTP Error 403", "generate_once.js token generation timed out after 20.0 seconds",
                       "No module named yt_dlp"):
            with self.subTest(stderr=stderr):
                error = subprocess.CalledProcessError(1, "yt-dlp", stderr=stderr)
                with patch.object(upvote.subprocess, "run", side_effect=error) as run:
                    with self.assertRaises(RuntimeError):
                        upvote._ytdlp("video-url")
                    self.assertEqual(run.call_count, 1)

    def test_digest_reports_failures_and_leaves_them_pending(self):
        with patch.object(upvote, "DB_PATH", ":memory:"):
            with closing(upvote._db()) as connection, patch.object(upvote, "_db", return_value=connection):
                with upvote._db() as db:
                    db.executemany("INSERT INTO yt(id,chan,title,views) VALUES (?,?,?,?)",
                                   [("download-failed", "channel", "title", 3),
                                    ("split-failed", "channel", "title", 2),
                                    ("completed", "channel", "title", 1)])
                with patch.object(upvote, "_audio", side_effect=[RuntimeError("download"), Path("a"), Path("b")]), \
                        patch.object(upvote, "_transcribe", return_value=[{"text": "story"}]), \
                        patch.object(upvote, "_split", side_effect=[RuntimeError("split"), []]):
                    with self.assertLogs("upvote", level="INFO") as logs:
                        self.assertEqual(upvote.digest(3), 0)
                self.assertTrue(any("1/3 videos completed" in line for line in logs.output))
                self.assertTrue(any("2 video(s) left for next run: download-failed, split-failed"
                                    in line for line in logs.output))
                with upvote._db() as db:
                    self.assertEqual(dict(db.execute("SELECT id,done FROM yt")),
                                     {"download-failed": 0, "split-failed": 0, "completed": 1})

    def test_title_verdicts_do_not_exclude_videos(self):
        with patch.object(upvote, "DB_PATH", ":memory:"):
            with closing(upvote._db()) as connection, patch.object(upvote, "_db", return_value=connection):
                with connection as db:
                    db.executemany("INSERT INTO yt(id,chan,title,views,keep) VALUES (?,?,?,?,?)",
                                   [("rejected", "channel", "industry secrets", 3, 0),
                                    ("unjudged", "channel", "clients", 2, None),
                                    ("accepted", "channel", "story", 1, 1)])
                with patch.object(upvote, "_audio", return_value=Path("audio")) as audio, \
                        patch.object(upvote, "_transcribe", return_value=[{"text": "story"}]), \
                        patch.object(upvote, "_split", return_value=[]):
                    self.assertEqual(upvote.digest(3), 0)
                self.assertEqual([c.args[0] for c in audio.call_args_list],
                                 ["rejected", "unjudged", "accepted"])
                self.assertEqual(upvote.judge(), (2, 0))
                self.assertEqual(upvote.judge(), (0, 0))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM yt WHERE keep=1").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
