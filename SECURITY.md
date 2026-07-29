# Security

747 Terminal Games is a Claude Code plugin. It runs on your machine, so here is
exactly what it does and does not do.

## What it does
- Splits a **tmux pane** in your current window and runs a Python (`curses`) game in it.
- Writes small runtime files under `~/.747-terminal-games/` (or `$BREAKOUT747_STATE`):
  a one-word `state-<session>` file (`thinking`/`idle`/`end`), a `mode` file
  (`ask`/`auto`/`off`), a one-word `game` file naming which title auto-opens, and
  per-session `declined-<session>` markers.
- Keeps one local play-stats file per title — `stats-breakout.json`,
  `stats-skyrun.json`, `stats-jetwash.json`, `stats-astros.json` — holding counts
  and personal bests only (runs, distance, score, chamber/wave reached). They
  **stay on your disk**: nothing reads them but the game, and there is no code path
  that sends them anywhere. Print SKYRUN's with
  `python3 games/skyrun.py --export-stats`; turn stats off for good with
  `touch ~/.747-terminal-games/no-stats`, after which none of the four is ever
  written or read again.
- Registers three hooks (UserPromptSubmit / Stop / SessionEnd) that call
  `hooks/breakout-hook.sh` to open, pause, and close the game.

## What it does NOT do
- **No network.** Zero outbound calls, no telemetry, no analytics. Play stats are
  local-only and never leave the machine; sharing them is a manual copy, by you.
  CI fails the build if a network reference so much as appears in `games/` or `hooks/`.
- **No dependencies.** Pure Python standard library + bash + tmux. Nothing to
  supply-chain-attack.
- **No secrets, no credentials, no environment scraping.** It never reads your
  API keys, tokens, or files outside its own state directory.
- **No `eval`/`exec` of external data.** State files are compared as fixed strings.

## Hardening notes
- `session_id` (supplied by Claude Code in the hook event) is stripped to
  `[A-Za-z0-9-]` before it is ever used in a shell command or a file path, so a
  hostile value cannot break shell quoting or traverse the filesystem. Claude
  Code session ids are UUIDs, so this is defense-in-depth.
- Runtime state files are garbage-collected after 12h and deleted on session end.

## Reporting
Found something? Open an issue (no sensitive details in public) or email the maintainer.
