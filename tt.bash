# tt — automatic per-issue time tracking. The logic lives in tt.py, next to this file.
#
# Sourced from ~/.bash_profile or ~/.bashrc by install.sh. It does two things:
#   1. every prompt records "epoch cwd" as the bash activity signal (no process spawned)
#   2. `tt` runs tt.py:  tt [status] | tt stop | tt report [range] | tt issue | tt log
#
# Works on macOS's bash 3.2, which has no $EPOCHSECONDS: the epoch is computed once at load and
# advanced with $SECONDS, so the prompt hook never forks.

[ -n "${_TT_HOME:-}" ] && return 0   # already loaded (sourced from both profile and rc)
_TT_HOME=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
_TT_EPOCH0=$(( $(date +%s) - SECONDS ))
[ -d ~/.local/state/tt ] || mkdir -p ~/.local/state/tt

_tt_beat() { printf '%s %s\n' "$(( _TT_EPOCH0 + SECONDS ))" "$PWD" 2>/dev/null >| ~/.local/state/tt/beat-bash; }
case ";${PROMPT_COMMAND:-};" in
  *";_tt_beat;"*) ;;
  *) PROMPT_COMMAND="_tt_beat${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac

tt() { /usr/bin/python3 "$_TT_HOME/tt.py" "$@"; }
