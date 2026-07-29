#!/bin/bash
# breakout-hook.sh <thinking|idle|end|toggle> [title]
# Claude Code plugin bridge for 747 Terminal Games.
#   thinking (UserPromptSubmit) -> game runs; opens the pane if appropriate
#   idle     (Stop)             -> game pauses
#   end      (SessionEnd)       -> game exits, pane closes, state cleaned up
#   toggle   (/breakin, /skyrun, /jetwash, /astros, /jaywalk) -> open free-play pane / close it
# Per-session state files keyed by session_id so parallel sessions never fight.
set -u

STATE_DIR="${BREAKOUT747_STATE:-$HOME/.747-terminal-games}"
mkdir -p "$STATE_DIR"
EV="${1:-}"

# ${CLAUDE_PLUGIN_ROOT} is set by Claude Code when a plugin hook runs. The manual
# toggle path (run from a shell) falls back to this script's own location.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# --- which title -------------------------------------------------------------
# Every shipped title. This list IS the whitelist: the chosen title flows into a
# file path and into a shell exec string, so it is never taken on trust — same
# threat model as session_id below.
GAMES="breakout skyrun jetwash astros jaywalk"
GAMES_RE="$(printf '%s' "$GAMES" | tr ' ' '|')"
valid_game() { case " $GAMES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# A slash command names its title explicitly ($2). Auto-launch reads the one-word
# file ~/.747-terminal-games/game (absent/garbage -> breakout). It deliberately
# does not match the state-*/declined-* GC globs below.
TITLE="${2:-}"
# Guarded with -r on purpose: `< missing_file` fails BEFORE a 2>/dev/null on the same
# command is applied, so the shell would print a redirect error on every single prompt
# for every user who has not chosen a title (i.e. the default).
[ -z "$TITLE" ] && [ -r "$STATE_DIR/game" ] && TITLE=$(tr -d '[:space:]' < "$STATE_DIR/game")
valid_game "$TITLE" || TITLE=breakout
GAME="$PLUGIN_ROOT/games/$TITLE.py"

IN=""
SID=""
if [ "$EV" = "toggle" ]; then
  # A SLASH COMMAND GETS NO HOOK PAYLOAD, so it has no session_id on stdin —
  # and that was the bug: a toggled pane was keyed "free" while every real hook
  # event was keyed by session id, so idle never ghosted it, end never signalled
  # it, and the pane + the game process outlived the Claude session. Exactly the
  # "extra tab you have to close" this plugin exists to prevent.
  # Claude Code exports the id to command shells; fall back to "free" (below)
  # on any version that does not, where the alias in ALL_TITLES still finds it.
  SID="${CLAUDE_CODE_SESSION_ID:-}"
else
  IN=$(cat 2>/dev/null || true)
  SID=$(printf '%s' "$IN" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")' 2>/dev/null)
fi
# Harden: session_id flows into a shell exec string (open_pane) AND file paths.
# Strip to a safe charset so a hostile value can never break quoting or traverse
# the filesystem. Claude Code session ids are UUIDs, so this is defense-in-depth.
SID="${SID//[^A-Za-z0-9-]/}"

# ONE KEY, USED EVERYWHERE: the state file, the OSC-2 pane title, the game's
# --session argument and the pkill pattern. They used to disagree three ways.
KEY="${SID:-free}"
STATE_FILE="$STATE_DIR/state-$KEY"

# --- pane identity -----------------------------------------------------------
# Each game sets its own OSC-2 pane title at startup (BREAKOUT747-<sid>,
# SKYRUN747-<sid>, ...). Every lookup below matches ALL of them, not just the
# currently selected title: a pane banished under the old title must still be
# found, rejoined and killed, or it leaks into the hidden window forever — which
# is exactly the "extra tab you have to close" this plugin exists to prevent.
osc_title() { printf '%s747-%s' "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')" "${2:-$KEY}"; }
PANE_TITLE="$(osc_title "$TITLE")"
ALL_TITLES=""
for g in $GAMES; do
  ALL_TITLES="$ALL_TITLES $(osc_title "$g")"
  # ...and the "free" alias. On a Claude Code that does not export
  # CLAUDE_CODE_SESSION_ID, a toggled pane is keyed "free"; without this alias
  # nothing would ever find it again and it would leak into the hidden window
  # forever. Matching it costs nothing when the primary key is a session id.
  [ "$KEY" != "free" ] && ALL_TITLES="$ALL_TITLES $(osc_title "$g" free)"
done

TGT="${TMUX_PANE:+-t $TMUX_PANE}"
win_panes() { tmux list-panes $TGT -F '#{pane_id} #{pane_title}' 2>/dev/null; }
any_panes() { tmux list-panes -a -F '#{pane_id} #{pane_title}' 2>/dev/null; }
ours_id() {   # stdin: "<pane_id> <pane_title>" lines -> first pane of ANY of our titles
  awk -v tl="$ALL_TITLES" \
    'BEGIN{n=split(tl,a," ");for(i=1;i<=n;i++)if(a[i]!="")w[a[i]]=1} $2 in w {print $1; exit}'
}
this_id() {   # same, but only the title we were asked about
  awk -v t="$PANE_TITLE" '$2==t{print $1; exit}'
}

open_pane() {  # $1 = extra flags for the game. Ghost-pane aware: a banished run rejoins.
  [ -z "${TMUX:-}" ] && return 1
  # one game pane per session, whichever title it is
  [ -n "$(win_panes | ours_id)" ] && return 0
  # banished run waiting in the hidden window? rejoin it — same process, same score.
  local hp
  hp=$(any_panes | ours_id)
  if [ -n "$hp" ]; then
    # -l 16 can be refused in an already-divided window; fall back to tmux's own
    # default size rather than dropping through and spawning a SECOND game.
    tmux join-pane -d -v -l 16 -s "$hp" $TGT 2>/dev/null && return 0
    tmux join-pane -d -v -s "$hp" $TGT 2>/dev/null && return 0
  fi
  # Size to the space we actually have: a fixed 16 rows makes tmux REFUSE the split
  # in an already-divided window (common — grids, cockpits), and the game would
  # silently never open. Take half, capped at 16, floored at 8; retry at tmux's
  # own default if even that is refused.
  local ph want np cmd
  ph=$(tmux display-message -p $TGT '#{pane_height}' 2>/dev/null); [ -z "$ph" ] && ph=24
  want=$(( ph / 2 )); [ "$want" -gt 16 ] && want=16; [ "$want" -lt 8 ] && want=8
  # printf %q, not hand-rolled single quotes: $STATE_DIR and $GAME come from the
  # install path, and ONE APOSTROPHE in it (/Users/O'Brien/...) broke the quoting
  # so tmux split-window failed with no error, no fallback and no message — the
  # plugin just silently never opened. The iTerm2 path below always did this
  # correctly; this one did not.
  cmd="exec env BREAKOUT747_STATE=$(printf '%q' "$STATE_DIR") python3 $(printf '%q' "$GAME") $1 --session $(printf '%q' "$KEY")"
  np=$(BREAKOUT747_STATE="$STATE_DIR" tmux split-window -d -P -F '#{pane_id}' -v -l "$want" $TGT "$cmd" 2>/dev/null) \
    || np=$(BREAKOUT747_STATE="$STATE_DIR" tmux split-window -d -P -F '#{pane_id}' -v $TGT "$cmd" 2>/dev/null) \
    || return 1
  [ -n "$np" ] && tmux select-pane -t "$np" -T "$PANE_TITLE" 2>/dev/null
}

game_running() {  # duplicate guard for the windowed fallback (one game per session key)
  pgrep -f "($GAMES_RE)\.py.*--session $KEY" >/dev/null 2>&1
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
  cmd="clear; BREAKOUT747_STATE=$(printf '%q' "$STATE_DIR") python3 $(printf '%q' "$GAME") $1 --session $(printf '%q' "$KEY"); exit"
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

case "$EV" in
  idle)
    echo idle > "$STATE_FILE"
    # GHOST PANE: the game VANISHES the instant Claude replies — terminal back to
    # normal, run kept alive (paused) in a hidden window; rejoins on next prompt.
    if [ -n "${TMUX:-}" ]; then
      p=$(win_panes | ours_id)
      [ -n "$p" ] && tmux break-pane -d -s "$p" -n B747BG 2>/dev/null
    fi
    exit 0 ;;
  end)
    # Signal exit; the game deletes its own state file as it quits. Only remove
    # the declined marker here (no running game owns it).
    echo end > "$STATE_FILE"
    rm -f "$STATE_DIR/declined-$KEY" 2>/dev/null
    # BELT AND BRACES for the "free" fallback (see ALL_TITLES): if a pane keyed
    # `free` is ours — i.e. it is one of our titles and it is in this window, or
    # already banished to the hidden window — signal it too, or its process
    # outlives the session with nothing left to tell it to quit.
    # SessionEnd must be fast, so this is ONE tmux call and one awk pass.
    if [ "$KEY" != "free" ] && [ -n "${TMUX:-}" ]; then
      FREE_TITLES=""
      for g in $GAMES; do FREE_TITLES="$FREE_TITLES $(osc_title "$g" free)"; done
      if any_panes | awk -v tl="$FREE_TITLES" \
           'BEGIN{n=split(tl,a," ");for(i=1;i<=n;i++)if(a[i]!="")w[a[i]]=1}
            $2 in w {f=1} END{exit !f}'; then
        echo end > "$STATE_DIR/state-free"
      fi
    fi
    exit 0 ;;
  toggle)
    if [ -n "${TMUX:-}" ]; then
      vis=$(win_panes)
      ex=$(printf '%s\n' "$vis" | this_id)
      if [ -n "$ex" ]; then
        tmux kill-pane -t "$ex"            # same title showing -> close it
      else
        other=$(printf '%s\n' "$vis" | ours_id)
        [ -n "$other" ] && tmux kill-pane -t "$other" 2>/dev/null  # swap titles, never stack two
        open_pane --free
      fi
    else
      if game_running; then pkill -f "($GAMES_RE)\.py.*--session $KEY" 2>/dev/null
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
[ -f "$STATE_DIR/declined-$KEY" ] && exit 0
FLAGS=""
[ "$MODE" = "ask" ] && FLAGS="--ask"
open_game "$FLAGS"
exit 0
