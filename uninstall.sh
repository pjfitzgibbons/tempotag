#!/bin/sh
# Remove what install.sh added. Leaves Timewarrior and its data alone.
set -eu

AGENT="$HOME/Library/LaunchAgents/local.tt.tick.plist"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
SETTINGS="$HOME/.claude/settings.json"

if [ -f "$AGENT" ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm "$AGENT" && echo "launchd: removed $AGENT"
fi

# install.sh marks its lines, so they're found wherever the checkout was when it ran.
MARK="# tt time tracking"
for rc in "$ZSHRC" "$HOME/.bash_profile" "$HOME/.bashrc"; do
  if grep -qsF "$MARK" "$rc"; then
    cp "$rc" "$rc.bak-tt"
    grep -vF "$MARK" "$rc.bak-tt" > "$rc" || true
    echo "shell: removed the tt line from $rc (backup: $(basename "$rc").bak-tt)"
  fi
  grep -sE "^[[:space:]]*(source|\\.)[[:space:]].*tt\\.(zsh|bash)" "$rc" | while IFS= read -r l; do
    echo "shell: $rc also has a line not added by install.sh; remove it yourself:  $l"
  done
done

if grep -qsF "tt.py beat claude" "$SETTINGS"; then
  cp "$SETTINGS" "$SETTINGS.bak-tt"
  /usr/bin/python3 - "$SETTINGS" <<'EOF'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
groups = data.get("hooks", {}).get("UserPromptSubmit", [])
for g in groups:
    g["hooks"] = [h for h in g.get("hooks", []) if "tt.py beat claude" not in h.get("command", "")]
data["hooks"]["UserPromptSubmit"] = [g for g in groups if g["hooks"]]
if not data["hooks"]["UserPromptSubmit"]:
    del data["hooks"]["UserPromptSubmit"]
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
EOF
  echo "claude: removed the hook from $SETTINGS (backup: settings.json.bak-tt)"
fi

echo "State in ~/.local/state/tt is kept; delete it by hand if you want."
