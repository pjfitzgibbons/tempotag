# tt

Automatic per-issue time tracking on macOS, on top of [Timewarrior](https://timewarrior.net). You don't start or stop timers. tt watches where you're working and whether you're at the keyboard, and keeps Timewarrior's open interval tagged with the GitHub issue you're on.

## How it decides

- **Where:** three signals, newest wins.
  - **Shell:** every zsh or bash prompt records the current directory.
  - **Claude Code:** every prompt you send records the session's directory.
  - **Editor:** the frontmost VS Code, Zed, JetBrains or Xcode window, matched by its title against repos tt has already seen.
- **Whether:** keyboard and mouse idle time. Ten minutes idle stops the interval, backdated to your last input. If the Mac sleeps, the interval stops at the last tick. Activity resumes it.
- **Which issue:** the branch name. `42-fix-login` or `feat/42-fix-login` tags issue #42.

Each interval is tagged `owner/repo#42`, `repo:owner/repo` and `project:NAME`.

## Requirements

- macOS, with `/usr/bin/python3` (`xcode-select --install` provides it). tt uses only the standard library.
- Timewarrior: `brew install timewarrior`
- zsh or bash (macOS's bash 3.2 works). The Claude Code hook is optional, and is added only if `~/.claude` exists.

## Install

```sh
git clone <url> ~/tools/tt      # or unzip tt.zip anywhere you'll keep it
cd ~/tools/tt
./install.sh --dry-run          # shows what it would change
./install.sh
```

tt runs from wherever you put it. The installer links everything to that directory, so `git pull` there upgrades it, and moving it means running `./install.sh` again. It changes three things, and each step is skipped if already done:

1. **Shell startup files:** one line that sources `tt.zsh` in `~/.zshrc`, and `tt.bash` in `~/.bash_profile` and `~/.bashrc`. It hooks each one that exists, plus your login shell's if it doesn't. macOS Terminal starts bash as a login shell, which reads `.bash_profile`; loading the hook twice is harmless.
2. **`~/Library/LaunchAgents/local.tt.tick.plist`:** runs `tt.py tick` every minute.
3. **`~/.claude/settings.json`:** a `UserPromptSubmit` hook. The file is backed up to `settings.json.bak-tt` first.

The first time it runs, macOS asks whether `python3` may control "System Events". Allow it, or the editor signal stays off (Settings → Privacy & Security → Automation).

## Set up a repo

tt only tracks repos you've opted in:

```sh
git config tt.project my-project
```

A repo without that key stops the timer when you work in it, and shows one notification saying how to set it up. tt also reads the older key `lanes.project`, so repos set up before the rename keep tracking. To move one over: `git config tt.project "$(git config lanes.project)" && git config --unset lanes.project`.

## Use

```sh
tt              # status: what's tracking, idle time, last tick, each signal
tt stop         # pause until your next command, Claude prompt or editor switch
tt report       # this week, per issue per day, in 30-minute blocks (12+ min remainder rounds up)
tt report :month
tt issue        # the tags tt would use here
```

`tt report` rounds into billing blocks. The raw intervals in Timewarrior stay exact (`timew summary :ids`).

## Correcting time

- Time worked before a branch existed lands on the repo with no issue tag. Fix it with `timew tag @ID 'owner/repo#42'`.
- Quote the tag. With zsh's `extendedglob` on, an unquoted `#` is a glob.

## Uninstall

```sh
./uninstall.sh
```

This removes the shell lines, the launchd agent and the Claude hook, and backs up each file it edits. Timewarrior's data and `~/.local/state/tt` are left alone.

## Tests

```sh
/usr/bin/python3 -m unittest test_tt
```
