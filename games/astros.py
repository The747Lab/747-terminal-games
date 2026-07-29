#!/usr/bin/env python3
"""ASTROS — terminal invaders that runs in a tmux pane while Claude thinks.

Seven waves, then the big one. Auto-pauses the instant Claude replies (the Stop
hook writes 'idle'), resumes on your next prompt ('thinking'), and exits when
the session ends. Stays alive while ghost-paned, so one run can survive a whole
conversation.

The 747 is IN the game, never on the label: the mystery ship pays 747, the combo
caps at x7, and wave 7 is a jumbo-jet fleet worth 7470.

Zero dependencies: Python 3 stdlib + curses. Runs on 3.9 through 3.13.

Developed by The 747 Lab.
"""
import argparse
import curses
import json
import math
import os
import random
import sys
import time

STATE_DIR = os.environ.get("BREAKOUT747_STATE") or os.path.expanduser("~/.747-terminal-games")
# Set once in __main__ (module scope, so no `global` needed), read by
# back_to_menu(). It is the ONLY safe source of the flags THIS process was
# launched with: game.manual_play is not it, because [space] toggles that, so a
# paused-then-resumed run would hand the picker a --free it never had.
LAUNCH_ARGS = None
TICK = 0.033          # ~30 fps render cadence
STATE_POLL = 0.1      # re-read the state file 10x/s — playing AND idle (seamless contract)
POLL_IDLE = 0.1       # sleep between idle polls: 'end' is honoured in <= ~0.2s
ASK_TIMEOUT = 45      # ask screen auto-closes after this many seconds

FIXED_DT = 1.0 / 60.0  # fixed-timestep accumulator: the sim never varies with frame rate
DT_MAX = 0.05          # a stalled pane may never teleport the world past a hazard
DT_REJOIN = 0.35       # bigger gap than this => a ghost-pane return: skip the sim frame
MAX_SUBSTEPS = 4

MIN_W, MIN_H = 40, 8   # below this we print the notice and idle politely — never crash


# ---------------------------------------------------------------------------
# state protocol — byte-identical to breakout.py's, because the hook matches it
# ---------------------------------------------------------------------------
def state_path(session):
    # guard: never let a session value traverse out of STATE_DIR (defense-in-depth)
    safe = "".join(c for c in session if c.isalnum() or c == "-")
    return os.path.join(STATE_DIR, f"state-{safe}" if safe else "state")


def read_state(session):
    try:
        with open(state_path(session)) as f:
            return f.read().strip()
    except OSError:
        return "thinking"


def write_mode(mode):
    with open(os.path.join(STATE_DIR, "mode"), "w") as f:
        f.write(mode + "\n")


def remove_state(session):
    try:
        os.remove(state_path(session))
    except OSError:
        pass


def set_pane_title(session=""):
    # OSC 2 sets the tmux pane title so the launcher can detect a live game.
    # Session-keyed so the ghost-pane banish/rejoin can find THIS session's run.
    # THE EMITTED STRING IS FROZEN: the hook derives it from the FILE key, never
    # from the display name. Change it and every previously-opened pane leaks.
    sys.stdout.write(f"\033]2;ASTROS747-{session or 'free'}\033\\")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# persisted stats — LOCAL ONLY. There is no code path that could transmit this,
# and CI proves it (the no-network grep covers this whole directory).
# ---------------------------------------------------------------------------
STATS_DEFAULT = {"v": 1, "best_stage": 0, "best_score": 0, "runs": 0, "cleared": False, "eggs": 0}


def stats_enabled():
    """Opt out by touching ~/.747-terminal-games/no-stats — silently, both ways."""
    return not os.path.exists(os.path.join(STATE_DIR, "no-stats"))


def load_stats():
    if not stats_enabled():
        return dict(STATS_DEFAULT)
    out = dict(STATS_DEFAULT)
    try:
        with open(os.path.join(STATE_DIR, "stats-astros.json")) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in ("best_stage", "best_score", "runs", "eggs"):
                if isinstance(raw.get(k), int):
                    out[k] = raw[k]
            out["cleared"] = bool(raw.get("cleared", False))
    except Exception:
        pass  # absent or corrupt -> zeros, never a traceback
    return out


def save_stats(st):
    """Atomic (tmp + os.replace), and only ever on run end — never per frame."""
    if not stats_enabled():
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, "stats-astros.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# glyphs — THE SHAPE LAW. One silhouette family per role, so every object is
# readable from shape alone before colour. Full ASCII fallback: on a non-UTF-8
# terminal (or with 747_ASCII=1) every class stays unambiguous by glyph.
# ---------------------------------------------------------------------------
G = {
    "invader": "▼▼",       # ▼▼  solid, 2 cells, in a 4-column cell
    "diver": "▽",               # ▽   OPEN triangle — a different SHAPE, not a brighter one
    "mystery": "◄▓►",  # ◄▓► the only gold thing on screen
    "player": "▲",              # ▲   always the bottom row: position disambiguates it
    "shot": "│",                # │   thin vertical, travelling up
    "bomb": "!",                     # !   used for NOTHING else in the entire game
    "bunker": "▓▒░",  # ▓▒░ erosion grammar, same as BREAK-IN's hatches
    "boss": "█",                # █   the 5-row bitmap font the whole line speaks
    "core": "▓▒░",    # ▓▒░ armoured cores crack as you hit them
    "debris": "·",              # ·
    "pip_on": "▰",              # ▰
    "pip_off": "▱",             # ▱
    "life": "▲",                # ▲
    "tick": "▌",                # ▌   the studio tick
    "sep": "·",                 # ·
    "pause": "⏸",               # ⏸
    "dash": "—",                # —
    "arrows": "←→",        # ←→
    "times": "×",               # ×
    "keys": " ←→ · [space] fire · [q] quit ",
    "teach": "← → move   [space] fire   dodge the !",
}

ASCII_G = {
    "invader": "vv", "diver": "V", "mystery": "<#>", "player": "^",
    "shot": "|", "bomb": "!", "bunker": "#*:", "boss": "#", "core": "#*:",
    "debris": ".", "pip_on": "|", "pip_off": ".", "life": "^", "tick": "|",
    "sep": "-", "pause": "||", "dash": "-", "arrows": "<>", "times": "x",
    "keys": " <> - [space] fire - [q] quit ",
    "teach": "<- -> move   [space] fire   dodge the !",
}


def use_ascii():
    """Honest degradation, not mojibake. Triggered by a non-UTF-8 stdout or by
    747_ASCII=1 in the environment (which is how the mono test is run in CI)."""
    # `747_ASCII=1 cmd` is not settable from sh/bash/zsh at all — a shell
    # identifier may not start with a digit, so it needs `env 747_ASCII=1 ...`.
    # LAB747_ASCII is the same switch, spelled so a human can actually type it.
    forced = any(os.environ.get(k, "") not in ("", "0")
                 for k in ("747_ASCII", "LAB747_ASCII"))
    enc = (sys.stdout.encoding or "").lower()
    if forced or "utf" not in enc:
        G.update(ASCII_G)
        return True
    return False


# ---------------------------------------------------------------------------
# THE UNIFIED PALETTE. Every role is a same-shape list ordered
# [near/bright, mid, far, fog], so no render site ever asks "am I in 256 mode?".
# Pairs 100-139 are the reserved shared range; on an 8/16-colour terminal
# COLOR_PAIRS is often only 64, so we fall back to a low free base rather than
# silently losing all colour to init_pair() failures.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# COLOUR CAPABILITY, GUARDED — the two calls that take a pane down on a mono
# terminal, and the ONE place they are allowed to be made.
#
# On a terminal where start_color() failed (TERM=vt100, TERM=dumb — exactly the
# 16-colour/mono floor the ASCII fallback exists to serve) curses.COLOR_PAIRS is
# 0, and CPython 3.10+ raises **ValueError**, not curses.error, out of BOTH
# init_pair() and color_pair():
#     ValueError: Color pair is greater than COLOR_PAIRS-1 (-1)
# A guard that catches only curses.error is therefore a crash on 3.10-3.14 and a
# pass on 3.9. Catch both, always.
# ---------------------------------------------------------------------------
def cpair(i):
    try:
        return curses.color_pair(i)
    except (curses.error, ValueError):
        return curses.A_NORMAL


def ipair(i, fg, bg=-1):
    try:
        curses.init_pair(i, fg, bg)
    except (curses.error, ValueError):
        return curses.A_NORMAL
    return cpair(i)


class Palette(object):
    def __init__(self):
        try:
            has256 = curses.COLORS >= 256
        except (curses.error, AttributeError):
            has256 = False
        try:
            base = 100 if curses.COLOR_PAIRS > 140 else 20
        except (curses.error, AttributeError):
            base = 20
        self._next = base

        if has256:
            self.star = [self.mk(255, bold=True), self.mk(250), self.mk(244), self.mk(238)]
            self.hazard = [self.mk(210, bold=True), self.mk(174), self.mk(96), self.mk(60)]
            self.target = [self.mk(195, bold=True), self.mk(87), self.mk(44), self.mk(24)]
            self.pickup = [self.mk(120, bold=True), self.mk(40), self.mk(34), self.mk(22)]
            self.gold = [self.mk(226, bold=True), self.mk(220), self.mk(178), self.mk(136)]
            self.player = [self.mk(231, bold=True), self.mk(252), self.mk(246)]
            self.damage = [self.mk(203, bold=True), self.mk(160)]
            self.struct = [self.mk(250), self.mk(244), self.mk(238)]
            self.text_hi = self.mk(231, bold=True)
            self.text = self.mk(189)
            self.text_dim = self.mk(103)
            self.accent = [self.mk(141), self.mk(97), self.mk(61)]
            # THE VALUE RAMP. Colour IS value, so the 30/20/10 table needs no
            # manual: top row coolest (studio violet) -> bottom row hazard red.
            self.row = [self.mk(141, bold=True), self.mk(176), self.mk(175),
                        self.mk(174), self.mk(210, bold=True)]
        else:
            R, Y, G_, C = (curses.COLOR_RED, curses.COLOR_YELLOW,
                           curses.COLOR_GREEN, curses.COLOR_CYAN)
            M, W, B = curses.COLOR_MAGENTA, curses.COLOR_WHITE, curses.COLOR_BLUE
            red, yel, grn = self.mk(R), self.mk(Y), self.mk(G_)
            cyn, mag, wht, blu = self.mk(C), self.mk(M), self.mk(W), self.mk(B)
            D, BO = curses.A_DIM, curses.A_BOLD
            self.star = [wht, wht | D, wht | D, wht | D]
            self.hazard = [red | BO, red, red | D, red | D]      # a hazard is ALWAYS bold up close
            self.target = [cyn | BO, cyn, cyn | D, cyn | D]
            self.pickup = [grn | BO, grn, grn | D, grn | D]
            self.gold = [yel | BO, yel, yel | D, yel | D]
            self.player = [wht | BO, wht, wht | D]
            self.damage = [red | BO, red]
            self.struct = [wht | D, wht | D, wht | D]
            self.text_hi = wht | BO
            self.text = curses.A_NORMAL
            self.text_dim = curses.A_DIM
            self.accent = [mag, mag | D, mag | D]
            self.row = [mag | BO, mag, blu | BO, red, red | BO]

    def mk(self, fg, bold=False, dim=False):
        i = self._next
        self._next += 1
        a = ipair(i, fg)
        if bold:
            a |= curses.A_BOLD
        if dim:
            a |= curses.A_DIM
        return a


# ---------------------------------------------------------------------------
# THE 747 BOSS — wave 7. The same 5-row bitmap language as breakout's brick wall
# and skyrun's gate: full blocks, hand-authored, read as a jumbo from above.
# '#' is hull, 'O' is an armoured core (3 hits each, cracks ▓->▒->░).
# Kill all three cores and the sky is clear.
# ---------------------------------------------------------------------------
BOSS_BIG = [
    "..........##..........",   # nose
    ".........####.........",   # forward fuselage
    "######################",   # the wing, full span
    ".......########.......",   # aft fuselage
    "....OO....OO....OO....",   # THREE ENGINE PODS — the cores, on the leading edge
]
BOSS_SMALL = [
    ".....##.....",
    "############",
    "..OO..OO..OO",
]

# THE CORES HANG BELOW THE HULL, and that is a mechanical requirement, not a
# style choice. Your shot travels UP: any hull cell in the same column between
# you and a core absorbs the shot first, so a core tucked under a fuselage can
# never be hit at all. (Measured on the first draft, which put them on the wing
# row: 227 hull hits to 3 core hits — the boss was effectively invulnerable.)
# On the leading edge every core has clear air beneath it.
# Each core is TWO cells wide — one invader-width — so the same aiming rule that
# governs MARCH_CAP governs the boss. A 1-cell target on a moving body is a coin
# flip, not a boss. Four empty columns between pods keeps the gutter law.
# The boss tells you where to shoot without ever drawing a health bar.


def boss_cells(rows):
    """-> (hull cells set, cores list). Each core is a contiguous run of 'O' in
    one row, kept together so it erodes and is hit as ONE two-cell object."""
    hull, cores = set(), []
    for y, line in enumerate(rows):
        run = []
        for x, ch in enumerate(line):
            if ch == "#":
                hull.add((y, x))
            if ch == "O":
                run.append((y, x))
            elif run:
                cores.append(run)
                run = []
        if run:
            cores.append(run)
    return hull, cores


class Game(object):
    # ---- wave model -------------------------------------------------------
    WAVES = 7
    COMBO_WINDOW = 1.2      # a kill this soon after the last one compounds
    COMBO_CAP = 7           # the 747 number again
    SHOT_CAP = 2            # capping at 2 is what makes aiming matter; 4 is spray
    SHOT_COOLDOWN = 0.12
    SHOT_SPEED = 55.0       # rows/s, travelling up
    MYSTERY_SPEED = 24.0    # cols/s
    DIVER_SPEED = 7.0       # rows/s down

    # MARCH CAP, DERIVED — not tuned. A shot's worst-case flight is
    # (player_row - top fleet row) / SHOT_SPEED ~= 21/55 = 0.38 s. An invader is
    # 2 cells wide, so if the fleet can slide more than one sprite width during
    # that flight, a correctly aimed shot becomes a coin flip — the same failure
    # the telegraph floor exists to prevent, just on the offensive axis.
    #   2 cells / 0.38 s = 5.2 c/s  ->  MARCH_CAP 5.2
    # Raise SHOT_SPEED and this cap may rise with it. Never the other way round.
    MARCH_CAP = 5.2
    DIVER_BLINK = 0.8       # the telegraph, before it ever detaches
    CLEAR_HOLD = 1.4        # the wave-clear beat, sim frozen
    TEACH_UNTIL = 15.0      # the signposting line: zero-tutorial != zero-signposting

    # WORST-CASE TELEGRAPH: 1000 ms / full fall distance at wave 8+, bomb fall
    # capped at 14 rows/s. Guaranteed BY CONSTRUCTION, not by tuning: a bomb is
    # only ever spawned from an invader at least (fall_speed x 1.0 s) rows above
    # the player row, and its fall speed is additionally clamped to
    # distance / 1.0 s. So every bomb in the game is visible for >= 1.0 s before
    # it can reach you. Raise BOMB_FALL_CAP and the clamp keeps the floor; the
    # speed is DERIVED from the telegraph, never chosen against it.
    BOMB_FALL_CAP = 14.0
    TELEGRAPH_S = 1.0

    def __init__(self, scr, session, free, best=0):
        self.scr = scr
        self.session = session
        self.manual_play = free
        self.best = best
        self.pal = Palette()

        self.score = 0
        self.score_shown = 0.0     # counts up at 400 pts/s so you see WHICH act paid
        self.lives = 3
        self.wave = 1
        self.mult = 1
        self.last_kill = -99.0
        self.t = 0.0               # simulated run clock — never advanced by wall-clock gaps
        self.eggs = 0
        self.next_extra = 5000     # extra life at 5,000 then 15,000
        self.won = False
        self.phase = "play"
        self.phase_t = 0.0
        self.banner = ""

        # juice state (all of it reset by on_rejoin — a ghost return must never
        # resume mid-flash, mid-shake or mid-hitstop)
        self.hitstop = 0.0
        self.shake_t = 0.0
        self.shake_amp = 0
        self.flash_full = 0.0
        self.invuln = 0.0
        self.debris = []
        self.flashes = []
        self.floaters = []

        self.bunkers = {}
        self.bunkers_built = False
        self.first_key = False
        self.hint_pulse = 0.0

        self.layout()
        self.begin_wave(first=True)

    # ---- layout -----------------------------------------------------------
    def layout(self):
        """Recomputed every frame the pane changes size — a ghost cycle resizes
        this pane three or more times."""
        self.h, self.w = self.scr.getmaxyx()
        self.small = self.h < MIN_H or self.w < MIN_W
        self.pf_top = 1                       # HUD owns row 0 and row h-1. Forever.
        self.pf_bot = max(2, self.h - 2)
        self.pf_h = self.pf_bot - self.pf_top + 1
        self.player_y = self.pf_bot
        self.player_x = min(max(2, getattr(self, "player_x", self.w // 2)), self.w - 3)
        self.cell_w = 4                       # 2-cell invader + 2 empty columns, always

    def fleet_shape(self):
        """5x8 at 100x24; clamps down so the fleet always leaves the player room
        to see a bomb coming. Never fewer than 1 row / 3 columns."""
        rows = max(1, min(5, self.pf_h - 3))
        cols = max(3, min(8, (self.w - 4) // self.cell_w))
        return rows, cols

    # ---- wave construction ------------------------------------------------
    def begin_wave(self, first=False):
        rows, cols = self.fleet_shape()
        self.rows, self.cols = rows, cols
        self.dir = 1
        self.fleet_off = 0
        self.march_acc = 0.0
        self.bullets = []
        self.bombs = []
        self.divers = []
        self.diver_cd = 8.0
        self.diving = None                     # (r, c, blink_timer) — the 0.8 s telegraph
        self.mystery = None
        self.mystery_cd = random.uniform(12.0, 22.0)
        self.mystery_seen = False
        self.first_bomb_done = False
        self.shot_cd = 0.0
        self.perfect = True
        self.boss = None
        self.wave_t = 0.0

        if self.wave == self.WAVES:
            self.build_boss()
            self.fleet = set()
            self.fleet_start = 9               # 3 cores x 3 hits — the fleet bar reads cores
        else:
            self.fleet = {(r, c) for r in range(rows) for c in range(cols)}
            self.fleet_start = len(self.fleet)
            # each wave starts one row lower, capped at row 4, and never so low
            # that the fleet floor is already inside the player's space
            start = min(4, 1 + self.wave)
            self.fleet_row0 = max(self.pf_top + 1,
                                  min(start, self.player_y - 3 - rows))
            self.fleet_x0 = max(2, (self.w - cols * self.cell_w) // 2)

        # BUNKERS from wave 3 — the classic missing piece, and what makes waves
        # 5-7 survivable. They persist across waves and regenerate one cell each.
        if self.wave >= 3 and self.bunkers_fit():
            if not self.bunkers_built:
                self.build_bunkers()
            else:
                self.regen_bunkers()
        elif not self.bunkers_fit():
            self.bunkers = {}                  # no room between them and the player

    def build_boss(self):
        rows = BOSS_BIG if (self.w >= 30 and self.pf_h >= 9) else BOSS_SMALL
        hull, cores = boss_cells(rows)
        # THE BOSS BAND IS DERIVED FROM THE PANE, never assumed. Its bob must not
        # put the hull inside the player's own rows: at 80x8 the first draft did
        # exactly that, so the cores sat ON the player row (a shot spawns ABOVE
        # it and travels up, so they could never be hit) while the floor rule
        # drained a life every few seconds. head = the vertical room that
        # actually exists between the boss's parked position and the player.
        head = (self.player_y - 2 - (len(rows) - 1)) - (self.pf_top + 1)
        self.boss = {
            "rows": rows,
            "w": len(rows[0]),
            "hull": hull,
            "cores": [{"cells": run, "hp": 3} for run in cores],
            "x": (self.w - len(rows[0])) / 2.0,
            "y": float(self.pf_top + 1),
            "y0": float(self.pf_top + 1),
            "bob": min(1.2, max(0.0, head / 2.0)),
            "t": 0.0,
        }

    def bunkers_fit(self):
        """Shelters need real ROOM, not just a spare row. The two bunker rows sit
        at player_y-3 and player_y-2; if the fleet's own rows reach down into
        them the shelters stop being cover and become a wall between you and
        everything you must shoot — measured at 80x8, where they made the wave-7
        boss literally unkillable. Derived, so it holds at every pane size."""
        rows, _ = self.fleet_shape()
        return (self.player_y - 3) > (self.pf_top + 1 + rows + 1)

    def build_bunkers(self):
        """Three shelters, two rows each, with the classic doorway. Cells erode
        from whichever side is hit: 3 -> 2 -> 1 -> gone."""
        self.bunkers = {}
        shape = ["#####", "##.##"]
        y0 = self.player_y - 3
        if y0 <= self.pf_top + 1:
            return
        gap = self.w // 4
        for i in range(3):
            bx = gap * (i + 1) - 2
            if bx < 1 or bx + 5 >= self.w - 1:
                continue
            for dy, line in enumerate(shape):
                for dx, ch in enumerate(line):
                    if ch == "#":
                        self.bunkers[(y0 + dy, bx + dx)] = 3
        self.bunkers_built = True

    def regen_bunkers(self):
        """One cell back per bunker per wave clear — relief, never a reset."""
        y0 = self.player_y - 3
        shape = ["#####", "##.##"]
        gap = self.w // 4
        for i in range(3):
            bx = gap * (i + 1) - 2
            for dy, line in enumerate(shape):
                done = False
                for dx, ch in enumerate(line):
                    key = (y0 + dy, bx + dx)
                    if ch == "#" and self.bunkers.get(key, 0) < 3:
                        self.bunkers[key] = min(3, self.bunkers.get(key, 0) + 1)
                        done = True
                        break
                if done:
                    break

    # ---- wave tuning ------------------------------------------------------
    def march_speed(self):
        # scales with 12/len(fleet) so the LAST invader is always frantic —
        # then clamped to MARCH_CAP, because franticness is delivered by the
        # STEP RATE, never by making a well-aimed shot a lottery
        n = max(1, len(self.fleet))
        return min(self.MARCH_CAP, 2.0 + 1.0 * (self.wave - 1) + 12.0 / n)

    def bomb_rate(self):
        """Bombs per second (frame-rate independent — a per-frame probability
        would make the game harder on a faster terminal)."""
        return 1.2 + 0.4 * (self.wave - 1)

    def max_bombs(self):
        return min(5, 1 + (self.wave - 1))

    def bomb_fall(self):
        return min(self.BOMB_FALL_CAP, 8.0 + 1.0 * (self.wave - 1))

    def cell_xy(self, r, c):
        return (self.fleet_x0 + int(self.fleet_off) + c * self.cell_w,
                self.fleet_row0 + r)

    def row_value(self, r):
        """Value by ROW — the arcade grammar. Top row 30, middle 20, bottom 10,
        taught by the colour ramp rather than by text."""
        if r == 0:
            return 30
        return 20 if r <= (self.rows - 1) // 2 else 10

    def row_attr(self, r):
        idx = int(round(r * 4.0 / max(1, self.rows - 1)))
        return self.pal.row[max(0, min(4, idx))]

    def front_line(self):
        """Only the FRONT-most invader in a column may fire — the arcade rule.
        It is also a readability rule: a bomb that spawns behind the fleet spends
        its first rows overlapping other invaders, which eats the telegraph."""
        front = {}
        for (r, c) in self.fleet:
            if c not in front or r > front[c]:
                front[c] = r
        return [(r, c) for (c, r) in front.items()]

    def remaining(self):
        if self.boss is not None:
            return sum(max(0, c["hp"]) for c in self.boss["cores"])
        return len(self.fleet) + len(self.divers) + (1 if self.diving else 0)

    # ---- seamless contract ------------------------------------------------
    def on_rejoin(self):
        """Called the moment the pane comes back from 'idle'. Reset every
        accumulator a wall-clock gap would corrupt — without this the telegraph
        guarantee is a lie the first time the pane hitches, and this pane
        hitches by design."""
        self.hitstop = 0.0
        self.shake_t = 0.0
        self.shake_amp = 0
        self.flash_full = 0.0
        self.debris = []
        self.flashes = []
        self.floaters = []
        self.shot_cd = 0.0
        self.hint_pulse = 0.0
        if self.invuln > 0.0:
            self.invuln = 0.6                 # keep the mercy, drop the stale clock
        if self.diving:
            r, c, _ = self.diving
            self.diving = (r, c, self.DIVER_BLINK)   # re-arm the full 0.8 s telegraph
        self.last_kill = self.t - self.COMBO_WINDOW - 1.0   # combo cannot survive a pause

    # ---- input ------------------------------------------------------------
    def handle_key(self, ch, playing):
        if ch in (ord("q"), ord("Q")):
            return "quit"
        self.first_key = True
        if ch == ord(" "):
            if not playing:
                self.manual_play = True       # "[space] play anyway"
            else:
                self.fire()
        elif ch in (curses.KEY_LEFT, ord("a"), ord("A")):
            self.player_x = max(2, self.player_x - 2)
        elif ch in (curses.KEY_RIGHT, ord("d"), ord("D")):
            self.player_x = min(self.w - 3, self.player_x + 2)
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, _, _, _ = curses.getmouse()
                self.player_x = max(2, min(self.w - 3, mx))
            except curses.error:
                pass
        return None

    def fire(self):
        if self.phase != "play" or self.shot_cd > 0.0:
            return
        if len(self.bullets) >= self.SHOT_CAP:
            return
        self.shot_cd = self.SHOT_COOLDOWN
        self.bullets.append([self.player_x, float(self.player_y - 1)])

    # ---- juice ------------------------------------------------------------
    def kill_juice(self, x, y, big=False):
        """0.05-0.07 s hitstop, 2-frame flash, shake, and debris that PERSISTS
        0.4 s. The persistence is the load-bearing part: a flash you can miss by
        blinking is not feedback."""
        self.hitstop = max(self.hitstop, 0.07 if big else 0.05)
        self.shake_t = max(self.shake_t, 0.13 if big else 0.09)
        self.shake_amp = 2 if big else 1
        self.flashes.append([y, x, self.t + 0.07])
        for _ in range(5 if big else 4):
            self.debris.append([float(x), float(y),
                                random.uniform(-9.0, 9.0), random.uniform(-7.0, 1.0),
                                self.t + 0.4])

    def damage_juice(self):
        self.hitstop = max(self.hitstop, 0.10)
        self.shake_t = max(self.shake_t, 0.20)
        self.shake_amp = 2
        self.flash_full = 0.05
        self.invuln = 1.0

    def add_score(self, n, x=None, y=None, label=""):
        self.score += n
        if x is not None and self.pf_h >= 6:
            self.floaters.append([float(x), float(y), self.t + 0.35,
                                  ("+%d %s" % (n, label)).strip()])
        while self.next_extra and self.score >= self.next_extra:
            # cap the AWARD at 5 pips; never let the award REDUCE a life count
            if self.lives < 5:
                self.lives += 1
            self.flash_full = max(self.flash_full, 0.04)
            self.next_extra = 15000 if self.next_extra == 5000 else 0

    def bump_combo(self):
        if self.t - self.last_kill <= self.COMBO_WINDOW:
            self.mult = min(self.COMBO_CAP, self.mult + 1)
        else:
            self.mult = 1
        self.last_kill = self.t

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        """One fixed slice. Returns None, 'over' or 'won'."""
        self.t += dt

        # timers that run even in hitstop (hitstop is a FEEL device, not a stall)
        self.hitstop = max(0.0, self.hitstop - dt)
        self.shake_t = max(0.0, self.shake_t - dt)
        self.flash_full = max(0.0, self.flash_full - dt)
        self.hint_pulse = max(0.0, self.hint_pulse - dt)
        self.score_shown = min(float(self.score), self.score_shown + 400.0 * dt)
        self.step_particles(dt)

        if self.mult > 1 and self.t - self.last_kill > self.COMBO_WINDOW:
            self.mult = 1

        # the [space] fire hint pulses ONCE at t=2.5 if untouched. Signposting,
        # never autoplay.
        if (not self.first_key and self.wave == 1
                and 2.5 <= self.t < 2.5 + dt):
            self.hint_pulse = 0.6

        if self.phase == "clear":
            self.phase_t -= dt
            if self.phase_t <= 0.0:
                if self.won:
                    return "won"
                self.wave += 1
                self.begin_wave()
                self.phase = "play"
            return None

        if self.hitstop > 0.0:
            return None

        self.wave_t += dt
        self.invuln = max(0.0, self.invuln - dt)
        self.shot_cd = max(0.0, self.shot_cd - dt)

        if self.boss is not None:
            self.step_boss(dt)
        else:
            self.step_fleet(dt)
        self.step_mystery(dt)
        self.step_divers(dt)
        self.step_shots(dt)

        res = self.step_collisions()
        if res:
            return res

        if self.remaining() <= 0 and self.phase == "play":
            self.clear_wave()
        return None

    def step_particles(self, dt):
        for d in self.debris:
            d[0] += d[2] * dt
            d[1] += d[3] * dt
            d[3] += 22.0 * dt                  # gravity: debris falls, it does not float
        self.debris = [d for d in self.debris if d[4] > self.t]
        self.flashes = [f for f in self.flashes if f[2] > self.t]
        for f in self.floaters:
            f[1] -= 8.0 * dt                   # +n floats up ~3 rows over 0.35 s
        self.floaters = [f for f in self.floaters if f[2] > self.t]

    def step_fleet(self, dt):
        if not self.fleet:
            return
        # THE FLEET STEPS, it does not slide. Space Invaders moves a whole cell
        # at a time and is STATIONARY in between — which is what makes it
        # aimable: hitting is a question of timing, never of computing a lead.
        # A continuously sliding fleet turns every shot into a lead calculation
        # the player cannot see. Same cells/second, completely different feel.
        self.march_acc += self.march_speed() * dt
        while self.march_acc >= 1.0:
            self.march_acc -= 1.0
            self.fleet_off += self.dir
        cs = [c for (_, c) in self.fleet]
        left = self.fleet_x0 + int(self.fleet_off) + min(cs) * self.cell_w
        right = self.fleet_x0 + int(self.fleet_off) + max(cs) * self.cell_w + 1
        if right >= self.w - 2 and self.dir > 0:
            self.dir = -1
            self.fleet_row0 += 1
        elif left <= 2 and self.dir < 0:
            self.dir = 1
            self.fleet_row0 += 1

        # DIVERS from wave 5: blink 0.8 s (the telegraph), then detach and fly a
        # sine path down your column. An OPEN triangle, so "this one is coming
        # for you" is a glance read at any distance.
        if self.wave >= 5:
            if self.diving is None:
                self.diver_cd -= dt
                if self.diver_cd <= 0.0 and self.fleet:
                    r, c = random.choice(sorted(self.fleet))
                    self.diving = (r, c, self.DIVER_BLINK)
            else:
                r, c, tleft = self.diving
                tleft -= dt
                if tleft <= 0.0:
                    if (r, c) in self.fleet:
                        self.fleet.discard((r, c))
                        x, y = self.cell_xy(r, c)
                        # it LOCKS ON at detach and then COMMITS to that lane.
                        # Re-aiming every frame makes a heat-seeker you cannot
                        # escape by moving — which is a coin flip, not a threat.
                        self.divers.append({"x": float(x), "y": float(y),
                                            "x0": float(x), "aim": float(self.player_x),
                                            "t": 0.0, "r": r})
                    self.diving = None
                    self.diver_cd = random.uniform(7.0, 9.0)
                else:
                    self.diving = (r, c, tleft)

        # enemy fire. THE TELEGRAPH IS STRUCTURAL: only invaders at least
        # (fall x 1.0 s) rows above the player may fire, and the shot's own speed
        # is clamped to distance / 1.0 s. Every bomb is visible for >= 1 s.
        quiet = (self.wave == 1 and self.t < 3.0)   # the scripted opening owns the first 3 s
        if (not quiet and self.fleet and len(self.bombs) < self.max_bombs()
                and random.random() < self.bomb_rate() * dt):
            self.spawn_bomb(random.choice(sorted(self.front_line())))

        # the SCRIPTED first bomb at t=0.8, from the invader directly above you,
        # falling slowly (8 rows/s ~= 1.7 s of travel at 100x24). Unmissable;
        # one keypress dodges it. Fires exactly once, ever.
        if self.wave == 1 and not self.first_bomb_done and self.t >= 0.8 and self.fleet:
            pick = min(sorted(self.front_line()),
                       key=lambda rc: abs(self.cell_xy(*rc)[0] - self.player_x))
            self.spawn_bomb(pick, speed=8.0)
            self.first_bomb_done = True

    def spawn_bomb(self, rc, speed=None):
        bx, by = self.cell_xy(*rc)
        self.drop_bomb(bx, by + 1, speed)

    # THE DODGE LANE. Two bombs closer together than this can arrive as a WALL:
    # you sidestep the first and land under the second, which reads as a coin
    # flip no matter how long the telegraph was. The gutter law (>=2 empty
    # columns between objects) applied to the time axis — difficulty comes from
    # density and speed, never from removing the answer.
    BOMB_GUTTER = 3

    def drop_bomb(self, bx, by, speed=None):
        """THE ONE PLACE A BOMB IS EVER CREATED, so the telegraph guarantee lives
        in a single expression instead of being re-derived per caller."""
        dist = self.player_y - by
        if dist < 2:
            return                               # too close to telegraph: it does not fire
        for other in self.bombs:
            if abs(other[0] - bx) < self.BOMB_GUTTER:
                return                           # a sidestep must always exist
        v = self.bomb_fall() if speed is None else speed
        v = min(v, dist / self.TELEGRAPH_S)      # the floor holds, always
        self.bombs.append([float(bx), float(by), v])

    # BOSS LATERAL CAP, derived exactly like MARCH_CAP: peak |dx/dt| is
    # BOSS_SPAN x BOSS_W, and it must stay under one sprite width per shot
    # flight. 12 x 0.38 = 4.6 c/s — a jumbo banking, not a blur.
    BOSS_SPAN = 10.0        # cells either side of centre
    BOSS_W = 0.35           # rad/s  ->  peak 3.5 c/s, under one core width per shot
    BOSS_DESCENT = 0.16     # rows/s — you have time, but not forever

    def step_boss(self, dt):
        b = self.boss
        b["t"] += dt
        span = min(self.BOSS_SPAN, max(2.0, (self.w - b["w"] - 6) / 2.0))
        # a shallow S, not the marching square: it moves like something flying
        b["x"] = (self.w - b["w"]) / 2.0 + span * math.sin(b["t"] * self.BOSS_W)
        b["y"] = b["y0"] + b["bob"] * (1.0 + math.sin(b["t"] * 0.7)) \
            + b["t"] * self.BOSS_DESCENT
        if len(self.bombs) < 5 and random.random() < 2.2 * dt:
            live = [c for c in b["cores"] if c["hp"] > 0]
            if live:
                dy, dx = random.choice(live)["cells"][0]
                self.drop_bomb(int(b["x"]) + dx, int(b["y"]) + dy + 1)

    def step_mystery(self, dt):
        self.mystery_cd -= dt
        # SCRIPTED first appearance at t=6.5 in wave 1: by then the player has
        # seen the whole vocabulary except the gold one.
        due = (self.wave == 1 and not self.mystery_seen and self.t >= 6.5)
        if self.mystery is None and (due or self.mystery_cd <= 0.0):
            if self.remaining() > 0 and self.w > 12:
                d = 1 if random.random() < 0.5 else -1
                self.mystery = [1.0 if d > 0 else float(self.w - 4), d]
                self.mystery_seen = True
        if self.mystery is not None:
            self.mystery[0] += self.mystery[1] * self.MYSTERY_SPEED * dt
            if not (0 < self.mystery[0] < self.w - 3):
                self.mystery = None
                self.mystery_cd = random.uniform(12.0, 22.0)

    def step_divers(self, dt):
        for d in self.divers:
            d["t"] += dt
            d["y"] += self.DIVER_SPEED * dt
            # curves toward the lane it committed to, so it reads as intent
            # rather than as gravity — and stepping out of that lane WORKS
            aim = d["x0"] + (d["aim"] - d["x0"]) * min(1.0, d["t"] * 0.55)
            d["x"] = aim + 2.2 * math.sin(d["t"] * 4.0)
        self.divers = [d for d in self.divers if d["y"] < self.player_y + 1]

    def step_shots(self, dt):
        for b in self.bullets:
            b[1] -= self.SHOT_SPEED * dt
        self.bullets = [b for b in self.bullets if b[1] > self.pf_top - 1]
        for b in self.bombs:
            b[1] += b[2] * dt
        self.bombs = [b for b in self.bombs if b[1] < self.player_y + 1]

    # ---- collisions -------------------------------------------------------
    def step_collisions(self):
        for b in list(self.bullets):
            bx, by = int(round(b[0])), int(round(b[1]))
            if self.hit_bunker(bx, by):
                if b in self.bullets:
                    self.bullets.remove(b)
                continue
            if self.mystery is not None and by <= self.pf_top and abs(b[0] - self.mystery[0]) <= 2:
                self.bullets.remove(b)
                self.mystery = None
                self.mystery_cd = random.uniform(12.0, 22.0)
                self.eggs += 1
                self.kill_juice(bx, by, big=True)
                self.add_score(747, bx, by, "the wink")   # flat 747: nothing else pays it
                self.bump_combo()
                continue
            if self.boss is not None and self.hit_boss(b, bx, by):
                continue
            hit = None
            for d in self.divers:
                if abs(d["x"] - b[0]) <= 1.0 and abs(d["y"] - b[1]) <= 0.8:
                    hit = d
                    break
            if hit is not None:
                self.divers.remove(hit)
                self.bullets.remove(b)
                self.kill_juice(bx, by)
                self.bump_combo()
                self.add_score(30 * self.mult, bx, by)
                continue
            done = False
            for (r, c) in sorted(self.fleet):
                cx, cy = self.cell_xy(r, c)
                if by == cy and cx <= bx <= cx + 1:
                    self.fleet.discard((r, c))
                    if self.diving and self.diving[0] == r and self.diving[1] == c:
                        self.diving = None
                        self.diver_cd = random.uniform(7.0, 9.0)
                    self.bullets.remove(b)
                    self.kill_juice(cx, cy)
                    self.bump_combo()
                    self.add_score(self.row_value(r) * self.mult, cx, cy)
                    done = True
                    break
            if done:
                continue

        for bomb in list(self.bombs):
            bx, by = int(round(bomb[0])), int(round(bomb[1]))
            if self.hit_bunker(bx, by):
                if bomb in self.bombs:
                    self.bombs.remove(bomb)
                continue
            if by >= self.player_y and abs(bomb[0] - self.player_x) <= 1:
                self.bombs.remove(bomb)
                r = self.lose_life()
                if r:
                    return r
                # lose_life() clears the bomb list wholesale — anything left in
                # THIS snapshot is already gone, and removing it again raises.
                break

        for d in list(self.divers):
            if d["y"] >= self.player_y - 0.4 and abs(d["x"] - self.player_x) <= 1.2:
                self.divers.remove(d)
                r = self.lose_life()
                if r:
                    return r
                break                            # same reason as the bomb loop

        # the fleet reaching your row costs a life AND resets it to the top, so
        # you are never dead on arrival
        floor = None
        if self.fleet:
            floor = self.fleet_row0 + max(r for (r, _) in self.fleet)
        elif self.boss is not None:
            floor = int(self.boss["y"]) + len(self.boss["rows"]) - 1
        if floor is not None and floor >= self.player_y:
            return self.lose_life(reset=True)
        return None

    def hit_boss(self, b, bx, by):
        bo = self.boss
        dy, dx = by - int(bo["y"]), bx - int(bo["x"])
        for core in bo["cores"]:
            if core["hp"] > 0 and (dy, dx) in core["cells"]:
                core["hp"] -= 1
                self.bullets.remove(b)
                self.kill_juice(bx, by, big=(core["hp"] <= 0))
                if core["hp"] <= 0:
                    self.bump_combo()
                    self.add_score(250 * self.mult, bx, by, "core")
                if all(c["hp"] <= 0 for c in bo["cores"]):
                    self.add_score(7470, bx, by, "sky clear")
                    self.won = True
                    self.boss = None
                    self.clear_wave()
                return True
        if (dy, dx) in bo["hull"]:
            self.bullets.remove(b)
            self.flashes.append([by, bx, self.t + 0.07])   # armour: it sparks, it does not pay
            self.debris.append([float(bx), float(by), random.uniform(-6, 6), -5.0,
                                self.t + 0.25])
            return True
        return False

    def hit_bunker(self, x, y):
        hp = self.bunkers.get((y, x))
        if not hp:
            return False
        self.bunkers[(y, x)] = hp - 1
        if hp - 1 <= 0:
            del self.bunkers[(y, x)]
        self.debris.append([float(x), float(y), random.uniform(-5, 5),
                            random.uniform(-4, 0), self.t + 0.3])
        return True

    def lose_life(self, reset=False):
        if self.invuln > 0.0 and not reset:
            return None
        self.lives -= 1
        self.mult = 1
        self.perfect = False
        self.bombs = []
        self.divers = []
        self.diving = None
        self.damage_juice()
        if reset:
            self.fleet_row0 = max(self.pf_top + 1, min(2, self.player_y - 3 - self.rows))
            self.fleet_off = 0
            self.march_acc = 0.0
            if self.boss is not None:
                self.boss["t"] = 0.0
                self.boss["y"] = self.boss["y0"]
        return "over" if self.lives <= 0 else None

    def clear_wave(self):
        bonus = 100 * self.wave
        if self.perfect:
            bonus *= 2
        if self.won:
            self.banner = "SKY CLEAR"
        else:
            self.banner = "WAVE %d CLEAR%s  +%d" % (
                self.wave, "  PERFECT " + G["times"] + "2" if self.perfect else "", bonus)
        self.add_score(bonus)
        self.phase = "clear"
        self.phase_t = self.CLEAR_HOLD
        self.flash_full = 0.06                 # 2-frame full-width white flash
        self.bombs = []
        self.bullets = []

    # ---- rendering --------------------------------------------------------
    def put(self, y, x, txt, attr=0):
        """Every draw goes through here: clipped, and never the bottom-right
        cell (which is a curses error on every terminal ever made)."""
        if y < 0 or y >= self.h or not txt:
            return
        if x < 0:
            txt = txt[-x:]
            x = 0
        lim = self.w - (1 if y == self.h - 1 else 0)
        if x >= lim:
            return
        txt = txt[:lim - x]
        if not txt:
            return
        try:
            self.scr.addstr(y, x, txt, attr)
        except curses.error:
            pass

    def draw(self, playing):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if (h, w) != (self.h, self.w):
            self.layout()
            if not self.small:
                self.relayout_wave()
        if self.small:
            self.draw_too_small()
            s.refresh()
            return
        p = self.pal

        sx = sy = 0
        if self.shake_t > 0.0:
            sx = random.randint(-self.shake_amp, self.shake_amp)
            sy = random.randint(-1, 1) if self.shake_amp > 1 else 0

        # --- the teach line: zero-tutorial is not zero-signposting (<= 15 s).
        #     Drawn FIRST, as a background layer: in a short pane it shares a row
        #     with live play, and a hint that can hide a falling bomb is a defect,
        #     not a hint. Everything below overpaints it.
        near = any(b[1] > self.player_y - 4 for b in self.bombs) or self.divers
        if (self.wave == 1 and self.t < self.TEACH_UNTIL and self.pf_h >= 6
                and self.phase == "play" and not near):
            # ...and it YIELDS to an incoming threat rather than being punched
            # full of holes by it. The hint is never the most important thing on
            # its row.
            ln = G["teach"]
            attr = p.text_hi | curses.A_BOLD if self.hint_pulse > 0.0 else p.text_dim
            self.put(self.player_y - 1, max(0, (self.w - len(ln)) // 2), ln, attr)

        # --- bunkers: the same erosion grammar BREAK-IN uses for its hatches ---
        for (by, bx), hp in self.bunkers.items():
            self.put(by + sy, bx + sx, G["bunker"][3 - hp], p.struct[3 - hp])

        # --- the fleet. Colour IS value: top row coolest, bottom row hazard red
        for (r, c) in sorted(self.fleet):
            cx, cy = self.cell_xy(r, c)
            attr = self.row_attr(r)
            if self.diving and self.diving[0] == r and self.diving[1] == c:
                # THE TELEGRAPH: 0.8 s of blinking before it ever detaches
                if int(self.t * 8.0) % 2 == 0:
                    attr = p.player[0] | curses.A_REVERSE
            if self.pf_top <= cy <= self.pf_bot:
                self.put(cy + sy, cx + sx, G["invader"], attr | curses.A_BOLD)

        # --- the 747 boss ---
        if self.boss is not None:
            self.draw_boss(sx, sy)

        # --- divers: an OPEN triangle. A different shape, not a brighter blob ---
        for d in self.divers:
            self.put(int(d["y"]) + sy, int(d["x"]) + sx, G["diver"],
                     p.player[0] | curses.A_BOLD)

        # --- the mystery 747: the only gold thing on screen, ever ---
        if self.mystery is not None:
            self.put(self.pf_top + sy, int(self.mystery[0]) + sx, G["mystery"],
                     p.gold[0] | curses.A_BOLD)

        # --- shots and bombs ---
        for b in self.bullets:
            self.put(int(round(b[1])) + sy, int(round(b[0])) + sx, G["shot"],
                     p.pickup[0] | curses.A_BOLD)
        for b in self.bombs:
            self.put(int(round(b[1])) + sy, int(round(b[0])) + sx, G["bomb"],
                     p.hazard[0] | curses.A_BOLD)

        # --- debris that PERSISTS 0.4 s: proof the world reacted to you ---
        for d in self.debris:
            self.put(int(round(d[1])) + sy, int(round(d[0])) + sx, G["debris"],
                     p.struct[1])
        for f in self.floaters:
            self.put(int(round(f[1])), int(round(f[0])), f[3], p.text_hi)
        for fl in self.flashes:
            self.put(fl[0] + sy, fl[1] + sx, "  ", curses.A_REVERSE | curses.A_BOLD)

        # --- you ---
        if not (self.invuln > 0.0 and int(self.t * 20.0) % 2 == 0):
            self.put(self.player_y, self.player_x, G["player"],
                     p.player[0] | curses.A_BOLD)

        if self.phase == "clear" and self.banner:
            self.overlay([self.banner], p.gold[0] if self.won else p.text_hi)

        self.draw_hud(playing)
        if self.flash_full > 0.0:
            # the full-screen inverse: one or two frames, never longer
            for y in range(self.h):
                try:
                    s.chgat(y, 0, max(0, self.w - (1 if y == self.h - 1 else 0)),
                            curses.A_REVERSE)
                except curses.error:
                    pass
        if not playing:
            self.overlay([G["pause"] + "  CLAUDE'S DONE " + G["dash"] + " READING TIME",
                          "resumes on your next prompt " + G["sep"] + " [space] play anyway"],
                         p.text_hi)
        s.refresh()

    def relayout_wave(self):
        """A resize must not strand the fleet off-screen or bury the bunkers."""
        rows, cols = self.fleet_shape()
        if self.boss is not None:
            self.boss["x"] = min(self.boss["x"], max(0, self.w - self.boss["w"] - 1))
            return
        if (rows, cols) != (self.rows, self.cols):
            keep_score = self.remaining()
            self.rows, self.cols = rows, cols
            self.fleet = {(r, c) for r in range(rows) for c in range(cols)
                          if r * cols + c < keep_score}
            if not self.fleet:
                self.fleet = {(0, 0)}
        self.fleet_x0 = max(2, (self.w - self.cols * self.cell_w) // 2)
        self.fleet_row0 = max(self.pf_top + 1,
                              min(self.fleet_row0, self.player_y - 3 - self.rows))
        if self.bunkers_built and self.pf_h >= 6:
            self.build_bunkers()

    def draw_boss(self, sx, sy):
        b, p = self.boss, self.pal
        x0, y0 = int(b["x"]), int(b["y"])
        for (dy, dx) in sorted(b["hull"]):
            self.put(y0 + dy + sy, x0 + dx + sx, G["boss"], p.gold[1] | curses.A_BOLD)
        for core in b["cores"]:
            hp = core["hp"]
            for (dy, dx) in core["cells"]:
                if hp > 0:
                    self.put(y0 + dy + sy, x0 + dx + sx, G["core"][3 - hp],
                             p.struct[0] | curses.A_BOLD)
                else:
                    self.put(y0 + dy + sy, x0 + dx + sx, " ", 0)

    def draw_too_small(self):
        """Below the floor we do not crash and we do not exit — the pane must
        still honour 'end'."""
        msg = "TERMINAL TOO SMALL " + G["dash"] + " 80x8 MINIMUM"
        if len(msg) > self.w - 1:          # a half-word is worse than a short word
            msg = "TOO SMALL"
        self.put(max(0, self.h // 2), max(0, (self.w - len(msg)) // 2), msg, curses.A_BOLD)

    def overlay(self, lines, attr=0):
        for i, ln in enumerate(lines):
            y = self.h // 2 - 1 + i
            self.put(y, max(0, (self.w - len(ln)) // 2), ln, attr | curses.A_BOLD)

    # ---- THE HUD LAW: exactly row 0 and row h-1, forever -------------------
    def draw_hud(self, playing):
        p, w = self.pal, self.w
        R = curses.A_REVERSE
        self.put(0, 0, " " * w, R)

        # the degradation ladder: >=78 full · 62-77 drop the keys · 46-61 the
        # title shortens but the integer NEVER leaves col 14 · <46 packed
        title = "ASTROS" if w >= 62 else "AST"
        # the primary integer is ALWAYS %7d in a fixed field: a score whose
        # digits reflow is the most common terminal-HUD sin
        num = "%7d" % int(self.score_shown)
        lives = (G["life"] * self.lives) if self.lives <= 5 \
            else (G["life"] + G["times"] + "%d" % self.lives)
        stage = ("W%d/%d" % (self.wave, self.WAVES)) if self.wave <= self.WAVES \
            else ("W%d" % self.wave)

        if w >= 46:
            self.put(0, 0, G["tick"], p.accent[0] | R)
            self.put(0, 2, "%-10s" % title, R | curses.A_BOLD)
            self.put(0, 12, G["sep"], R | curses.A_DIM)
            self.put(0, 14, num, R | curses.A_BOLD)
            self.put(0, 22, lives, p.player[0] | R)
            self.put(0, 28, stage, R)
            self.draw_fleet_bar(35, R)
            if self.mult > 1:
                self.put(0, 50, G["times"] + "%d" % self.mult,
                         p.gold[0] | R | curses.A_BOLD)
            keys = G["keys"]
            kx = w - len(keys) - 1
            if kx >= 57:      # never let the keys hint eat the multiplier at col 50
                self.put(0, kx, keys, R | curses.A_DIM)
        else:
            # < 46: primary integer + lives only, left-packed
            self.put(0, 0, G["tick"], p.accent[0] | R)
            self.put(0, 2, num, R | curses.A_BOLD)
            self.put(0, 10, lives, p.player[0] | R)
            self.put(0, 16, stage, R)

        # row h-1: per-title telemetry left, the one piece of branding right
        foot = "WAVE %d %s BEST %s" % (self.wave, G["sep"], "{:,}".format(self.best))
        if self.wave > self.WAVES:
            foot = "OVERRUN %s BEST %s" % (G["sep"], "{:,}".format(self.best))
        self.put(self.h - 1, 1, foot, p.text_dim)
        tag = "THE 747 LAB "
        self.put(self.h - 1, max(0, w - len(tag) - 1), tag, p.text_dim)

    def draw_fleet_bar(self, col, R):
        """Progress is never inferred: filled pips = what is left to kill."""
        if self.w < col + 12:
            return
        start = max(1, self.fleet_start)
        frac = max(0.0, min(1.0, self.remaining() / float(start)))
        n = int(round(frac * 10))
        bar = G["pip_on"] * n + G["pip_off"] * (10 - n)
        self.put(0, col, bar, self.pal.target[0] | R)


# ---------------------------------------------------------------------------
# blocking screens — EVERY ONE polls for 'end'. A screen that can hang a pane
# is a seamless-contract violation even if the game itself is perfect.
# ---------------------------------------------------------------------------
def _screen(scr, session, lines, keys, deadline=None):
    scr.nodelay(False)
    scr.timeout(200)      # so 'end' is noticed within ~0.2 s on any blocking screen
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        top = max(0, h // 2 - len(lines) // 2)
        for i, ln in enumerate(lines):
            try:
                scr.addstr(top + i, max(0, (w - len(ln)) // 2), ln[:max(0, w - 1)],
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        try:
            scr.addstr(h - 1, max(0, w - 13), "THE 747 LAB ", curses.A_DIM)
        except curses.error:
            pass
        scr.refresh()
        ch = scr.getch()
        if ch != -1:
            for k, val in keys:
                if ch in k:
                    return val
        if read_state(session) == "end":
            return "end"
        if deadline is not None and time.time() > deadline:
            return "timeout"


def ask_screen(scr, session):
    r = _screen(scr, session,
                ["PLAY ASTROS WHILE CLAUDE THINKS?", "",
                 "[y] yes   [n] not now   [a] always auto-open   [o] never ask again"],
                [((ord("y"), ord("Y")), "yes"),
                 ((ord("a"), ord("A")), "always"),
                 ((ord("n"), ord("N")), "no"),
                 ((ord("o"), ord("O")), "off"),
                 ((ord("q"), ord("Q")), "no")],
                deadline=time.time() + ASK_TIMEOUT)
    if r == "always":
        write_mode("auto")
        return True
    if r == "yes":
        return True
    if r == "off":
        write_mode("off")
        return False
    if session and r in ("no", "timeout"):
        open(os.path.join(STATE_DIR, f"declined-{session}"), "w").close()
    return False


def menu_available():
    """Only offer [m] when the picker is actually next to us. A single-title
    install has no menu to go back to, and an offered key that does nothing is
    worse than no key at all."""
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "breakout.py"))


def back_to_menu(session):
    """[m] on the game-over screen: hand this pane back to the picker IN THIS
    PROCESS SLOT (os.execv, the same handoff breakout.launch_title uses), so the
    ghost-pane / pause / resume / OSC-title contract survives untouched.

    --picker enters the menu directly: a player who just finished a run has
    already sat through the 7.47s intro. Every launch flag travels with it —
    dropping --free would make the next title obey Claude's state instead of
    playing free, and --session keyed wrong orphans the pane from the hook.
    Never returns."""
    here = os.path.dirname(os.path.abspath(__file__))
    # --session stays ahead of any trailing flag: the hook's pgrep/pkill pattern
    # is "<title>\.py.*--session <key>", and every pane it banishes is matched on it.
    argv = [sys.executable, os.path.join(here, "breakout.py"), "--picker",
            "--session", session]
    if getattr(LAUNCH_ARGS, "free", False):
        argv.append("--free")
    try:
        curses.endwin()
    except curses.error:
        pass
    try:
        sys.stdout.write("\033[?1003l")   # a leaked mouse mode outlives the pane
        sys.stdout.flush()
    except Exception:
        pass
    os.execv(sys.executable, argv)


def game_over_screen(scr, game, session, st):
    best = max(st["best_score"], game.score)
    lines = ["GAME OVER " + G["sep"] + " WAVE %d" % min(game.wave, Game.WAVES),
             "",
             "SCORE %s      BEST %s" % ("{:,}".format(game.score), "{:,}".format(best)),
             "",
             ("[r] again " + G["sep"] + " [m] menu " + G["sep"] + " [q] close")
             if menu_available() else "[r] again " + G["sep"] + " [q] close"]
    keys = [((ord("r"), ord("R")), "again"), ((ord("q"), ord("Q")), "quit")]
    if menu_available():                     # never bind a key the build cannot honour
        keys.insert(1, ((ord("m"), ord("M")), "menu"))
    r = _screen(scr, session, lines, keys)
    # Intercepted HERE, not in run(): back_to_menu never returns, so run()'s
    # 'again'/'quit'/'end' contract stays exactly as it was.
    if r == "menu":
        back_to_menu(session)                # never returns
    return r


def victory_screen(scr, game, session, st):
    best = max(st["best_score"], game.score)
    lines = ["SKY CLEAR " + G["sep"] + " 7/7",
             "",
             "SCORE %s      BEST %s" % ("{:,}".format(game.score), "{:,}".format(best)),
             "",
             "the fleet is down. OVERRUN is endless.",
             "",
             "[c] keep flying " + G["sep"] + " [r] restart " + G["sep"] + " [q] close"]
    return _screen(scr, session, lines,
                   [((ord("c"), ord("C"), ord(" ")), "continue"),
                    ((ord("r"), ord("R")), "again"),
                    ((ord("q"), ord("Q")), "quit")])


# ---------------------------------------------------------------------------
def run(scr, args, st):
    """One run. Returns 'quit' | 'again' | 'end'."""
    game = Game(scr, args.session, args.free, best=st["best_score"])
    scr.nodelay(True)
    last = time.time()
    last_poll = 0.0
    state = "thinking"
    idle_drawn = False
    acc = 0.0

    while True:
        now = time.time()
        if now - last_poll >= STATE_POLL:
            last_poll = now
            new = read_state(args.session)
            if new != state:
                if state == "idle" and new != "end":
                    # a ghost-pane return: never resume mid-flash or mid-bank
                    game.on_rejoin()
                    last = time.time()
                    acc = 0.0
                state = new
                idle_drawn = False
        if state == "end":
            return "end"

        playing = (state == "thinking") or game.manual_play

        ch = scr.getch()
        while ch != -1:
            if game.handle_key(ch, playing) == "quit":
                return "quit"
            idle_drawn = False
            playing = (state == "thinking") or game.manual_play
            ch = scr.getch()

        if not playing:
            # IDLE: freeze the sim on this frame, draw the overlay ONCE, then
            # zero bytes on the wire while ghosted.
            if not idle_drawn:
                game.draw(False)
                idle_drawn = True
            last = time.time()
            acc = 0.0
            time.sleep(POLL_IDLE)
            continue

        dt = now - last
        last = now
        if dt > DT_REJOIN:
            dt = 0.0                    # a resumed pane may never teleport the world
        dt = min(dt, DT_MAX)

        result = None
        if not game.small:
            acc += dt
            n = 0
            while acc >= FIXED_DT and n < MAX_SUBSTEPS:
                result = game.step(FIXED_DT)
                acc -= FIXED_DT
                n += 1
                if result:
                    break
            if n >= MAX_SUBSTEPS:
                acc = 0.0
        game.draw(True)

        if result == "over":
            st["runs"] += 1
            st["best_score"] = max(st["best_score"], game.score)
            st["best_stage"] = max(st["best_stage"], min(game.wave, Game.WAVES))
            st["eggs"] += game.eggs
            save_stats(st)
            r = game_over_screen(scr, game, args.session, st)
            return r if r in ("again", "end") else "quit"
        if result == "won":
            st["runs"] += 1
            st["cleared"] = True
            st["best_score"] = max(st["best_score"], game.score)
            st["best_stage"] = Game.WAVES
            st["eggs"] += game.eggs
            game.eggs = 0
            save_stats(st)
            v = victory_screen(scr, game, args.session, st)
            if v == "continue":         # OVERRUN: endless, pure score
                game.wave = Game.WAVES + 1
                game.won = False
                game.phase = "play"
                game.begin_wave()
                game.best = st["best_score"]
                game.on_rejoin()
                scr.nodelay(True)
                last = time.time()
                acc = 0.0
                continue
            return v if v in ("again", "end") else "quit"

        time.sleep(TICK)


def main(scr, args):
    # EVERY one of these can raise on a terminal that lacks the capability —
    # vt100 has no civis, a mono terminal has no colour, a serial console has no
    # mouse. None of them is a reason to refuse to play, and a traceback here
    # takes the pane down with it. Guarded individually, in the correct order
    # (start_color BEFORE use_default_colors).
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()
        curses.use_default_colors()
    except (curses.error, ValueError):
        pass
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1003h", end="", flush=True)   # mouse motion tracking
    except (curses.error, OSError):
        pass

    if args.ask and not ask_screen(scr, args.session):
        return

    st = load_stats()
    while True:
        r = run(scr, args, st)
        if r == "end":
            remove_state(args.session)          # the game owns its own state file
            return
        if r != "again":
            return


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="ASTROS - invaders that runs while Claude thinks.")
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    LAUNCH_ARGS = args      # module scope: back_to_menu() reads it
    os.makedirs(STATE_DIR, exist_ok=True)
    use_ascii()
    # Session-keyed, like every other title: the hook matches this exact string
    # to banish, rejoin and close the pane. Set BEFORE curses.wrapper.
    set_pane_title(args.session)
    try:
        curses.wrapper(main, args)
    finally:
        # a leaked mouse mode outlives the pane and lands in the user's shell —
        # the single most obnoxious way to fail the seamless contract
        print("\033[?1003l", end="", flush=True)
