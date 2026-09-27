"""Regression check for the first milestone scan and the five-day boundary."""

import contextlib
import datetime
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import publish


class MilestoneTest(unittest.TestCase):
    def test_young_videos_remain_eligible_after_baseline(self):
        day = 24 * 60 * 60
        started = datetime.datetime(2026, 9, 27, 18, 32, 46,
                                    tzinfo=datetime.timezone.utc).timestamp()
        videos = [
            {"id": "1", "create_time": started - 10 * day,
             "view_count": 15_001, "title": "old"},
            {"id": "2", "create_time": started - 4 * day,
             "view_count": 15_001, "title": "young at activation"},
            {"id": "3", "create_time": started - day,
             "view_count": 15_001, "title": "still young at first scan"},
            {"id": "4", "create_time": started - 10 * day,
             "view_count": 9_000, "title": "late threshold"},
        ]
        for video in videos:
            video["share_url"] = f"https://www.tiktok.com/@x/video/{video['id']}"

        created = []

        def gh(args, **_kwargs):
            if args[2] == "list":
                return types.SimpleNamespace(stdout="[]")
            created.append(args)
            return types.SimpleNamespace(stdout="https://example.test/issue")

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            try:
                os.chdir(tmp)
                Path(".github").mkdir()
                with (mock.patch.dict(os.environ, {"TIKTOK_MILESTONES_SINCE":
                                                   "2026-09-27T18:32:46+00:00"}),
                      mock.patch.object(publish, "videos", return_value=videos),
                      mock.patch.object(publish, "CHANNEL", "ru"),
                      mock.patch.object(publish.subprocess, "run", side_effect=gh),
                      contextlib.redirect_stdout(io.StringIO())):
                    with mock.patch.object(publish.time, "time", return_value=started + 2 * day):
                        publish.report_10k()
                    self.assertEqual([c[c.index("--title") + 1][-3:] for c in created],
                                     ["[2]"])

                    videos[3]["view_count"] = 15_001
                    with mock.patch.object(publish.time, "time", return_value=started + 4 * day):
                        publish.report_10k()
                        publish.report_10k()

                self.assertEqual([c[c.index("--title") + 1][-3:] for c in created],
                                 ["[2]", "[3]", "[4]"])
                state = json.loads(Path(".github/tiktok-milestones-ru.json").read_text())
                self.assertEqual(state, ["1", "2", "3", "4"])
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
