#!/usr/bin/env python3
"""JETWASH — a side-on sky runner that plays while Claude thinks.

You are a white arrow flying a service corridor above a city. The world comes
at you from the right; you have two verbs. UP jumps (tap short, hold high).
DOWN slams (a fast-fall that ducks you under trouble and smashes a cyan crate
on the way through). Everything else is reading the shape in front of you and
deciding: over it, under it, or through it.

ONE NUMBER: metres. There are seven gold gates, one per 1,000 m, and a finish
line at 7,470 m — roughly half a minute, which is roughly one Claude think
block. Cross it and the run is CLEARED; the game keeps going into OVERTIME for
anyone whose turn ran long, so there is a real win state and still no ceiling.

Lives in a tmux pane split below the Claude Code session. Auto-pauses when
Claude finishes a turn (Stop hook writes 'idle'), resumes on the next prompt
(UserPromptSubmit writes 'thinking'), and exits when the session ends. It
survives being banished to a hidden window and rejoined mid-run: the pane
vanishes, the process stays alive and frozen, and the run comes back exactly
where it was left.

Zero dependencies (stdlib + curses), zero network, zero silent telemetry.
Developed by The 747 Lab.
"""
import argparse
import curses
import json
import locale
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
FRAME = 0.033         # ~30 fps, deadline-corrected
POLL_PLAY = 0.12      # state re-read while running
POLL_IDLE = 0.20      # state re-read while paused / banished (never slower: a
                      # slow idle poll is a visibly hung pane at session end)
ASK_TIMEOUT = 45      # ask screen auto-closes after this many seconds
DT_MAX = 0.05         # fixed-timestep spiral guard — the world may never
                      # teleport past a hazard because the pane hitched
DT_REJOIN = 0.25      # a dt spike this big means ghost-pane rejoin: do not simulate

# ---------------------------------------------------------------------------
# THE SPEED CAP IS DERIVED, NOT TUNED.
#
# The player sits at a fixed screen column (PLAYER_COL). Hazards are generated
# and fully rendered the moment they touch the right edge, so the approach is
# (w - 1 - PLAYER_COL) columns — 87 at w=100. The COMMIT LINE is the last
# column from which an input can still clear a shape: 40 columns out.
#
# WORST-CASE TELEGRAPH: 1000 ms / 40 cols at 40 c/s (thrust 10, FINAL 470, w>=90)
#                       1250 ms / 40 cols at 32 c/s (narrow pane, w<90)
# Full read-to-impact at that worst case is 87 cols = 2175 ms, so a shape has
# been legible for ~1175 ms before the decision window even opens. Falcon's
# floor is 1000 ms AND 12 columns; we run 1.0x on time and 3.3x on columns at
# the absolute worst moment of the hardest stretch, and ~4.8x/7x at rest.
#
# If a speed increase would break these numbers, the speed does not increase.
# That is why OVERTIME (past the finish) buys difficulty with DENSITY ONLY and
# never with scroll speed — see step().
# ---------------------------------------------------------------------------
SPEED_CAP_WIDE = 40.0     # c/s at w >= 90
SPEED_CAP_NARROW = 32.0   # c/s at w <  90 (shorter approach, same reaction budget)
SURGE_MAX = 6.0           # c/s of headroom RESERVED for bonus payouts (below)
COMMIT_COLS = 40          # reaction budget, in columns, at the cap
M_PER_COL = 10            # 1 column = 10 metres. 747 columns = 7,470 m.
FINISH_COLS = 747
GATE_COLS = 100           # a gold gate every 1,000 m
FINAL_COLS = 700          # FINAL 470 starts here

# ---- physics, in rows and seconds -----------------------------------------
GRAV = 62.0           # rows/s^2
JUMP_V = 24.9         # sqrt(2*GRAV*5) -> a full-hold jump apexes at 5 rows
JUMP_HOLD = 0.28      # hold this long for the full 5 rows
JUMP_CUT = 0.63       # releasing early keeps this much of the climb (tap ~2 rows)
COYOTE = 0.08         # forgiveness after walking off an edge
SLAM_V = 20.0         # the dive starts here...
SLAM_G = 3.0          # ...and falls at 3x gravity
SLAM_BREAK = 14.0     # fall speed at which a cyan crate shatters instead of hurting
WASH_LIFT = 110.0     # rows/s^2 of upward push inside a magenta band
WASH_VMAX = 18.0      # the band cannot fling you faster than this

THRUST_MAX = 10
THRUST_DECAY = 2.0    # -1 thrust every 2.0 s. This is the engine of the loop:
                      # stop eating and you sink back to 18 c/s.
HULL_MAX = 4          # four pips. Also load-bearing for the egg (see egg_check).

STATS_NAME = "stats-jetwash.json"
NO_STATS = "no-stats"

# ---- glyphs. Set from the locale in __main__ (or 747_ASCII=1): a mojibake
#      corridor is worse than a low-res one. Every object class stays
#      unambiguous by SHAPE alone in both sets — that is the mono test.
UTF = True
PLAYER_CH, PLAYER_BOOST, PLAYER_SLAM = "►", "»", "▼"   # > D v
SOLID = "█"                     # barricade / overhang body
BAR_TOP_L, BAR_TOP_R = "▟", "▙"    # the chamfered top of a barricade
OVER_BOT_L, OVER_BOT_R = "▛", "▜"  # ...and the underside of an overhang
CRATE_CH = "▒"                  # brittle: a DIFFERENT FILL DENSITY, not a hue
WASH_CH = "≈"                   # wavy, never solid, never square
FUEL_CH = "◈"                   # the only non-rectilinear glyph in the game
REPAIR_CH = "+"
GATE_CH = "║"
GATE_OPEN_L, GATE_OPEN_R = "╠", "╣"   # the ring expanding as you cross
GROUND_CH = "▀"
CEIL_CH = "═"
DECK_CH, DECK_FAST = "╱", "═"
STAR_CH, WISP_CH = "·", "-"
TOWER_CH, MAST_CH = "▓", "┬"
HULL_F, HULL_E = "▣", "▢"
THR_F, THR_E = "▮", "▯"
BAR_F, BAR_E = "▸", "▹"
TICK_CH = "˙"                   # the pre-echo radar blip
DEBRIS_CH = "·"
STUDIO, DASHCH, PAUSE_CH = "▌", "·", "⏸"
UP_CH, DOWN_CH, MULT_CH = "↑", "↓", "×"


def use_ascii():
    """Fall back to a pure-ASCII glyph set on a non-UTF-8 terminal."""
    global UTF, PLAYER_CH, PLAYER_BOOST, PLAYER_SLAM, SOLID
    global BAR_TOP_L, BAR_TOP_R, OVER_BOT_L, OVER_BOT_R
    global CRATE_CH, WASH_CH, FUEL_CH, GATE_CH, GATE_OPEN_L, GATE_OPEN_R
    global GROUND_CH, CEIL_CH, DECK_CH, DECK_FAST, STAR_CH, WISP_CH
    global TOWER_CH, MAST_CH, HULL_F, HULL_E, THR_F, THR_E, BAR_F, BAR_E
    global TICK_CH, DEBRIS_CH, STUDIO, DASHCH, PAUSE_CH, UP_CH, DOWN_CH, MULT_CH
    UTF = False
    UP_CH, DOWN_CH, MULT_CH = "^", "v", "x"
    PLAYER_CH, PLAYER_BOOST, PLAYER_SLAM = ">", "D", "v"
    SOLID = "#"
    BAR_TOP_L = BAR_TOP_R = OVER_BOT_L = OVER_BOT_R = "#"
    CRATE_CH = ":"                   # solid # vs brittle : survives with no colour
    WASH_CH = "~"
    FUEL_CH = "*"
    GATE_CH = GATE_OPEN_L = GATE_OPEN_R = "|"
    GROUND_CH, CEIL_CH = "_", "="
    DECK_CH, DECK_FAST = "/", "="
    STAR_CH, WISP_CH = ".", "-"
    TOWER_CH, MAST_CH = "#", "T"
    HULL_F, HULL_E = "#", "-"
    THR_F, THR_E = "|", "."
    BAR_F, BAR_E = ">", "-"
    TICK_CH, DEBRIS_CH = "'", "."
    STUDIO, DASHCH, PAUSE_CH = "|", "-", "||"


# ---------------------------------------------------------------------------
# state protocol — same shape as breakout.py, retitled JETWASH747-
# ---------------------------------------------------------------------------
def state_path(session):
    # guard: never let a session value traverse out of STATE_DIR (defense-in-depth)
    safe = "".join(c for c in session if c.isalnum() or c == "-")
    return os.path.join(STATE_DIR, "state-%s" % safe if safe else "state")


def read_state(session):
    try:
        with open(state_path(session)) as f:
            return f.read().strip()
    except OSError:
        return "thinking"


def write_mode(mode):
    try:
        with open(os.path.join(STATE_DIR, "mode"), "w") as f:
            f.write(mode + "\n")
    except OSError:
        pass


def remove_state(session):
    try:
        os.remove(state_path(session))
    except OSError:
        pass


def set_pane_title(session=""):
    # OSC 2 sets the tmux pane title so the launcher can detect a live game.
    # Session-keyed so the ghost-pane banish/rejoin can find THIS session's run.
    sys.stdout.write("\033]2;JETWASH747-%s\033\\" % (session or "free"))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# stats — LOCAL ONLY. Never transmitted, never read by anything but this file.
# `touch ~/.747-terminal-games/no-stats` and it is never written or read again.
# ---------------------------------------------------------------------------
STATS_DEFAULT = {
    "v": 1, "game": "jetwash", "runs": 0,
    "best_stage": 0,      # best gate reached, 1-8 (8 == crossed the finish)
    "best_score": 0,      # best metres
    "cleared": False,
    "eggs": 0,
    "best_time_ms": 0,    # fastest clear. jetwash-specific: the win is a TIME,
                          # so a distance-only best would hide the real mastery.
    "resumed_after_banish": 0,
}


def stats_disabled():
    return os.path.exists(os.path.join(STATE_DIR, NO_STATS))


def load_stats():
    s = dict(STATS_DEFAULT)
    if stats_disabled():
        return s
    try:
        with open(os.path.join(STATE_DIR, STATS_NAME)) as f:
            got = json.load(f)
        if isinstance(got, dict):
            for k in STATS_DEFAULT:
                if k in got and isinstance(got[k], type(STATS_DEFAULT[k])):
                    s[k] = got[k]
    except (OSError, ValueError):
        pass          # a broken or absent stats file must never break a game
    return s


def save_stats(s):
    """Atomic: write a temp file, then replace. Never called from the frame loop."""
    if stats_disabled():
        return
    tmp = os.path.join(STATE_DIR, STATS_NAME + ".tmp")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(s, f, sort_keys=True)
        os.replace(tmp, os.path.join(STATE_DIR, STATS_NAME))
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def commas(n):
    return "{:,}".format(int(n))


def hsh(a, b):
    """A cheap avalanche hash. The backdrop is generated from world coordinates
    rather than stored, so it costs nothing to scroll — but `x * K % M` is a
    LINEAR sequence, which draws the starfield as evenly spaced vertical lines.
    Mixing is not a nicety here; it is the difference between a sky and a grid."""
    v = (a * 374761393 + b * 668265263) & 0xFFFFFFFF
    v = ((v ^ (v >> 13)) * 1274126177) & 0xFFFFFFFF
    return (v ^ (v >> 16)) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# palette — every role is a same-shape list of opaque attr ints, so no render
# site ever asks "am I in 256 mode?". Pairs 100-139 are the reserved shared
# range (breakout uses 1-8 and 30-56, skyrun 60+).
#
# THE LAW: red = do not touch. cyan = break it. green = collect it. gold = the
# 747 and nothing else. white-bold = you. Shape carries the same information
# again so the whole thing survives with colour switched off.
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
        counter = [100]

        def mk(fg, bold=False):
            i = counter[0]
            counter[0] += 1
            a = ipair(i, fg)
            return (a | curses.A_BOLD) if bold else a

        if has256:
            self.star = [mk(255, True), mk(250), mk(244), mk(238)]
            self.hazard = [mk(210, True), mk(174), mk(96), mk(60)]
            self.target = [mk(195, True), mk(87), mk(44), mk(24)]
            self.pickup = [mk(120, True), mk(40), mk(34), mk(22)]
            self.gold = [mk(226, True), mk(220), mk(178), mk(136)]
            self.player = mk(231, True)
            self.damage = mk(203, True)
            self.struct = [mk(250), mk(244), mk(238)]
            self.text_hi = mk(231, True)
            self.text = mk(189)
            self.text_dim = mk(103)
            self.accent = [mk(141), mk(97), mk(61)]
        else:
            red, ylw, grn = cpair(1), cpair(2), cpair(3)
            cyn, mag, wht = cpair(4), cpair(5), cpair(6)
            dim = curses.A_DIM
            self.star = [curses.A_NORMAL, dim, dim, dim]
            self.hazard = [red | curses.A_BOLD, red, red, red | dim]
            self.target = [cyn | curses.A_BOLD, cyn, cyn, cyn | dim]
            self.pickup = [grn | curses.A_BOLD, grn, grn, grn | dim]
            self.gold = [ylw | curses.A_BOLD, ylw, ylw, ylw | dim]
            self.player = wht | curses.A_BOLD
            self.damage = red | curses.A_BOLD
            self.struct = [wht, wht | dim, dim]
            self.text_hi = wht | curses.A_BOLD
            self.text = curses.A_NORMAL
            self.text_dim = dim
            self.accent = [mag, mag | dim, dim]


# ---------------------------------------------------------------------------
# the four-engine heavy. Same 5-row bitmap language breakout's brick wall and
# skyrun's gate speak — the 747 is in the TEXTURE of the far sky, never on the
# label. It has no hitbox, never flashes, and is drawn dim and un-bold so it
# can never be mistaken for a threat. Second-look discovery.
# ---------------------------------------------------------------------------
HEAVY = [
    "......#......",
    "......#......",
    "#############",
    "..#.#...#.#..",
    ".....###.....",
]

# object kinds
K_BAR, K_OVER, K_CRATE, K_WASH, K_FUEL, K_REPAIR, K_GATE, K_PIT = range(8)
HAZARD_KINDS = (K_BAR, K_OVER, K_CRATE, K_PIT)


class Obj(object):
    """One world object. x is an absolute WORLD COLUMN (floats never needed:
    terrain is authored on the column grid, which is what makes the gutter rule
    checkable instead of hopeful)."""
    __slots__ = ("kind", "x", "w", "y0", "y1", "dead", "flag", "hit")

    def __init__(self, kind, x, w, y0, y1, flag=0):
        self.kind = kind
        self.x = x            # leftmost world column
        self.w = w            # width in columns
        self.y0 = y0          # topmost row (screen row, layout is fixed)
        self.y1 = y1          # bottom row, inclusive
        self.dead = False
        self.flag = flag      # K_CRATE: 1 == authored as a bridge over a pit
        self.hit = False      # already resolved against the player


class Jet(object):
    # ---- the hand-authored opening. The first ~100 columns are terrain, not a
    #      spawner. This is the Mario 1-1 method and it cannot be delegated:
    #      every beat teaches exactly one thing and never two at once.
    #
    #      (kind, world column, parameter)   dy = rows above the rest height
    #      NOTE ON TIMING: the contract's beat table quotes wall-clock seconds
    #      at a standing start. Scroll speed is THRUST and thrust is what the
    #      player is collecting, so the clock compresses as they learn — all
    #      eight beats land in ~5.5 s rather than 8.0 s. The ORDER is the
    #      contract; the seconds are the player's.
    OPENING = [
        # 0-17: EMPTY. Ground, ceiling, near layer streaking. Nothing to get wrong.
        ("fuel", 20, 0),                      # arrives at rest height: zero input
        ("fuel", 30, 0), ("fuel", 34, 2), ("fuel", 37, 3),   # the inviting arc
        ("bar", 46, 2),                       # red on the floor -> JUMP (same height)
        ("fuel", 56, 0),                      # BREATH. never stack two beats.
        ("over", 66, 3), ("fuel", 66, 0),     # red on the ceiling -> STAY LOW.
                                              # the bait is DELIBERATELY in the
                                              # hazard's column: it puts your body
                                              # exactly where safety is.
        ("fuel", 76, 1), ("fuel", 79, 3), ("fuel", 82, 4),   # the arc that lands...
        ("crate", 85, 0),                     # ...on a crate, hard enough to break
                                              # it. No special-case tutorial crate,
                                              # just honest physics plus placement.
        # 87-99: debris settles, CHAIN visible. Then GATE 1 lands at column 100.
    ]

    def __init__(self, scr, session, pal, stats, heavy_mode=False):
        self.scr = scr
        self.session = session
        self.pal = pal
        self.stats = stats
        self.rng = random.Random()
        self.heavy_mode = heavy_mode
        self.manual_play = False
        self.state = "thinking"
        self.idle_drawn = False
        self.tiny = False
        self.too_small = False
        self.h = self.w = 0
        self.cb = []
        self.ab = []
        h, w = scr.getmaxyx()
        self.on_resize(h, w)
        self.reset_run()

    # ---- layout -----------------------------------------------------------
    def on_resize(self, h, w):
        """Rebuild the layer plan. Called EVERY frame that the pane changed —
        a live ghost cycle resizes a pane three or more times.

        The plan is built even when the pane is UNDER the size floor, from a
        clamped virtual size. A too-small pane draws a banner instead of a
        world, but it must never be the difference between a live object and a
        half-constructed one: the pane can be dragged back up at any moment and
        every field has to be where the run left it."""
        self.h, self.w = h, w
        self.cb = [[None] * w for _ in range(h)]
        self.ab = [[0] * w for _ in range(h)]
        self.too_small = (h < 8 or w < 80)
        lh, lw = max(8, h), max(80, w)
        old = (getattr(self, "pb_top", None), getattr(self, "pb_bot", None))
        n = lh - 2                                 # rows 1 .. lh-2 are ours
        self.play_rows = int(clamp(n - 10, 4, 8))  # the play band never grows
        rest = n - self.play_rows - 2              # ...so jump heights are the
        under = min(3, max(0, rest // 3))          # same at every pane size
        rest -= under
        skyline = min(3, max(0, rest // 2))
        rest -= skyline
        far = max(0, rest)
        r = 1
        self.far_top, self.far_rows = r, far
        r += far
        self.sky_top, self.sky_rows = r, skyline
        r += skyline
        self.ceil_row = r
        r += 1
        self.pb_top = r
        r += self.play_rows
        self.pb_bot = r - 1
        self.ground_row = r
        r += 1
        self.deck_top, self.deck_rows = r, under
        self.foot_row = lh - 1
        # the player's column, and therefore the approach length and the cap
        self.px = 12 if lw >= 90 else max(6, lw // 8)
        self.cap = SPEED_CAP_WIDE if lw >= 90 else SPEED_CAP_NARROW
        self.approach = lw - 1 - self.px
        self.rest_y = float(self.pb_bot)
        if old[0] is not None and old != (self.pb_top, self.pb_bot):
            self.remap_rows(old[0], old[1])

    def remap_rows(self, o_top, o_bot):
        """The band moved under a live world. Re-anchor every object to the
        surface it belongs to — ground things to the ground, ceiling things to
        the ceiling — instead of leaving them floating at rows that no longer
        mean anything. Without this, one ghost-pane resize turns a barricade
        into an obstacle hanging in mid-air."""
        dtop = self.pb_top - o_top
        dbot = self.pb_bot - o_bot
        for o in getattr(self, "objs", []):
            if o.kind == K_OVER:
                o.y0 += dtop
                o.y1 += dtop
            elif o.kind == K_GATE:
                o.y0, o.y1 = self.pb_top, self.pb_bot
            elif o.kind == K_PIT:
                o.y0 = o.y1 = self.ground_row
            else:
                o.y0 += dbot
                o.y1 += dbot
            o.y0 = int(clamp(o.y0, self.pb_top, self.pb_bot))
            o.y1 = int(clamp(o.y1, o.y0, self.pb_bot))
        self.py = clamp(self.py + dbot, float(self.pb_top), float(self.pb_bot))

    # ---- run state --------------------------------------------------------
    def reset_run(self):
        self.dist = 0.0            # world columns travelled. metres = dist * 10.
        self.gen_x = 0             # world column terrain has been authored to
        self.objs = []
        self.next_feature = 108    # the spawner takes over after the opening
        self.feature_n = 0
        self.taught = set()
        self.py = self.rest_y
        self.vy = 0.0
        self.grounded = True
        self.coyote = 0.0
        self.jump_t = -1.0         # >=0 while a jump is being held
        self.slamming = False
        self.thrust = 0.0
        self.decay_t = 0.0
        self.surge = 0.0           # metres owed, paid out of reserved headroom
        self.hull = HULL_MAX
        self.chain = 0
        self.clean_gate = True     # no hit since the last ring -> repair on cross
        self.gate = 1
        self.invuln = 0.0
        self.hitstop = 0.0
        self.shake = 0.0
        self.shake_x = self.shake_y = 0
        self.invert = 0            # frames of full-screen red inversion
        self.white = 0             # frames of full-width white flash
        self.flashes = []          # [y0, x0, y1, x1, frames] local white bursts
        self.debris = []           # [x, y, vx, vy, life]
        self.floats = []           # [text, x, y, life, attr]
        self.play_t = 0.0          # SIM time. a ghost gap must not eat the
                                   # 15-second teach footer or the clear time.
        self.heavy_t = 6.0
        self.heavy_x = None
        self.egg_heavy = 0.0
        self.cleared = False
        self.clear_t = 0.0
        self.banner = 0.0
        self.pit_out = 0.0         # >0 while being fished out of a pit
        self.dead = False
        if self.heavy_mode:
            # unlocked by a first clear: gate-4 pacing from column zero
            self.hull = 3
            self.thrust = 6.0
        self.author_opening()

    def on_rejoin(self):
        """A ghost-paned return must never resume mid-anything. Every
        accumulator a wall-clock gap could corrupt is zeroed here; the frame
        that follows is also skipped entirely (dt = 0 in run())."""
        self.hitstop = 0.0
        self.shake = 0.0
        self.shake_x = self.shake_y = 0
        self.invert = 0
        self.white = 0
        self.flashes = []
        self.debris = []
        self.floats = []
        self.invuln = 0.0
        self.jump_t = -1.0
        self.coyote = 0.0
        self.decay_t = 0.0
        self.pit_out = 0.0
        self.banner = 0.0
        self.idle_drawn = False

    # ---- terrain ----------------------------------------------------------
    def add(self, kind, x, w, y0, y1, flag=0):
        """The ONLY way an object enters the world, so the play band is a hard
        boundary rather than a convention. At four playfield rows an authored
        coin arc peaking four rows up would otherwise land a pickup ON the
        ceiling girder — structure, not playfield. Pits are exempt: they live
        on the ground line by definition."""
        if kind != K_PIT:
            y0 = int(clamp(y0, self.pb_top, self.pb_bot))
            y1 = int(clamp(y1, y0, self.pb_bot))
        self.objs.append(Obj(kind, x, w, y0, y1, flag))

    def author_opening(self):
        """Beat for beat, by hand. Nothing here is procedural."""
        for kind, x, p in self.OPENING:
            if kind == "fuel":
                self.add(K_FUEL, x, 1, self.pb_bot - p, self.pb_bot - p)
            elif kind == "bar":
                hgt = min(p, self.play_rows - 2)
                self.add(K_BAR, x, 2, self.pb_bot - hgt + 1, self.pb_bot)
            elif kind == "over":
                dep = min(p, max(1, self.play_rows - 4))
                self.add(K_OVER, x, 2, self.pb_top, self.pb_top + dep - 1)
            elif kind == "crate":
                self.add(K_CRATE, x, 2, self.pb_bot - 1, self.pb_bot)
                # a fuel cell visible INSIDE the dither: the reward is legible
                # before the verb that earns it is understood
                self.add(K_FUEL, x, 1, self.pb_bot - 1, self.pb_bot - 1)
        # 99, not 100: gen_ahead() pre-increments, so gen_x is the LAST column
        # already authored. Off by one here and GATE 1 never spawns at all.
        self.gen_x = 99
        self.taught.add(K_BAR)
        self.taught.add(K_CRATE)

    def gate_index(self, x):
        return int(min(7, x // GATE_COLS + 1))

    def unlocked(self, g):
        """One new verb per gate, never two."""
        k = [K_BAR]
        if g >= 2:
            k.append(K_OVER)
        if g >= 3:
            k.append(K_CRATE)
        if g >= 4:
            k.append(K_WASH)
        return k

    def gen_ahead(self):
        """Author terrain out to the right edge and a little beyond. Objects
        are born FULLY RENDERED at the edge — the approach IS the telegraph."""
        want = int(self.dist) + self.approach + 8
        while self.gen_x < want:
            self.gen_x += 1
            x = self.gen_x
            if x % GATE_COLS == 0 and 0 < x <= FINAL_COLS:
                # the ring. Two columns, full play-band height, and the only
                # gold in the game: 7 gates, 7,470 m, FINAL 470.
                self.add(K_GATE, x, 2, self.pb_top, self.pb_bot)
                self.next_feature = max(self.next_feature, x + 6)
                continue
            if x >= self.next_feature:
                self.spawn_feature(x)

    def gap_for(self, g, x):
        """Difficulty is DENSITY and SPEED, never a shorter telegraph. Past the
        finish only this number moves — the scroll cap does not."""
        base = 30.0 - 2.0 * (g - 1)
        if x >= FINAL_COLS:
            base /= 1.6
        elif g >= 7:
            base /= 1.3
        if self.heavy_mode:
            base /= 1.25
        over = max(0.0, (x - FINISH_COLS) / 250.0)     # OVERTIME: density only
        base /= (1.0 + 0.18 * over)
        return max(10.0, base + self.rng.uniform(-3.0, 4.0))

    def spawn_feature(self, x):
        """Place one feature and reserve the gutter after it.

        THE GUTTER RULE, enforced structurally rather than hoped for: the next
        feature cannot start until this one's width plus a gap has passed, and
        the gap floor is 10 columns — so two different classes are never within
        two columns of each other and nothing can merge into an unreadable blob.
        """
        # THE RING OWNS ITS OWN AIR. gen_ahead() reserves six columns after a
        # gate, but a feature scheduled BEFORE one can still run into it — and
        # a red barricade drawn flush against the gold ring is exactly the
        # two-classes-merged failure the gutter rule exists to prevent.
        gcol = int(math.ceil((x - 12) / float(GATE_COLS))) * GATE_COLS
        if 0 < gcol <= FINAL_COLS and gcol - 12 <= x <= gcol + 3:
            self.next_feature = gcol + 4
            return
        g = self.gate_index(x)
        self.feature_n += 1
        avail = self.unlocked(g)
        # A NEW CLASS IS ALWAYS INTRODUCED SOLO, with a clear gutter either side.
        new = [k for k in avail if k not in self.taught]
        if new:
            kind = new[0]
            self.taught.add(kind)
        elif self.feature_n % 6 == 0:
            # BREATH: unbroken pressure makes fatigue, and fatigue closes the pane.
            self.fuel_arc(x)
            self.next_feature = x + 22
            return
        elif g >= 6 and self.rng.random() < 0.22:
            self.spawn_pit(x)
            return
        elif g >= 5 and self.rng.random() < 0.20:
            self.spawn_thread(x)
            return
        else:
            kind = avail[self.rng.randrange(len(avail))]

        wid = 2
        if kind == K_BAR:
            hgt = 1 if self.play_rows <= 5 else (3 if (g >= 5 and self.rng.random() < 0.3) else 2)
            self.add(K_BAR, x, wid, self.pb_bot - hgt + 1, self.pb_bot)
            if self.rng.random() < 0.25:      # the reward sits on the brave line
                self.add(K_FUEL, x, 1, self.pb_bot - hgt - 2, self.pb_bot - hgt - 2)
        elif kind == K_OVER:
            dep = min(3 if self.play_rows > 5 else 1, max(1, self.play_rows - 4))
            self.add(K_OVER, x, wid, self.pb_top, self.pb_top + dep - 1)
            if self.rng.random() < 0.25:
                self.add(K_FUEL, x, 1, self.pb_bot, self.pb_bot)
        elif kind == K_CRATE:
            self.add(K_CRATE, x, wid, self.pb_bot - 1, self.pb_bot)
            if self.rng.random() < 0.4:
                self.add(K_FUEL, x, 1, self.pb_bot - 1, self.pb_bot - 1)
        elif kind == K_WASH:
            rows = 3 if self.play_rows > 5 else 2
            top = self.pb_bot - rows - 1
            wid = 3
            self.add(K_WASH, x, wid, top, top + rows - 1)
            # the band is only worth entering if there is a reason to be high
            self.add(K_FUEL, x + 1, 1, top - 2, top - 2)
        if self.rng.random() < 0.10:
            self.add(K_REPAIR, x + wid + 3, 1, self.pb_bot - 2, self.pb_bot - 2)
        self.next_feature = x + wid + int(self.gap_for(g, x))

    def spawn_thread(self, x):
        """Gate 5: barricade and overhang in the same column set, leaving a
        two-row slot. No new input — new precision."""
        hgt = 2 if self.play_rows > 5 else 1
        slot = 2
        dep = max(1, self.play_rows - hgt - slot)
        self.add(K_BAR, x, 2, self.pb_bot - hgt + 1, self.pb_bot)
        self.add(K_OVER, x, 2, self.pb_top, self.pb_top + dep - 1)
        self.next_feature = x + 2 + int(self.gap_for(self.gate_index(x), x))

    def spawn_pit(self, x):
        """Gate 6: a hole in the ground, sometimes bridged by a crate you must
        NOT break. The first inversion of a learned rule."""
        wid = self.rng.randrange(5, 9)
        self.add(K_PIT, x, wid, self.ground_row, self.ground_row)
        if self.rng.random() < 0.45:
            bx = x + max(0, (wid - 2) // 2)
            self.add(K_CRATE, bx, 2, self.pb_bot - 1, self.pb_bot, 1)
        self.next_feature = x + wid + int(self.gap_for(self.gate_index(x), x))

    def fuel_arc(self, x):
        """Three cells in a rising arc. The first sits at rest height, so the
        arc INVITES rather than demands — missing it costs nothing."""
        for i, dy in enumerate((0, 3, 4)):
            self.add(K_FUEL, x + i * 4, 1, self.pb_bot - dy, self.pb_bot - dy)

    # ---- input ------------------------------------------------------------
    def drain(self):
        scr = self.scr
        ch = scr.getch()
        while ch != -1:
            if ch in (ord("q"), ord("Q")):
                return "quit"
            elif ch == ord(" ") and not (self.state == "thinking" or self.manual_play):
                self.manual_play = True     # play anyway while Claude's reply waits
                self.idle_drawn = False
            elif ch in (curses.KEY_UP, ord(" "), ord("w"), ord("k")):
                self.press_jump()
            elif ch in (curses.KEY_DOWN, ord("s"), ord("j")):
                self.press_slam()
            ch = scr.getch()
        return None

    def press_jump(self):
        if self.grounded or self.coyote > 0.0:
            self.vy = -JUMP_V
            self.jump_t = 0.0
            self.grounded = False
            self.coyote = 0.0
            self.slamming = False

    def press_slam(self):
        if self.jump_t >= 0.0:
            self.jump_t = -1.0
            if self.vy < 0.0:
                self.vy *= JUMP_CUT
        if not self.grounded:
            self.slamming = True
            self.vy = max(self.vy, SLAM_V)

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        self.play_t += dt
        if self.invuln > 0.0:
            self.invuln = max(0.0, self.invuln - dt)
        if self.invert > 0:
            self.invert -= 1
        if self.white > 0:
            self.white -= 1
        if self.banner > 0.0:
            self.banner = max(0.0, self.banner - dt)
        if self.egg_heavy > 0.0:
            self.egg_heavy = max(0.0, self.egg_heavy - dt)
        for f in self.flashes:
            f[4] -= 1
        self.flashes = [f for f in self.flashes if f[4] > 0]

        # HITSTOP. The world stops; the frame keeps arriving. Cheapest "that
        # connected" there is, and with no audio it is doing all the work.
        if self.hitstop > 0.0:
            self.hitstop = max(0.0, self.hitstop - dt)
            self.decay_shake(dt)
            return False

        # ---- thrust: the whole economy. Eat to go faster, or sink back. ----
        floor = 6.0 if int(self.dist) >= FINAL_COLS else 0.0
        self.decay_t += dt
        while self.decay_t >= THRUST_DECAY:
            self.decay_t -= THRUST_DECAY
            self.thrust = max(floor, self.thrust - 1.0)
        self.thrust = max(self.thrust, floor)

        # ---- scroll. base speed is capped BELOW the derived cap so that the
        #      bonus surge always has somewhere to go: a +100 m award is paid
        #      as real forward motion out of reserved headroom, which is how
        #      ONE NUMBER can be both "distance" and "score" without the HUD
        #      ever teleporting (and without the telegraph ever shrinking).
        base_cap = self.cap - SURGE_MAX
        base = min(18.0 + 1.6 * self.thrust, base_cap)
        pay = min(SURGE_MAX, self.surge * 3.0) if self.surge > 0.0 else 0.0
        cps = base + pay
        moved = cps * dt
        if pay > 0.0:
            self.surge = max(0.0, self.surge - pay * dt)
        prev = self.dist
        self.dist += moved
        self.gen_ahead()

        # ---- player physics ----
        if self.pit_out > 0.0:
            self.pit_out = max(0.0, self.pit_out - dt)
            if self.pit_out == 0.0:
                self.py = self.rest_y
                self.vy = 0.0
                self.grounded = True
            self.decay_shake(dt)
            self.tick_fx(dt)
            return False

        prev_py = self.py
        g = GRAV * (SLAM_G if self.slamming else 1.0)
        if self.jump_t >= 0.0:
            self.jump_t += dt
            if self.jump_t >= JUMP_HOLD:
                self.jump_t = -1.0            # full hold: let gravity have it
            else:
                self.vy = -JUMP_V             # sustain the climb while held
        self.vy += g * dt
        # jetwash bands: lift, and the reason the game is called what it is
        if self.in_wash():
            self.vy -= WASH_LIFT * dt
            self.vy = max(self.vy, -WASH_VMAX)
        self.py += self.vy * dt
        if self.py < self.pb_top:
            self.py = float(self.pb_top)
            self.vy = max(self.vy, 0.0)

        # ---- support: the ground, or the top of a crate ----
        # SWEPT against prev_py, never against the current row. A slam covers
        # two rows in a frame, so a "am I standing on it?" test would tunnel
        # straight through the crate lid — and the break would silently become
        # a side collision, which is the opposite outcome for the player.
        support = None if self.over_pit() else float(self.pb_bot)
        for o in self.crates_here():
            top = float(o.y0 - 1)
            if prev_py > top + 0.5 or self.vy < 0.0:
                continue                       # came at it from the side
            if self.vy >= SLAM_BREAK and self.py >= top:
                self.break_crate(o)            # smashed clean through the lid
                continue
            support = top if support is None else min(support, top)
        was_ground = self.grounded
        self.grounded = False
        if support is not None and self.py >= support and self.vy >= 0.0:
            landed_hard = self.vy >= SLAM_BREAK
            self.py = support
            self.grounded = True
            if landed_hard and self.slamming:
                self.landing_shock()
            self.vy = 0.0
            self.slamming = False
            self.jump_t = -1.0
        if was_ground and not self.grounded and self.vy >= 0.0:
            self.coyote = COYOTE
        elif self.coyote > 0.0:
            self.coyote = max(0.0, self.coyote - dt)

        # fell into a pit
        if self.py > self.pb_bot + 1.0:
            self.take_hit("pit")
            self.pit_out = 0.4
            self.decay_shake(dt)
            self.tick_fx(dt)
            return self.hull <= 0

        self.collide(prev)
        self.gate_check(prev)
        self.sweep_passed()
        # ONE SOURCE OF TRUTH for "which gate am I in". Derived from distance,
        # never from a counter that a missed crossing could drift out of sync.
        self.gate = 8 if self.cleared else min(8, int(self.dist // GATE_COLS) + 1)
        self.cull()
        self.decay_shake(dt)
        self.tick_fx(dt)

        # ---- the heavy that crosses the far sky, every ~18 s ----
        if self.heavy_x is None:
            # only counts down where there is a sky band to cross; a pane with
            # no far sky simply never sees one
            if self.far_rows >= 3:
                self.heavy_t -= dt
                if self.heavy_t <= 0.0:
                    self.heavy_x = float(self.w + 14)
        else:
            self.heavy_x -= 9.0 * dt
            if self.heavy_x < -16.0 or self.far_rows < 3:
                self.heavy_x = None
                self.heavy_t = 18.0

        # ---- the finish ----
        if not self.cleared and self.dist >= FINISH_COLS:
            self.finish()
        return self.hull <= 0

    def decay_shake(self, dt):
        if self.shake > 0.0:
            self.shake = max(0.0, self.shake - dt * 9.0)
            self.shake_x = self.rng.randrange(-1, 2) if self.shake > 0.3 else 0
            self.shake_y = self.rng.randrange(-1, 2) if self.shake > 0.7 else 0
        else:
            self.shake_x = self.shake_y = 0

    def tick_fx(self, dt):
        """Debris PERSISTS 0.4 s. A flash you can miss by blinking is not
        feedback; debris still on screen half a second later is proof the world
        reacted to you."""
        for d in self.debris:
            d[0] += d[2] * dt
            d[1] += d[3] * dt
            d[3] += 40.0 * dt
            d[4] -= dt
        self.debris = [d for d in self.debris if d[4] > 0.0]
        for f in self.floats:
            f[2] -= 8.6 * dt
            f[3] -= dt
        self.floats = [f for f in self.floats if f[3] > 0.0]

    def cull(self):
        left = self.dist - 6
        if len(self.objs) > 24:
            self.objs = [o for o in self.objs if not o.dead and o.x + o.w >= left]

    # ---- collision --------------------------------------------------------
    def spans(self, o, prev):
        """Swept test: at 40 c/s and a clamped dt the world can move two whole
        columns in a frame, so a one-column pickup must never be stepped over."""
        return o.x < self.dist + 0.75 and o.x + o.w > prev - 0.25

    def crates_here(self):
        out = []
        for o in self.objs:
            if o.kind == K_CRATE and not o.dead and o.x <= self.dist + 0.5 < o.x + o.w:
                out.append(o)
        return out

    def over_pit(self):
        for o in self.objs:
            if o.kind == K_PIT and o.x - 0.5 <= self.dist < o.x + o.w - 0.5:
                return True
        return False

    def in_wash(self):
        row = self.py
        for o in self.objs:
            if o.kind == K_WASH and o.x <= self.dist < o.x + o.w:
                if o.y0 - 1 <= row <= o.y1 + 1:
                    return True
        return False

    def collide(self, prev):
        row = self.py
        rr = int(round(row))
        for o in self.objs:
            if o.dead or o.hit or not self.spans(o, prev):
                continue
            if o.kind == K_FUEL:
                if abs(row - o.y0) <= 0.9:
                    o.dead = True
                    o.hit = True
                    self.collect_fuel(o)
            elif o.kind == K_REPAIR:
                if abs(row - o.y0) <= 0.9:
                    o.dead = True
                    o.hit = True
                    self.hull = min(HULL_MAX, self.hull + 1)
                    self.floats.append(["+HULL", o.x, float(o.y0), 0.55, self.pal.pickup[0]])
                    self.flash_cell(o.y0, o.x, o.y0, o.x)
            elif o.kind == K_BAR:
                if rr >= o.y0:
                    self.take_hit("bar", o)
            elif o.kind == K_OVER:
                if rr <= o.y1:
                    self.take_hit("over", o)
            elif o.kind == K_CRATE:
                # landing from above is resolved by the swept support pass in
                # step(); anything left here arrived from the SIDE, which is
                # the "bounced off it instead of slamming it" hit.
                if row <= o.y0 - 0.5:
                    continue
                self.take_hit("crate", o)

    def sweep_passed(self):
        """CLEAN CHAIN: a hazard survived is the only thing that feeds it.

        Deliberately a sweep over everything behind the player rather than a
        test inside collide(): collide() only ever sees objects that are
        SPANNING the player's column, and at 40 c/s with a clamped dt that
        window is ~1.2 columns wide — narrow enough to occasionally miss, which
        would silently eat a chain the player earned."""
        for o in self.objs:
            if o.hit or o.dead or o.kind not in HAZARD_KINDS:
                continue
            if o.x + o.w < self.dist:
                o.hit = True
                self.chain += 1
                if self.chain % 10 == 0:
                    # the quiet wink. Styled exactly like breakout's 47-point
                    # glyph bricks: it pays, it never announces.
                    self.award(47, "+47 m", self.dist,
                               (self.pb_top + self.pb_bot) // 2)

    def collect_fuel(self, o):
        self.thrust = min(float(THRUST_MAX), self.thrust + 1.0)
        # NO hitstop on a collect. Never interrupt flow to reward.
        self.flash_cell(o.y0, o.x, o.y0, o.x)
        self.floats.append(["+1", float(o.x), float(o.y0), 0.35, self.pal.pickup[0]])

    def break_crate(self, o):
        o.dead = True
        o.hit = True
        self.hitstop = 0.07
        self.shake = 1.6
        self.flash_cell(o.y0, o.x, o.y1, o.x + o.w - 1)
        self.award(100, "+100 m", o.x, o.y0 - 1)
        self.chain += 1
        for i in range(5):
            a = i * 1.2566
            self.debris.append([float(o.x + (i % 2)), float(o.y0 + (i % 2)),
                                math.cos(a) * 16.0, math.sin(a) * 9.0 - 6.0, 0.4])
        self.vy = -JUMP_V * 0.42          # the dive bounces you back up
        self.slamming = False

    def landing_shock(self):
        """DOWNWELL's dive: the landing itself is an attack. Anything brittle
        within two columns goes with it."""
        self.shake = max(self.shake, 1.2)
        for o in self.objs:
            if o.kind == K_CRATE and not o.dead and abs(o.x - self.dist) <= 2.5:
                self.break_crate(o)

    def award(self, metres, label, x, y):
        """A bonus is REAL FORWARD MOTION, paid out of reserved speed headroom.
        The number never teleports, and the player can see which action paid."""
        self.surge += metres / float(M_PER_COL)
        self.floats.append([label, float(x), float(y), 0.55, self.pal.text_hi])

    def flash_cell(self, y0, x0, y1, x1):
        self.flashes.append([y0, x0, y1, x1, 2])

    def take_hit(self, why, o=None):
        if o is not None:
            o.hit = True
        if self.invuln > 0.0:
            return
        self.hull -= 1
        self.invuln = 0.5
        self.hitstop = 0.10
        self.invert = 1              # one frame of full-screen red
        self.shake = 2.4
        self.chain = 0
        self.clean_gate = False
        # THE REAL COST IS THE CLOCK. Thrust to the floor is ~300 m of lost
        # progress — damage does not erase the mistake, it converts it into
        # distance you now have to fly again.
        self.thrust = min(self.thrust, 2.0)
        self.surge = 0.0
        if why == "crate":
            self.vy = -JUMP_V * 0.35
        for i in range(4):
            self.debris.append([self.dist, self.py, 10.0 - i * 7.0,
                                -8.0 + i * 3.0, 0.4])

    def gate_check(self, prev):
        for o in self.objs:
            if o.kind != K_GATE or o.hit:
                continue
            if o.x <= self.dist and o.x + o.w > prev - 0.25:
                o.hit = True
                self.white = 2
                self.award(200, "+200 m", o.x, (self.pb_top + self.pb_bot) // 2)
                if self.clean_gate and self.hull < HULL_MAX:
                    self.hull += 1
                    self.floats.append(["+HULL", float(o.x),
                                        float((self.pb_top + self.pb_bot) // 2 + 1),
                                        0.55, self.pal.pickup[0]])
                self.clean_gate = True
                nxt = min(8, int(o.x // GATE_COLS) + 1)
                if self.stats["best_stage"] < nxt:
                    self.stats["best_stage"] = nxt

    # ---- the finish, and the egg -----------------------------------------
    def finish(self):
        self.cleared = True
        self.clear_t = self.play_t
        self.banner = 3.0
        self.white = 2
        self.gate = 8
        self.stats["cleared"] = True
        self.stats["best_stage"] = max(self.stats["best_stage"], 8)
        ms = int(self.clear_t * 1000)
        if self.stats["best_time_ms"] == 0 or ms < self.stats["best_time_ms"]:
            self.stats["best_time_ms"] = ms
        self.egg_check()

    def egg_check(self):
        """THE HIDDEN 747, layer three. Cross the finish with THRUST exactly 7,
        HULL exactly 4 and a CHAIN that is a multiple of 7 and the heavy drops
        out of the sky band to fly alongside you for three seconds, engines
        lit gold, while the metres field reads 747.

        No achievement text, no announcement. The hull is FOUR because of this
        egg — the brand defines the mechanic, not the other way round."""
        if (int(self.thrust) == 7 and self.hull == HULL_MAX
                and self.chain > 0 and self.chain % 7 == 0):
            self.egg_heavy = 3.0
            self.stats["eggs"] += 1

    # ---- rendering --------------------------------------------------------
    def put(self, y, x, ch, attr):
        if 0 <= y < self.h and 0 <= x < self.w:
            self.cb[y][x] = ch
            self.ab[y][x] = attr

    def text_at(self, y, x, s, attr):
        for i, c in enumerate(s):
            self.put(y, x + i, c, attr)

    def render(self, playing):
        if self.too_small:
            self.render_too_small()
            return
        w, h = self.w, self.h
        pal = self.pal
        for y in range(h):
            row_c = self.cb[y]
            row_a = self.ab[y]
            for x in range(w):
                row_c[x] = None
                row_a[x] = 0

        self.draw_backdrop()
        self.draw_structure()
        self.draw_objects()
        self.draw_fx()
        self.draw_player(playing)
        self.draw_hud()
        self.draw_footer()
        if self.banner > 0.0:
            self.overlay([
                "RUN CLEARED  %s 7,470 m %s %.1fs" % (DASHCH, DASHCH, self.clear_t),
                "OVERTIME %s the corridor keeps coming" % DASHCH,
            ], pal.gold[0])
        if not playing:
            self.overlay(["%s  CLAUDE'S DONE %s READING TIME" % (PAUSE_CH, DASHCH),
                          "resumes on your next prompt %s [space] play anyway" % DASHCH],
                         pal.text_hi)
        self.blit()

    def draw_backdrop(self):
        """Parallax. SPEED IS SOLD BY THE FOREGROUND: the 1.8x under-deck does
        the work so the play band can scroll slowly enough to actually read.
        Nothing back here is ever bold — a hazard is always bold, so a backdrop
        can never be misread as a threat."""
        pal = self.pal
        w = self.w
        far = int(self.dist * 0.15)
        for i in range(self.far_rows):
            y = self.far_top + i
            for x in range(w):
                k = hsh(far + x, y) % 200
                if k < 5:
                    self.put(y, x, STAR_CH, pal.star[2])
                elif k == 7:
                    self.put(y, x, WISP_CH, pal.text_dim)
        if self.heavy_x is not None and self.far_rows >= 3:
            self.draw_heavy(int(self.heavy_x), self.far_top, pal.text_dim,
                            self.far_rows)
        sky = int(self.dist * 0.35)
        if self.sky_rows:
            for x in range(w):
                blk = (sky + x) // 5
                if hsh(blk, 3) % 10 >= 6:
                    continue
                if (sky + x) - blk * 5 >= 3 + hsh(blk, 7) % 3:
                    continue
                hgt = 1 + hsh(blk, 5) % self.sky_rows
                for i in range(hgt):
                    self.put(self.sky_top + self.sky_rows - 1 - i, x,
                             TOWER_CH, pal.struct[2])
                if hsh(blk, 9) % 5 == 0 and hgt < self.sky_rows:
                    self.put(self.sky_top + self.sky_rows - 1 - hgt, x,
                             MAST_CH, pal.struct[2])
        deck = int(self.dist * 1.8)
        fast = self.thrust >= 7
        ch = DECK_FAST if fast else DECK_CH
        for i in range(self.deck_rows):
            y = self.deck_top + i
            for x in range(w):
                if (deck + x + i * 2) % 3 == 0:
                    self.put(y, x, ch, self.pal.text_dim)

    def draw_structure(self):
        pal = self.pal
        w = self.w
        for x in range(w):
            self.put(self.ceil_row, x, CEIL_CH, pal.struct[1])
        pits = [o for o in self.objs if o.kind == K_PIT and not o.dead]
        for x in range(w):
            wx = self.dist + (x - self.px)
            gap = False
            for o in pits:
                if o.x - 0.5 <= wx < o.x + o.w - 0.5:
                    gap = True
                    break
            if not gap:
                self.put(self.ground_row, x, GROUND_CH, pal.struct[0])
        # PRE-ECHO: a dim radar blip at the edge, six columns before the shape
        # itself resolves. It lands the eye in the right place first.
        edge = self.dist + self.approach
        for o in self.objs:
            if o.dead or o.kind not in HAZARD_KINDS:
                continue
            if edge < o.x <= edge + 6:
                row = self.ceil_row if o.kind == K_OVER else self.ground_row
                self.put(row, w - 1, TICK_CH, pal.text_dim)

    def sx(self, wx):
        return int(round(self.px + (wx - self.dist))) + self.shake_x

    def draw_objects(self):
        pal = self.pal
        vis = []
        for o in self.objs:
            if o.dead or o.kind in (K_PIT,):
                continue
            x0 = self.sx(o.x)
            if x0 > self.w or x0 + o.w < 0:
                continue
            vis.append((o, x0))
        # THE 1-CELL GUTTER, drawn. The spawner already guarantees two empty
        # columns between classes; blanking each silhouette's perimeter inside
        # the play band before ANY body is drawn is the belt to that braces, so
        # two shapes can never fuse into one unreadable mass.
        for o, x0 in vis:
            if o.kind in (K_FUEL, K_REPAIR, K_GATE):
                continue
            for y in range(o.y0 - 1, o.y1 + 2):
                if not (self.pb_top <= y <= self.pb_bot):
                    continue
                for x in range(x0 - 1, x0 + o.w + 1):
                    if o.y0 <= y <= o.y1 and x0 <= x < x0 + o.w:
                        continue
                    self.put(y, x, " ", 0)
        for o, x0 in vis:
            if o.kind == K_WASH:
                ph = self.play_t * 9.0
                for i, y in enumerate(range(o.y0, o.y1 + 1)):
                    for j in range(o.w):
                        if (int(ph + i * 2 + j) % 4) != 3:
                            self.put(y, x0 + j, WASH_CH, pal.accent[0])
            elif o.kind == K_BAR:
                for y in range(o.y0, o.y1 + 1):
                    top = (y == o.y0)
                    self.put(y, x0, BAR_TOP_L if top else SOLID, pal.hazard[0])
                    self.put(y, x0 + 1, BAR_TOP_R if top else SOLID, pal.hazard[0])
            elif o.kind == K_OVER:
                for y in range(o.y0, o.y1 + 1):
                    bot = (y == o.y1)
                    self.put(y, x0, OVER_BOT_L if bot else SOLID, pal.hazard[0])
                    self.put(y, x0 + 1, OVER_BOT_R if bot else SOLID, pal.hazard[0])
            elif o.kind == K_CRATE:
                for y in range(o.y0, o.y1 + 1):
                    for j in range(o.w):
                        self.put(y, x0 + j, CRATE_CH, pal.target[0])
            elif o.kind == K_GATE:
                ch = GATE_CH
                for y in range(o.y0, o.y1 + 1):
                    self.put(y, x0, GATE_OPEN_L if o.hit else ch, pal.gold[0])
                    self.put(y, x0 + 1, GATE_OPEN_R if o.hit else ch, pal.gold[0])
        # PICKUP IS "ALWAYS ISOLATED", and it is the one class the pass above
        # deliberately skips — a fuel cell is authored INSIDE the crate's own
        # columns so the reward is legible before the verb that earns it is
        # understood. Without its own halo that is a green diamond embedded in
        # a cyan mass: two classes, zero gutter, exactly the blob the shape law
        # exists to prevent. Give it the halo instead of moving it, and the
        # teaching placement survives AND the silhouette stays isolated.
        for o, x0 in vis:
            if o.kind not in (K_FUEL, K_REPAIR):
                continue
            y = o.y0 + self.shake_y
            for yy in (y - 1, y, y + 1):
                if not (self.pb_top <= yy <= self.pb_bot):
                    continue
                for xx in (x0 - 1, x0, x0 + 1):
                    if yy == y and xx == x0:
                        continue
                    self.put(yy, xx, " ", 0)
        for o, x0 in vis:
            if o.kind == K_FUEL:
                self.put(o.y0 + self.shake_y, x0, FUEL_CH, pal.pickup[0])
            elif o.kind == K_REPAIR:
                self.put(o.y0 + self.shake_y, x0, REPAIR_CH, pal.pickup[0])

    def draw_fx(self):
        """Everything here is CLIPPED TO THE PLAY BAND. A `+100 m` that rises
        three rows is juice; the same string parked on the ceiling girder is a
        rendering bug wearing the costume of one."""
        pal = self.pal
        top, bot = self.pb_top, self.pb_bot
        for d in self.debris:
            y = int(round(d[1])) + self.shake_y
            if top <= y <= bot:
                self.put(y, self.sx(d[0]), DEBRIS_CH,
                         pal.text_hi if d[4] > 0.25 else pal.text_dim)
        for f in self.flashes:
            for y in range(max(top, f[0]), min(bot, f[2]) + 1):
                for x in range(self.sx(f[1]), self.sx(f[3]) + 1):
                    self.put(y, x, SOLID, pal.text_hi)
        for f in self.floats:
            y = int(round(f[2]))
            if top <= y <= bot:
                self.text_at(y, self.sx(f[1]) - 1, f[0], f[4])

    def draw_player(self, playing):
        pal = self.pal
        if self.pit_out > 0.0:
            return
        y = int(round(self.py)) + self.shake_y
        x = self.px + self.shake_x
        if self.invuln > 0.0 and int(self.play_t * 24) % 2 == 0:
            return                            # the flicker IS the damage read
        ch = PLAYER_SLAM if self.slamming else (
            PLAYER_BOOST if self.thrust >= 7 else PLAYER_CH)
        if self.thrust >= 7:                  # top speed leaves a trail
            for i in range(1, 4):
                self.put(y, x - i, DEBRIS_CH, pal.text_dim)
        self.put(y, x, ch, pal.player)
        if self.egg_heavy > 0.0:
            self.draw_heavy(x + 6, max(self.pb_top, min(y - 2, self.pb_bot - 2)),
                            pal.gold[0], min(3, self.play_rows))

    def draw_heavy(self, x0, y0, attr, max_rows):
        """The 5-row bitmap language breakout's wall and skyrun's gate already
        speak. Clipped to the rows it was given: a heavy that spills out of the
        far-sky band and into the skyline is a smear, not an aircraft."""
        rows = HEAVY if max_rows >= 5 else HEAVY[1:1 + max(0, max_rows)]
        for i, row in enumerate(rows):
            y = y0 + i
            if not (0 < y < self.h - 1):
                continue
            for j, c in enumerate(row):
                if c == "#":
                    self.put(y, x0 + j, SOLID, attr)

    # ---- HUD --------------------------------------------------------------
    def draw_hud(self):
        """Two rows, forever: row 0 and row h-1. Fixed columns, so the eye
        lands in the same place it does in every other title in the line."""
        pal = self.pal
        w = self.w
        rev = curses.A_REVERSE
        for x in range(w):
            self.put(0, x, " ", rev)
        metres = 747 if self.egg_heavy > 0.0 else int(self.dist * M_PER_COL)
        self.put(0, 0, STUDIO, pal.accent[0] | rev)
        if w >= 62:
            self.text_at(0, 2, "JETWASH   ", pal.text_hi | rev)
            self.put(0, 12, DASHCH, pal.text_dim | rev)
        elif w >= 46:
            self.text_at(0, 2, "JET", pal.text_hi | rev)
        else:
            # < 46: the primary integer and the hull, left-packed. Nothing else.
            self.text_at(0, 2, "%d" % metres, pal.text_hi | rev)
            self.text_at(0, 10, HULL_F * self.hull + HULL_E * (HULL_MAX - self.hull),
                         pal.player | rev)
            return
        # THE PRIMARY INTEGER IS %7d IN A FIXED FIELD. A score whose digits
        # reflow as it grows makes the whole bar shudder on every 10x.
        self.text_at(0, 14, "%7d" % metres, pal.text_hi | rev)
        self.text_at(0, 22, HULL_F * self.hull + HULL_E * (HULL_MAX - self.hull),
                     pal.player | rev)
        if w < 62:
            return
        gi = min(8, self.gate)
        self.text_at(0, 28, "G%d" % gi, pal.text | rev)
        if w >= 78:
            self.draw_gate_bar(35, rev)
        # METRES, HULL AND THRUST ARE NEVER DROPPED. Everything else may be:
        # the chain and the best go first, then the THRUST label, then the bar.
        t = int(self.thrust)
        pips = THR_F * t + THR_E * (THRUST_MAX - t)
        if w >= 90:
            self.text_at(0, 50, MULT_CH + "%d" % self.chain,
                         (pal.gold[0] if self.chain > 1 else pal.text) | rev)
            self.text_at(0, 57, "THRUST " + pips, pal.text_hi | rev)
        elif w >= 78 and self.h > 10:
            self.text_at(0, 47, pips, pal.text_hi | rev)   # clear of the 10-cell bar
        else:
            self.text_at(0, 42, pips, pal.text_hi | rev)   # bar is absent or collapsed
        if w >= 96:
            best = max(self.stats["best_score"], int(self.dist * M_PER_COL))
            s = "BEST %s" % commas(best)
            self.text_at(0, max(74, w - 1 - len(s)), s, pal.text_dim | rev)

    def draw_gate_bar(self, col, rev):
        pal = self.pal
        x = self.dist
        if x >= FINAL_COLS and not self.cleared:
            # FINAL 470: the label flips and the bar DRAINS to the finish
            frac = 1.0 - clamp((x - FINAL_COLS) / float(FINISH_COLS - FINAL_COLS), 0.0, 1.0)
            label = "FINAL 470"
        else:
            frac = (x % GATE_COLS) / float(GATE_COLS)
            label = None
        if self.h <= 10:
            # 80x8: the bar collapses to a number. Metres, thrust and hull
            # are never dropped; everything else may be.
            self.text_at(0, col, "%d%%" % int(frac * 100), pal.text | rev)
            return
        n = 10
        k = int(frac * n)
        self.text_at(0, col, BAR_F * k + BAR_E * (n - k),
                     (pal.gold[0] if label else pal.text_hi) | rev)
        if label and self.w >= 96:
            self.text_at(0, col + n + 1, label, pal.gold[0] | rev)

    def draw_footer(self):
        pal = self.pal
        y = self.foot_row
        if self.play_t < 15.0:
            # Second channel, never the primary one — then it fades for good.
            #
            # NAME THE SHAPES, NOT THE HUES. This line used to read
            # "red = solid · cyan = break" and rendered verbatim on a mono
            # pane where neither colour exists — teaching the game's one
            # risk/reward distinction in a vocabulary the screen was not
            # speaking. Built from the LIVE glyph constants, so it self-corrects
            # in UTF-8 and in ASCII and can never drift from what is drawn.
            if self.w >= 96:
                hint = ("%s jump %s %s slam %s %s solid %s %s breaks "
                        "%s %s thrust" % (UP_CH, DASHCH, DOWN_CH, DASHCH,
                                          SOLID, DASHCH, CRATE_CH, DASHCH,
                                          FUEL_CH))
            else:
                # never clip a word in half — drop the tail instead
                hint = "%s jump %s %s slam %s %s solid %s %s breaks" % (
                    UP_CH, DASHCH, DOWN_CH, DASHCH, SOLID, DASHCH, CRATE_CH)
                if len(hint) > self.w - 16:
                    hint = "%s jump %s %s slam" % (UP_CH, DASHCH, DOWN_CH)
            self.text_at(y, 1, hint[: max(0, self.w - 14)], pal.text_dim)
        elif self.w >= 62:
            best = self.stats["best_score"]
            s = "GATE %d %s BEST %s m" % (min(8, self.gate), DASHCH, commas(best))
            self.text_at(y, 1, s, pal.text_dim)
        tag = "THE 747 LAB "
        self.text_at(y, max(0, self.w - len(tag) - 1), tag, pal.text_dim)

    def overlay(self, lines, attr):
        """Centre text over the frozen world — inside a cleared box.

        The paused overlay is the single most-seen screen in this product: it
        is what the user looks at every time Claude replies. Letting a red
        barricade run through the middle of the word READING is the difference
        between a paused game and a broken one."""
        wide = max(len(s) for s in lines)
        x0 = max(0, (self.w - wide) // 2)
        # Centred in the PLAY BAND, and the cleared box never leaves it: the
        # ceiling girder and the ground line are structure, and a gap chewed
        # out of them reads as corruption, not as an overlay.
        top, bot = self.pb_top, self.pb_bot
        y0 = max(top, (top + bot) // 2 - len(lines) // 2)
        y0 = max(top, min(y0, bot - len(lines) + 1))
        for y in range(max(top, y0 - 1), min(bot, y0 + len(lines)) + 1):
            for x in range(x0 - 2, x0 + wide + 2):
                self.put(y, x, " ", 0)
        for i, ln in enumerate(lines):
            if y0 + i > bot:
                break
            x = max(0, (self.w - len(ln)) // 2)
            self.text_at(y0 + i, x, ln[: max(0, self.w - 1)], attr | curses.A_BOLD)

    def render_too_small(self):
        scr = self.scr
        scr.erase()
        msg = "TERMINAL TOO SMALL %s 80x8 MINIMUM" % DASHCH
        try:
            scr.addstr(max(0, self.h // 2), max(0, (self.w - len(msg)) // 2),
                       msg[: max(0, self.w - 1)], curses.A_DIM)
        except curses.error:
            pass
        scr.refresh()

    def blit(self):
        """Run-length per row: the wire is the ceiling, not the CPU."""
        scr = self.scr
        h, w = self.h, self.w
        extra = curses.A_REVERSE if self.invert > 0 else 0
        wht = curses.A_REVERSE if self.white > 0 else 0
        scr.erase()
        for y in range(h):
            rc, ra = self.cb[y], self.ab[y]
            band = wht if (self.pb_top <= y <= self.pb_bot) else 0
            x = 0
            while x < w:
                if rc[x] is None:
                    x += 1
                    continue
                a = ra[x]
                j = x
                buf = []
                while j < w and rc[j] is not None and ra[j] == a:
                    buf.append(rc[j])
                    j += 1
                s = "".join(buf)
                if y == h - 1 and j >= w:        # avoid the bottom-right cell error
                    s = s[:-1]
                if s:
                    try:
                        scr.addstr(y, x, s, a | extra | band)
                    except curses.error:
                        pass
                x = j
        scr.refresh()

    # ---- the loop ---------------------------------------------------------
    def run(self):
        scr = self.scr
        scr.nodelay(True)
        last = time.monotonic()
        next_t = last
        poll_t = 0.0
        self.state = read_state(self.session)
        while True:
            now = time.monotonic()
            dt = now - last
            last = now
            rejoin = dt > DT_REJOIN
            if rejoin:
                dt = 0.0                    # never simulate a rejoin frame
            dt = min(dt, DT_MAX)

            h, w = scr.getmaxyx()           # EVERY frame: a live ghost cycle
            if (h, w) != (self.h, self.w):  # resizes the pane 3+ times
                self.on_resize(h, w)

            playing = (self.state == "thinking") or self.manual_play
            interval = POLL_PLAY if playing else POLL_IDLE
            if now - poll_t >= interval:
                poll_t = now
                new_state = read_state(self.session)
                if new_state != self.state:
                    if self.state == "idle" and new_state == "thinking":
                        self.stats["resumed_after_banish"] += 1
                        self.on_rejoin()
                    self.state = new_state
                    self.idle_drawn = False
                if self.state == "end":
                    return "end"
                playing = (self.state == "thinking") or self.manual_play

            if self.drain() == "quit":
                return "quit"

            if not playing:
                # PAUSE INSTANTLY, on this frame. Draw the overlay ONCE, then
                # sleep: zero bytes on the wire while ghosted. A ghost pane
                # that keeps redrawing is a bug even though nobody can see it.
                if not self.idle_drawn:
                    self.render(False)
                    self.idle_drawn = True
                time.sleep(POLL_IDLE)
                continue
            self.idle_drawn = False

            if self.too_small:
                self.render_too_small()
                time.sleep(0.2)             # idle politely; never crash, never exit
                continue

            if rejoin:
                self.on_rejoin()

            over = self.step(dt)
            self.render(True)
            if over:
                return "over"

            next_t += FRAME
            if next_t < now:
                next_t = now + FRAME
            time.sleep(max(0.0, next_t - time.monotonic()))


# ---------------------------------------------------------------------------
# screens — every blocking screen polls for 'end'
# ---------------------------------------------------------------------------
def ask_screen(scr, session):
    """Returns True to play. Handles n (this session), a (always), o (off)."""
    scr.nodelay(False)
    # 200 ms, not 1000: EVERY blocking screen polls for 'end', and the poll
    # interval IS the worst-case time a finished session can leave a pane on
    # screen. A whole second of a dead pane is a second the user notices.
    scr.timeout(200)
    start = time.time()
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [
            "FLY JETWASH WHILE CLAUDE THINKS?",
            "",
            "[y] yes   [n] not now   [a] always auto-open   [o] never ask again",
        ]
        for i, ln in enumerate(lines):
            try:
                scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln[: max(0, w - 1)],
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        try:
            scr.addstr(h - 1, max(0, w - 22), "THE 747 LAB ", curses.A_DIM)
        except curses.error:
            pass
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("a"), ord("A")):
            write_mode("auto")
            return True
        if ch in (ord("n"), ord("N")):
            if session:
                try:
                    open(os.path.join(STATE_DIR, "declined-%s" % session), "w").close()
                except OSError:
                    pass
            return False
        if ch in (ord("o"), ord("O")):
            write_mode("off")
            return False
        if ch in (ord("q"), ord("Q")):
            return False
        # A SESSION ENDING IS NOT THE USER DECLINING. These two used to share
        # one branch, so a session that closed while the ask prompt was up
        # wrote `declined-<sid>` — the hook's permanent "never open again for
        # this session" flag. The hook's `end` branch removes that file a
        # fraction of a second BEFORE the game notices 'end', so the marker
        # survived the cleanup and resuming that session id meant the game
        # never opened again for the rest of its life.
        if read_state(session) == "end":
            return False
        if time.time() - start > ASK_TIMEOUT:
            if session:
                try:
                    open(os.path.join(STATE_DIR, "declined-%s" % session), "w").close()
                except OSError:
                    pass
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


def game_over_screen(scr, jet, stats, session):
    """Death to the next attempt is one keypress. Returns 'again', 'heavy'
    or None."""
    scr.nodelay(False)
    # 200 ms, not 1000: EVERY blocking screen polls for 'end', and the poll
    # interval IS the worst-case time a finished session can leave a pane on
    # screen. A whole second of a dead pane is a second the user notices.
    scr.timeout(200)
    metres = int(jet.dist * M_PER_COL)
    best = stats["best_score"]
    lines = []
    if jet.cleared:
        lines.append("RUN CLEARED %s %s m %s %.1fs" % (DASHCH, commas(metres), DASHCH,
                                                       jet.clear_t))
    else:
        lines.append("DOWN AT %s m %s GATE %d/7" % (commas(metres), DASHCH,
                                                    min(7, jet.gate)))
    if metres > best:
        lines.append("NEW BEST")
    else:
        lines.append("BEST %s m" % commas(best))
    lines.append("")
    menu = "   [m] menu" if menu_available() else ""
    if stats["cleared"]:
        # the reason to come back. No unlock trees, no meta-grind: this game is
        # played in a pane that vanishes.
        lines.append("[r] run it again   [h] HEAVY MODE%s   [q] close" % menu)
    else:
        lines.append("[r] run it again%s   [q] close" % menu)
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        for i, ln in enumerate(lines):
            try:
                scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln[: max(0, w - 1)],
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        try:
            scr.addstr(h - 1, max(0, w - 22), "THE 747 LAB ", curses.A_DIM)
        except curses.error:
            pass
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("r"), ord("R")):
            return "again"
        if ch in (ord("h"), ord("H")) and stats["cleared"]:
            return "heavy"
        if ch in (ord("m"), ord("M")) and menu_available():
            back_to_menu(session)            # never returns
        if ch in (ord("q"), ord("Q"), 27):
            return None
        if read_state(session) == "end":
            return None


def record_run(stats, jet):
    stats["runs"] += 1
    stats["best_score"] = max(stats["best_score"], int(jet.dist * M_PER_COL))
    stats["best_stage"] = max(stats["best_stage"], min(8, jet.gate))


def main(stdscr, args):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()                   # start_color FIRST, then defaults
        curses.use_default_colors()
    except (curses.error, ValueError):
        pass                                   # a mono terminal is a supported
                                               # terminal: the shape law is what
                                               # carries the game, not the hue
    for i, col in enumerate([curses.COLOR_RED, curses.COLOR_YELLOW,
                             curses.COLOR_GREEN, curses.COLOR_CYAN,
                             curses.COLOR_MAGENTA], start=1):
        ipair(i, col)
    for i, col in ((6, curses.COLOR_WHITE), (7, curses.COLOR_YELLOW),
                   (8, curses.COLOR_YELLOW)):
        ipair(i, col)
    # No mouse tracking: JETWASH is two buttons. A leaked mouse mode outlives
    # the pane and lands in the user's shell, so the safest handling is not to
    # turn it on at all.
    pal = Palette()
    stats = load_stats()

    if args.ask and not ask_screen(stdscr, args.session):
        return

    heavy = False
    try:
        while True:
            jet = Jet(stdscr, args.session, pal, stats, heavy_mode=heavy)
            jet.manual_play = args.free        # manual launch: fly regardless
            result = jet.run()
            record_run(stats, jet)
            save_stats(stats)
            if result in ("end", "quit"):
                return
            again = game_over_screen(stdscr, jet, stats, args.session)
            if again is None:
                return
            heavy = (again == "heavy")
    finally:
        # THE GAME OWNS ITS OWN STATE FILE, and it owns it on EVERY exit path —
        # not just the one where the run loop happened to see 'end' first. A
        # session that ends while the player is sitting on the game-over screen
        # would otherwise leave the file behind for the next session to find.
        if read_state(args.session) == "end":
            remove_state(args.session)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="JETWASH - by The 747 Lab")
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    LAUNCH_ARGS = args      # module scope: back_to_menu() reads it
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    # nl_langinfo(CODESET) is the only honest answer here: on macOS
    # getpreferredencoding() reports UTF-8 even under LC_ALL=C, and a mojibake
    # corridor is worse than a low-res one. 747_ASCII=1 forces it, so the mono
    # test is runnable in CI.
    enc = ""
    try:
        if hasattr(locale, "nl_langinfo") and hasattr(locale, "CODESET"):
            enc = locale.nl_langinfo(locale.CODESET) or ""
    except (ValueError, TypeError, AttributeError):
        enc = ""
    if not enc:
        try:
            enc = locale.getpreferredencoding() or ""
        except (ValueError, TypeError):
            enc = ""
    # 747_ASCII is the name the build contract specifies, and it is honoured —
    # but no POSIX shell can SET it (an identifier may not start with a digit,
    # so `747_ASCII=1 game.py` is a command-not-found, not an assignment). Only
    # `env 747_ASCII=1 ...` works. LAB747_ASCII is the shell-settable alias, so
    # the mono test is runnable by a human as well as by CI.
    if "1" in (os.environ.get("747_ASCII", ""), os.environ.get("LAB747_ASCII", "")):
        use_ascii()
    elif "utf" not in enc.lower():
        use_ascii()
    set_pane_title(args.session)
    curses.wrapper(main, args)
