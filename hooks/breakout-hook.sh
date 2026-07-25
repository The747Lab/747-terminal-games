#!/bin/bash
# breakout-hook.sh <thinking|idle|end|toggle>
# Claude Code plugin bridge for 747 Terminal Games.
#   thinking (UserPromptSubmit) -> game runs; opens the pane if appropriate
#   idle     (Stop)             -> game pauses
#   end      (SessionEnd)       -> game exits, pane closes, state cleaned up
#   toggle   (/breakout command)-> open free-play pane / close it
# Per-session state files keyed by session_id so parallel sessions never fight.
set -u

STATE_DIR="${BREAKOUT747_STATE:-$HOME/.747-terminal-games}"
mkdir -p "$STATE_DIR"
EV="${1:-}"

# ${CLAUDE_PLUGIN_ROOT} is set by Claude Code when a plugin hook runs. The manual
# toggle path (run from a shell) falls back to this script's own location.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
GAME="$PLUGIN_ROOT/games/breakout.py"

IN=""
[ "$EV" != "toggle" ] && IN=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$IN" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")' 2>/dev/null)
# Harden: session_id flows into a shell exec string (open_pane) AND file paths.
# Strip to a safe charset so a hostile value can never break quoting or traverse
# the filesystem. Claude Code session ids are UUIDs, so this is defense-in-depth.
SID="${SID//[^A-Za-z0-9-]/}"

STATE_FILE="$STATE_DIR/state${SID:+-$SID}"

open_pane() {  # $1 = extra flags for the game
  [ -z "${TMUX:-}" ] && return 1
  local tgt="${TMUX_PANE:+-t $TMUX_PANE}"
  # already open in this window?
  if tmux list-panes $tgt -F '#{pane_title}' 2>/dev/null | grep -q '^BREAKOUT747$'; then
    return 0
  fi
  local np
  np=$(BREAKOUT747_STATE="$STATE_DIR" tmux split-window -d -P -F '#{pane_id}' -v -l 16 $tgt \
    "exec env BREAKOUT747_STATE='$STATE_DIR' python3 '$GAME' $1 --session '$SID'" 2>/dev/null) || return 1
  [ -n "$np" ] && tmux select-pane -t "$np" -T BREAKOUT747 2>/dev/null
}

case "$EV" in
  idle) echo idle > "$STATE_FILE"; exit 0 ;;
  end)
    # Signal exit; the game deletes its own state file as it quits. Only remove
    # the declined marker here (no running game owns it).
    echo end > "$STATE_FILE"
    rm -f "$STATE_DIR/declined-$SID" 2>/dev/null
    exit 0 ;;
  toggle)
    tgt="${TMUX_PANE:+-t $TMUX_PANE}"
    ex=$(tmux list-panes $tgt -F '#{pane_id} #{pane_title}' 2>/dev/null | awk '$2=="BREAKOUT747"{print $1; exit}')
    if [ -n "$ex" ]; then tmux kill-pane -t "$ex"; else open_pane --free; fi
    exit 0 ;;
  thinking)
    echo thinking > "$STATE_FILE"
    # GC dead-session runtime files (>12h old) so state-* never piles up
    find "$STATE_DIR" -maxdepth 1 -type f \( -name 'state-*' -o -name 'declined-*' \) \
      -mmin +720 -delete 2>/dev/null || true
    ;;
  *) exit 0 ;;
esac

# --- launch logic (thinking only) ---
MODE=$(cat "$STATE_DIR/mode" 2>/dev/null | tr -d '[:space:]'); [ -z "$MODE" ] && MODE=ask
[ "$MODE" = "off" ] && exit 0
[ -n "$SID" ] && [ -f "$STATE_DIR/declined-$SID" ] && exit 0
FLAGS=""
[ "$MODE" = "ask" ] && FLAGS="--ask"
open_pane "$FLAGS"
exit 0
