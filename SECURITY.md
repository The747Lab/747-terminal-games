# Security

747 Terminal Games is a Claude Code plugin. It runs on your machine, so here is
exactly what it does and does not do.

## What it does
- Splits a **tmux pane** in your current window and runs a Python (`curses`) game in it.
- Writes small runtime files under `~/.747-terminal-games/` (or `$BREAKOUT747_STATE`):
  a one-word `state-<session>` file (`thinking`/`idle`/`end`), a `mode` file
  (`ask`/`auto`/`off`), and per-session `declined-<session>` markers.
- Registers three hooks (UserPromptSubmit / Stop / SessionEnd) that call
  `hooks/breakout-hook.sh` to open, pause, and close the game.

## What it does NOT do
- **No network.** Zero outbound calls, no telemetry, no analytics.
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
