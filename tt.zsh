# tt — automatic per-issue time tracking. The logic lives in tt.py, next to this file.
#
# Sourced from ~/.zshrc by install.sh. It does two things:
#   1. every prompt records "epoch cwd" as the zsh activity signal (no process spawned)
#   2. `tt` runs tt.py:  tt [status] | tt stop | tt report [range] | tt issue

typeset -g _TT_HOME=${0:A:h}

zmodload zsh/datetime
[[ -d ~/.local/state/tt ]] || mkdir -p ~/.local/state/tt

_tt_beat() { print -r -- "$EPOCHSECONDS $PWD" >| ~/.local/state/tt/beat-zsh 2>/dev/null }
autoload -Uz add-zsh-hook
add-zsh-hook precmd _tt_beat

tt() { /usr/bin/python3 "$_TT_HOME/tt.py" "$@" }
