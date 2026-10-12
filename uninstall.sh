#!/bin/sh
# Remove what install.sh added. Leaves Timewarrior and its data alone.
set -eu

TT_HOME=$(cd "$(dirname "$0")" && pwd -P)
AGENT="$HOME/Library/LaunchAgents/local.tt.tick.plist"
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
SETTINGS="$HOME/.claude/settings.json"

if [ -f "$AGENT" ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm "$AGENT" && echo "launchd: removed $AGENT"
fi

for pair in "$ZSHRC:tt.zsh" "$HOME/.bash_profile:tt.bash" "$HOME/.bashrc:tt.bash"; do
  rc=${pair%:*} hook=${pair##*:}
  if grep -qsF "$TT_HOME/$hook" "$rc"; then
    cp "$rc" "$rc.bak-tt"
    grep -vF "$TT_HOME/$hook" "$rc.bak-tt" > "$rc"
    echo "shell: removed the tt line from $rc (backup: $(basename "$rc").bak-tt)"
  fi
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
json.dump(data, open(path, "w"), indent=2)
EOF
  echo "claude: removed the hook from $SETTINGS (backup: settings.json.bak-tt)"
fi

echo "State in ~/.local/state/tt is kept; delete it by hand if you want."
