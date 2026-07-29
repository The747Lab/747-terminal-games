# Contributing

Thanks for looking. This is a small project with a narrow scope, so a quick orientation:

## What this is

A Claude Code plugin that opens a terminal game while the model is working and gets out of
the way the moment it replies. Four titles so far — BREAK-IN, SKYRUN, JETWASH and
ASTROS — all pure Python `curses`, all running on Python 3.9 through 3.13.

## Ground rules

- **No dependencies.** Standard library only, in both the games and the hooks. The empty
  dependency tree is a feature — it is what makes this safe to install on a whim.
- **No network calls.** Nothing phones home. CI fails the build if a network reference
  appears in `games/` or `hooks/`.
- **The game never gets in the way.** It opens in place, it vanishes when Claude replies,
  and it is always one keypress from gone. Anything that interrupts, nags, or lingers is a
  bug, not a feature.
- **No flashing.** Full-field strobe effects are out — these panes run in peripheral vision.

## Adding a game

Each title lives in `games/` and follows the same contract:

- reads `--session` and honours the state file (`thinking` / `idle` / `end`)
- pauses when the state says `idle`, resumes on `thinking`, exits on `end`
- deletes its own state file on exit
- renders sanely from about 8 rows up, and degrades on a 16-colour terminal
- sets a **session-keyed** OSC-2 pane title, `<TITLE>747-<session>` — the hook matches
  that exact string to banish, rejoin and close the pane. Without the session suffix
  none of that can find it, and the pane leaks into a hidden window.
- is added to `GAMES` in `hooks/breakout-hook.sh` (that list is also the whitelist),
  to `CATALOGUE` in `games/breakout.py` (the picker — display name carries **no**
  "747" suffix; the separator goes in as the token `{sep}` so the ASCII fallback
  still works), to a `commands/<title>.md` slash command, and to the headless
  launch loop in `.github/workflows/ci.yml`

CI enforces the first four of those against the contents of `games/`, so a title that
is on disk but missing from any one of them fails the build rather than shipping half
wired. Display names are checked for a stray "747" in the same step.

## Before you open a PR

```
python -m compileall games/
bash -n hooks/breakout-hook.sh
env 747_ASCII=1 python3 games/<title>.py --free    # the mono/ASCII fallback
```

(`747_ASCII=1 python3 …` on its own will not work — a shell variable name cannot start
with a digit, so it needs `env`. `LAB747_ASCII=1` is the same switch, spelled typeably.)

Then actually play it in a tmux pane — start Claude Code, send a prompt, and watch the
open / pause / resume / exit cycle end to end. Most bugs here only show up live.

## Reporting something

Issues are welcome, especially "it did nothing on my setup" reports — include your terminal,
whether you were in tmux, and your OS. Security-relevant findings: see `SECURITY.md`.
