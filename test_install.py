"""Tests for install.sh and uninstall.sh. They run against a temporary HOME, with launchctl and
timew stubbed on PATH, so the real account is never touched. Run from this directory:
/usr/bin/python3 -m unittest test_install"""
import json
import os
import platform
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("install.sh", "uninstall.sh", "tt.py", "tt.zsh", "tt.bash", "local.tt.tick.plist.in")
CLAUDE = "tt.py beat claude"


@unittest.skipUnless(platform.system() == "Darwin", "install.sh is macOS only")
class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.home = os.path.join(root, "home")
        stubs = os.path.join(root, "stubs")
        for d in (self.home, stubs, os.path.join(self.home, ".claude")):
            os.makedirs(d)
        for name in ("launchctl", "timew"):
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        for name in (".zshrc", ".bash_profile"):
            open(self.rc(name), "w").close()
        with open(self.settings, "w") as f:
            json.dump({"hooks": {"UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "other-hook"}]}]}}, f)
        self.env = {"HOME": self.home, "PATH": stubs + ":/usr/bin:/bin", "SHELL": "/bin/zsh"}
        self.checkout = self.place("a")

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def settings(self):
        return os.path.join(self.home, ".claude", "settings.json")

    def rc(self, name):
        return os.path.join(self.home, name)

    def place(self, name):
        """Copy the scripts to <tmp>/<name>/tempotag, as a checkout would sit."""
        path = os.path.realpath(os.path.join(self.tmp.name, name, "tempotag"))
        os.makedirs(path)
        for f in FILES:
            shutil.copy2(os.path.join(HERE, f), path)
        return path

    def move(self, name):
        new = os.path.realpath(os.path.join(self.tmp.name, name, "tempotag"))
        os.makedirs(os.path.dirname(new))
        shutil.move(self.checkout, new)
        self.checkout = new

    def run_script(self, script, *args):
        out = subprocess.run(["/bin/sh", os.path.join(self.checkout, script), *args],
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def read(self, name):
        with open(self.rc(name)) as f:
            return f.read()

    def source_lines(self, name):
        return [line for line in self.read(name).splitlines() if "tt time tracking" in line]

    def claude_hooks(self):
        with open(self.settings) as f:
            data = json.load(f)
        return [h["command"] for g in data["hooks"].get("UserPromptSubmit", [])
                for h in g["hooks"]]

    def agent(self):
        with open(os.path.join(self.home, "Library/LaunchAgents/local.tt.tick.plist")) as f:
            return f.read()

    def assert_points_at(self, checkout):
        self.assertEqual(self.source_lines(".zshrc"),
                         ['source "%s/tt.zsh"  # tt time tracking' % checkout])
        self.assertEqual(self.source_lines(".bash_profile"),
                         ['source "%s/tt.bash"  # tt time tracking' % checkout])
        self.assertEqual([c for c in self.claude_hooks() if c.endswith(CLAUDE)],
                         ["/usr/bin/python3 %s/tt.py beat claude" % checkout])
        self.assertIn("other-hook", self.claude_hooks())
        self.assertIn("%s/tt.py" % checkout, self.agent())

    def test_install(self):
        self.run_script("install.sh")
        self.assert_points_at(self.checkout)

    def test_unknown_argument_changes_nothing(self):
        out = subprocess.run(["/bin/sh", os.path.join(self.checkout, "install.sh"), "--help"],
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("usage:", out.stderr)
        self.assertEqual(self.read(".zshrc"), "")

    def test_edited_files_are_backed_up(self):
        self.run_script("install.sh")
        for name in (".zshrc", ".bash_profile", ".claude/settings.json"):
            self.assertTrue(os.path.exists(self.rc(name + ".bak-tt")), name)
        with open(self.settings) as f:
            self.assertTrue(f.read().endswith("}\n"))

    def test_second_run_changes_nothing(self):
        self.run_script("install.sh")
        before = [self.read(".zshrc"), self.read(".bash_profile"), self.claude_hooks(), self.agent()]
        out = self.run_script("install.sh")
        after = [self.read(".zshrc"), self.read(".bash_profile"), self.claude_hooks(), self.agent()]
        self.assertEqual(before, after)
        self.assertNotIn("replac", out)

    def test_move_then_install_repoints_everything(self):
        self.run_script("install.sh")
        self.move("b")
        self.run_script("install.sh")
        self.assert_points_at(self.checkout)

    def test_dry_run_after_move_says_what_it_would_replace(self):
        old = self.checkout
        self.run_script("install.sh")
        self.move("b")
        out = self.run_script("install.sh", "--dry-run")
        self.assertIn('would replace source "%s/tt.zsh"' % old, out)
        self.assertIn("would replace /usr/bin/python3 %s/tt.py beat claude" % old, out)
        self.assertEqual(self.source_lines(".zshrc"), ['source "%s/tt.zsh"  # tt time tracking' % old])

    def test_hand_made_line_is_reported_not_removed(self):
        with open(self.rc(".zshrc"), "w") as f:
            f.write("source ~/.config/zsh/tt.zsh\n")
        out = self.run_script("install.sh")
        self.assertIn("source ~/.config/zsh/tt.zsh", self.read(".zshrc"))
        self.assertIn("not added by install.sh", out)
        self.assertIn("source ~/.config/zsh/tt.zsh", out)

    def test_uninstall_after_move_removes_everything(self):
        self.run_script("install.sh")
        self.move("b")
        self.run_script("uninstall.sh")
        self.assertEqual(self.source_lines(".zshrc"), [])
        self.assertEqual(self.source_lines(".bash_profile"), [])
        self.assertEqual(self.claude_hooks(), ["other-hook"])


if __name__ == "__main__":
    unittest.main()
