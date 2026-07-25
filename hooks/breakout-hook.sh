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

open_pane() {  # $1 = extra flags for the game. Ghost-pane aware: a banished run rejoins.
  [ -z "${TMUX:-}" ] && return 1
  local tgt="${TMUX_PANE:+-t $TMUX_PANE}"
  local title="BREAKOUT747-${SID:-free}"
  # already visible in this window?
  if tmux list-panes $tgt -F '#{pane_title}' 2>/dev/null | grep -qx "$title"; then
    return 0
  fi
  # banished run waiting in the hidden window? rejoin it — same process, same score.
  local hp
  hp=$(tmux list-panes -a -F '#{pane_id} #{pane_title}' 2>/dev/null \
       | awk -v t="$title" '$2==t{print $1; exit}')
  if [ -n "$hp" ]; then
    tmux join-pane -d -v -l 16 -s "$hp" $tgt 2>/dev/null && return 0
  fi
  # Size to the space we actually have: a fixed 16 rows makes tmux REFUSE the split
  # in an already-divided window (common — grids, cockpits), and the game would
  # silently never open. Take half, capped at 16, floored at 8; retry at tmux's
  # own default if even that is refused.
  local ph want np cmd
  ph=$(tmux display-message -p $tgt '#{pane_height}' 2>/dev/null); [ -z "$ph" ] && ph=24
  want=$(( ph / 2 )); [ "$want" -gt 16 ] && want=16; [ "$want" -lt 8 ] && want=8
  cmd="exec env BREAKOUT747_STATE='$STATE_DIR' python3 '$GAME' $1 --session '$SID'"
  np=$(BREAKOUT747_STATE="$STATE_DIR" tmux split-window -d -P -F '#{pane_id}' -v -l "$want" $tgt "$cmd" 2>/dev/null) \
    || np=$(BREAKOUT747_STATE="$STATE_DIR" tmux split-window -d -P -F '#{pane_id}' -v $tgt "$cmd" 2>/dev/null) \
    || return 1
  [ -n "$np" ] && tmux select-pane -t "$np" -T "$title" 2>/dev/null
}

game_running() {  # duplicate guard for the windowed fallback (one game per session key)
  pgrep -f "breakout\.py.*--session ${SID:-free}" >/dev/null 2>&1
}

as_escape() {  # make a shell command safe inside an AppleScript double-quoted string
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

open_iterm_split() {  # no tmux, but iTerm2: split the CURRENT window natively — same
  # in-place feel as a tmux pane. (A separate window would defeat the whole point:
  # the game replaces the wait IN PLACE, or it doesn't open at all.)
  [ "${TERM_PROGRAM:-}" = "iTerm.app" ] || return 1
  game_running && return 0
  local cmd esc
  cmd="clear; BREAKOUT747_STATE=$(printf '%q' "$STATE_DIR") python3 $(printf '%q' "$GAME") $1 --session $(printf '%q' "${SID:-free}"); exit"
  esc=$(as_escape "$cmd")
  osascript -e 'tell application "iTerm"' \
            -e 'tell current session of current window' \
            -e 'set newSession to (split horizontally with default profile)' \
            -e 'end tell' \
            -e "tell newSession to write text \"$esc\"" \
            -e 'end tell' >/dev/null 2>&1
}

open_game() {  # in-place only: tmux pane, or iTerm2 native split. Never a new window.
  open_pane "$1" || open_iterm_split "$1"
}

bg_win() {  # the hidden window holding this session's banished game pane, if any
  tmux list-panes -a -F '#{window_id} #{pane_title}' 2>/dev/null \
    | awk -v t="BREAKOUT747-${SID:-free}" '$2==t{print $1; exit}'
}

case "$EV" in
  idle)
    echo idle > "$STATE_FILE"
    # GHOST PANE: the game VANISHES the instant Claude replies — terminal back to
    # normal, run kept alive (paused) in a hidden window; rejoins on next prompt.
    if [ -n "${TMUX:-}" ]; then
      tgt="${TMUX_PANE:+-t $TMUX_PANE}"
      p=$(tmux list-panes $tgt -F '#{pane_id} #{pane_title}' 2>/dev/null \
          | awk -v t="BREAKOUT747-${SID:-free}" '$2==t{print $1; exit}')
      [ -n "$p" ] && tmux break-pane -d -s "$p" -n B747BG 2>/dev/null
    fi
    exit 0 ;;
  end)
    # Signal exit; the game deletes its own state file as it quits. Only remove
    # the declined marker here (no running game owns it).
    echo end > "$STATE_FILE"
    rm -f "$STATE_DIR/declined-$SID" 2>/dev/null
    exit 0 ;;
  toggle)
    if [ -n "${TMUX:-}" ]; then
      tgt="${TMUX_PANE:+-t $TMUX_PANE}"
      ex=$(tmux list-panes $tgt -F '#{pane_id} #{pane_title}' 2>/dev/null \
           | awk -v t="BREAKOUT747-${SID:-free}" '$2==t{print $1; exit}')
      if [ -n "$ex" ]; then tmux kill-pane -t "$ex"; else open_pane --free; fi
    else
      if game_running; then pkill -f "breakout\.py.*--session ${SID:-free}" 2>/dev/null
      else open_iterm_split --free; fi
    fi
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
open_game "$FLAGS"
exit 0
