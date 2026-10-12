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
[ "${1:-}" = "--dry-run" ] && DRY=1
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
hook_rc() {  # hook_rc SHELL-NAME RC-FILE HOOK-FILE
  if grep -qsF "$TT_HOME/$3" "$2"; then
    say "$1: $2 already sources $3"
  else
    say "$1: adding a line to $2"
    [ "$DRY" ] || printf '\n%s\n' "source \"$TT_HOME/$3\"  # tt time tracking" >> "$2"
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

# ---- 3. Claude Code hook
if [ -f "$SETTINGS" ] && grep -qsF "tt.py beat claude" "$SETTINGS"; then
  say "claude: hook already in $SETTINGS"
elif [ -d "$HOME/.claude" ]; then
  say "claude: adding the UserPromptSubmit hook to $SETTINGS (backup: settings.json.bak-tt)"
  if [ -z "$DRY" ]; then
    [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-tt"
    /usr/bin/python3 - "$SETTINGS" "$HOOK" <<'EOF'
import json, os, sys
path, command = sys.argv[1], sys.argv[2]
data = json.load(open(path)) if os.path.exists(path) else {}
groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
groups.append({"hooks": [{"type": "command", "command": command}]})
tmp = path + ".tmp"
json.dump(data, open(tmp, "w"), indent=2)
os.replace(tmp, path)
EOF
  fi
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
