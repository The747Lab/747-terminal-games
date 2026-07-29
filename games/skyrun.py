#!/usr/bin/env python3
"""SKYRUN — a POV flying-car delivery run that plays while Claude thinks.

THE GAME, IN ONE LINE: fly seven sectors and land the delivery.

  WIN   complete the 7-SECTOR RUN. Each sector is 600 m and ends at a gold
        gate. The HUD carries `S 3/7` and a bar that fills across the sector,
        so the answer to "what am I trying to do" is on screen from frame 1.
        Clear sector 7 and you get a real victory screen. Sector 8+ is
        OVERRUN: endless, pure score, for the turn that ran long.
  LOSE  SHIELDS ONLY — three of them, drawn ▰▰▱. There are no lives; the
        old shields/lives ambiguity is deleted. One shield back per 400 m of
        clean flight.
  SCORE coin 10 · clean dodge 5 · thread a rock 12 · alien 40 · artifact 40
        and a shell · gate seam 100 · the eye of the 4 = 747 · sector clear
        200×sector · a full 7/7 run +1000. Every one of those prints a
        one-line whisper in the footer, so no number is ever unexplained.

Steer with the arrow keys or the mouse, dodge the red rocks, shoot the cyan
aliens, scoop the green coins, and shave a rock close enough to earn a shell
for the gun — bravery is the only ammo factory. Every 600 m there is a gold
747 with a hole in it, and the hole is a real flight window.

Lives in a tmux pane split below the Claude Code session. Auto-pauses when
Claude finishes a turn (Stop hook writes 'idle'), resumes on the next prompt
(UserPromptSubmit writes 'thinking'), and exits when the session ends. It
survives being banished to a hidden window and rejoined mid-run: the pane
vanishes, the process stays alive and frozen, and your run comes back exactly
where you left it. A small break for the user, not a tab they have to close.

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
FRAME = 0.030         # 33 fps, deadline-corrected (bandwidth, not CPU, is the ceiling)
POLL_PLAY = 0.12      # state re-read while flying
POLL_IDLE = 0.25      # state re-read while paused / banished
ASK_TIMEOUT = 45      # ask screen auto-closes after this many seconds
DT_MAX = 0.05         # spiral guard — tighter than breakout's 0.1 (anti-tunnel at v=34)
DT_REJOIN = 0.25      # a dt spike this big means ghost-pane rejoin: do not simulate

NEAR = 1.0            # near clip
HIT_Z = 1.2           # the collision plane, in front of the camera
SPAWN_Z = 46.0        # objects are born this far ahead

# ---- THE RUN. This is the answer to "how do I win", and it is the only
#      structural number in the file that a player ever has to feel.
SECTORS = 7           # a delivery run is seven sectors. Then you have WON.
SECTOR_M = 600.0      # metres per sector -> 4,200 m, ~3 minutes at ~24 m/s,
                      # i.e. one Claude think-block, which is the whole product
                      # thesis: closure inside the wait, not an abandoned score.
V_BASE = 16.0         # sector-1 speed
V_GAIN = 18.0         # asymptotic gain -> the cap is 34.0 u/s and it is a
                      # LIMIT, not a ramp: v = 16 + 18*(1 - e^(-dist/900)).
# WORST-CASE TELEGRAPH: 1,318 ms / 44.8 world units at 34.0 u/s in sector 7.
#   (SPAWN_Z 46.0 - HIT_Z 1.2) / 34.0 u/s = 1.318 s of visibility. MEASURED,
#   not asserted: a headless 7-sector sim reports max raw speed 33.83 u/s and
#   a minimum gate-chunk spawn depth of 45.2 units.
#   Floor is 1.000 s (Falcon: human visual RT ~250 ms, runners give ~4x), so
#   we run 1.35x the time floor at the hardest moment of the hardest sector.
#   DIFFICULTY IS DELIVERED BY DENSITY AND LANE COUNT, NEVER BY SPEED: the
#   speed asymptotes at 34 and OVERRUN does not raise it. If a change would
#   push v past 34, the telegraph breaks first, so the change does not ship.
#   (Afterburner multiplies v by 1.4 for 7.47 s, which would be 966 ms — but
#   during afterburner NOTHING can hurt you: rocks and gate chunks are
#   destroyed on contact for points. There is no threat to telegraph.)
V_CAP = 34.0
BYTES_MAX = 4000      # est. bytes per blit — the real ceiling is the wire, not
                      # the CPU. A run of constant attr is nearly free, so the
                      # budget counts emitted RUNS + text, never occupied cells.
MAXOBJ = 96           # fixed pool ceiling — the world never grows unbounded
# Lateral camera clamp, +/-. This MUST stay inside the outer lane's hitbox
# (|7.2| + 1.5 = 8.7) or the tube wall becomes a safe parking spot: holding one
# arrow pins you at the clamp, and at 9.0 the outer lane rock can never reach
# you. Measured before the fix: 2 hits per 9500 m at the wall vs 16-57 anywhere
# else — i.e. hold-left-and-win. 8.4 keeps 0.3 units of margin inside that
# hitbox while still leaving the 747 gate a clear flank (see the gate audit).
TUBE_X = 8.4

LANE_X_5 = [-7.2, -3.6, 0.0, 3.6, 7.2]     # authoring grid
LANE_X_3 = [-5.4, 0.0, 5.4]                # narrow / short panes

GATE_CW = 1.4         # world units per gate column
GATE_HX = 0.45        # gate chunk half-width  (seams between chunks are real)
EYE_HX = 0.60         # the eye of the 4 — how precise the line has to be

# ---- the optional chase camera ('v'). It is a real camera: the eye point moves
# BACK and UP in world space and the car is then drawn as an ordinary world
# object at the ship's actual position, so it translates when you steer and the
# world z-buffer occludes it. The collider never moves — the ship is still the
# thing that gets hit, the camera just stops sitting inside it.
CHASE_BACK = 7.4      # world units behind the ship
CHASE_UP = 1.9        # ...and above it (world -y is up), looking slightly down
CHASE_MIN_H = 12      # a chase view needs vertical room or the car eats the road

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


STATS_NAME = "stats-skyrun.json"          # one schema, one shape, all four titles
STATS_LEGACY = "skyrun-stats.json"        # pre-3.0 name; read once, never written
NO_STATS = "no-stats"

# object slots: a plain list is the hot path — no objects, no dataclasses.
# O_R / O_RY are WORLD half-extents and are used for BOTH the silhouette and
# the hitbox, so what you see is exactly what you hit. Lane rocks are shards
# that span the tube vertically — otherwise a player parks at the top of the
# tube and is never hit again.
O_KIND, O_X, O_Y, O_Z, O_R, O_RY, O_VX, O_FLG = range(8)
K_ROCK, K_COIN, K_GATE, K_POD, K_EYE, K_ALIEN = range(6)
# THE LAW, and the whole answer to "I'm not sure which objects I shoot":
#   K_ALIEN  = alien hull / artifact  -> SHOOTABLE. Cyan-green. Alien glyphs.
#   K_ROCK   = asteroid               -> DODGE ONLY. Warm rock tones. Never shootable.
# NOTE: an earlier comment here claimed "the fiction teaches the mechanic, so no legend
# is needed". That was EXPERIMENTALLY REFUTED — the Founder played it and said "not sure
# which objects I shoot or not". Lore does not teach a glance-read; SHAPE and COLOUR do,
# and the HUD says it outright. Do not re-introduce that assumption.
F_DEAD = 1        # consumed / shattered — no longer collidable
F_EVAL = 2        # already crossed the hit plane and was evaluated
F_THREAD = 4      # already paid a thread credit
F_ARTIFACT = 8    # an alien ARTIFACT rather than a hull (rarer, worth more)

# ---- glyphs (set from the locale in __main__; a mojibake windshield is worse
#      than a low-res one) --------------------------------------------------
UTF = True
RAMP = "·░▒▓█"
BLOCK = "█"
MIDBLOCK = "▓"
DOT = "·"                         # STAR, AND ONLY STAR. See below.
COIN_CH = "◈"                     # PICKUP, near — hollow diamond
COIN_FAR = "◆"                    # PICKUP, far  — the same family, one cell
# WHY THERE IS NO `SOFT_CH` ANY MORE.
# There used to be one, "◆", and draw_rock() used it for every rock in the
# 0.75 <= sc < 2.6 band — which is most of a rock's on-screen life. A filled
# diamond is the PICKUP family (◈ ◆ ⋄ +). So the thing that kills you and the
# thing you collect differed by one hole in a diamond, at the exact distance
# where the player has to classify them. That IS "I'm not sure what I shoot",
# shipped. HAZARD now comes from RAMP and only from RAMP, at every distance.
#
# Likewise DOT. A far rock and a far coin were both a bare "·", which is also
# what a star is: three roles, one cell, no way to tell them apart with colour
# off (and, on 16 colours, no way to tell them apart WITH colour — see the
# Palette fog tier). The dot is now scenery and nothing else:
#   far rock -> RAMP[1]  (a mass, dimmest tier)
#   far coin -> COIN_FAR (a diamond, one cell)
#   star     -> DOT
SHIELD_F, SHIELD_E = "▰", "▱"
AMMO_F, AMMO_E = "▮", "▯"
ARROW = "▲"
RET_L, RET_R = "‹", "›"
MUZ_L, MUZ_R = "◤", "◥"
PAUSE_CH, DASHCH = "⏸", "—"
TICK = "▌"                        # the studio tick, HUD column 0
BAR_F, BAR_E = "▸", "▹"           # the sector distance bar — THE win condition
# the cockpit you fly FROM. Founder playtest #1: "you can't really tell what is
# flying" — because POV drew a bare hood line and nothing else, so the reticle
# WAS the ship. A windshield needs a frame, a nose and engines to read as a car.
#
# THE WING FIX (Founder: "the WINGS of the POV move weird when we pan left and
# right, just doesn't look clean"). The old cockpit was two sweeping struts
# STRUT_L/STRUT_R that slid sideways by an int(round()) column offset while the
# hood ends snapped a whole row on a boolean threshold. Five faults compounded:
#   1. the struts TRANSLATED — a canopy is bolted to your skull, it cannot
#      slide across your eye. That alone reads as wrong instantly.
#   2. two disagreeing step functions on one rigid body (row snap vs column
#      slide, different thresholds) — that is the "swim".
#   3. int(round()) on a signal sitting on the boundary: vx*0.10 == 0.5 at
#      vx == 5.0, so the smallest jitter flipped a whole column back and forth.
#   4. dx = half + r + r//2 steps +1,+3,+4,+6 per row while the ╲ glyph
#      promises 1:1 — the diagonal was never a diagonal.
#   5. THERE WAS NO BANK TO RENDER. vx is a POSITION ERROR that halves every
#      frame, so |vx| > 2.5 held for ~3 frames = 90 ms. Human visual
#      integration is ~100 ms. It was a POP, by construction, and no drawing
#      polish fixes a signal that does not exist.
# The fix: roll gets its own spring-damped physics (~300 ms, one overshoot, so
# the eye reads MASS); the frame is rigid and the WORLD leans; the roll is
# rendered SUB-CELL through the partial-height ramp; and the A-pillars read the
# roll by LIGHT rather than by motion.
HOOD8 = "▁▂▃▄▅▆▇█"                # U+2581..U+2588 — eight exact sub-row heights
PILLAR_L, PILLAR_R = "▏", "▕"     # A-pillars: FIXED columns, forever
RAIL_ON, RAIL_OFF = "▪", "·"      # your lane vs the others
NOSE = "▲"                        # the hood's centre peak
ENGINE = "◉"                      # thruster glow at each lower corner
ROLL_MAX = 0.92                   # rows of bonnet-tip displacement at full bank
ALIEN_CH = "☩"                    # alien hull — SHOOTABLE (see the fiction rule)
ARTIFACT_CH = "⬢"                 # alien artifact — SHOOTABLE, higher value
POD_CH = "⬡"                      # ammo pod — COLLECTABLE. Hollow, so it can
                                  # never be mistaken for the solid artifact.
# the alien's swept wings. NOT ▓: that glyph belongs to the HAZARD shading
# ramp (· ░ ▒ ▓ █), and a shootable craft wearing a hazard glyph is exactly
# the "I'm not sure what I shoot" bug in miniature. ◄ ► are angular and
# directional — the TARGET family — so the wings say 'made by someone'.
WING_L, WING_R = "◄", "►"
# (NOT ◈ — that is COIN_CH. An artifact that looks like a coin recreates the very
#  ambiguity this whole pass exists to kill.)


def use_ascii():
    """Fall back to a pure-ASCII glyph set on a non-UTF-8 terminal.

    THE MONO TEST: with colour off and this set forced, every object class must
    still be unambiguous by glyph alone — # solid rock, A alien, H artifact,
    o coin, U pod. If a title only separates by hue, its silhouette system is
    fake. Forced by `747_ASCII=1` so the test is runnable in CI."""
    global UTF, RAMP, BLOCK, MIDBLOCK, DOT, COIN_CH, COIN_FAR, SHIELD_F, SHIELD_E
    global AMMO_F, AMMO_E, ARROW, RET_L, RET_R, MUZ_L, MUZ_R
    global PAUSE_CH, DASHCH, TICK, BAR_F, BAR_E
    global HOOD8, PILLAR_L, PILLAR_R, NOSE, ENGINE, ALIEN_CH, ARTIFACT_CH
    global RAIL_ON, RAIL_OFF, POD_CH, WING_L, WING_R
    UTF = False
    # 8 sub-row steps collapse to 4 visible levels. Honest degradation: the
    # bank still moves through four heights instead of snapping between two.
    HOOD8 = "..__--=#"
    PILLAR_L = PILLAR_R = "|"
    TICK = "|"
    BAR_F, BAR_E = ">", "-"
    RAIL_ON, RAIL_OFF = "+", "."
    NOSE, ENGINE = "^", "O"
    ALIEN_CH, ARTIFACT_CH, POD_CH = "A", "H", "U"
    # "=" and not "<"/">": those are the reticle in ASCII, and a wing
    # that wears the reticle's glyph is a lock indicator that lies.
    WING_L = WING_R = "="
    RAMP = ".:*#@"
    # COIN_FAR stays "o" too: one PICKUP family, and "*" is RAMP[2] (a rock).
    BLOCK, MIDBLOCK, DOT, COIN_CH, COIN_FAR = "#", "*", ".", "o", "o"
    SHIELD_F, SHIELD_E = "#", "-"
    AMMO_F, AMMO_E = "|", "."
    ARROW = "^"
    RET_L, RET_R = "<", ">"
    MUZ_L, MUZ_R = "\\", "/"
    PAUSE_CH, DASHCH = "||", "-"


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def fmt_int(n):
    """1234567 -> '1,234,567'. Locale-independent on purpose: format(n, ',d')
    honours LC_NUMERIC and would silently produce a different width on a
    European terminal, and this number sits in a fixed HUD column."""
    s = str(int(n))
    out = []
    for i, c in enumerate(reversed(s)):
        if i and i % 3 == 0:
            out.append(",")
        out.append(c)
    return "".join(reversed(out))


def fmt_time(sec):
    """Seconds -> m:ss. The run clock is the currency on the victory screen."""
    sec = int(max(0.0, sec))
    return "%d:%02d" % (sec // 60, sec % 60)


def txt(s):
    """Down-convert the typographic characters on a non-UTF-8 terminal."""
    if UTF:
        return s
    for a, b in (("·", "-"), ("×", "x"), ("—", "-"), ("⏸", "||"),
                 ("←", "<"), ("→", ">"), ("↑", "^"), ("↓", "v"),
                 ("▸", ">"), ("▹", "-"), ("▌", "|")):
        s = s.replace(a, b)
    return s


# ---------------------------------------------------------------------------
# state protocol — copied verbatim from breakout.py, retitled SKYRUN747-
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
    sys.stdout.write("\033]2;SKYRUN747-%s\033\\" % (session or "free"))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# stats — LOCAL ONLY. Never transmitted, never read by anything but this file.
# `touch ~/.747-terminal-games/no-stats` and it is never written again.
# ---------------------------------------------------------------------------
STATS_DEFAULT = {
    "v": 1, "game": "skyrun", "runs": 0, "restarts": 0,
    "best_stage": 0,          # best SECTOR reached — the number the run is about
    "cleared": False,         # has this player ever landed a full 7/7 run
    "eggs": 0,                # eyes of the 4 threaded, all-time
    "best_dist": 0, "best_score": 0, "total_seconds": 0.0,
    "gates_seen": 0, "eyes_threaded": 0, "afterburners": 0, "threads": 0,
    "resumed_after_banish": 0, "continued_after_idle": 0,
    "first_run_ts": 0, "last_run_ts": 0,
}


def stats_disabled():
    return os.path.exists(os.path.join(STATE_DIR, NO_STATS))


def _read_stats_file(name, into):
    with open(os.path.join(STATE_DIR, name)) as f:
        got = json.load(f)
    if isinstance(got, dict):
        for k in STATS_DEFAULT:
            # bool before int: isinstance(True, int) is True, so a bare int
            # check would happily accept 1 for "cleared" and 0 for a counter
            if k in got and isinstance(got[k], type(STATS_DEFAULT[k])):
                into[k] = got[k]


def load_stats():
    s = dict(STATS_DEFAULT)
    for name in (STATS_NAME, STATS_LEGACY):
        try:
            _read_stats_file(name, s)
            break             # the new file wins; the legacy one is a migration
        except (OSError, ValueError):
            continue          # a broken stats file must never break a game
    return s


def save_stats(s):
    """Atomic: write a temp file, then replace. Never called from the frame loop."""
    if stats_disabled():
        return
    tmp = os.path.join(STATE_DIR, STATS_NAME + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(s, f, sort_keys=True)
        os.replace(tmp, os.path.join(STATE_DIR, STATS_NAME))
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Frame — the per-cell z-buffer compositor, lifted out of welcome_flyby's
# closure. Nearer z wins; objects are NEVER depth-sorted, that is the whole
# reason this exists.
# ---------------------------------------------------------------------------
class Frame(object):
    INF = 1e9

    def __init__(self):
        self.h = 0
        self.w = 0
        self.zb = []
        self.cb = []
        self.ab = []
        self._zt = []
        self._ct = []
        self._at = []
        self.n = 0          # cells written this frame
        self.cost = 0       # est. bytes the LAST blit put on the wire

    def resize(self, h, w):
        self.h, self.w = h, w
        self._zt = [Frame.INF] * w
        self._ct = [None] * w
        self._at = [0] * w
        self.zb = [self._zt[:] for _ in range(h)]
        self.cb = [self._ct[:] for _ in range(h)]
        self.ab = [self._at[:] for _ in range(h)]

    def clear(self):
        # slice-assign, not realloc: this loop runs for minutes, not 7.47 seconds
        zt, ct, at = self._zt, self._ct, self._at
        zb, cb, ab = self.zb, self.cb, self.ab
        for y in range(self.h):
            zb[y][:] = zt
            cb[y][:] = ct
            ab[y][:] = at
        self.n = 0

    def put(self, y, x, ch, attr, z):
        if 0 <= y < self.h and 0 <= x < self.w:
            row = self.zb[y]
            if z < row[x]:
                row[x] = z
                self.cb[y][x] = ch
                self.ab[y][x] = attr
                self.n += 1

    def blit(self, scr):
        h, w = self.h, self.w
        cost = 0
        scr.erase()
        for y in range(h):
            rc, raw = self.cb[y], self.ab[y]
            x = 0
            while x < w:
                if rc[x] is None:
                    x += 1
                    continue
                a = raw[x]
                j = x
                buf = []
                while j < w and rc[j] is not None and raw[j] == a:
                    buf.append(rc[j])
                    j += 1
                s = "".join(buf)
                if y == h - 1 and j >= w:      # avoid the bottom-right cell error
                    s = s[:-1]
                if s:
                    # ~12 bytes of cursor-move + SGR per run, plus the text
                    # itself (UTF-8 box glyphs are 3 bytes each)
                    cost += 12 + len(s.encode("utf-8", "replace"))
                    try:
                        scr.addstr(y, x, s, a)
                    except curses.error:
                        pass
                x = j
        self.cost = cost
        scr.refresh()


# ---------------------------------------------------------------------------
# palette — every family is a same-shape list of opaque attr ints, so no render
# code ever asks "am I in 256 mode?". That discipline is why the 16-colour
# fallback cannot drift out of sync. Event flashes are one list swap.
# ---------------------------------------------------------------------------
class Palette(object):
    def __init__(self):
        try:
            # AttributeError, not just curses.error: on a terminal where
            # start_color() failed, curses.COLORS is never DEFINED at all, and
            # an uncaught AttributeError here takes the whole pane down on
            # exactly the terminals the 16-colour fallback exists to serve.
            has256 = curses.COLORS >= 256
        except (curses.error, AttributeError):
            has256 = False
        counter = [60]        # breakout reserves 1-8, its flyby uses 30-56

        def mk(fg, bold=False):
            i = counter[0]
            counter[0] += 1
            a = ipair(i, fg)
            return (a | curses.A_BOLD) if bold else a

        if has256:
            # STAR is a NEUTRAL GREY ramp. It used to end on index 60, which is
            # bit-identical to the rock's fog tier — a far rock and a far star
            # rendered as the same cell, which is the single cheapest way to
            # make a runner feel like a coin flip. Backdrop is never bold.
            self.b_star = [mk(255, True), mk(250), mk(244), mk(238)]
            # HAZARD — DO NOT TOUCH. Obstacles must be the highest-contrast
            # thing on screen; debris-realism is a trap. A hazard is always bold.
            self.b_rock = [mk(210, True), mk(174), mk(96), mk(60)]
            # TARGET — DESTROY THIS. Cyan, and cyan means exactly one thing.
            self.b_target = [mk(195, True), mk(87), mk(44), mk(24)]
            # PICKUP — COLLECT THIS. Green. A thing you SHOOT and a thing you
            # COLLECT used to share this list, which is why the Founder could
            # not tell what he was meant to shoot. Two shades of one hue is not
            # a fix; it is the same bug at higher resolution. Roles get hues.
            self.b_pickup = [mk(120, True), mk(40), mk(34), mk(22)]
            # RESERVED brand-wide: the 747 gate, the afterburner, and the score
            # at multiples of 747. Nothing else in this game is ever gold.
            self.b_gold = [mk(226, True), mk(220), mk(178), mk(136)]
            self.b_hull = [mk(203, True), mk(160)]
            # STRUCT — the cockpit frame and nothing else. The A-pillars read
            # the bank by stepping through this ramp, so it must be a real ramp.
            self.b_struct = [mk(250), mk(244), mk(238)]
            self.b_accent = [mk(141), mk(97), mk(61)]     # studio violet, chrome
            self.b_dash = mk(146)
            self.b_dash_hi = mk(231, True)
            self.b_whisper = mk(189)
        else:
            # guarded for the same reason mk() is: a terminal that cannot do
            # colour must degrade to attributes, never to a traceback
            red = cpair(1)
            grn = cpair(3)
            cyn = cpair(4)
            mag = cpair(5)
            wht = cpair(6)
            ylw = cpair(8)
            # THE FOG TIER KEEPS ITS HUE. Index [3] used to be a bare A_DIM on
            # STAR, HAZARD, TARGET and PICKUP alike — four roles, one attribute,
            # at the exact tier that is supposed to be the TELEGRAPH. On a
            # 16-colour terminal (the declared floor) a far rock, a far coin and
            # a star were then pixel-identical. Only the star is allowed to be
            # colourless, because the star is the thing that means nothing.
            # A hazard is always bold up close and never the dimmest thing on
            # screen at distance.
            self.b_star = [curses.A_NORMAL, curses.A_DIM, curses.A_DIM,
                           curses.A_DIM]
            self.b_rock = [red | curses.A_BOLD, red, red | curses.A_DIM,
                           red | curses.A_DIM]
            self.b_target = [cyn | curses.A_BOLD, cyn, cyn | curses.A_DIM,
                             cyn | curses.A_DIM]
            self.b_pickup = [grn | curses.A_BOLD, grn, grn | curses.A_DIM,
                             grn | curses.A_DIM]
            self.b_gold = [ylw | curses.A_BOLD, ylw, ylw | curses.A_DIM,
                           ylw | curses.A_DIM]
            self.b_hull = [red | curses.A_BOLD, red]
            self.b_struct = [wht, wht | curses.A_DIM, curses.A_DIM]
            self.b_accent = [mag, mag | curses.A_DIM, curses.A_DIM]
            self.b_dash = curses.A_DIM
            self.b_dash_hi = wht | curses.A_BOLD
            self.b_whisper = curses.A_NORMAL
        self.mode = None
        self.set_mode("normal")

    @staticmethod
    def _dimmer(lst):
        n = len(lst)
        return [lst[min(n - 1, i + 1)] for i in range(n)]

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "gold":                       # AFTERBURNER: the world turns gold
            g = self.b_gold
            self.star = g[:]
            self.rock = g[:]
            self.target = g[:]
            self.pickup = g[:]
            self.gold = g[:]
            self.struct = [g[0], g[1], g[2]]
            self.accent = [g[0], g[1], g[2]]
            self.dash = g[1]
            self.dash_hi = g[0]
            self.whisper = g[0]
        elif mode == "hit":                      # 2-frame damage flash
            hh = self.b_hull[0]
            self.star = [hh] * 4
            self.rock = [hh] * 4
            self.target = [hh] * 4
            self.pickup = [hh] * 4
            self.gold = [hh] * 4
            self.struct = [hh] * 3
            self.accent = [hh] * 3
            self.dash = hh
            self.dash_hi = hh
            self.whisper = hh
        elif mode == "dim":                      # held while Claude's reply waits
            self.star = self._dimmer(self.b_star)
            self.rock = self._dimmer(self.b_rock)
            self.target = self._dimmer(self.b_target)
            self.pickup = self._dimmer(self.b_pickup)
            self.gold = self._dimmer(self.b_gold)
            self.struct = self._dimmer(self.b_struct)
            self.accent = self._dimmer(self.b_accent)
            self.dash = self.b_dash
            self.dash_hi = self.b_dash
            self.whisper = self.b_whisper
        else:
            self.star = self.b_star[:]
            self.rock = self.b_rock[:]
            self.target = self.b_target[:]
            self.pickup = self.b_pickup[:]
            self.gold = self.b_gold[:]
            self.struct = self.b_struct[:]
            self.accent = self.b_accent[:]
            self.dash = self.b_dash
            self.dash_hi = self.b_dash_hi
            self.whisper = self.b_whisper


def depth_tier(depth):
    return 0 if depth < 6 else 1 if depth < 15 else 2 if depth < 28 else 3


# ---------------------------------------------------------------------------
# the 747 — same 5-row bitmap font the brick wall and the welcome flyby use.
# The gate speaks the game's own visual language, and the resolve is free.
# ---------------------------------------------------------------------------
FLYBY_FONT = {
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    " ": [".", ".", ".", ".", "."],
}


def gate_rows():
    """The wall: 7 4 7, one blank column between digits. 11 wide, 5 tall.

    Every '#' is a solid world-space chunk. Column 5, rows 0-1 is the enclosed
    gap in the 4 — the eye. It is a real flight window.
    """
    seven, four = FLYBY_FONT["7"], FLYBY_FONT["4"]
    return [seven[i] + "." + four[i] + "." + seven[i] for i in range(5)]


# ---------------------------------------------------------------------------
# the game
# ---------------------------------------------------------------------------
class Sky(object):
    def __init__(self, scr, session, pal, stats):
        self.scr = scr
        self.session = session
        self.pal = pal
        self.stats = stats
        self.frame = Frame()
        self.h = 0
        self.w = 0

        # camera / ship
        self.z_cam = 0.0
        self.z_prev = 0.0
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.lane_i = 2               # LANE SNAP: discrete position, never a drift.
        self.lane_t = 0.0             # "Subway Surfers" means lanes — that is the
                                      # whole readability fix.
        self.vx = 0.0
        self.vy = 0.0
        # ROLL. Its own second-order state, because vx is a position ERROR and
        # not a velocity — see the cockpit note at the top of the file.
        self.roll = 0.0            # cockpit roll, in ROWS of tip displacement
        self.roll_v = 0.0
        self.m_target = None
        # render camera (see CHASE_BACK). rx/ry/rz is the EYE; cam_x/cam_y/z_cam
        # stays the SHIP and remains the only thing collision ever asks about.
        self.chase = False
        self.chase_k = 0.0        # eased 0..1 — 'v' is a camera move, not a cut
        self.lag_x = 0.0
        self.lag_y = 0.0
        self.rx = 0.0
        self.ry = 0.0
        self.rz = 0.0
        self.zoff = 0.0           # z_cam - rz; keeps every LOOK threshold POV-true
        self.ship_sx = 0
        self.ship_sy = 0
        self.ship_d = 0.0

        # ---- run state. THE MECHANICS, SET. ------------------------------
        # A run is SECTORS x SECTOR_M metres. `sector` is the number on the HUD
        # and the number the game is about; `dist` is how the bar fills.
        self.dist = 0.0
        self.sector = 1
        self.overrun = False       # cleared 7/7 and chose to keep flying
        self.won = False           # latched for one frame so run() can report it
        self.seconds = 0.0         # run clock, for the victory screen
        self.score = 0.0
        self.score_shown = 0.0
        self.chain = 0
        self.chain_t = 0.0
        self.chain_f = 0.0
        self.shields = 3           # SHIELDS ONLY. There are no lives.
        self.shields_lost = 0
        self.ammo = 4
        self.threads = 0
        self.thread_credit = 0
        self.clean_m = 0.0
        self.gates_seen = 0
        self.eyes = 0
        self.ammo_pulse = 0.0      # the ammo bar pulses when the first alien lands
        self.first_alien = True
        self.gate_pending = False  # a gate plane is being crossed this frame
        self.gate_touched = False

        # timers / effects
        self.hitstop = 0.0
        self.invuln = 0.6
        self.after = 0.0
        self.flash = 0
        self.shake = 0.0
        self.shake_x = 0
        self.shake_y = 0
        self.speed_scale = 1.0
        self.spool = 0.9
        self.cooldown = 0.0
        self.want_fire = False
        self.msg = ""
        self.msg_t = 0.0
        self.gold_t = 0.0
        self.muzzle = 0
        self.tracer = None
        self.fx = []          # [t, kind, xw, yw, zw, vx, vy]
        self.next_747 = 747

        # world
        self.objs = []
        self.rng = random.Random(747)
        self.next_z = 0.0
        self.pattern_n = 0
        # THE GATE OWNS THE SECTOR BOUNDARY. It is emitted early enough to
        # resolve out of scatter at depth 28, and it is MET at exactly
        # sector * SECTOR_M, so crossing it and clearing the sector are the
        # same event to the player.
        self.gate_z = SECTOR_M
        self.next_pod = 420.0
        self.gate_live = False
        self.gate_base_z = 0.0
        self.gate_told = True

        # loop bookkeeping
        self.manual_play = False
        self.state = "thinking"
        self.idle_drawn = False
        self.frame_dt = FRAME
        self.slow_run = 0
        self.throttled = False
        self.tiny = False
        self.vcx = 0
        self.vcy = 0
        self.t_start = time.time()

        self.stars = []
        self.solve_camera()
        h, w = scr.getmaxyx()
        self.on_resize(h, w)
        self.emit_opening()

    # ---- camera -----------------------------------------------------------
    def solve_camera(self):
        """POV and chase are ONE camera with a blend. At k=0 the eye is exactly
        the ship, so the POV path is bit-for-bit what it always was."""
        k = self.chase_k
        self.zoff = CHASE_BACK * k
        self.rx = self.cam_x + (self.lag_x - self.cam_x) * k
        self.ry = self.cam_y + (self.lag_y - CHASE_UP - self.cam_y) * k
        self.rz = self.z_cam - self.zoff

    # ---- layout -----------------------------------------------------------
    def on_resize(self, h, w):
        self.h, self.w = h, w
        self.frame.resize(h, w)
        self.tiny = (h < 8 or w < 40)
        # THE HUD LAW: the bar owns row 0, the footer owns row h-1, and nothing
        # else ever writes there. SKYRUN used to own only h-1, which is why its
        # onboarding hint had to fight the playfield at pf_top + 1.
        self.bar_row = 0
        self.dash_row = h - 1
        self.pf_top = 1
        self.pf_h = max(1, h - 2)
        # focal lengths scale with the pane — a fixed focal is the easiest way
        # to look wrong on half of everyone's terminals
        self.focal_x = clamp(26.0 * w / 100.0, 14.0, 40.0)
        self.tube_y = clamp(self.pf_h / 14.0 * 4.5, 1.6, 3.4)
        self.focal_y = min(self.focal_x * 0.5,
                           (self.pf_h * 0.42) * 8.0 / max(0.5, self.tube_y))
        self.refresh_lanes()
        self.gate_ch = clamp(self.tube_y * 0.47, 0.55, 1.05)
        self.gate_hy = self.gate_ch * 0.40
        self.eye_cy = -1.5 * self.gate_ch
        self.duck_ok = (self.pf_h >= 10)
        self.narrow = (w < 62)
        if self.pf_h < CHASE_MIN_H:            # shrunk out of chase range
            self.chase = False
        # FLOAT, always: rounding the vanishing point makes the ENTIRE field pop
        # one cell on a single frame (see render()).
        self.vcx = float(w // 2)
        self.vcy = float(self.pf_top + self.pf_h // 2)
        # a resize is a camera cut: give the player their bearings back
        self.invuln = max(self.invuln, 0.6)
        self.spool = max(self.spool, 0.5)
        self.idle_drawn = False
        n = clamp(int(w * self.pf_h / 90.0) + 8, 8, 34)
        rng = random.Random(747)          # seeded 747 — the same sky every run
        self.stars = []
        for _ in range(n):
            th = rng.uniform(0, 2 * math.pi)
            bt = 2 if rng.random() < 0.62 else (1 if rng.random() < 0.6 else 0)
            self.stars.append((math.cos(th), math.sin(th) * 0.5, rng.random(),
                               0.6 + rng.random() * 0.9, bt))

    def refresh_lanes(self):
        """THE SKY WIDENS AT SECTOR 4. Sectors 1-3 are a three-lane road, which
        is the readable default (middle lane = maximum reaction time, the
        Subway Surfers rule). Sector 4 opens it to five, and that widening is
        itself a progression beat the player can see. A narrow or short pane
        stays at three lanes forever — the lane count is a legibility budget,
        never a difficulty knob."""
        room = not (self.w < 60 or self.pf_h < 6)
        want = LANE_X_5 if (room and self.sector >= 4) else LANE_X_3
        old = getattr(self, "lanes", None)
        if old is want:
            return
        self.lanes = want
        # keep the player where they physically are: snap to the nearest lane
        # of the new grid rather than to the same INDEX, which would teleport
        # them across the tube on the frame the road widens.
        best, bd = 0, 1e9
        for i, lx in enumerate(want):
            d = abs(lx - self.cam_x)
            if d < bd:
                best, bd = i, d
        self.lane_i = best

    def on_rejoin(self):
        """Ghost pane came back. Never resume the player into an unavoidable hit."""
        for o in self.objs:
            if o[O_Z] - self.z_cam < 14.0:
                # a rejoin clears the whole near field, eye included: a lone eye
                # floating in a wall that is no longer there is not a secret,
                # it is a bug the player would rightly report
                o[O_FLG] |= F_DEAD | F_EVAL
                if o[O_KIND] == K_EYE:
                    self.gate_live = False
        self.reap()
        self.invuln = max(self.invuln, 0.6)
        self.spool = 0.9                  # the world is already there; speed ramps up
        self.vx = self.vy = 0.0
        # a wall-clock gap must never resume the player mid-bank: the spring
        # would be integrating against a stale error and the cockpit would
        # snap on the rejoin frame — the exact class of pop this pass killed
        self.roll = self.roll_v = 0.0
        self.hitstop = 0.0
        self.shake = 0.0
        self.shake_x = self.shake_y = 0
        self.lag_x, self.lag_y = self.cam_x, self.cam_y   # no camera whip on return
        self.solve_camera()
        self.idle_drawn = False

    # ---- world ------------------------------------------------------------
    # THE GUTTER, ENFORCED STRUCTURALLY as well as drawn (see halo_spans).
    # Role of each object kind, for the "never adjacent to a different class"
    # rule. The gate and its egg are exempt: the gate is 24 authored chunks
    # that ARE one object, direct() already reserves clear air either side of
    # it, and the eye lives inside it by design.
    ROLE = {K_ROCK: 0, K_ALIEN: 1, K_COIN: 2, K_POD: 2}
    SEP_X = 3.0        # world units of lateral clearance between two classes
    SEP_Z = 4.5        # ...and of longitudinal clearance

    def add(self, kind, xw, yw, zw, r, ry=None, vxw=0.0, flg=0):
        if len(self.objs) >= MAXOBJ:
            return None
        role = self.ROLE.get(kind)
        if role is not None:
            # No object is ever born in the footprint of a different class.
            # The halo makes two touching objects legible; this makes them not
            # touch in the first place, which is cheaper and reads better.
            # Push back in z rather than drop: an authored pattern keeps every
            # beat it was written with, just spaced.
            for _ in range(4):
                clash = False
                for p in self.objs:
                    if p[O_FLG] & F_DEAD:
                        continue
                    pr = self.ROLE.get(p[O_KIND])
                    if pr is None or pr == role:
                        continue
                    if (abs(p[O_Z] - zw) < self.SEP_Z
                            and abs(p[O_X] - xw) < self.SEP_X):
                        clash = True
                        break
                if not clash:
                    break
                zw += self.SEP_Z
        o = [kind, xw, yw, zw, r, r if ry is None else ry, vxw, flg]
        self.objs.append(o)
        return o

    def reap(self):
        objs = self.objs
        i = 0
        while i < len(objs):
            o = objs[i]
            # cull against the EYE, not the ship: under a chase camera an object
            # that has passed the ship is still in shot for another 7 units, and
            # culling at the ship makes it vanish in mid-air
            if (o[O_FLG] & F_DEAD) or (o[O_Z] - self.rz) <= NEAR:
                objs[i] = objs[-1]        # swap-with-last, never an O(n) removal
                objs.pop()
            else:
                i += 1

    def speed(self):
        v = self.raw_speed()
        v *= self.speed_scale
        if self.after > 0.0:
            v *= 1.4
        return v * (1.0 - clamp(self.spool / 0.9, 0.0, 1.0))

    def raw_speed(self):
        # ASYMPTOTIC, never linear: 16 -> 34 u/s and it never exceeds 34, in
        # sector 7 or in OVERRUN. That cap is the telegraph number at the top
        # of this file, and it is derived from reaction time, not tuned.
        return V_BASE + V_GAIN * (1.0 - math.exp(-self.dist / 900.0))

    # ---- the run ----------------------------------------------------------
    def sector_t(self):
        """0.0 -> 1.0 across the current sector. This one float IS the answer
        to 'what am I trying to do', and it is on the HUD at all times."""
        return clamp((self.dist - (self.sector - 1) * SECTOR_M) / SECTOR_M,
                     0.0, 1.0)

    def sector_label(self):
        if self.sector <= SECTORS:
            return "S %d/%d" % (self.sector, SECTORS)
        return "OVR%2d" % (self.sector - SECTORS)

    def pattern_gap(self):
        """Sector density, published as a table rather than a formula so the
        difficulty curve is inspectable. The old `22 - dist/150` collapsed to
        the 11-unit floor by sector 4 and then had nothing left to give."""
        gaps = (22.0, 20.0, 18.0, 16.0, 15.0, 13.0, 11.0)
        i = min(self.sector, SECTORS) - 1
        return gaps[i]

    def clear_sector(self):
        """The sector boundary is crossed. This is the only place the run
        structure advances, and it always pays, always says so."""
        n = self.sector
        gain = 200 * min(n, SECTORS)
        self.score += gain
        self.whisper("+%d  sector %d clear" % (gain, n), 1.4, gold=True)
        self.flash = 2
        self.sector += 1
        self.gate_z = self.sector * SECTOR_M
        self.refresh_lanes()
        if not self.overrun and self.sector > SECTORS:
            # THE WIN. A real one, with a screen, which no previous build had.
            self.score += 1000
            self.won = True

    def begin_overrun(self):
        """Cleared 7/7 and chose to keep flying. Endless, pure score — the
        forever-run appetite survives without costing the game a win state.
        Speed does NOT uncap; only density stays at sector-7 pressure."""
        self.overrun = True
        self.won = False
        self.whisper("OVERRUN", 1.6, gold=True)

    # ---- the director -----------------------------------------------------
    def lane(self, i):
        return self.lanes[i % len(self.lanes)]

    def spawn_alien(self, xw, zw, yw=0.0, r=None, ry=None, vxw=0.0, artifact=False):
        """An alien craft or artifact: same collider as an asteroid, but it can
        be SHOT. Routed through the same spawner so every authored pattern gets
        shootable targets without pattern code knowing about it."""
        if r is None:
            r = 1.5
        if ry is None:
            ry = r * 0.8
        self.add(K_ALIEN, xw, yw, zw, r, ry, vxw, F_ARTIFACT if artifact else 0)

    def spawn_rock(self, xw, zw, yw=0.0, r=None, ry=None, vxw=0.0):
        """A lane obstacle. A share of them are ALIEN CRAFT rather than rock —
        routed here so every authored pattern gets shootable targets for free.

        The two classes teach the two controls:
          asteroid shard -> spans the tube vertically, so you must go AROUND it
          alien craft    -> short, so you can shoot it OR fly OVER/UNDER it
        That is how the player discovers the vertical axis without a tutorial.

        SECTOR 1 IS ROCKS ONLY. One verb per sector: sector 1 teaches steering
        and nothing else, and the gun arrives in sector 2 with the ammo bar
        pulsing. Two new mechanics in the same minute is how a runner becomes
        noise."""
        if r is None:
            r = 1.2 + self.rng.random() * 0.4
        if self.sector >= 2 and self.rng.random() < 0.38 and ry is None:
            artifact = self.rng.random() < 0.22
            self.spawn_alien(xw, zw, yw, r * 0.9,
                             1.15 if not artifact else 0.9, vxw, artifact)
            return
        if ry is None:
            ry = self.tube_y + 0.7
        self.add(K_ROCK, xw, yw, zw, r, ry, vxw)

    def spawn_coin(self, xw, zw, yw=0.0):
        self.add(K_COIN, xw, yw, zw, 0.55, 0.55)

    def emit_opening(self):
        """THE FIRST 8 SECONDS — hand-authored, never procedural, and the only
        place in the file where the world is written by hand. This is the Mario
        1-1 method: the level teaches the game, text does not.

        Spool ramps 0 -> 16 u/s over 0.9 s (7.2 m), then 16 u/s, so:

          t 0.0  empty sky, S 1/7, an empty bar. NOTHING to hit for a full
                 second — the eye gets to read the frame before it reads a
                 threat, and the bar gets to be noticed before it matters.
          t 1.0  ONE COIN dead centre in your lane. Do nothing, collect it,
                 +10, whisper. -> GREEN IS TAKE.
          t 2.5  ONE ROCK, red, full tube height, centre lane. It has been on
                 screen since frame one (~2 s of telegraph, 8x human RT). One
                 keypress solves it. -> RED IS MOVE.
          t 4.5  a 5-COIN ARC curving centre -> right. -> coins reward a LINE,
                 not a lane.
          t 6.0  ONE ALIEN, cyan, visibly SHORT, ammo bar pulsing, whisper
                 'space'. Shooting it and flying over it are BOTH correct.
                 -> the second object class, and the vertical axis.
          t 8.0  the sector bar has visibly moved.

        After that the director takes over at z = 110."""
        mid = len(self.lanes) // 2
        cx = self.lane(mid)
        right = self.lane(min(len(self.lanes) - 1, mid + 1))
        self.spawn_coin(cx, 8.8)                                   # t ~1.0
        self.add(K_ROCK, cx, 0.0, 32.8, 1.4, self.tube_y + 0.7)    # t ~2.5
        for i in range(5):                                         # t ~4.5
            t = i / 4.0
            self.spawn_coin(cx + (right - cx) * t, 64.8 + i * 2.2)
        # the alien is added directly, never through spawn_rock: the opening is
        # authored and must not depend on a dice roll to teach the gun
        self.add(K_ALIEN, cx, 0.0, 88.8, 1.35, 1.15)               # t ~6.0
        self.next_z = 110.0
        self.pattern_n = 0

    def direct(self):
        """Emit authored patterns on the lane grid. The ship flies analog; the
        WORLD is authored — readable, rhythmic and provably solvable."""
        # THE GATE OWNS THE SECTOR BOUNDARY, and it is emitted on its OWN
        # schedule — the instant it enters spawn range — never folded into the
        # pattern cursor. Folding it in was a real bug: next_z can already be
        # up to a pattern-plus-gap PAST the boundary when the cursor catches
        # up, and snapping the gate back to the boundary from there would
        # spawn all 24 chunks at depth 6, with no telegraph at all.
        if self.gate_z > 0.0 and self.z_cam + SPAWN_Z >= self.gate_z:
            self.emit_gate(self.gate_z)
            self.next_z = max(self.next_z, self.gate_z + 46.0)
            self.gate_z = 0.0
        while self.z_cam + SPAWN_Z >= self.next_z:
            base = self.next_z
            if self.gate_z > 0.0 and base + 24.0 >= self.gate_z:
                # a pattern that would land in the gate's approach is deferred
                # to the gate itself: the 747 must resolve out of scatter with
                # clear air around it, or the reveal reads as more debris
                self.next_z = self.gate_z
                break
            self.pattern_n += 1
            if self.pattern_n % 5 == 0:
                length = self.emit_breath(base)      # tension -> release
            else:
                length = self.emit_pattern(base)
            gap = self.pattern_gap()
            if self.dist >= self.next_pod and self.ammo <= 1:
                self.add(K_POD, self.lane(self.rng.randrange(len(self.lanes))),
                         0.0, base + length * 0.5, 0.6, 0.6)
                self.next_pod = self.dist + 420.0
            self.next_z = base + length + gap

    def emit_breath(self, base):
        # 22 units of empty sky and a single coin arc. Unbroken pressure makes
        # fatigue, and fatigue closes the pane.
        self.emit_coin_arc(base + 6.0)
        return 22.0

    def emit_pattern(self, base):
        """ONE NEW IDEA PER SECTOR, never two. The bag is gated on the SECTOR,
        not on raw distance, so the difficulty curve and the number on the HUD
        are the same thing — which is what makes progress legible:

          S1 steering only, rocks only        S5 DUCK (the vertical axis)
          S2 the gun arrives                  S6 swarms
          S3 drifting rocks, bait             S7 everything, 34 u/s, gap 11
          S4 five lanes, slalom
        """
        s = min(self.sector, SECTORS)
        bag = ["single", "pair", "coin_string"]
        if s >= 2:
            bag += ["gate_rocks", "coin_arc"]
        if s >= 3:
            bag += ["bait", "drift"]
        if s >= 4:
            bag.append("slalom")
        if s >= 5 and self.duck_ok:
            bag.append("duck")
        if s >= 6:
            bag.append("swarm")
        p = bag[self.rng.randrange(len(bag))]
        n = len(self.lanes)
        if p == "single":
            self.spawn_rock(self.lane(self.rng.randrange(n)), base)
            return 4.0
        if p == "pair":
            a = self.rng.randrange(n)
            b = (a + 1 + self.rng.randrange(n - 1)) % n
            self.spawn_rock(self.lane(a), base)
            self.spawn_rock(self.lane(b), base + 1.5)
            return 6.0
        if p == "coin_string":
            ln = self.lane(self.rng.randrange(n))
            k = 5 + self.rng.randrange(3)
            for i in range(k):
                self.spawn_coin(ln, base + i * 2.2)
            return k * 2.2
        if p == "gate_rocks":
            free = self.rng.randrange(n)
            for i in range(n):
                if i != free:
                    self.spawn_rock(self.lane(i), base)
            return 5.0
        if p == "slalom":
            # the solvability gate: never demand a lane change faster than the
            # ship can physically make it
            v = max(8.0, self.raw_speed())
            step = max(7.0, abs(self.lanes[0] - self.lanes[-1]) * 0.16 * v)
            side = 0
            for i in range(4):
                self.spawn_rock(self.lane(0 if side else n - 1), base + i * step)
                side ^= 1
            return 3 * step + 4.0
        if p == "coin_arc":
            return self.emit_coin_arc(base)
        if p == "bait":
            ln = self.lane(self.rng.randrange(n))
            self.spawn_rock(ln, base)
            for i in range(5):
                self.spawn_coin(ln, base + 3.0 + i * 2.2)
            return 16.0
        if p == "drift":
            a = self.rng.randrange(n)
            sgn = 1.0 if a < n // 2 else -1.0
            v = max(8.0, self.raw_speed())
            self.spawn_rock(self.lane(a), base, vxw=sgn * 3.6 * 0.16 * v / 2.2)
            return 8.0
        if p == "duck":
            # rocks up high, the clean line is low — the one pattern that is
            # solved with the vertical axis, hence the playfield_h >= 10 gate
            for i in range(n):
                self.spawn_rock(self.lane(i), base + (i % 2) * 1.2,
                                yw=-self.tube_y * 0.62,
                                ry=self.tube_y * 0.55)
            return 8.0
        if p == "swarm":
            free = self.rng.randrange(n)
            for i in range(5):
                ln = self.rng.randrange(n)
                if ln == free:
                    ln = (ln + 1) % n
                self.spawn_rock(self.lane(ln), base + i * 3.0, r=0.9, ry=1.1,
                                yw=(self.rng.random() - 0.5) * self.tube_y)
            return 16.0
        return 6.0

    def emit_coin_arc(self, base):
        n = len(self.lanes)
        a = self.rng.randrange(max(1, n - 2))
        for i in range(7):
            t = i / 6.0
            xw = self.lane(a) + (self.lane(min(n - 1, a + 2)) - self.lane(a)) * t
            self.spawn_coin(xw, base + i * 2.2)
        return 16.0

    def emit_gate(self, base):
        """THE 747 GATE. A wide scatter of chunks at depth 46 that resolves into
        three unmistakable gold digits at depth 28-14 — while the player is
        already choosing a line through it."""
        rows = gate_rows()
        for r in range(5):
            line = rows[r]
            for c in range(11):
                if line[c] != "#":
                    continue
                self.add(K_GATE, (c - 5.0) * GATE_CW, (r - 2.0) * self.gate_ch,
                         base, GATE_HX, self.gate_hy)
        self.add(K_EYE, 0.0, self.eye_cy, base, 0.5, 0.5)
        self.gates_seen += 1
        self.gate_live = True
        self.gate_base_z = base
        self.gate_told = False
        return 6.0

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        pal = self.pal
        # timers run on real time; the SIM freezes during hitstop
        sim = 0.0 if self.hitstop > 0.0 else dt
        if self.hitstop > 0.0:
            self.hitstop -= dt
        if self.invuln > 0.0:
            self.invuln -= dt
        if self.spool > 0.0:
            self.spool = max(0.0, self.spool - dt)
        if self.msg_t > 0.0:
            self.msg_t -= dt
        if self.gold_t > 0.0:
            self.gold_t -= dt
        if self.cooldown > 0.0:
            self.cooldown -= dt
        if self.muzzle > 0:
            self.muzzle -= 1
        if self.flash > 0:
            self.flash -= 1
        if self.shake > 0.0:
            self.shake = max(0.0, self.shake - dt * 9.0)
        if self.after > 0.0:
            self.after -= dt
            if self.after <= 0.0:
                self.after = 0.0
                self.whisper("afterburner out", 1.0)
        self.speed_scale += (1.0 - self.speed_scale) * min(1.0, dt / 1.5)

        # ---- steering: impulse + exponential damping. OS key-repeat delivers
        #      discrete repeats, never key-down/up, so direct position stepping
        #      feels choppy and hold-to-steer feels broken.
        self.vx *= math.exp(-6.0 * dt)
        self.vy *= math.exp(-7.0 * dt)
        if self.m_target is not None:
            tx, ty = self.m_target
            k = min(1.0, 12.0 * dt)
            self.cam_x += (tx - self.cam_x) * k
            self.cam_y += (ty - self.cam_y) * k
            self.m_target = None
        # LANE SNAP: tween hard to the lane centre (~90ms). Discrete position is
        # what makes an incoming object readable — you know which lane it is in
        # and which lane you are in, with no estimation.
        self.lane_i = int(clamp(self.lane_i, 0, len(self.lanes) - 1))
        tgt = self.lanes[self.lane_i]
        self.cam_x += (tgt - self.cam_x) * min(1.0, dt * 16.0)
        if self.lane_t > 0.0:
            self.lane_t = max(0.0, self.lane_t - dt)
        self.vx = (tgt - self.cam_x) * 6.0      # keep bank/lean visuals alive
        # ---- ROLL. vx is a position ERROR and collapses in ~4 frames (90 ms) —
        #      shorter than visual integration, which is why the old strut lean
        #      read as a POP and not as a bank. Roll gets its own second-order
        #      state: a spring toward the commanded bank, damped just under
        #      critical so it overshoots ONCE and settles. The overshoot is
        #      what the eye reads as MASS. MEASURED on a real lane hop at
        #      dt=0.030: 17 frames = 510 ms of visible roll, peak +0.30 rows,
        #      settling through zero. The same input through the old strut
        #      lean produced the integer column trace [1, 1, 0, 0, 0, ...] —
        #      a 60 ms twitch and then nothing, which is the "pop".
        cmd = clamp(self.vx * 0.085, -ROLL_MAX, ROLL_MAX)
        self.roll_v += (cmd - self.roll) * 90.0 * dt      # k  -> ~1.5 Hz
        self.roll_v *= math.exp(-7.0 * dt)                # zeta ~0.37
        self.roll = clamp(self.roll + self.roll_v * dt, -ROLL_MAX, ROLL_MAX)
        self.cam_x = clamp(self.cam_x, -TUBE_X, TUBE_X)
        self.cam_y = clamp(self.cam_y + self.vy * dt, -self.tube_y, self.tube_y)

        # ---- fly forward
        v = self.speed()
        self.z_prev = self.z_cam
        self.z_cam += v * sim
        self.dist += v * sim
        self.clean_m += v * sim
        self.seconds += dt
        if self.ammo_pulse > 0.0:
            self.ammo_pulse -= dt
        # ---- THE RUN ADVANCES. One line, and it is the whole win condition.
        # cannot double-fire: dist does not advance again inside this frame,
        # and clear_sector() raises the next boundary by a full SECTOR_M
        if self.dist >= self.sector * SECTOR_M:
            self.clear_sector()

        # ---- the render camera. Solved BEFORE direct()/reap() so the spawn and
        #      cull planes are the ones the player can actually see.
        self.chase_k += ((1.0 if self.chase else 0.0) - self.chase_k) * min(1.0, dt * 5.0)
        if self.chase_k < 0.004:
            self.chase_k = 0.0
        elif self.chase_k > 0.996:
            self.chase_k = 1.0
        # a first-order lag on the eye is the whole read of a chase camera: the
        # car slides across the frame when you steer instead of being welded to
        # the centre of it
        self.lag_x += (self.cam_x - self.lag_x) * min(1.0, dt * 4.0)
        self.lag_y += (self.cam_y - self.lag_y) * min(1.0, dt * 5.5)
        self.solve_camera()

        # ---- chain decay: missing a coin must never punish, or players start
        #      playing safe, and safe is boring
        self.chain_t += dt
        if self.chain_t > 2.5 and self.chain > 0:
            self.chain_f += 4.0 * dt
            while self.chain_f >= 1.0 and self.chain > 0:
                self.chain_f -= 1.0
                self.chain -= 1

        self.direct()
        # THE GATE ANNOUNCES ITSELF ONCE PER SECTOR, on the frame it stops
        # being scatter and resolves into three gold digits. This is the line
        # that turns an invisible Easter egg into something players chase.
        if (self.gate_live and not self.gate_told
                and self.gate_base_z - self.z_cam < 28.0):
            self.gate_told = True
            self.whisper(txt("747 · thread the hole in the 4"), 1.6, gold=True)
        # THE GUN ANNOUNCES ITSELF ONCE, when the first alien is genuinely on
        # screen — not on a timer, and never again. An unteachable mechanic is
        # an absent mechanic; a mechanic taught twice is a nag.
        if self.first_alien:
            for o in self.objs:
                if (o[O_KIND] == K_ALIEN and not (o[O_FLG] & F_DEAD)
                        and o[O_Z] - self.z_cam < 30.0):
                    self.first_alien = False
                    self.ammo_pulse = 1.8
                    # the SHAPE, not the hue — this whisper fires on a mono
                    # pane too, where "the cyan" names nothing on screen
                    self.whisper(txt("[space] shoots the %s" % ALIEN_CH), 1.8)
                    break
        if self.want_fire:
            self.want_fire = False
            self.fire()

        self.collide(sim, dt)
        self.reap()
        self.effects(dt)

        # score counts up visibly instead of teleporting
        if self.score_shown < self.score:
            self.score_shown = min(self.score, self.score_shown + 400.0 * dt)
        elif self.score_shown > self.score:
            self.score_shown = self.score
        if self.score >= self.next_747:
            self.next_747 += 747
            self.gold_t = 1.5

        # a shield back for every 400 m of clean flight — the comeback
        if self.clean_m >= 400.0:
            self.clean_m = 0.0
            if self.shields < 3:
                self.shields += 1
                self.whisper("shield restored", 1.2, gold=True)

        if self.shake > 0.0:
            self.shake_x = int(round(self.shake * (1 if self.rng.random() < 0.5 else -1)))
            self.shake_y = int(round(self.shake * 0.5 * (1 if self.rng.random() < 0.5 else -1)))
        else:
            self.shake_x = self.shake_y = 0
        if self.after > 0.0:
            pal.set_mode("gold")
        elif self.flash > 0:
            pal.set_mode("hit")
        else:
            pal.set_mode("normal")
        return self.shields <= 0

    def whisper(self, text, t=0.9, gold=False):
        """WHISPERS. Every named scoring event says what it paid, for 0.9 s, in
        the footer — never over the playfield. No number on this screen is ever
        unexplained, so the scoring is learnable instead of folklore. (The
        clean-dodge +5 is deliberately silent: it fires several times a second
        and a whisper on it would strobe. It is listed on the game-over table
        with everything else.)"""
        self.msg, self.msg_t = text, t
        if gold:
            self.gold_t = max(self.gold_t, t)

    def mult(self):
        m = min(5, 1 + self.chain // 8)
        return m * (7 if self.after > 0.0 else 1)

    def fire(self):
        if self.cooldown > 0.0:
            return
        self.cooldown = 0.25
        if self.ammo <= 0:
            self.whisper("dry · thread a rock to reload")
            self.shake = 0.5              # the click you feel when there is nothing
            return
        self.ammo -= 1
        self.muzzle = 3
        # HITSCAN, not a projectile: a travelling bullet tunnels on exactly the
        # frames where dt is clamped after a rejoin.
        best = None
        bz = 1e9
        for o in self.objs:
            if o[O_KIND] != K_ALIEN or (o[O_FLG] & F_DEAD):
                continue
            depth = o[O_Z] - self.z_cam
            if depth <= HIT_Z or depth > 44.0:
                continue
            if abs(o[O_X] - self.cam_x) < 2.2 and abs(o[O_Y] - self.cam_y) < 1.3:
                if depth < bz:
                    bz, best = depth, o
        if best is None:
            self.tracer = [0.18, self.cam_x, self.cam_y, self.z_cam + 40.0]
            return
        self.tracer = [0.26, best[O_X], best[O_Y], best[O_Z]]   # longer, more visible
        best[O_FLG] |= F_DEAD | F_EVAL
        gain = 40 * self.mult()
        self.score += gain
        self.chain += 1
        self.chain_t = 0.0
        if best[O_FLG] & F_ARTIFACT:
            # the artifact carries a shell: the aggressive line is also the
            # sustainable one, which is the whole economy of this game
            if self.ammo < 6:
                self.ammo += 1
                self.whisper("+%d  artifact · +1 shell" % gain)
            else:
                self.score += 20
                self.whisper("+%d  artifact" % gain)
        else:
            self.whisper("+%d  alien" % gain)
        # THE KILL MUST BE FELT. hitstop is the cheapest "that connected" trick
        # there is, and with no audio yet it is doing all of the work.
        self.hitstop = 0.055
        self.flash = 2
        self.shake = 1.8
        self.vy -= 1.1                    # recoil: the nose kicks up off the shot
        # a real burst, not five polite sparks
        for i in range(12):
            a = i * 0.5236
            sp = 3.4 + (i % 3) * 1.5
            self.fx.append([0.42, "spark", best[O_X], best[O_Y], best[O_Z],
                            math.cos(a) * sp, math.sin(a) * sp * 0.7])
        self.fx.append([0.30, "kring", best[O_X], best[O_Y], best[O_Z], 0.0, 0.0])

    def eye_pass(self):
        """THE EYE IS EVALUATED FIRST, ALWAYS — before any gate chunk can fire.

        The 23 gate chunks and the eye share one world z, so they cross the hit
        plane on the SAME frame, and reap()'s swap-with-last makes list order
        meaningless. Whoever is evaluated first owns the frame: if a chunk went
        first it called damage(), which blanks every object within 12 units —
        the eye included — so the one line that TEACHES the secret never fired.
        Nine of sixteen held lines through the gate said nothing at all.

        Hoisting the eye out of the main sweep makes the verdict order-free,
        and it means threading the eye now genuinely shatters the wall you are
        already inside instead of being overruled by the chunk beside it."""
        zc, zp = self.z_cam, self.z_prev
        for o in self.objs:
            if o[O_KIND] != K_EYE or (o[O_FLG] & (F_DEAD | F_EVAL)):
                continue
            if not ((o[O_Z] - zp) > HIT_Z >= (o[O_Z] - zc)):
                continue
            o[O_FLG] |= F_DEAD | F_EVAL
            self.gate_live = False
            # the gate plane is being crossed THIS frame. Whether it pays the
            # seam bonus or costs a shield is decided at the end of collide(),
            # once every chunk on this plane has been evaluated.
            self.gate_pending = True
            self.gate_touched = False
            dy = o[O_Y] - self.cam_y
            if (abs(o[O_X] - self.cam_x) < EYE_HX
                    and -0.45 * self.gate_ch < dy < 0.60 * self.gate_ch):
                self.trigger_afterburner()
            else:
                # the miss TEACHES the mechanic — this one line is what turns
                # a hidden Easter egg into something players chase
                self.whisper("747 · there is a hole in the 4", 1.5, gold=True)

    def collide(self, sim, dt):
        """Swept, world-space, asymmetric. At v=34 the camera advances a full
        world unit per frame — an instantaneous test tunnels."""
        self.eye_pass()
        zc, zp = self.z_cam, self.z_prev
        cx, cy = self.cam_x, self.cam_y
        for o in self.objs:
            k = o[O_KIND]
            if k == K_EYE:                     # owned by eye_pass(), never here
                continue
            if o[O_VX] != 0.0:
                o[O_X] += o[O_VX] * sim
                if abs(o[O_X]) > TUBE_X:
                    o[O_VX] = -o[O_VX]
            depth = o[O_Z] - zc
            if k == K_COIN and depth < 8.0 and abs(o[O_X] - cx) < 3.5:
                # magnetism: coins visibly curve into you over the last 0.3 s
                kk = min(1.0, 7.0 * dt)
                o[O_X] += (cx - o[O_X]) * kk
                o[O_Y] += (cy - o[O_Y]) * kk
            if o[O_FLG] & (F_DEAD | F_EVAL):
                continue
            prev = o[O_Z] - zp
            if not (prev > HIT_Z >= depth):
                continue
            o[O_FLG] |= F_EVAL
            dx, dy = o[O_X] - cx, o[O_Y] - cy
            adx, ady = abs(dx), abs(dy)
            if k == K_COIN:
                if adx < 2.2 and ady < 1.6:
                    o[O_FLG] |= F_DEAD
                    gain = 10 * self.mult()
                    self.score += gain
                    self.chain += 1
                    self.chain_t = 0.0
                    self.whisper("+%d  coin" % gain, 0.7)
                    self.fx.append([0.25, "ring", o[O_X], o[O_Y], o[O_Z], 0.0, 0.0])
            elif k == K_POD:
                if adx < 2.4 and ady < 1.8:
                    o[O_FLG] |= F_DEAD
                    self.ammo = min(6, self.ammo + 2)
                    self.ammo_pulse = 1.0
                    self.whisper("+2 shells", 1.0)
            elif k == K_GATE:
                if adx < GATE_HX + 0.30 and ady < self.gate_hy + 0.25:
                    if self.after > 0.0:
                        o[O_FLG] |= F_DEAD
                        self.score += 25 * self.mult()
                    else:
                        self.gate_touched = True
                        self.damage(gate=True)
            else:                                  # rock
                hy = o[O_RY]
                if adx < 1.5 and ady < hy:
                    if self.after > 0.0:           # you are the bullet
                        o[O_FLG] |= F_DEAD
                        self.score += 25 * self.mult()
                        for i in range(5):
                            a = i * 1.2566
                            self.fx.append([0.35, "spark", o[O_X], o[O_Y], o[O_Z],
                                            math.cos(a) * 3.0, math.sin(a) * 2.0])
                    else:
                        self.damage()
                elif adx < 2.9 and ady < hy + 0.9:
                    # THREAD: pass within 2.9 world units and the greedy read is
                    # also the correct read. Ammo is earned by bravery, so the
                    # aggressive line is the sustainable one.
                    o[O_FLG] |= F_THREAD
                    self.threads += 1
                    gain = 12 * self.mult()
                    self.score += gain
                    self.chain += 1
                    self.chain_t = 0.0
                    self.thread_credit += 1
                    if self.thread_credit >= 3:
                        self.thread_credit = 0
                        if self.ammo < 6:
                            self.ammo += 1
                            self.ammo_pulse = 1.0
                            self.whisper("+%d  thread · +1 shell" % gain)
                        else:
                            self.score += 20
                            self.whisper("+%d  thread" % gain, 0.7)
                    else:
                        self.whisper("+%d  thread" % gain, 0.7)
                else:
                    self.score += 5 * self.mult()  # clean dodge (silent: it
                                                   # fires several times a
                                                   # second and would strobe)
        # ---- THE GATE VERDICT. Resolved after the whole plane is evaluated,
        #      because the eye is hoisted out of the sweep and fires first: at
        #      eye time we do not yet know whether a chunk was hit.
        if self.gate_pending:
            self.gate_pending = False
            if not self.gate_touched and self.after <= 0.0:
                gain = 100 * self.mult()
                self.score += gain
                self.whisper("+%d  gate seam" % gain, 1.2, gold=True)

    def damage(self, gate=False):
        if self.invuln > 0.0:
            return
        self.shields -= 1
        self.shields_lost += 1
        self.chain = 0
        self.clean_m = 0.0
        self.hitstop = 0.09                 # with no audio, hitstop IS the impact
        self.flash = 2
        self.shake = 2.5
        self.invuln = 1.2
        self.speed_scale = 0.55             # the knockback is a gift disguised
        if gate:
            # flying into the wall is the single highest-value teaching moment
            # in the game, so it says MORE than a clean pass, never less. And
            # the wall is NEVER something you can be stuck against: the blank
            # below knocks you straight through it.
            self.whisper("747 · there is a hole in the 4", 2.0, gold=True)
        elif self.shields > 0:
            self.whisper("shield down · %d left" % self.shields, 1.2)
        for o in self.objs:                 # you can never insta-die twice
            # the eye is NEVER blanked here: it is the only object whose
            # evaluation is a message rather than a hit, and swallowing it is
            # exactly how the secret stayed invisible
            if o[O_KIND] != K_EYE and o[O_Z] - self.z_cam < 12.0:
                o[O_FLG] |= F_DEAD | F_EVAL

    def trigger_afterburner(self):
        self.after = 7.47
        self.eyes += 1
        self.ammo = 6
        self.shields = min(3, self.shields + 1)
        self.score += 747
        self.whisper("+747  THE EYE OF THE 4", 2.0, gold=True)
        self.invuln = max(self.invuln, 0.4)

    def effects(self, dt):
        i = 0
        while i < len(self.fx):
            e = self.fx[i]
            e[0] -= dt
            if e[0] <= 0.0:
                self.fx[i] = self.fx[-1]
                self.fx.pop()
                continue
            e[2] += e[5] * dt
            e[3] += e[6] * dt
            i += 1
        if self.tracer is not None:
            self.tracer[0] -= dt
            if self.tracer[0] <= 0.0:
                self.tracer = None

    # ---- input ------------------------------------------------------------
    def drain(self):
        scr = self.scr
        ch = scr.getch()
        n = 0
        mouse = None
        while ch != -1 and n < 64:            # capped: a fast mouse sweep at
            n += 1                            # ?1003h can otherwise starve a frame
            if ch in (ord("q"), ord("Q")):
                return "quit"
            if ch in (curses.KEY_LEFT, ord("a"), ord("A")):
                if self.lane_i > 0:
                    self.lane_i -= 1
                    self.lane_t = 0.09
            elif ch in (curses.KEY_RIGHT, ord("d"), ord("D")):
                if self.lane_i < len(self.lanes) - 1:
                    self.lane_i += 1
                    self.lane_t = 0.09
            elif ch in (curses.KEY_UP, ord("w"), ord("W")):
                self.vy -= 6.5          # was 4.5: climbing felt inert next to
            elif ch in (curses.KEY_DOWN, ord("s"), ord("S")):   # lateral (7.0),
                self.vy += 6.5          # so the axis read as MISSING entirely
            elif ch == ord(" "):
                self.want_fire = True
            elif ch in (ord("p"), ord("P")):
                self.manual_play = not self.manual_play
                self.idle_drawn = False
                if self.manual_play and self.state == "idle":
                    # they chose the game over the answer — the honest metric
                    self.stats["continued_after_idle"] += 1
            elif ch in (ord("v"), ord("V")):
                if self.chase:
                    self.chase = False
                    self.whisper("windshield")
                elif self.pf_h >= CHASE_MIN_H:
                    self.chase = True
                    self.whisper("chase camera")
                else:
                    self.whisper("pane too short for chase", 1.2)
                self.idle_drawn = False
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, _ = curses.getmouse()
                    mouse = (mx, my)          # keep only the LAST position
                except curses.error:
                    pass
            ch = scr.getch()
        if mouse is not None:
            mx, my = mouse
            # ABSOLUTE mapping (screen position -> tube position). The chase lift
            # is a CONSTANT row offset, never a function of cam_y — referencing
            # the drawn car would make the mouse push itself.
            yref = self.vcy + self.chase_k * (CHASE_UP * self.focal_y
                                              / (CHASE_BACK + 22.0))
            tx = clamp(((mx - self.w / 2.0) / max(1.0, self.w * 0.42)) * TUBE_X,
                       -TUBE_X, TUBE_X)
            ty = clamp(((my - yref) / max(1.0, self.pf_h * 0.5)) * self.tube_y,
                       -self.tube_y, self.tube_y)
            self.m_target = (tx, ty)
        self.vx = clamp(self.vx, -14.0, 14.0)
        self.vy = clamp(self.vy, -8.0, 8.0)
        return None

    # ---- rendering --------------------------------------------------------
    def project(self, xw, yw, depth):
        # rx/ry, never cam_x/cam_y: the projection origin IS the eye. At
        # chase_k == 0 they are identical, so POV is untouched.
        return (self.vcx + (xw - self.rx) * self.focal_x / depth,
                self.vcy + (yw - self.ry) * self.focal_y / depth)

    def render(self, playing):
        fr = self.frame
        fr.clear()
        # the world leans when you turn — four lines, and the entire windshield
        # feeling. The HUD never shakes; the vanishing point absorbs it all.
        # under a chase camera the lag already shows the turn, so the world-lean
        # is dialled back rather than doubled up
        # NOT int(round(...)): rounding the vanishing point makes the ENTIRE
        # field pop one cell on a single frame. As a float, each star and each
        # object rounds at its own sub-cell phase, so a 0.4-cell lean moves
        # ~40% of the field — the pan becomes progressive instead of stepped.
        # Every consumer (project(), draw_stars(), the mouse yref) is float
        # math already, so this costs nothing.
        self.vcx = (self.w // 2 + self.shake_x
                    - self.vx * 0.55 * (1.0 - 0.55 * self.chase_k))
        self.vcy = float(self.pf_top + self.pf_h // 2 + self.shake_y)
        # resolved BEFORE anything is drawn: the tracer starts at the gun, and
        # the gun is wherever the ship is on screen this frame
        if self.chase_k >= 0.5:
            self.ship_d = max(NEAR + 0.5, self.zoff)
            sx, sy = self.project(self.cam_x, self.cam_y, self.ship_d)
            self.ship_sx, self.ship_sy = int(round(sx)), int(round(sy))
        else:
            self.ship_d = 0.0                  # POV: unchanged, the hood line
            self.ship_sx = self.w // 2
            self.ship_sy = self.pf_top + self.pf_h - 1
        self.draw_stars()
        # RAILS REMOVED 2026-07-27 — Founder: "the lines of the POV whenever I click
        # right or left are bad graphics". Falcon rec #2 agrees: on a ~22-row pane a
        # converging-line field SWIMS when the camera pans and reads as noise, not
        # structure. Kept draw_rails() below (unused) only as the record of what was
        # tried; the side-on title is where lane structure actually belongs.
        # near objects always draw in full; if the LAST blit already overran the
        # wire budget, the far field degrades to its point state this frame
        cheap_far = fr.cost > BYTES_MAX
        zo = self.zoff
        far = []
        for o in self.objs:
            depth = o[O_Z] - self.rz
            if depth <= NEAR:
                continue
            if depth - zo < 20.0:
                self.draw_obj(o, depth, False)
            else:
                far.append((o, depth))
        for o, depth in far:
            self.draw_obj(o, depth, cheap_far)
        self.draw_fx()
        self.draw_ship()
        self.draw_hud(playing)
        if not playing:
            self.draw_pause()
        fr.blit(self.scr)

    def draw_rails(self):
        """FIVE CONVERGING RAILS — the whole readability fix. Free-flight over an
        empty starfield gave the player no frame of reference, so an approaching
        object was an ambiguous growing dot. The rails give position, lane and
        gap at a glance, permanently."""
        fr = self.frame
        pal = self.pal
        gy = self.tube_y + 0.9              # the "floor" the rails lie on
        near = max(NEAR + 1.0, self.zoff + 1.5)
        cur = self.lane_i if hasattr(self, "lane_i") else -1
        for li, lx in enumerate(self.lanes):
            mine = (li == cur)
            d = near
            step = 0.9
            while d < 52.0:
                sx, sy = self.project(lx, gy, d)
                x, y = int(round(sx)), int(round(sy))
                if 0 <= y < self.h and 0 <= x < self.w:
                    tier = depth_tier(d - self.zoff)
                    if mine:
                        ch = RAIL_ON
                        attr = pal.target[0]
                    else:
                        ch = RAIL_OFF
                        attr = pal.star[min(2, tier)]
                    fr.put(y, x, ch, attr, d + 60.0)   # always behind objects
                d += step
                step *= 1.16                # perspective: sparser as it recedes

    def draw_stars(self):
        fr = self.frame
        pal = self.pal
        vcx, vcy = self.vcx, self.vcy
        w, ph = self.w, self.pf_h
        # trails are driven by dz/dt, never by a per-FRAME delta — a frame-delta
        # surge test fires early on a slow terminal
        surge = self.after > 0.0 or self.speed() > 26.0
        for ux, uy, base, spd, bt in self.stars:
            rr = (base + self.rz * 0.020 * spd) % 1.0
            if rr < 0.08 or rr > 0.92:
                continue
            rr2 = rr * rr
            sx = vcx + ux * rr2 * (w * 0.62)
            sy = vcy + uy * rr2 * (ph * 1.15)
            tier = 2 if (rr < 0.2 or rr > 0.82) else bt
            fr.put(int(round(sy)), int(round(sx)), DOT, pal.star[tier], 90)
            if surge and rr > 0.45:
                fr.put(int(round(sy - uy * 1.6)), int(round(sx - ux * 1.6)),
                       DOT, pal.star[2], 91)

    def draw_obj(self, o, depth, cheap):
        k = o[O_KIND]
        if k == K_EYE:
            self.draw_eye(o, depth)
            return
        scx, scy = self.project(o[O_X], o[O_Y], depth)
        if scx < -30 or scx > self.w + 30:
            return
        # tiers, fog and the gate's gold reveal are all keyed to the distance
        # from the SHIP, not from the eye — otherwise the whole palette shifts
        # the moment you press 'v', and the 747 resolves 7 units late.
        tier = depth_tier(depth - self.zoff)
        if k == K_GATE:
            self.draw_gate_block(o, depth, scx, scy, tier, cheap)
        elif k == K_COIN:
            self.draw_coin(o, depth, scx, scy, tier, cheap)
        elif k == K_ALIEN:
            self.draw_alien(o, depth, scx, scy, tier, cheap)
        elif k == K_POD:
            # ammo pod: PICKUP green, same role as the coin, different
            # silhouette. That is the role system working, not a clash.
            # Far away it wears COIN_FAR — the PICKUP family — never the star's
            # bare dot.
            py, px = int(round(scy)), int(round(scx))
            self.frame.put(py, px, POD_CH if depth < 12 else COIN_FAR,
                           self.pal.pickup[tier], depth)
            self.halo_box(py, py, px, px, depth)
        else:
            self.draw_rock(o, depth, scx, scy, tier, cheap)

    # ---- THE 1-CELL GUTTER ------------------------------------------------
    # "Every object renders a 1-cell halo of background around its own
    #  silhouette at its own z minus epsilon."  Frame.put() z-tests, so a
    # NEARER object still overwrites the halo correctly and only a FARTHER one
    # is kept out — cost is the perimeter, not the field.
    #
    # This is what was missing. Without it a rock rendered strictly inside a
    # coin ring ("◈◈◆   ◈◈") and a rock sat in the cell touching the alien it
    # is supposed to be distinguishable from ("·◆☩"): two objects, one blob,
    # at the exact moment the player has to classify them.
    #
    # Three shapes, because one generic set-based pass would cost ~9 lookups
    # per drawn cell and a near shard is 300 cells:
    #   halo_box    — a solid rectangle (single cells, pods, gate chunks)
    #   halo_spans  — a CONVEX FILLED silhouette given as inclusive row runs
    #                 (rocks, coin rings); O(perimeter), never writes on us
    #   halo_cells  — the contract's reference implementation, for sparse
    #                 silhouettes small enough not to care (the alien)
    HALO_EPS = 0.001

    def halo_box(self, y0, y1, x0, x1, z):
        fr = self.frame
        zz = z - self.HALO_EPS
        for x in range(x0 - 1, x1 + 2):
            fr.put(y0 - 1, x, " ", 0, zz)
            fr.put(y1 + 1, x, " ", 0, zz)
        for y in range(y0, y1 + 1):
            fr.put(y, x0 - 1, " ", 0, zz)
            fr.put(y, x1 + 1, " ", 0, zz)

    def halo_spans(self, spans, z):
        if not spans:
            return
        fr = self.frame
        zz = z - self.HALO_EPS
        by = {}
        for (y, a, b) in spans:
            p = by.get(y)
            if p is None:
                by[y] = [a, b]
            else:
                if a < p[0]:
                    p[0] = a
                if b > p[1]:
                    p[1] = b
        for y in by:
            a, b = by[y]
            fr.put(y, a - 1, " ", 0, zz)
            fr.put(y, b + 1, " ", 0, zz)
            for yy in (y - 1, y + 1):
                n = by.get(yy)
                if n is None:
                    for x in range(a - 1, b + 2):
                        fr.put(yy, x, " ", 0, zz)
                else:
                    # the neighbour row is ours too: only reserve the cells it
                    # does not already cover, so we never blank our own body
                    for x in range(a - 1, n[0]):
                        fr.put(yy, x, " ", 0, zz)
                    for x in range(n[1] + 1, b + 2):
                        fr.put(yy, x, " ", 0, zz)

    def halo_cells(self, cells, z):
        fr = self.frame
        own = cells if isinstance(cells, set) else set(cells)
        zz = z - self.HALO_EPS
        for (y, x) in own:
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    p = (y + dy, x + dx)
                    if p not in own:
                        fr.put(p[0], p[1], " ", 0, zz)

    def draw_alien(self, o, depth, scx, scy, tier, cheap):
        """Alien hull or artifact — the SHOOTABLE class. TARGET cyan, and cyan
        means exactly one thing in this game: destroy it. It also keeps a hard
        geometric silhouette (a cross / a hex, both with negative space) at
        every distance, so 'shootable' is legible before the shape is — a
        DIFFERENT SILHOUETTE CLASS, never a different hue of the same blob."""
        fr = self.frame
        attr = self.pal.target[tier]        # TARGET, never PICKUP (they used to
                                            # share one list — that is exactly
                                            # why "what do I shoot" was unclear)
        R = o[O_R]
        sc = R * self.focal_x / depth
        art = bool(o[O_FLG] & F_ARTIFACT)
        ch = ARTIFACT_CH if art else ALIEN_CH
        y0, x0 = int(round(scy)), int(round(scx))
        if sc < 0.75 or cheap:
            fr.put(y0, x0, ch, attr, depth)
            self.halo_box(y0, y0, x0, x0, depth)
            return
        if depth < 2.6:
            return
        rx = int(min(6.0, sc))
        fr.put(y0, x0, ch, attr, depth)
        cells = [(y0, x0)]
        # wings: a horizontal bar makes a CRAFT read as a craft, not a pebble
        for i in range(1, rx + 1):
            fr.put(y0, x0 - i, WING_L if i < rx else ch, attr, depth + 0.01)
            fr.put(y0, x0 + i, WING_R if i < rx else ch, attr, depth + 0.01)
            cells.append((y0, x0 - i))
            cells.append((y0, x0 + i))
        if rx >= 2:
            fr.put(y0 - 1, x0, ch, attr, depth + 0.02)
            cells.append((y0 - 1, x0))
        self.halo_cells(cells, depth)

    def draw_rock(self, o, depth, scx, scy, tier, cheap):
        fr = self.frame
        attr = self.pal.rock[tier]
        R = o[O_R]
        sc = R * self.focal_x / depth
        # THREE clean states, never stroke soup. Far away the SHAPE is already
        # the telegraph — RAMP[1], a mass, never the star's bare dot — and the
        # colour only sharpens it.
        if sc < 0.75 or cheap:
            fr.put(int(round(scy)), int(round(scx)), RAMP[1], attr, depth)
            self.halo_box(int(round(scy)), int(round(scy)),
                          int(round(scx)), int(round(scx)), depth)
            return
        if depth < 2.6:
            return
        rx = min(12.0, sc)
        ry = min(float(self.pf_h), o[O_RY] * self.focal_y / depth)
        if ry < 0.5:
            ry = 0.5
        fog = max(0.15, 1.0 - (depth - self.zoff) / 46.0)
        y0, y1 = int(math.floor(scy - ry)), int(math.ceil(scy + ry))
        spans = []
        for y in range(y0, y1 + 1):
            v = (y + 0.5 - scy) / ry
            vv = v * v
            if vv > 1.0:
                continue
            # solve the span analytically instead of testing rejected cells —
            # a near shard is otherwise a four-figure put() count per frame
            du = math.sqrt(1.0 - vv)
            x0 = int(math.floor(scx - du * rx))
            x1 = int(math.ceil(scx + du * rx))
            spans.append((y, x0, x1))
            for x in range(x0, x1 + 1):
                u = (x + 0.5 - scx) / rx
                q = u * u + vv
                if q > 1.0:
                    continue
                nz = math.sqrt(1.0 - q)
                # per-cell surface normal: the difference between a circle and a
                # ROCK, and it makes two overlapping rocks interpenetrate right
                lam = u * -0.55 + v * -0.60 + nz * 0.58
                if lam < 0.0:
                    lam = 0.0
                rim = (1.0 - nz) * (1.0 - nz) * 0.34
                sh = 0.12 + 0.82 * lam + rim
                sh = (1.0 if sh > 1.0 else sh) * fog
                # RAMP, ALWAYS. The floor of 1 keeps a fogged near-rock off the
                # bare "·" that belongs to the stars; the ceiling is the ramp's
                # own top. A hazard never leaves the shading family.
                ch = RAMP[max(1, int(sh * 4))]
                # z = depth - R*nz: two overlapping rocks interpenetrate
                # correctly at their silhouettes, for one extra term
                fr.put(y, x, ch, attr, depth - R * nz)
        self.halo_spans(spans, depth)

    def draw_coin(self, o, depth, scx, scy, tier, cheap):
        fr = self.frame
        attr = self.pal.pickup[tier]        # PICKUP green: collect, never shoot
        R = o[O_R]
        sc = R * self.focal_x / depth
        if sc < 0.75 or cheap:
            # COIN_FAR, not DOT: a far coin used to be the same bare cell as a
            # far rock and as a star. One cell, one meaning.
            fr.put(int(round(scy)), int(round(scx)), COIN_FAR, attr, depth)
            self.halo_box(int(round(scy)), int(round(scy)),
                          int(round(scx)), int(round(scx)), depth)
            return
        # STOP DRAWING AT 3.4 UNITS, same discipline as the rock's 2.6. Coins
        # magnetise toward the camera over the last 8 units, so a 5-coin arc
        # arrives as a cluster: without this cut-off three near rings overlap
        # into one green mass that owns half the windshield on the exact frame
        # the player needs to read the next hazard. A pickup is never allowed
        # to grow a body — it stays a small isolated shape and then it is gone.
        if depth < 3.4:
            return
        rx = min(4.0, sc)
        ry = max(0.5, min(2.5, o[O_RY] * self.focal_y / depth))
        spans = []
        for y in range(int(math.floor(scy - ry)), int(math.ceil(scy + ry)) + 1):
            v = (y + 0.5 - scy) / ry
            vv = v * v
            if vv > 1.0:
                continue
            du = math.sqrt(1.0 - vv)
            x0 = int(math.floor(scx - du * rx))
            x1 = int(math.ceil(scx + du * rx))
            spans.append((y, x0, x1))
            for x in range(x0, x1 + 1):
                u = (x + 0.5 - scx) / rx
                q = u * u + vv
                if q > 1.0:
                    continue
                if sc > 2.2 and q < 0.30:                  # a ring up close
                    # THE HOLE IS OURS. A hazard parked inside the ring was the
                    # "◈◈◆   ◈◈" frame — a rock rendered strictly inside a coin.
                    fr.put(y, x, " ", 0, depth - self.HALO_EPS)
                    continue
                fr.put(y, x, COIN_CH, attr, depth)
        self.halo_spans(spans, depth)

    def draw_gate_block(self, o, depth, scx, scy, tier, cheap):
        fr = self.frame
        # THE RESOLVE: rock-coloured debris out beyond 28, unmistakable gold
        # inside it. Gold has been reserved for nothing else all game.
        # The far chunks wear MIDBLOCK, a HAZARD-ramp glyph, and that is the
        # ONE deliberate exception to the shape law in this file — because out
        # there the gate genuinely IS a hazard: it is solid, it will cost you a
        # shield, and it should read as a wall until you find the hole in it.
        # The glyph is telling the truth, which is the law's actual purpose.
        wd = depth - self.zoff                 # distance from the SHIP
        attr = self.pal.gold[tier] if wd < 28.0 else self.pal.rock[tier]
        hw = GATE_HX * self.focal_x / depth
        hh = self.gate_hy * self.focal_y / depth
        if hw < 0.55 or cheap:
            # RAMP[1] and not DOT: out here the gate reads as a wall, and the
            # bare dot belongs to the stars.
            fr.put(int(round(scy)), int(round(scx)),
                   MIDBLOCK if wd < 34 else RAMP[1], attr, depth)
            return
        ch = BLOCK if wd < 20.0 else MIDBLOCK
        hw = min(hw, 10.0)
        hh = max(0.5, min(5.0, hh))
        for y in range(int(math.floor(scy - hh)), int(math.ceil(scy + hh))):
            for x in range(int(math.floor(scx - hw)), int(math.ceil(scx + hw))):
                fr.put(y, x, ch, attr, depth)

    def draw_eye(self, o, depth):
        """THE EGG, MADE FINDABLE. We promised this publicly, so an invisible
        secret is not good enough: the hole in the 4 is a real flight window
        and it has to be seen to be flown.

        Three layers, none of them a banner:
          - out past 20 units it is a quiet gold shimmer inside the hole,
            reading as 'something is in there' rather than as an instruction;
          - inside 20 units two gold TICKS ‹ › bracket it, which is the same
            language the reticle already speaks, so it reads as a target;
          - and the first time you clip the wall instead, the whisper says
            'there is a hole in the 4' out loud.
        Threading it pays 747, 7.47 s of afterburner, a shield and full ammo."""
        wd = depth - self.zoff
        if wd > 20.0 or depth <= NEAR:
            return
        fr = self.frame
        scx, scy = self.project(o[O_X], o[O_Y], depth)
        y, x = int(round(scy)), int(round(scx))
        t = (time.time() * 6.0) % 2.0
        attr = self.pal.gold[0] if t < 1.0 else self.pal.gold[2]
        fr.put(y, x, DOT, attr, depth - 0.05)
        if wd < 20.0:
            # the ticks sit OUTSIDE the eye's own half-width, so they frame the
            # window instead of blocking the line through it
            off = max(1, int(round(EYE_HX * self.focal_x / depth)) + 1)
            fr.put(y, x - off, RET_L, self.pal.gold[0], depth - 0.06)
            fr.put(y, x + off, RET_R, self.pal.gold[0], depth - 0.06)

    def draw_fx(self):
        fr = self.frame
        pal = self.pal
        for e in self.fx:
            depth = e[4] - self.rz
            if depth <= NEAR:
                continue
            scx, scy = self.project(e[2], e[3], depth)
            if e[1] == "ring" or e[1] == "kring":
                # the ring is painted in the ROLE it came from: a collect ring
                # is green, a kill ring is cyan. Feedback in the wrong hue is
                # feedback that teaches the wrong thing.
                ratt = pal.target[0] if e[1] == "kring" else pal.pickup[0]
                r = (0.25 - e[0]) / 0.25
                rad = 1.0 + r * 3.0
                for a in range(0, 8):
                    th = a * 0.785
                    fr.put(int(round(scy - r * 2.0 + math.sin(th) * rad * 0.5)),
                           int(round(scx + math.cos(th) * rad)),
                           DOT, ratt, depth - 0.1)
            else:
                # shatter debris is HAZARD-derived, so it wears the hazard ramp.
                # A red "·" would have been a star wearing a threat's colour.
                fr.put(int(round(scy)), int(round(scx)), RAMP[1], pal.rock[0],
                       depth - 0.1)
        if self.tracer is not None:
            t, xw, yw, zw = self.tracer
            depth = max(NEAR + 0.5, zw - self.rz)
            scx, scy = self.project(xw, yw, depth)
            # the tracer leaves the GUN, wherever the gun happens to be on
            # screen — under a chase camera that is the car, not the hood line
            bx, by = self.ship_sx, self.ship_sy
            for i in range(1, 7):
                f = i / 7.0
                fr.put(int(round(by + (scy - by) * f)),
                       int(round(bx + (scx - bx) * f)),
                       ARROW, self.pal.gold[min(3, i // 2)], -5.5)

    def draw_ship(self):
        """The ship and the reticle. The hood and the chase car are MUTUALLY
        EXCLUSIVE: seeing your own windshield AND a car in front of you is not
        a camera mode, it is a bug, and it is what the old cosmetic decal did."""
        fr = self.frame
        pal = self.pal
        if self.chase_k >= 0.5:
            self.draw_chase_ship()
        else:
            b = self.pf_top + self.pf_h - 1
            if self.pf_h >= 4:
                half = max(3, int(self.w * 0.17))
                cxn = self.w // 2
                roll = self.roll
                # --- THE BONNET. One solid mass; its TOP EDGE is positioned to
                #     1/8 of a row via the partial-height ramp. At roll 0.0 it
                #     is exactly one flat row of █ — dead level, no ghost edge.
                #     MEASURED at w=100: 45 distinct silhouettes across the
                #     roll range, where the old strut lean had 2 (and spent 22
                #     of its 24 frames on the same one). The frame never
                #     TRANSLATES; only its attitude changes, because your head
                #     is bolted to it.
                for i in range(-half, half + 1):
                    t = i / float(half)                     # -1 .. +1 across it
                    hgt = clamp(1.0 + roll * t, 0.15, 3.0)  # rows of bonnet
                    full = int(hgt)
                    a = pal.dash_hi if abs(i) <= 1 else pal.dash
                    for r in range(full):
                        fr.put(b - r, cxn + i, BLOCK, a, -5.0)
                    k = int((hgt - full) * 8.0)
                    if k > 0:
                        fr.put(b - full, cxn + i, HOOD8[k - 1], a, -5.0)
                if self.pf_h >= 6:
                    fr.put(b - 1, cxn, NOSE, pal.dash_hi, -5.0)
                # --- A-PILLARS. Fixed columns, fixed rows, forever. A canopy is
                #     bolted to your skull; it cannot slide across your eye.
                #     The roll is read here by LIGHT — the inboard pillar
                #     catches the sun. A brightness step never reads as a
                #     position error, which is the entire point.
                if self.pf_h >= 7:
                    px = half + 2
                    ptop = max(self.pf_top + 1, b - 4)
                    for yy in range(ptop, b):
                        for sgn, ch in ((-1.0, PILLAR_L), (1.0, PILLAR_R)):
                            q = roll * sgn
                            at = (pal.struct[0] if q > 0.55 else
                                  pal.struct[1] if q > 0.20 else pal.struct[2])
                            fr.put(yy, cxn + int(sgn * px), ch, at, -5.0)
                # --- thrusters: they burn brighter with speed, and go gold
                #     under afterburner, so the car reacts to its own throttle
                eatt = pal.gold[0] if self.after > 0.0 else (
                    pal.dash_hi if self.speed_scale > 0.85 else pal.dash)
                fr.put(b, cxn - half - 1, ENGINE, eatt, -5.0)
                fr.put(b, cxn + half + 1, ENGINE, eatt, -5.0)
        blink = self.invuln > 0.0 and int(time.time() * 6.0) % 2 == 0
        if not blink:
            # LOCK: is a shootable alien actually in the firing line right now?
            # Same test fire() uses, so the reticle can never lie about it.
            lock = False
            for o in self.objs:
                if o[O_KIND] != K_ALIEN or (o[O_FLG] & F_DEAD):
                    continue
                d = o[O_Z] - self.z_cam
                if d <= HIT_Z or d > 44.0:
                    continue
                if abs(o[O_X] - self.cam_x) < 2.2 and abs(o[O_Y] - self.cam_y) < 1.3:
                    lock = True
                    break
            attr = pal.gold[0] if self.after > 0.0 else (
                pal.target[0] if lock else pal.dash_hi)
            # the reticle marks the AIM LINE, which is the ship's world x/y
            # projected 22 units ahead of it. At chase_k == 0 that is exactly
            # (vcx, vcy) — the POV reticle never moves.
            rx, ry = self.project(self.cam_x, self.cam_y, self.zoff + 22.0)
            # THE ONE SURVIVING PIECE OF THE STEERING CHANNEL: a 1-COLUMN lean
            # on the reticle alone, driven off the SAME roll float as the
            # bonnet and the pillars. One 1-cell element on one signal cannot
            # disagree with anything — which is precisely why the yaw ribbon
            # was dropped: a second independently-quantised motion channel
            # inside the frame is the original swim, rebuilt in a new costume.
            rx += clamp(int(round(self.roll * 1.1)), -1, 1)
            rx, ry = int(round(rx)), int(round(ry))
            off = 1 if lock else 2      # the reticle CLOSES on a valid target
            fr.put(ry, rx - off, RET_L, attr, -5.0)
            fr.put(ry, rx + off, RET_R, attr, -5.0)
            if lock and self.ammo > 0:
                fr.put(ry, rx, NOSE if not UTF else "◦", attr, -5.0)

    def draw_chase_ship(self):
        """The car, drawn as an ORDINARY WORLD OBJECT at the ship's real
        position and at the real camera distance. That is the whole difference
        between a chase camera and a decal: it translates when you steer, it
        scales with the camera, and the z-buffer lets the world pass in front
        of it."""
        fr = self.frame
        pal = self.pal
        d = self.ship_d
        sx, sy = self.ship_sx, self.ship_sy
        attr = pal.gold[0] if self.after > 0.0 else pal.dash_hi
        hw = clamp(1.55 * self.focal_x / d, 2.0, 16.0)
        rows = 3 if self.pf_h >= 14 else 2
        # the tail slides on a hard turn — off self.roll, NEVER off vx. The
        # chase camera must tilt on the same signal as the cockpit or the two
        # modes disagree about what a turn feels like.
        bank = self.roll * 0.85
        # the NOSE sits on the projected ship point (which is also the collider,
        # so what you aim is what you hit) and the body widens DOWNWARD toward
        # the camera — the tail is nearer, so it is bigger and lower in frame
        for i in range(rows):
            # a blunt nose taper, not a spike: at 3 rows a linear ramp gives
            # 5/7/11 cells, which reads as a triangle rather than a car
            f = 0.42 + 0.58 * ((i + 1.0) / rows)
            half = max(1, int(round(hw * f)))
            x0 = sx - half + int(round(bank * i))
            for x in range(x0, x0 + 2 * half + 1):
                fr.put(sy + i, x, BLOCK, attr, d - 0.02 * i)
        # the canopy: one dim cell that tells you which end is the front
        fr.put(sy, sx, MIDBLOCK, pal.dash, d - 0.05)

    def _put(self, y, x, s, attr, z=-10.0):
        """Write a string into the frame one cell at a time, clipped. The HUD
        is composited like everything else so it can never be overdrawn."""
        w = self.w
        for i, c in enumerate(s):
            if 0 <= x + i < w:
                self.frame.put(y, x + i, c, attr, z)

    def draw_hud(self, playing):
        self.draw_bar()
        self.draw_footer()
        fr = self.frame
        pal = self.pal
        if self.muzzle > 0:                     # the flash IS the shot's impact
            mx = self.w // 2
            fr.put(self.pf_top + self.pf_h - 1, max(0, mx - 4), MUZ_L,
                   pal.gold[0], -6.0)
            fr.put(self.pf_top + self.pf_h - 1, min(self.w - 1, mx + 4), MUZ_R,
                   pal.gold[0], -6.0)

    def draw_bar(self):
        """ROW 0 — THE BAR, and the answer to 'how do I win'.

        ▌SKYRUN     ·   4,120 ▰▰▱  S 3/7  ▸▸▸▸▸▹▹▹  ×3     ←→ · [space] fire

        Fixed columns, so the eye lands in the same place in every title of the
        line. Two rules with teeth:
          - the primary integer is %7d in a FIXED field. A score whose digits
            reflow as it grows makes the whole bar shudder on every 10x and the
            eye has to re-find it. Never str(score), never centred.
          - shields are ICONIC, never numeric. Three pips subitise in <200 ms;
            the numeral '3' costs a fixation and a decode.
        """
        fr = self.frame
        pal = self.pal
        w = self.w
        y = self.bar_row
        rev = curses.A_REVERSE
        hi = rev | curses.A_BOLD
        for x in range(w):                     # opaque strip (bleed-kill)
            fr.put(y, x, " ", rev, -9.0)
        # the degradation ladder. The primary integer and the shields are the
        # last two things standing, in every title.
        wide = w >= 78
        med = w >= 62
        short = w < 46
        fr.put(y, 0, TICK, pal.accent[0] | rev, -10.0)
        # the ladder: >=78 full bar · 62-77 drop the keys · 46-61 title goes to
        # 3 chars but the primary integer NEVER leaves col 14 · <46 integer and
        # shields only. The number the player is chasing is the last thing cut.
        name = "SKYRUN" if w >= 62 else "SKY"
        self._put(y, 2, name, hi)
        if not short:
            self._put(y, 12, txt("·"), rev)
        self._put(y, 14, "%7d" % int(self.score_shown), hi)
        sh = SHIELD_F * self.shields + SHIELD_E * (3 - self.shields)
        self._put(y, 22, sh, hi)
        if short:
            return
        self._put(y, 28, self.sector_label(), rev)
        # THE SECTOR BAR. This is the win condition, drawn.
        t = self.sector_t()
        if self.h < 10 or w < 62:
            self._put(y, 35, "%3d%%" % int(t * 100.0), rev)
        else:
            n = 8
            k = int(t * n + 0.0001)
            bar = BAR_F * min(n, k) + BAR_E * max(0, n - k)
            self._put(y, 35, bar, pal.target[1] | rev)
            self._put(y, 35 + n + 1, "%3d%%" % int(t * 100.0), rev)
        m = self.mult()
        if med:
            att = (pal.gold[1] | hi) if m > 1 else rev
            self._put(y, 50, txt("×%d" % m), att)
        if wide:
            keys = txt(" ←→ · [space] fire · [q] quit ")
            if 56 + len(keys) <= w:
                self._put(y, w - len(keys), keys, rev)

    def draw_footer(self):
        """ROW h-1 — THE FOOTER. Telemetry left, the whisper in the middle,
        `THE 747 LAB` right and unmoved. That tag is the only branding on
        screen and it never gives up its columns."""
        fr = self.frame
        pal = self.pal
        w = self.w
        y = self.dash_row
        for x in range(w):
            fr.put(y, x, " ", pal.dash, -9.0)
        # BELT AND BRACES: a teach line runs for the first 15 seconds only and
        # then the telemetry takes the space back. Second channel, never the
        # primary one — the level design is what actually teaches this game.
        if self.seconds < 15.0 and w >= 62:
            # NAME THE SHAPES, NOT THE HUES. This line used to read
            # "shoot cyan · dodge red" and rendered verbatim on a mono or
            # 16-colour-less pane where neither colour exists — teaching the
            # one distinction the game is built on in a vocabulary the screen
            # was not speaking. Built from the LIVE glyph constants, so it
            # self-corrects in UTF-8 and in ASCII and can never drift out of
            # sync with what is actually being drawn.
            teach = txt("←→ move · [space] shoot %s · dodge %s · grab %s"
                        % (ALIEN_CH, RAMP[4], COIN_CH))
            fade = pal.dash if self.seconds > 11.0 else pal.dash_hi
            self._put(y, 1, teach, fade)
        else:
            mach = self.raw_speed() / V_CAP * 7.47
            am = AMMO_F * self.ammo + AMMO_E * (6 - self.ammo)
            aatt = pal.gold[0] if self.ammo_pulse > 0.0 else pal.dash_hi
            if self.after > 0.0:
                self._put(y, 1, txt("%s AFTERBURNER ×7  %.2f" %
                                    (ARROW, max(0.0, self.after))), pal.gold[0])
            elif w < 62:
                self._put(y, 1, "%dm" % int(self.dist), pal.dash_hi)
            else:
                self._put(y, 1, "MACH %.2f" % mach, pal.dash_hi)
                self._put(y, 12, am, aatt)
                self._put(y, 20, "%s m" % fmt_int(int(self.dist)), pal.dash_hi)
        tag = "THE 747 LAB "
        if w >= 40:
            self._put(y, w - len(tag), tag, pal.dash)
        # the whisper lives HERE, never over the playfield — a scoring readout
        # that covers the sky is a scoring readout that gets you hit.
        if self.msg_t > 0.0 and self.msg and self.h >= 10:
            m = txt(self.msg)
            x0 = max(34, w - len(tag) - len(m) - 2)
            if x0 + len(m) < w - len(tag):
                attr = pal.gold[0] if self.gold_t > 0.0 else pal.whisper
                self._put(y, x0, m, attr, -10.5)

    def draw_pause(self):
        fr = self.frame
        lines = [txt("%s  CLAUDE'S DONE %s READING TIME" % (PAUSE_CH, DASHCH)),
                 txt("your run is held · resumes on your next prompt · [p] keep flying")]
        if self.narrow:
            lines = [txt("%s  CLAUDE'S DONE" % PAUSE_CH), "[p] keep flying"]
        for i, ln in enumerate(lines):
            row = self.pf_top + self.pf_h // 2 - 1 + i
            x0 = max(0, (self.w - len(ln)) // 2)
            for x in range(max(0, x0 - 1), min(self.w, x0 + len(ln) + 1)):
                fr.put(row, x, " ", self.pal.dash, -8.5)
            for j, c in enumerate(ln[:self.w]):
                fr.put(row, x0 + j, c, self.pal.dash_hi, -9.5)

    def render_tiny(self):
        """A banished pane can be any size. Never crash, never exit — the pane
        must still be sitting there honouring `end` when the session closes.
        The floor is 40x8; the full HUD ladder holds down to 40 columns."""
        scr = self.scr
        scr.erase()
        msg = txt("SKYRUN — TERMINAL TOO SMALL")
        try:
            scr.addstr(max(0, self.h // 2), max(0, (self.w - len(msg)) // 2),
                       msg[: max(0, self.w - 1)], curses.A_DIM)
        except curses.error:
            pass
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
                dt = 0.0                      # do not simulate a rejoin frame
            dt = min(dt, DT_MAX)

            h, w = scr.getmaxyx()             # EVERY frame: a live ghost cycle
            if (h, w) != (self.h, self.w):    # resizes the pane 3+ times
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
                if not self.idle_drawn:
                    self.pal.set_mode("dim")
                    if self.tiny:
                        self.render_tiny()
                    else:
                        self.render(False)
                    self.idle_drawn = True
                time.sleep(POLL_IDLE)         # ~0 bytes on the wire while ghosted
                continue
            self.idle_drawn = False

            if self.tiny:
                self.render_tiny()
                time.sleep(0.25)
                continue

            if rejoin:
                self.on_rejoin()

            t0 = time.monotonic()
            over = self.step(dt)
            self.render(True)
            spent = time.monotonic() - t0
            # auto-throttle: protects every slow-terminal and long-link user.
            # Never promotes back.
            if not self.throttled:
                if spent > 0.012:
                    self.slow_run += 1
                    if self.slow_run >= 10:
                        self.throttled = True
                        self.frame_dt = 0.05
                        self.stars = self.stars[: max(6, len(self.stars) // 2)]
                else:
                    self.slow_run = 0
            if over:
                return "over"
            if self.won:
                # 7/7. A real finish, with a real screen. run() is re-entrant:
                # main() may hand the same Sky back after the victory screen
                # with begin_overrun() set, and the flight continues.
                self.won = False
                return "win"

            next_t += self.frame_dt
            if next_t < now:
                next_t = now + self.frame_dt
            time.sleep(max(0.0, next_t - time.monotonic()))


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
def title_flyby(scr, pal, session=""):
    """A 2.47 s cold open: you fly at a debris wall in the void and it resolves
    into 747, in gold, a heartbeat before you reach it. Any key skips. Tiny
    panes skip entirely (reliable-or-silent). Only ever runs behind --ask."""
    h, w = scr.getmaxyx()
    if h < 10 or w < 46:
        return
    fr = Frame()
    fr.resize(h, w)
    rng = random.Random(747)
    stars = []
    for _ in range(20):
        th = rng.uniform(0, 2 * math.pi)
        bt = 2 if rng.random() < 0.62 else (1 if rng.random() < 0.6 else 0)
        stars.append((math.cos(th), math.sin(th) * 0.5, rng.random(),
                      0.6 + rng.random() * 0.9, bt))
    rows = gate_rows()
    focal_x = clamp(26.0 * w / 100.0, 14.0, 40.0)
    focal_y = focal_x * 0.5
    vcx, vcy = w // 2, h // 2
    ZW, T = 38.0, 2.47
    scr.nodelay(True)
    t0 = time.time()
    n = 0
    while True:
        now = time.time() - t0
        if scr.getch() != -1:
            break
        if now >= T:
            break
        n += 1
        if n % 25 == 0 and read_state(session) == "end":
            break
        u = min(1.0, now / T)
        e = u * u * u * (u * (u * 6 - 15) + 10)          # smootherstep
        z_cam = (ZW - 4.5) * e
        depth = ZW - z_cam
        fr.clear()
        for ux, uy, base, spd, bt in stars:
            rr = (base + z_cam * 0.030 * spd) % 1.0
            if rr < 0.08 or rr > 0.92:
                continue
            rr2 = rr * rr
            tier = 2 if (rr < 0.2 or rr > 0.82) else bt
            fr.put(int(round(vcy + uy * rr2 * (h * 1.15))),
                   int(round(vcx + ux * rr2 * (w * 0.62))),
                   DOT, pal.star[tier], 90)
        tier = depth_tier(depth)
        attr = pal.gold[tier] if depth < 28.0 else pal.rock[tier]
        hw = GATE_HX * focal_x / depth
        hh = 1.05 * 0.40 * focal_y / depth
        for r in range(5):
            line = rows[r]
            for c in range(11):
                if line[c] != "#":
                    continue
                scx = vcx + ((c - 5.0) * GATE_CW) * focal_x / depth
                scy = vcy + ((r - 2.0) * 1.05) * focal_y / depth
                if hw < 0.55:
                    fr.put(int(round(scy)), int(round(scx)), DOT, attr, depth)
                else:
                    for y in range(int(math.floor(scy - hh)),
                                   int(math.ceil(scy + hh))):
                        for x in range(int(math.floor(scx - hw)),
                                       int(math.ceil(scx + hw))):
                            fr.put(y, x, BLOCK if depth < 20 else MIDBLOCK,
                                   attr, depth)
        if now > 1.1:
            # the display name carries NO numeral. The 747 is IN the game —
            # it is the wall you are flying at right now — never on the label.
            sub = "S K Y R U N"
            x0 = max(0, (w - len(sub)) // 2)
            a = pal.dash_hi if now > 1.5 else pal.dash
            for k, c in enumerate(sub):
                fr.put(min(h - 2, vcy + 4), x0 + k, c, a, -12.0)
        fr.blit(scr)
        time.sleep(0.02)
    scr.nodelay(False)


def ask_screen(scr, session):
    """Returns True to play. Handles n (this session), a (always), o (off)."""
    scr.nodelay(False)
    scr.timeout(1000)
    start = time.time()
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [
            "FLY SKYRUN WHILE CLAUDE THINKS?",
            "",
            "seven sectors, one delivery run  ·  about three minutes",
            "",
            "[y] yes   [n] not now   [a] always auto-open   [o] never ask again",
        ]
        for i, ln in enumerate(lines):
            try:
                scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln,
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


# THE SCORING TABLE. Printed on the game-over screen, so the scoring is
# LEARNABLE rather than folklore — the same list the whispers narrate live.
SCORE_TABLE = [
    "coin 10   ·   clean dodge 5   ·   thread a rock 12",
    "alien 40   ·   artifact 40 + a shell   ·   gate seam 100",
    "the eye of the 4 = 747   ·   sector clear 200 × sector",
    "multiplier ×1-×5 on your chain, ×7 under afterburner",
]


def _screen(scr, session, build, keys):
    """Shared blocking screen. `build(h, w)` is re-run every frame, so a pane
    that is resized (or banished and rejoined at a different size) re-lays out
    instead of overflowing — a screen composed once at launch is a screen that
    prints through its own brand tag at 40 columns.

    Every blocking screen in this file polls for session end. A closing session
    that hangs a pane on a menu nobody can see is the worst failure this plugin
    has, because the pane outlives the thing that created it."""
    scr.nodelay(False)
    scr.timeout(1000)
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = build(h, w)
        top = max(0, h // 2 - len(lines) // 2)
        last = top + len(lines) - 1
        for i, ln in enumerate(lines):
            if top + i >= h:
                break
            try:
                scr.addstr(top + i, max(0, (w - len(ln)) // 2),
                           ln[: max(0, w - 1)],
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        # the tag only takes row h-1 when row h-1 is actually free
        if last < h - 1 and w >= 24:
            try:
                scr.addstr(h - 1, max(0, w - 13), "THE 747 LAB ", curses.A_DIM)
            except curses.error:
                pass
        scr.refresh()
        ch = scr.getch()
        for want, out in keys:
            if ch in want:
                return out
        if read_state(session) == "end":
            return "end"


def victory_screen(scr, sky, stats, session):
    """THE WIN. No previous build of this game had one, which is precisely why
    'how do we win' had no answer. Returns 'continue' / 'again' / 'close'."""
    sh = SHIELD_F * sky.shields + SHIELD_E * (3 - sky.shields)

    def build(h, w):
        lines = [
            txt("RUN COMPLETE · %d/%d · %s" % (SECTORS, SECTORS, sh)),
            "",
            txt("%s m in %s   ·   %s points"
                % (fmt_int(int(sky.dist)), fmt_time(sky.seconds),
                   fmt_int(int(sky.score)))),
        ]
        if sky.shields_lost == 0:
            lines.append("NOT A SCRATCH")
        if sky.eyes:
            lines.append(txt("%d × the eye of the 4" % sky.eyes))
        if int(sky.score) > stats["best_score"]:
            lines.append("NEW BEST")
        if w >= 62:
            lines += ["", txt("[c] keep flying — OVERRUN   ·   "
                              "[r] fly again   ·   [q] close")]
        else:
            lines += ["", txt("[c] OVERRUN · [r] again · [q] close")]
        return lines

    return _screen(scr, session, build, [
        ((ord("c"), ord("C"), ord(" "), 10, 13, curses.KEY_ENTER), "continue"),
        ((ord("r"), ord("R")), "again"),
        ((ord("q"), ord("Q")), "close"),
    ])


def game_over_screen(scr, sky, stats, session):
    """The retention screen. Death to the next attempt is one keypress, and it
    always says HOW SHORT you were — a run you nearly won is the one you replay."""
    dist, score = int(sky.dist), int(sky.score)
    reached = min(sky.sector, SECTORS)
    short = SECTORS * SECTOR_M - dist

    def build(h, w):
        lines = [txt("FLAMED OUT · SECTOR %d/%d · %s m · %s"
                     % (reached, SECTORS, fmt_int(dist), fmt_int(score)))]
        if dist > stats["best_dist"]:
            lines.append("NEW BEST")
        elif stats["best_dist"] - dist > 0:
            lines.append("%s m short of your best"
                         % fmt_int(stats["best_dist"] - dist))
        if short > 0:
            lines.append(txt("%s m short of the full 7/7 run"
                             % fmt_int(int(short))))
        if sky.eyes:
            lines.append(txt("%d × the eye of the 4" % sky.eyes))
        elif sky.gates_seen:
            lines.append(txt("747 · there is a hole in the 4"))
        # the scoring table is the retention payload, but it needs room: on a
        # pane too small to hold it, the KEYS matter more than the lesson
        if w >= 74 and h >= len(lines) + len(SCORE_TABLE) + 5:
            lines += [""] + [txt(t) for t in SCORE_TABLE]
        lines += ["", txt("[r] fly again   ·   [q] close")]
        return lines

    # returns 'again' / 'close' / 'end'. NOT a bool: 'the session ended' and
    # 'the player pressed q' are different exits — only the first one owns the
    # state file, and collapsing them leaked a stale state-<sid> on disk every
    # time a session closed while this screen was up.
    return _screen(scr, session, build, [
        ((ord("r"), ord("R"), ord(" "), 10, 13, curses.KEY_ENTER), "again"),
        ((ord("q"), ord("Q")), "close"),
    ])


# ---------------------------------------------------------------------------
def record_run(stats, sky, seconds):
    stats["runs"] += 1
    stats["total_seconds"] = round(stats["total_seconds"] + seconds, 1)
    stats["best_stage"] = max(stats["best_stage"], min(sky.sector, SECTORS))
    if sky.sector > SECTORS:
        stats["cleared"] = True
    stats["eggs"] += sky.eyes
    stats["best_dist"] = max(stats["best_dist"], int(sky.dist))
    stats["best_score"] = max(stats["best_score"], int(sky.score))
    stats["gates_seen"] += sky.gates_seen
    stats["eyes_threaded"] += sky.eyes
    stats["afterburners"] += sky.eyes
    stats["threads"] += sky.threads
    now = int(time.time())
    if not stats["first_run_ts"]:
        stats["first_run_ts"] = now
    stats["last_run_ts"] = now


def main(stdscr, args):
    # every one of these is optional on a real terminal, and an unguarded
    # start_color() on a mono TERM is a crash, not a fallback
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()                   # start_color FIRST, then defaults
    except (curses.error, ValueError):
        pass
    try:
        curses.use_default_colors()
    except (curses.error, ValueError):
        pass
    for i, col in enumerate([curses.COLOR_RED, curses.COLOR_YELLOW,
                             curses.COLOR_GREEN, curses.COLOR_CYAN,
                             curses.COLOR_MAGENTA], start=1):
        ipair(i, col)
    for i, col in ((6, curses.COLOR_WHITE), (7, curses.COLOR_YELLOW),
                   (8, curses.COLOR_YELLOW)):
        ipair(i, col)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1003h", end="", flush=True)   # mouse motion tracking
    except (curses.error, OSError):
        pass
    pal = Palette()

    stats = load_stats()
    if args.ask:
        title_flyby(stdscr, pal, args.session)
        if not ask_screen(stdscr, args.session):
            # declining is not the same as the session ending: only the second
            # one means this process owns the state file and must clear it
            if read_state(args.session) == "end":
                remove_state(args.session)
            return

    restart = False
    while True:
        sky = Sky(stdscr, args.session, pal, stats)
        sky.manual_play = args.free            # manual launch: fly regardless
        t0 = time.time()
        result = sky.run()
        if result == "win":
            # 7/7. The victory screen is the product: closure inside the wait.
            # OVERRUN hands the SAME run back so the score and the sector count
            # carry on — a "you won, now keep going" that costs nothing.
            choice = victory_screen(stdscr, sky, stats, args.session)
            if choice == "continue":
                sky.begin_overrun()
                result = sky.run()
            elif choice == "again":
                result = "again"
            else:
                result = "quit" if choice == "close" else "end"
        record_run(stats, sky, time.time() - t0)
        if restart:
            stats["restarts"] += 1
        save_stats(stats)
        if result == "end":
            remove_state(args.session)         # the game owns its own state file
            return
        if result == "quit":
            return
        if result != "again":
            out = game_over_screen(stdscr, sky, stats, args.session)
            if out == "end":
                remove_state(args.session)
                return
            if out != "again":
                return
        restart = True


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="SKYRUN — a 7-sector delivery run. By The 747 Lab.")
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    p.add_argument("--export-stats", action="store_true",
                   help="print your local stats file to stdout and exit")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    if args.export_stats:
        # the ONLY export path, and it is a deliberate human action
        print(json.dumps(load_stats(), indent=2, sort_keys=True))
        raise SystemExit(0)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    # nl_langinfo(CODESET) is the only honest answer here: on macOS
    # getpreferredencoding() reports UTF-8 even under LC_ALL=C, and a mojibake
    # windshield is worse than a low-res one.
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
    # Force the mono/ASCII path so the MONO TEST is runnable in CI and by hand
    # on a terminal that would otherwise report UTF-8. Both spellings are
    # honoured on purpose: `747_ASCII` is the name in the build contract, but
    # no POSIX shell can set it with `VAR=1 cmd` because an identifier may not
    # begin with a digit — you need `env 747_ASCII=1 ...`. LAB747_ASCII is the
    # one a human can actually type, and neither is more official than the other.
    if ("utf" not in enc.lower()
            or os.environ.get("747_ASCII") == "1"
            or os.environ.get("LAB747_ASCII") == "1"):
        use_ascii()
    set_pane_title(args.session)
    try:
        curses.wrapper(main, args)
    finally:
        print("\033[?1003l", end="", flush=True)
