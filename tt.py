#!/usr/bin/python3
"""tt — automatic per-issue time tracking on top of Timewarrior.

Signals say *where* you are working; keyboard/mouse idle time says *whether*:

  zsh      every prompt writes `epoch path` to ~/.local/state/tt/beat-zsh
  bash     the same, to ~/.local/state/tt/beat-bash
  claude   a Claude Code UserPromptSubmit hook runs `tt.py beat claude`
  editor   the frontmost VS Code / Zed / JetBrains / Xcode window title,
           matched against repos already seen through the other two signals

`tt.py tick`, run every minute by launchd, picks the newest signal that resolves
to a GitHub repo and keeps Timewarrior's open interval on it. Ten minutes without
input stops the interval backdated to the last input; a gap between ticks (the Mac
slept) stops it at the last tick. Activity resumes it.

Each interval is tagged:  owner/repo#ID   repo:owner/repo   project:NAME
ID comes from the branch name: 42 from 42-x or feat/42-x, tt-kovar from Plan's
tt-kovar-x or feat/tt-kovar-x. NAME comes from `git config tt.project`
(or the older `lanes.project`). A repo without either is not set up: activity there
stops the timer, and a notification (once per repo) says how to set it up.

Every tick appends what it saw, the rule decide() applied and timew's results to
~/.local/state/tt/ticks/YYYY-MM-DD.jsonl (7 days kept); `tt log` prints it.

Raw intervals stay exact. Rounding happens only in `tt report`: per issue, per day,
30-minute blocks, and a remainder of 12+ minutes counts as one more block.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

IDLE_CUTOFF = 10 * 60      # no input this long is a pause, cut from the record
SLEEP_GAP = 3 * 60         # ticks this far apart mean the Mac slept
RESUME_WINDOW = 2 * 3600   # after a pause, a terminal/Claude signal older than this won't restart tracking
BLOCK = 30                 # billing block, minutes
TRIGGER = 12               # minutes of remainder that earn another block
EDITORS = {"Code", "Zed", "zed", "idea", "pycharm", "Xcode"}  # System Events process names
SOURCES = ("zsh", "bash", "claude")
RULES = ("idle", "no signal", "suspended", "resume window", "not set up", "tags changed", "no change")
LOG_DAYS = 7               # day files in ticks/ older than this are deleted
LOG_SHOW = 30              # ticks `tt log` prints by default

STATE_DIR = os.path.expanduser("~/.local/state/tt")
STATE_FILE = os.path.join(STATE_DIR, "state.json")


# ---------------------------------------------------------------- pure logic

def repo_slug(url):
    """git@host:owner/repo.git, https://host/owner/repo, ssh://git@host/owner/repo.git -> owner/repo"""
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in re.split(r"[:/]", u) if p]
    return "%s/%s" % (parts[-2], parts[-1]) if len(parts) >= 2 else None


CVCVC = "[b-df-hj-np-tv-z][aeiou][b-df-hj-np-tv-z][aeiou][b-df-hj-np-tv-z]"


def issue_id(branch):
    """The issue a branch is for, after any `word/` prefix: the leading digits of N-slug,
    or moniker-id of Plan's moniker-id-slug, where the moniker is two letters."""
    name = branch.rsplit("/", 1)[-1]
    m = re.match(r"\d+", name)
    if m:
        return m.group(0)
    m = re.match(r"[a-z]{2}-%s(?=-|$)" % CVCVC, name)
    return m.group(0) if m else None


def tags_for(slug, branch, project):
    tags = []
    n = issue_id(branch) if branch else None
    if n:
        tags.append("%s#%s" % (slug, n))
    tags.append("repo:" + slug)
    tags.append("project:" + project)
    return sorted(tags)


def billed_minutes(raw):
    blocks, rem = divmod(int(raw), BLOCK)
    return (blocks + (1 if rem >= TRIGGER else 0)) * BLOCK


def match_title(title, repos):
    """repos: {dirname: path}. The longest dirname appearing as a whole word in the title wins."""
    for name in sorted(repos, key=len, reverse=True):
        if re.search(r"(?<![\w.-])%s(?![\w.-])" % re.escape(name), title):
            return repos[name]
    return None


def decide(now, idle, last_tick, active, signal, suspended_at):
    """What the tick should do to Timewarrior, and which rule said so.

    active: {"start": epoch, "tags": [...]} or None
    signal: (epoch, tags, live) or None. live means observed right now (editor in focus);
            tags None means the newest activity is in a repo that isn't set up
    Returns (rule, actions). actions is a list of ("stop", epoch) / ("cancel",) / ("start", tags).
    rule is one of RULES, prefixed "slept+" when a sleep gap closed the interval first.
    """
    actions = []

    def close(at):  # timew gets whole seconds (local_iso), so a stop in the start's own second cancels
        actions.append(("stop", at) if int(at) > active["start"] else ("cancel",))

    def done(rule):
        return ("slept+" + rule if slept else rule), actions

    slept = bool(active and last_tick is not None and now - last_tick > SLEEP_GAP)
    if slept:
        close(last_tick)
        active = None
    if idle >= IDLE_CUTOFF:
        if active:
            close(now - idle)
        return done("idle")
    if signal is None:
        return done("no signal")
    t, tags, live = signal
    if suspended_at is not None and t <= suspended_at:
        return done("suspended")
    if active is None and not live and now - t > RESUME_WINDOW:
        return done("resume window")
    if tags is None:
        if active:
            close(now)
        return done("not set up")
    if active is None or sorted(active["tags"]) != sorted(tags):
        actions.append(("start", tags))
        return done("tags changed")
    return done("no change")


def classify(tags):
    """-> (project, key) for a report row."""
    # owner/repo#ID, where ID is whatever issue_id() read: 42, or a Plan id such as tt-kovar.
    issue = next((t for t in tags if re.fullmatch(r"[^:\s#]+/[^\s#]+#[^\s#]+", t)), None)
    repo = next((t[5:] for t in tags if t.startswith("repo:")), None)
    project = next((t[8:] for t in tags if t.startswith("project:")), "-")
    if issue:
        return project, issue
    if repo:
        return project, repo + " (no issue)"
    return project, " ".join(tags) or "(untagged)"


def parse_ts(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def day_seconds(intervals, now):
    """{(local date, project, key): seconds}, splitting intervals at local midnight."""
    out = {}
    for iv in intervals:
        start = parse_ts(iv["start"]).astimezone()
        end = parse_ts(iv["end"]).astimezone() if "end" in iv else now
        project, key = classify(iv.get("tags", []))
        while start < end:
            midnight = datetime.combine(start.date() + timedelta(days=1), datetime.min.time()).astimezone()
            seg_end = min(end, midnight)
            k = (start.date(), project, key)
            out[k] = out.get(k, 0) + (seg_end - start).total_seconds()
            start = seg_end
    return out


def fmt_raw(minutes):
    return "%dh%02dm" % divmod(int(minutes), 60)


def fmt_billed(minutes):
    return "%.1fh" % (minutes / 60)


def render_report(rows):
    """rows: day_seconds() output -> report text."""
    lines = ["rule: per issue per day, %dm blocks, remainder >= %dm adds a block" % (BLOCK, TRIGGER), ""]
    by_issue, by_project = {}, {}
    total_raw = total_billed = 0
    last_day = None
    for (day, project, key), secs in sorted(rows.items()):
        raw = int(secs // 60)
        billed = billed_minutes(raw)
        if day != last_day:
            lines.append(day.isoformat() + " " + day.strftime("%a"))
            last_day = day
        lines.append("  %-40s %8s %7s" % (key, fmt_raw(raw), fmt_billed(billed)))
        for bucket, k in ((by_issue, (project, key)), (by_project, project)):
            r, b = bucket.get(k, (0, 0))
            bucket[k] = (r + raw, b + billed)
        total_raw += raw
        total_billed += billed
    lines += ["", "by issue"]
    for (project, key), (r, b) in sorted(by_issue.items()):
        lines.append("  %-40s %8s %7s" % (key, fmt_raw(r), fmt_billed(b)))
    lines += ["", "by project"]
    for project, (r, b) in sorted(by_project.items()):
        lines.append("  %-40s %8s %7s" % (project, fmt_raw(r), fmt_billed(b)))
    lines += ["", "  %-40s %8s %7s" % ("total", fmt_raw(total_raw), fmt_billed(total_billed))]
    return "\n".join(lines)


def tick_failed(rec):
    return any(a.get("exit") for a in rec.get("actions") or [])


def tick_changed(rec):
    return bool(rec.get("actions")) or tick_failed(rec)


def fmt_secs(secs):
    secs = int(secs or 0)
    return "%ds" % secs if secs < 60 else "%dm%02ds" % divmod(secs, 60) if secs < 3600 else "%dh%02dm" % divmod(secs // 60, 60)


def fmt_action(a):
    if a["action"] == "stop":
        return "stop " + datetime.fromtimestamp(a["arg"]).strftime("%H:%M:%S")
    if a["action"] == "start":
        tags = a["arg"] or []
        label = next((t for t in tags if "#" in t), None) or next((t for t in tags if t.startswith("repo:")), None)
        return "start " + (label or " ".join(tags))
    return a["action"]


LOG_HEADER = "  %-14s  %7s  %-6s  %-19s  %s" % ("time", "idle", "signal", "rule", "actions")


def fmt_tick(rec, width=100):
    """One tick log record -> its `tt log` line. A failed timew call marks the line with !
    and adds an indented line per failure with timew's stderr."""
    actions = rec.get("actions") or []
    lines = ["%s %-14s  %7s  %-6s  %-19s  %s" % (
        "!" if tick_failed(rec) else " ",
        datetime.fromtimestamp(rec.get("time", 0)).strftime("%m-%d %H:%M:%S"),
        fmt_secs(rec.get("idle")), rec.get("chosen") or "-", rec.get("rule", "?"),
        ", ".join(fmt_action(a) for a in actions) or "-")]
    lines += ["!   timew %s exit %s: %s" % (a["action"], a["exit"], a.get("stderr") or "-")
              for a in actions if a.get("exit")]
    return "\n".join(l if len(l) <= width else l[:width - 1] + "…" for l in lines)


# ---------------------------------------------------------------- effects

def run(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, **kw)


def timew(*args):
    return run("timew", *args)


def local_iso(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def log_dir():
    return os.path.join(STATE_DIR, "ticks")


def log_day(epoch):
    """The day file a tick at epoch goes in, by local date."""
    return os.path.join(log_dir(), datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") + ".jsonl")


def log_files():
    """Day files, newest first."""
    try:
        names = os.listdir(log_dir())
    except FileNotFoundError:
        return []
    return [os.path.join(log_dir(), n) for n in sorted(names, reverse=True)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", n)]


def log_time(line):
    """A log line's epoch, or None if it doesn't parse."""
    try:
        return float(json.loads(line)["time"])
    except (ValueError, KeyError, TypeError):
        return None


def append_log(rec, now):
    """Append one tick to today's day file, then delete day files older than LOG_DAYS.
    Nothing is rewritten: a whole day goes at once when its file is old enough."""
    os.makedirs(log_dir(), mode=0o700, exist_ok=True)
    os.chmod(log_dir(), 0o700)  # repo paths and timew stderr are nobody else's business
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with open(log_day(now), "ab+") as f:
        f.seek(0, os.SEEK_END)
        if f.tell():  # a write cut short leaves no newline; don't glue this line onto it
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode())
    oldest = (datetime.fromtimestamp(now).date() - timedelta(days=LOG_DAYS)).isoformat() + ".jsonl"
    for path in log_files():
        if os.path.basename(path) < oldest:
            os.remove(path)


def log_lines_reversed(path, block=1 << 16):
    """A file's lines, newest first, read from the end."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, buf = f.tell(), b""
        while pos > 0:
            n = min(block, pos)
            pos -= n
            f.seek(pos)
            parts = (f.read(n) + buf).split(b"\n")
            buf = parts[0]
            for part in reversed(parts[1:]):
                if part.strip():
                    yield part.decode("utf-8", "replace")
        if buf.strip():
            yield buf.decode("utf-8", "replace")


def read_log(n, changes_only=False):
    """The last n parseable ticks, oldest first, as (raw line, record). Reads back a day file
    at a time and stops once it has n. [] if there's no log."""
    out = []
    for path in log_files():
        try:
            for raw in log_lines_reversed(path):
                if len(out) >= n:
                    return out[::-1]
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (not changes_only or tick_changed(rec)):
                    out.append((raw, rec))
        except FileNotFoundError:  # deleted by a tick while we read
            continue
    return out[:n][::-1]


def idle_seconds():
    out = run("/usr/sbin/ioreg", "-c", "IOHIDSystem").stdout
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0.0


def active_interval():
    since = local_iso(time.time() - 2 * 86400)
    r = timew("export", "from", since)
    if r.returncode != 0:
        raise RuntimeError("timew export failed: " + r.stderr.strip())
    for iv in json.loads(r.stdout or "[]"):
        if "end" not in iv:
            return {"start": parse_ts(iv["start"]).timestamp(), "tags": iv.get("tags", [])}
    return None


class NotSetUp(Exception):
    def __init__(self, top):
        super().__init__(top)
        self.top = top


def resolve(path, st):
    """Directory -> tags, or None if it isn't inside a repo with an origin remote.
    Raises NotSetUp for a repo without tt.project."""
    if not os.path.isdir(path):
        return None
    top = run("git", "-C", path, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    top = top.stdout.strip()
    url = run("git", "-C", top, "remote", "get-url", "origin")
    slug = repo_slug(url.stdout) if url.returncode == 0 else None
    if not slug:
        return None
    branch = run("git", "-C", top, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or None
    project = project_name(top)
    st.setdefault("repos", {})[os.path.basename(top)] = top
    if not project:
        raise NotSetUp(top)
    return tags_for(slug, branch, project)


def project_name(top):
    """tt.project, falling back to lanes.project, the key's name before tt stood alone."""
    for key in ("tt.project", "lanes.project"):
        name = run("git", "-C", top, "config", "--get", key).stdout.strip()
        if name:
            return name
    return ""


def setup_hint(top):
    return "%s isn't set up for tt: git -C %s config tt.project NAME" % (os.path.basename(top), top)


FRONT_WINDOW = '''
tell application "System Events"
  set p to first application process whose frontmost is true
  set n to name of p
  set t to ""
  try
    set t to name of front window of p
  end try
end tell
return n & tab & t
'''


def editor_signal(st, now):
    r = run("/usr/bin/osascript", "-e", FRONT_WINDOW)
    if r.returncode != 0:
        st["editor_error"] = r.stderr.strip()
        return None
    st.pop("editor_error", None)
    app, _, title = r.stdout.rstrip("\n").partition("\t")
    st["frontmost"] = [app, title]
    path = match_title(title, st.get("repos", {})) if app in EDITORS and title else None
    if not path:
        st["editor"] = None
        return None
    if st.get("editor") != path:
        st["editor"], st["editor_since"] = path, now
    return st["editor_since"], path, True


def read_beat(source):
    try:
        with open(os.path.join(STATE_DIR, "beat-" + source)) as f:
            t, _, path = f.read().strip().partition(" ")
        return float(t), path
    except (OSError, ValueError):
        return None


def current_signal(st, now):
    """-> (signal for decide(), top of the not-set-up repo it points at or None, seen)

    seen lists every signal for the tick log, newest first: {"source", "age", "path",
    "tags"} with tags a list, "not set up", or None (not a repo with an origin), and
    "chosen" on the one that decide() gets. Every signal is resolved so the log shows them all.
    """
    cands = []
    for source in SOURCES:
        b = read_beat(source)
        if b and b[1]:
            cands.append((b[0], b[1], False, source))
    ed = editor_signal(st, now)
    if ed:
        cands.append(ed + ("editor",))
    signal = unset = None
    seen = []
    for t, path, live, source in sorted(cands, reverse=True):
        entry = {"source": source, "age": round(now - t), "path": path}
        try:
            entry["tags"] = tags = resolve(path, st)
        except NotSetUp as e:
            entry["tags"], tags, top = "not set up", None, e.top
        else:
            top = None
        if signal is None and (tags or top):
            signal, unset = (t, tags, live), top
            entry["chosen"] = True
        seen.append(entry)
    return signal, unset, seen


def notify_once(st, top):
    warned = st.setdefault("warned", [])
    if top in warned:
        return
    warned.append(top)
    msg = setup_hint(top).replace("\\", "\\\\").replace('"', '\\"')
    run("/usr/bin/osascript", "-e", 'display notification "%s" with title "tt: not tracking"' % msg)


# ---------------------------------------------------------------- commands

def cmd_tick(_args):
    st = load_state()
    now = time.time()
    idle = idle_seconds()
    active = active_interval()
    signal, unset, seen = current_signal(st, now)
    if idle >= IDLE_CUTOFF:  # the log still shows the signals, but an idle tick acts on none of them
        signal = unset = None
        for entry in seen:
            entry.pop("chosen", None)
    if unset:
        notify_once(st, unset)
    rule, actions = decide(now, idle, st.get("last_tick"), active, signal, st.get("suspended_at"))
    done = []
    held = False
    for action in actions:
        if action[0] == "stop":
            r = timew("stop", local_iso(action[1]))
        elif action[0] == "cancel":
            r = timew("cancel")
        else:
            r = timew("start", *action[1])
        done.append({"action": action[0], "arg": action[1] if len(action) > 1 else None,
                     "exit": r.returncode, "stderr": r.stderr.strip()})
        if r.returncode != 0:
            print("tt tick: timew %s failed: %s" % (action[0], r.stderr.strip()), file=sys.stderr)
            # The interval is still open: a start now would close it at now, so run nothing more,
            # and keep last_tick so the next tick works out the same backdated stop and retries it
            if action[0] != "start":
                held = True
                break
    last_tick = st.get("last_tick")
    if not held:
        st["last_tick"] = now
    save_state(st)
    try:
        append_log({"time": round(now), "gap": None if last_tick is None else round(now - last_tick),
                    "idle": round(idle), "active": active, "signals": seen,
                    "chosen": next((e["source"] for e in seen if e.get("chosen")), None), "rule": rule,
                    "actions": done}, now)
    except Exception as e:  # the log is for debugging; it never stops a tick
        print("tt tick: writing the tick log in %s failed: %s" % (log_dir(), e), file=sys.stderr)


def cmd_beat(args):
    """beat SOURCE [PATH]. Without PATH, read the Claude Code hook's JSON from stdin. Never fails."""
    try:
        source = args[0]
        path = args[1] if len(args) > 1 else json.load(sys.stdin).get("cwd", "")
        if source in SOURCES and path:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "beat-" + source), "w") as f:
                f.write("%d %s\n" % (time.time(), path))
    except Exception:
        pass


def cmd_stop(_args):
    st = load_state()
    r = timew("stop")
    print((r.stdout or r.stderr).strip())
    st["suspended_at"] = time.time()
    save_state(st)
    print("tt: paused until your next command, Claude prompt, or editor-window switch")


def cmd_status(_args):
    st = load_state()
    now = time.time()
    active = active_interval()
    if active:
        print("tracking  %s  for %s" % (" ".join(active["tags"]), fmt_raw((now - active["start"]) // 60)))
    else:
        print("tracking  nothing")
    print("idle      %ds (cutoff %ds)" % (idle_seconds(), IDLE_CUTOFF))
    last = st.get("last_tick")
    print("last tick %s" % ("never - is the launchd agent loaded?" if last is None else
                            "%ds ago%s" % (now - last, "  <- launchd agent not running?" if now - last > SLEEP_GAP else "")))
    for source in SOURCES:
        b = read_beat(source)
        print("%-9s %s" % (source, "%ds ago  %s" % (now - b[0], b[1]) if b else "no signal yet"))
    if st.get("editor_error"):
        print("editor    unavailable: %s" % st["editor_error"])
    else:
        print("editor    front=%s  matched=%s" % (st.get("frontmost"), st.get("editor")))
    last_logged = read_log(1)
    if last_logged and tick_failed(last_logged[0][1]):
        print("log       the last tick's timew call failed: tt log --changes")
    print("repos     %s" % (", ".join(sorted(st.get("repos", {}))) or "-"))
    try:
        resolve(os.getcwd(), {})
    except NotSetUp as e:
        print("here      " + setup_hint(e.top))


def cmd_issue(_args):
    try:
        tags = resolve(os.getcwd(), {})
    except NotSetUp as e:
        print("tt: " + setup_hint(e.top), file=sys.stderr)
        sys.exit(1)
    if not tags:
        print("tt: not inside a git repo with an 'origin' remote", file=sys.stderr)
        sys.exit(1)
    print("\n".join(tags))


def cmd_report(args, only_day=None):
    r = timew("export", *(args or [":week"]))
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    rows = day_seconds(json.loads(r.stdout or "[]"), datetime.now().astimezone())
    if only_day:
        # timew exports whole intervals that overlap the range, so one running across midnight
        # brings its share of the day before.
        rows = {k: v for k, v in rows.items() if k[0] == only_day}
    print(render_report(rows) if rows else "no tracked time in that range")


def cmd_today(args):
    if args:
        print("usage: tt today  (tt report :today)", file=sys.stderr)
        sys.exit(2)
    cmd_report([":today"], only_day=datetime.now().astimezone().date())


def version_line(sha, date, branch, dirty):
    """tt has no version number: it runs from a git checkout, so the commit is the version."""
    if not sha:
        return "tt (not a git checkout; version unknown)"
    return f"tt {sha} {date} ({branch or 'detached'})" + (" +local changes" if dirty else "")


def cmd_version(_args):
    here = os.path.dirname(os.path.realpath(__file__))
    head = run("git", "-C", here, "log", "-1", "--format=%h %cs")
    if head.returncode != 0:
        print(version_line(None, None, None, False))
        return
    sha, date = head.stdout.split()
    branch = run("git", "-C", here, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    dirty = run("git", "-C", here, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    print(version_line(sha, date, branch, bool(dirty)))


def cmd_log(args):
    """log [N] [--json] [--changes]"""
    n, as_json, changes = LOG_SHOW, False, False
    for a in args:
        if a == "--json":
            as_json = True
        elif a == "--changes":
            changes = True
        elif a.isdigit() and int(a) > 0:
            n = int(a)
        else:
            print("usage: tt log [N] [--json] [--changes]", file=sys.stderr)
            sys.exit(2)
    ticks = read_log(n, changes)
    if not ticks:
        print("no ticks logged yet" if not read_log(1) else "no ticks with an action or error in the log")
        return
    if as_json:
        print("\n".join(raw for raw, _ in ticks))
        return
    print(LOG_HEADER)
    for _, rec in ticks:
        print(fmt_tick(rec))


COMMANDS = {"tick": cmd_tick, "beat": cmd_beat, "stop": cmd_stop, "status": cmd_status,
            "issue": cmd_issue, "report": cmd_report, "today": cmd_today, "log": cmd_log,
            "version": cmd_version}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "status"
    if name not in COMMANDS:
        print("usage: tt [status|stop|report [range]|today|issue|log [N] [--json] [--changes]|version]",
              file=sys.stderr)
        sys.exit(2)
    COMMANDS[name](sys.argv[2:])
