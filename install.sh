#!/bin/sh
# Install tt from this directory. Safe to run again: every step checks before it changes anything.
#
#   ./install.sh            install
#   ./install.sh --dry-run  say what would change, change nothing
#
# It wires three things to tt.py where it sits, so `git pull` here upgrades tt in place:
#   1. ~/.zshrc sources tt.zsh, and ~/.bash_profile or ~/.bashrc sources tt.bash
#      (the shell signal + the `tt` command), for whichever shells this account uses
#   2. a launchd agent runs `tt.py tick` each minute (~/Library/LaunchAgents/local.tt.tick.plist)
#   3. a Claude Code UserPromptSubmit hook          (~/.claude/settings.json, backed up first)
set -eu

DRY=
ADDING=adding
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ADDING="would add" ;;
  *) printf 'usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac
TT_HOME=$(cd "$(dirname "$0")" && pwd -P)
STATE_DIR="$HOME/.local/state/tt"
AGENT="$HOME/Library/LaunchAgents/local.tt.tick.plist"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
SETTINGS="$HOME/.claude/settings.json"
HOOK="/usr/bin/python3 $TT_HOME/tt.py beat claude"

say() { printf '%s\n' "$*"; }
do_() { if [ "$DRY" ]; then say "  would: $*"; else "$@"; fi; }

# ---- requirements
[ "$(uname -s)" = Darwin ] || { say "tt needs macOS (launchd, ioreg, System Events)."; exit 1; }
[ -x /usr/bin/python3 ] || { say "/usr/bin/python3 is missing: run xcode-select --install"; exit 1; }
TIMEW=$(command -v timew || true)
[ -n "$TIMEW" ] || { say "Timewarrior is missing: brew install timewarrior"; exit 1; }
say "tt from $TT_HOME"
say "timew at $TIMEW"

# ---- 1. shells: zsh and/or bash, whichever this account uses
# The marker finds lines this installer wrote, wherever the checkout was at the time, so
# running it again after a move replaces them. Lines without it are reported, never edited.
MARK="# tt time tracking"
hook_rc() {  # hook_rc SHELL-NAME RC-FILE HOOK-FILE
  line="source \"$TT_HOME/$3\"  $MARK"
  ours=$(grep -sF "$MARK" "$2" | grep -F "/$3\"" || true)
  grep -sE "^[[:space:]]*(source|\\.)[[:space:]].*$3" "$2" | grep -vF "$MARK" | while IFS= read -r l; do
    say "$1: $2 also has a line not added by install.sh; remove it yourself:  $l"
  done
  if [ "$ours" = "$line" ]; then
    say "$1: $2 already sources $3"
  elif [ -n "$ours" ]; then
    printf '%s\n' "$ours" | while IFS= read -r old; do
      say "$1: ${DRY:+would }replace $old with $line in $2"
    done
    if [ -z "$DRY" ]; then
      cp "$2" "$2.bak-tt"
      awk -v mark="$MARK" -v hook="/$3\"" -v line="$line" '
        index($0, mark) && index($0, hook) { if (!done) print line; done = 1; next }
        { print }' "$2.bak-tt" > "$2"
    fi
  else
    say "$1: $ADDING a line to $2"
    if [ -z "$DRY" ]; then
      [ -f "$2" ] && cp "$2" "$2.bak-tt"
      printf '\n%s\n' "$line" >> "$2"
    fi
  fi
}
LOGIN_SHELL=$(basename "${SHELL:-}")
HOOKED=
if [ -f "$ZSHRC" ] || [ "$LOGIN_SHELL" = zsh ]; then
  hook_rc zsh "$ZSHRC" tt.zsh; HOOKED=1
fi
# macOS Terminal starts bash as a login shell, which reads ~/.bash_profile, not ~/.bashrc.
# Hook every bash startup file that exists; tt.bash ignores a second load.
BASH_FILES=
for f in "$HOME/.bash_profile" "$HOME/.bashrc"; do [ -f "$f" ] && BASH_FILES="$BASH_FILES $f"; done
[ -z "$BASH_FILES" ] && [ "$LOGIN_SHELL" = bash ] && BASH_FILES="$HOME/.bash_profile"
for f in $BASH_FILES; do hook_rc bash "$f" tt.bash; HOOKED=1; done
[ "$HOOKED" ] || say "shell: no zsh or bash startup file found; source tt.zsh or tt.bash from yours"

# ---- 2. launchd
do_ mkdir -p "$STATE_DIR" "$(dirname "$AGENT")"
AGENT_PATH="$(dirname "$TIMEW"):/usr/bin:/bin"
RENDERED=$(sed -e "s|@TT_HOME@|$TT_HOME|g" -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@PATH@|$AGENT_PATH|g" \
  "$TT_HOME/local.tt.tick.plist.in")
if [ -f "$AGENT" ] && [ "$(cat "$AGENT")" = "$RENDERED" ]; then
  say "launchd: agent already installed"
else
  say "launchd: writing $AGENT"
  if [ -z "$DRY" ]; then
    printf '%s\n' "$RENDERED" > "$AGENT"
    launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
  fi
fi

# ---- 3. Claude Code hook. Any command ending "tt.py beat claude" is ours, wherever it points:
# the first is repointed here, and any others are dropped.
if [ -d "$HOME/.claude" ]; then
  /usr/bin/python3 - "$SETTINGS" "$HOOK" "$DRY" <<'EOF'
import json, os, shutil, sys
path, command, dry = sys.argv[1], sys.argv[2], sys.argv[3]
would = "would " if dry else ""
data = json.load(open(path)) if os.path.exists(path) else {}
groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
ours = [h for g in groups for h in g.get("hooks", [])
        if h.get("command", "").endswith("tt.py beat claude")]
if [h["command"] for h in ours] == [command]:
    print("claude: hook already in %s" % path)
    sys.exit(0)
if ours:
    for h in ours:
        print("claude: %sreplace %s with %s in %s" % (would, h["command"], command, path))
    keep, drop = ours[0], [id(h) for h in ours[1:]]
    keep["command"] = command
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if id(h) not in drop]
    groups[:] = [g for g in groups if g["hooks"]]
else:
    print("claude: %sadd the UserPromptSubmit hook to %s" % (would, path))
    groups.append({"hooks": [{"type": "command", "command": command}]})
if dry:
    sys.exit(0)
if os.path.exists(path):
    shutil.copy2(path, path + ".bak-tt")
    print("claude: backup in %s.bak-tt" % path)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
EOF
else
  say "claude: no ~/.claude, skipping the Claude Code hook"
fi

cat <<EOF

Done. Next:
  1. Open a new terminal.
  2. In each repo to track:  git config tt.project <project-name>
     Branches named 42-x or feat/42-x tag time to issue #42.
  3. macOS will ask once to let the agent control "System Events"; allow it, or the
     editor signal is off (Settings > Privacy & Security > Automation).
  4. Check it:  tt status
EOF
