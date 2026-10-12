"""Tests for tt's pure logic. Run from this directory: /usr/bin/python3 -m unittest test_tt"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tt  # noqa: E402

TAGS = ["acme/api#42", "project:acme", "repo:acme/api"]
OTHER = ["acme/web#7", "project:acme", "repo:acme/web"]
NOW = 1_000_000.0


def utc(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Parsing(unittest.TestCase):
    def test_repo_slug(self):
        for url in ("git@github.com:acme/api.git", "https://github.com/acme/api",
                    "ssh://git@github.com/acme/api.git\n"):
            self.assertEqual(tt.repo_slug(url), "acme/api")

    def test_issue_number(self):
        self.assertEqual(tt.issue_number("42-add-reindex"), "42")
        self.assertEqual(tt.issue_number("feat/123-thing"), "123")
        self.assertIsNone(tt.issue_number("main"))

    def test_tags_for(self):
        self.assertEqual(tt.tags_for("acme/api", "42-x", "acme"), TAGS)
        self.assertEqual(tt.tags_for("acme/web", "main", "acme"), ["project:acme", "repo:acme/web"])

    def test_match_title_prefers_longest_whole_word(self):
        repos = {"api": "/r/api", "api-gateway": "/r/api-gateway"}
        self.assertEqual(tt.match_title("main.py — api-gateway", repos), "/r/api-gateway")
        self.assertEqual(tt.match_title("api – handlers.py", repos), "/r/api")
        self.assertIsNone(tt.match_title("rapid.py — notes", repos))


class Rounding(unittest.TestCase):
    def test_billed_minutes(self):
        for raw, billed in ((0, 0), (11, 0), (12, 30), (30, 30), (41, 30), (42, 60), (54, 60), (60, 60)):
            self.assertEqual(tt.billed_minutes(raw), billed, raw)

    def test_report_rounds_per_issue_per_day(self):
        ivs = [  # #42: 25 + 20 + 9 = 54m -> 1.0h ; #7: 41m -> 0.5h
            {"start": utc("2026-10-06 09:00"), "end": utc("2026-10-06 09:25"), "tags": TAGS},
            {"start": utc("2026-10-06 10:00"), "end": utc("2026-10-06 10:20"), "tags": TAGS},
            {"start": utc("2026-10-06 11:00"), "end": utc("2026-10-06 11:09"), "tags": TAGS},
            {"start": utc("2026-10-06 13:00"), "end": utc("2026-10-06 13:41"), "tags": OTHER},
        ]
        rows = tt.day_seconds(ivs, datetime.now().astimezone())
        out = tt.render_report(rows)
        self.assertRegex(out, r"acme/api#42\s+0h54m\s+1\.0h")
        self.assertRegex(out, r"acme/web#7\s+0h41m\s+0\.5h")
        self.assertRegex(out, r"total\s+1h35m\s+1\.5h")

    def test_interval_split_at_local_midnight(self):
        ivs = [{"start": utc("2026-10-06 23:50"), "end": utc("2026-10-07 00:20"), "tags": TAGS}]
        rows = tt.day_seconds(ivs, datetime.now().astimezone())
        self.assertEqual(sorted(s // 60 for s in rows.values()), [10, 20])


class Decide(unittest.TestCase):
    active = {"start": NOW - 1800, "tags": TAGS}

    def test_starts_on_fresh_signal(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, None, (NOW - 10, TAGS, False), None), [("start", TAGS)])

    def test_keeps_running_on_same_tags(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, TAGS, False), None), [])

    def test_switches_issue(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, OTHER, False), None),
                         [("start", OTHER)])

    def test_idle_stops_backdated_to_last_input(self):
        self.assertEqual(tt.decide(NOW, 700, NOW - 60, self.active, None, None), [("stop", NOW - 700)])

    def test_idle_since_before_start_cancels(self):
        active = {"start": NOW - 100, "tags": TAGS}
        self.assertEqual(tt.decide(NOW, 700, NOW - 60, active, None, None), [("cancel",)])

    def test_sleep_stops_at_last_tick_then_resumes(self):
        active = {"start": NOW - 5000, "tags": TAGS}
        self.assertEqual(tt.decide(NOW, 3, NOW - 3600, active, (NOW - 5, TAGS, False), None),
                         [("stop", NOW - 3600), ("start", TAGS)])

    def test_stale_signal_does_not_resume(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 3 * 3600, TAGS, False), None), [])

    def test_live_editor_resumes_even_if_focused_long_ago(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 3 * 3600, TAGS, True), None),
                         [("start", TAGS)])

    def test_repo_not_set_up_stops_timer(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, None, False), None),
                         [("stop", NOW)])
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, None, (NOW - 10, None, False), None), [])

    def test_manual_stop_holds_until_newer_signal(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 100, TAGS, False), NOW - 50), [])
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 10, TAGS, False), NOW - 50),
                         [("start", TAGS)])


if __name__ == "__main__":
    unittest.main()
