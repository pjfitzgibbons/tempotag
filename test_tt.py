"""Tests for tt's pure logic. Run from this directory: /usr/bin/python3 -m unittest test_tt"""
import os
import subprocess
import sys
import tempfile
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


class ProjectName(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.top = self.dir.name
        subprocess.run(["git", "init", "-q", self.top], check=True)

    def tearDown(self):
        self.dir.cleanup()

    def config(self, key, value):
        subprocess.run(["git", "-C", self.top, "config", key, value], check=True)

    def test_tt_project_wins(self):
        self.config("lanes.project", "old")
        self.config("tt.project", "new")
        self.assertEqual(tt.project_name(self.top), "new")

    def test_falls_back_to_lanes_project(self):
        self.config("lanes.project", "old")
        self.assertEqual(tt.project_name(self.top), "old")

    def test_neither_is_not_set_up(self):
        self.assertEqual(tt.project_name(self.top), "")


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
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, None, (NOW - 10, TAGS, False), None),
                         ("tags changed", [("start", TAGS)]))

    def test_keeps_running_on_same_tags(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, TAGS, False), None),
                         ("no change", []))

    def test_switches_issue(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, OTHER, False), None),
                         ("tags changed", [("start", OTHER)]))

    def test_idle_stops_backdated_to_last_input(self):
        self.assertEqual(tt.decide(NOW, 700, NOW - 60, self.active, None, None), ("idle", [("stop", NOW - 700)]))

    def test_idle_since_before_start_cancels(self):
        active = {"start": NOW - 100, "tags": TAGS}
        self.assertEqual(tt.decide(NOW, 700, NOW - 60, active, None, None), ("idle", [("cancel",)]))

    def test_no_signal(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, None, None), ("no signal", []))

    def test_sleep_stops_at_last_tick_then_resumes(self):
        active = {"start": NOW - 5000, "tags": TAGS}
        self.assertEqual(tt.decide(NOW, 3, NOW - 3600, active, (NOW - 5, TAGS, False), None),
                         ("slept+tags changed", [("stop", NOW - 3600), ("start", TAGS)]))

    def test_sleep_without_an_interval_is_not_reported(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 3600, None, (NOW - 5, TAGS, False), None),
                         ("tags changed", [("start", TAGS)]))

    def test_stale_signal_does_not_resume(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 3 * 3600, TAGS, False), None),
                         ("resume window", []))

    def test_live_editor_resumes_even_if_focused_long_ago(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 3 * 3600, TAGS, True), None),
                         ("tags changed", [("start", TAGS)]))

    def test_repo_not_set_up_stops_timer(self):
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, self.active, (NOW - 10, None, False), None),
                         ("not set up", [("stop", NOW)]))
        self.assertEqual(tt.decide(NOW, 5, NOW - 60, None, (NOW - 10, None, False), None), ("not set up", []))

    def test_manual_stop_holds_until_newer_signal(self):
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 100, TAGS, False), NOW - 50), ("suspended", []))
        self.assertEqual(tt.decide(NOW, 3, NOW - 60, None, (NOW - 10, TAGS, False), NOW - 50),
                         ("tags changed", [("start", TAGS)]))

    def test_every_rule_is_known(self):
        for rule in ("idle", "no signal", "suspended", "resume window", "not set up", "tags changed", "no change"):
            self.assertIn(rule, tt.RULES)


def tick(t, actions=(), rule="no change"):
    return {"time": t, "gap": 60, "idle": 4, "active": None,
            "signals": [{"source": "zsh", "age": 5, "path": "/r/api", "tags": TAGS, "chosen": True}],
            "chosen": "zsh", "rule": rule, "actions": list(actions)}


START = {"action": "start", "arg": TAGS, "exit": 0, "stderr": ""}
FAILED = {"action": "stop", "arg": NOW - 30, "exit": 1,
          "stderr": "The end of a date range must be after the start."}


class TickLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = tt.STATE_DIR, tt.STATE_FILE
        tt.STATE_DIR = self.dir.name
        tt.STATE_FILE = os.path.join(self.dir.name, "state.json")

    def tearDown(self):
        tt.STATE_DIR, tt.STATE_FILE = self.saved
        self.dir.cleanup()

    def lines(self):
        with open(tt.log_day(NOW)) as f:
            return f.read().splitlines()

    def test_round_trip(self):
        for i in range(5):
            tt.append_log(tick(NOW + 60 * i), NOW + 60 * i)
        tt.append_log(tick(NOW + 300, [START], "tags changed"), NOW + 300)
        ticks = tt.read_log(3)
        self.assertEqual([r["time"] for _, r in ticks], [NOW + 180, NOW + 240, NOW + 300])
        self.assertEqual(ticks[-1][1], tick(NOW + 300, [START], "tags changed"))

    def test_no_log(self):
        self.assertEqual(tt.read_log(30), [])

    def test_changes_only(self):
        tt.append_log(tick(NOW), NOW)
        tt.append_log(tick(NOW + 60, [START]), NOW + 60)
        tt.append_log(tick(NOW + 120), NOW + 120)
        tt.append_log(tick(NOW + 180, [FAILED], "idle"), NOW + 180)
        self.assertEqual([r["time"] for _, r in tt.read_log(30, changes_only=True)], [NOW + 60, NOW + 180])

    def test_deletes_day_files_older_than_seven_days(self):
        day = 86400
        for t in (NOW - 9 * day, NOW - 8 * day, NOW - 7 * day, NOW - 6 * day):
            tt.append_log(tick(t), t)
        self.assertEqual(len(tt.log_files()), 4)  # each tick only deletes files older than itself allows
        tt.append_log(tick(NOW), NOW)
        self.assertEqual([r["time"] for _, r in tt.read_log(30)], [NOW - 7 * day, NOW - 6 * day, NOW])
        self.assertEqual(len(tt.log_files()), 3)

    def test_reads_back_across_day_files(self):
        day = 86400
        for t in (NOW - 2 * day, NOW - 2 * day + 60, NOW - day, NOW):
            tt.append_log(tick(t), t)
        self.assertEqual([r["time"] for _, r in tt.read_log(3)], [NOW - 2 * day + 60, NOW - day, NOW])
        self.assertEqual(len(tt.read_log(30)), 4)

    def test_skips_unparseable_and_partial_lines(self):
        tt.append_log(tick(NOW), NOW)
        with open(tt.log_day(NOW), "a") as f:
            f.write('{"time": 12')  # a write cut short
        tt.append_log(tick(NOW + 60), NOW + 60)
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual([r["time"] for _, r in tt.read_log(30)], [NOW, NOW + 60])

    def test_reads_across_blocks(self):
        for i in range(50):
            tt.append_log(tick(NOW + i), NOW + i)
        got = [tt.log_time(line) for line in tt.log_lines_reversed(tt.log_day(NOW), block=64)]
        self.assertEqual(got, [NOW + i for i in reversed(range(50))])

    def test_failed_tick_is_marked(self):
        line = tt.fmt_tick(tick(NOW, [FAILED], "idle"))
        self.assertTrue(line.startswith("!"))
        self.assertIn("The end of a date range", line)
        self.assertTrue(all(len(l) <= 100 for l in line.splitlines()))
        self.assertTrue(tt.fmt_tick(tick(NOW, [START], "tags changed")).startswith(" "))
        self.assertIn("start acme/api#42", tt.fmt_tick(tick(NOW, [START], "tags changed")))

    def test_failed_log_write_does_not_stop_the_tick(self):
        calls = []
        stubs = {
            "idle_seconds": lambda: 5.0,
            "active_interval": lambda: None,
            "current_signal": lambda st, now: ((now - 5, TAGS, False), None, []),
            "timew": lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
            "append_log": lambda rec, now: 1 / 0,
        }
        saved = {k: getattr(tt, k) for k in stubs}
        for k, v in stubs.items():
            setattr(tt, k, v)
        try:
            with open(os.devnull, "w") as null:
                stderr, sys.stderr = sys.stderr, null
                try:
                    tt.cmd_tick([])
                finally:
                    sys.stderr = stderr
        finally:
            for k, v in saved.items():
                setattr(tt, k, v)
        self.assertEqual(calls, [("start",) + tuple(TAGS)])
        self.assertIsNotNone(tt.load_state().get("last_tick"))


if __name__ == "__main__":
    unittest.main()
