# Contributing

Thanks for looking. This is a small project with a narrow scope, so a quick orientation:

## What this is

A Claude Code plugin that opens a terminal game while the model is working and gets out of
the way the moment it replies. Two games so far, both pure Python `curses`.

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

## Before you open a PR

```
python -m compileall games/
bash -n hooks/breakout-hook.sh
```

Then actually play it in a tmux pane — start Claude Code, send a prompt, and watch the
open / pause / resume / exit cycle end to end. Most bugs here only show up live.

## Reporting something

Issues are welcome, especially "it did nothing on my setup" reports — include your terminal,
whether you were in tmux, and your OS. Security-relevant findings: see `SECURITY.md`.
