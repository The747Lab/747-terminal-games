#!/usr/bin/env python3
"""JAYWALK — in-terminal arcade crossing game that runs while Claude thinks.

Cross the road. Ride the river. Fill all seven bays. Don't die.

THE THESIS (Founder, 2026-07-27): "bring arcade back to life through Claude —
those games are minimal but still hit hard." Our own playtest record is the
evidence: the titles he liked were the two we did NOT invent (Breakout,
Space Invaders); the two we designed from scratch drew "ehh" seven times over.
The arcade canon hands us forty years of proven, playtested mechanics for free,
so every ounce of craft goes into EXECUTION instead of into inventing a loop
that then has to be explained.

And it retires the clarity problem for good: nobody in the history of games has
asked "what do I do?" in Frogger. Cross the road. That reads in one second, in
any language, at any resolution — these games were built under constraints far
tighter than a 22-row pane, which is exactly why they translate to text so well.
Minimal by necessity is legible by construction.

Lives in a tmux pane split below the Claude Code session. Auto-pauses when
Claude finishes a turn (Stop hook writes 'idle'), resumes on the next prompt
(UserPromptSubmit writes 'thinking'). Exits when the session ends.

Developed by The 747 Lab.
"""
import argparse
import curses
import json
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
TICK = 0.033          # ~30 fps
STATE_POLL = 0.2      # how often to re-read the state file while playing
POLL_IDLE = 0.15      # ...and while ghosted. Must stay < 0.25 or session end hangs a pane.
IDLE_SLEEP = 0.05
DT_MAX = 0.05
DT_REJOIN = 0.35      # a gap longer than this is a ghost-pane return, not a slow frame

MIN_W, MIN_H = 40, 8

BAYS = 7              # seven home bays. The first 7 of 7-4-7.
ROAD_LANES = 4        # four lanes of traffic. The 4.
RIVER_LANES = 3       # ...and the river completes the second 7 (4 + 3).
START_LIVES = 3
STEP_COOLDOWN = 0.07  # hop rate limit: a held key must not teleport you
BAY_POP = 0.42        # landing celebration: long enough to SEE, short enough to
                      # never delay the next hop. 0.1s reads as a render glitch.


# ---------------------------------------------------------------------------
# COLOUR CAPABILITY, GUARDED. On a terminal where start_color() failed,
# curses.COLOR_PAIRS is 0 and CPython 3.10+ raises ValueError — NOT curses.error
# — out of both init_pair() and color_pair(). A guard that catches only
# curses.error is a crash on 3.10-3.14 and a pass on 3.9. Catch both, always:
# a missing colour capability costs colour, never the game.
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
        pass


# ---- glyphs. One shape family per role, never shared. ----------------------
UTF = True
# YOU ARE THE THINKING STAR. Founder call: the player should be the little
# asterisk Claude Code shows while it thinks, morphing between forms, in Claude's
# own orange. The thing that appears while it thinks is the thing crossing the
# road while it thinks — the theme closes on itself. It is the ONE warm ember on
# a cold board: everything else is a cool wash, so the eye finds you in one frame.
STAR_FRAMES = ["✳", "✶", "✷", "✸"]
STAR_RATE = 0.13                 # the real spinner's cadence, near enough
FROG, FROG_DEAD = STAR_FRAMES[0], "✻"
# the player, with presence: wings either side of the morphing star
WING_L, WING_R = "▌", "▐"   # half-blocks: they FILL, so the ember reads big
# HAZARD family — thin sportscar vs tall truck, nosed so DIRECTION reads on a mono
# terminal where colour is gone. Disjoint from every other role's glyphs.
CAR_BODY, TRUCK_BODY = "▬", "█"
NOSE_R, NOSE_L = "▶", "◀"
# RIDE family — a log floats HALF-SUBMERGED: lower-half block is wood, the top of
# the cell shows the water wash through it = a real waterline, depth for free.
LOG_BODY, LOG_END = "▄", "▄"
# SURFACE TEXTURE — sparse glints on a background WASH, never a full glyph field.
GLINT_A, GLINT_B = "·", "~"      # dim ripple · rare bright crest
DASH = "╌"                       # faint road lane mark
GRASS = "▒"                      # sparse verge tuft on the safe banks
# GOAL family
BAY_EMPTY, BAY_FULL = "▽", "▼"
GOLD_GLOW = "·"                  # the 747 bay's beckoning halo — reserved gold
LIFE = "◆"


def use_ascii():
    """Pure-ASCII glyph set for a non-UTF-8 terminal. Mojibake is worse than low-res."""
    global UTF, FROG, FROG_DEAD, STAR_FRAMES, LOG_BODY, LOG_END, WING_L, WING_R
    global CAR_BODY, TRUCK_BODY, NOSE_R, NOSE_L
    global BAY_EMPTY, BAY_FULL, GLINT_A, GLINT_B, DASH, GRASS, GOLD_GLOW, LIFE
    UTF = False
    STAR_FRAMES[:] = ["*", "+", "x", "+"]
    FROG, FROG_DEAD = "*", "x"
    CAR_BODY, TRUCK_BODY = "=", "#"
    NOSE_R, NOSE_L = ">", "<"
    LOG_BODY, LOG_END = "_", "_"      # low waterline plank — disjoint from car "="
    WING_L, WING_R = "[", "]"
    BAY_EMPTY, BAY_FULL = "v", "V"
    GLINT_A, GLINT_B = ".", "~"
    DASH, GRASS = "-", ":"
    GOLD_GLOW, LIFE = ".", "*"


def ascii_wanted():
    """ASCII when the terminal cannot encode UTF-8, or when forced for the mono
    test. `747_ASCII=1 python3 ...` is not a legal shell assignment prefix (a
    POSIX name may not start with a digit), so only `env 747_ASCII=1 ...` works —
    honour the documented name AND a shell-settable alias, exactly as the rest of
    the line does, or jaywalk is the one title the mono gate cannot drive."""
    if os.environ.get("747_ASCII") == "1" or os.environ.get("LAB747_ASCII") == "1":
        return True
    enc = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
           or os.environ.get("LANG") or "")
    return "utf" not in enc.lower()


def txt(s):
    if UTF:
        return s
    # ↑ was missing, so the HUD key hint shipped a multibyte glyph into a pane
    # that had just declared it cannot encode one — the one non-ASCII byte left
    # on screen under 747_ASCII=1.
    for a, b in (("·", "-"), ("×", "x"), ("—", "-"), ("▸", ">"), ("↑", "^")):
        s = s.replace(a, b)
    return s


# ---------------------------------------------------------------------------
# state protocol — byte-identical semantics to breakout.py. This is the code
# that must never drift between titles.
# ---------------------------------------------------------------------------
def state_path(session):
    safe = "".join(c for c in session if c.isalnum() or c == "-")
    return os.path.join(STATE_DIR, "state-%s" % safe if safe else "state")


def read_state(session):
    try:
        with open(state_path(session)) as f:
            return f.read().strip()
    except OSError:
        return "thinking"


def remove_state(session):
    try:
        os.remove(state_path(session))
    except OSError:
        pass


def set_pane_title(session=""):
    sys.stdout.write("\033]2;JAYWALK747-%s\033\\" % (session or "free"))
    sys.stdout.flush()


def stats_path():
    return os.path.join(STATE_DIR, "jaywalk-best")


def load_best():
    try:
        with open(stats_path()) as f:
            return json.load(f)
    except Exception:
        return {"best": 0, "runs": 0, "restarts": 0}


def save_best(d):
    """Local file only. There is no network code in this program, by design and
    by CI gate — zero-network is the security claim the whole line rests on."""
    if os.path.exists(os.path.join(STATE_DIR, "no-stats")):
        return
    try:
        tmp = stats_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, stats_path())
    except Exception:
        pass


class Palette(object):
    """The world is a set of dark background WASHES — safe LAND (green), cold
    WATER (blue), dead ROAD (charcoal). Actors ride on top as bright foreground,
    so value alone tells surface from thing. One hue per ROLE, never per
    decoration. GOLD is reserved line-wide for the hidden 747 and spent nowhere
    else; ORANGE is reserved for YOU, the one warm ember on a cold board."""

    def __init__(self):
        self.has_color = False
        try:
            curses.start_color()
            curses.use_default_colors()
            n = curses.COLORS
            self.has_color = n >= 8
        except Exception:
            n = 0
        self._cache = {}
        self._floor = {}        # bg -> a pair that at least keeps that surface
        self._next = 1
        try:
            self._max = curses.COLOR_PAIRS - 1
        except Exception:
            self._max = 0
        # NB: individual spaceless literals — the CI mono-text gate flags any
        # spaced string that contains a hue word ("gold"), even a role key list.
        keys = ("land", "land_hi", "water", "water_g", "water_hi", "road",
                "road_dash", "hud", "car", "truck", "log", "log_hi", "log_lo",
                "player", "player_dim", "gold", "dead", "bay_done", "dim",
                "hud_fg", "life", "bay_empty")
        if n >= 256:
            # value-ramped: each surface dark, each rider bright, a gap between.
            self.C = dict(land=22, land_hi=65, water=17, water_g=25, water_hi=45,
                          road=235, road_dash=239, hud=234,
                          car=204, truck=141, log=94, log_hi=137, log_lo=58,
                          player=173, player_dim=173, gold=220, dead=245,
                          bay_done=78, dim=246, hud_fg=252, life=173,
                          bay_empty=109)
        elif n >= 8:
            W, K, Y = curses.COLOR_WHITE, curses.COLOR_BLACK, curses.COLOR_YELLOW
            G, B, C_ = curses.COLOR_GREEN, curses.COLOR_BLUE, curses.COLOR_CYAN
            M = curses.COLOR_MAGENTA
            self.C = dict(land=G, land_hi=G, water=B, water_g=C_, water_hi=C_,
                          road=K, road_dash=W, hud=K, car=M, truck=B,
                          log=Y, log_hi=Y, log_lo=Y, player=Y, player_dim=Y,
                          gold=Y, dead=W, bay_done=G, dim=W, hud_fg=W, life=Y,
                          bay_empty=C_)
        else:
            self.C = dict((k, -1) for k in keys)
        self.reserve_surfaces()

    def reserve_surfaces(self):
        """Claim ONE PAIR PER SURFACE before anything else asks for a pair.

        The pair table is a fixed-size resource, and the old exhaustion path
        returned A_NORMAL — which paints the cell on the TERMINAL'S DEFAULT
        background. On a dark terminal that is a black hole punched through the
        board: the wash survives (its pair was cached early) while the actor on
        top of it loses its floor, and the eye reads that as residue, not as a
        thing. Registering every surface first guarantees there is always a wash
        pair to fall back TO, so the worst case is an actor that loses its own
        hue on top of the right floor — never a cell with no floor at all."""
        for role in ("land", "water", "road", "hud", "log"):
            bg = self.C.get(role)
            if bg is None or bg < 0:
                continue
            self._floor[bg] = self.pair(self.C["hud_fg"], bg)

    def pair(self, fg, bg=-1):
        """fg-on-bg colour attr, cached. Degrades gracefully when colour is
        unavailable or the pair table is exhausted — never a crash, and never a
        cell that silently loses its background wash."""
        if not self.has_color or fg is None or fg < 0:
            return curses.A_NORMAL
        if bg is None or bg < 0:
            bg = -1
        key = (fg, bg)
        got = self._cache.get(key)
        if got is not None:
            return got
        if self._next > self._max:
            # KEEP THE FLOOR, LOSE ONLY THE HUE.
            return self._floor.get(bg, curses.A_NORMAL)
        idx = self._next
        self._next += 1
        ipair(idx, fg, bg)
        p = cpair(idx)
        self._cache[key] = p
        return p


class Lane(object):
    """One row of traffic or river. Everything is a repeating pattern scrolling
    at a constant speed — which is what makes it readable: the beat is legible
    before the objects are."""

    def __init__(self, y, kind, speed, gap, size, rng):
        self.y = y
        self.kind = kind          # "road" | "river"
        self.speed = speed        # cells/sec, sign = direction
        self.size = size
        self.gap = gap
        self.off = rng.random() * gap
        self.period = size + gap

    def step(self, dt):
        self.off = (self.off + self.speed * dt) % self.period

    def spans(self, w):
        """Every occupied [x0, x1) run on this lane, in screen cells."""
        out = []
        start = -self.period + (self.off % self.period)
        x = start
        while x < w + self.period:
            out.append((x, x + self.size))
            x += self.period
        return out

    def occupied(self, x):
        for a, b in self.spans(10 ** 6):
            if a <= x < b:
                return (a, b)
        return None

    def occupied_near(self, x, w):
        for a, b in self.spans(w):
            if a - 0.001 <= x < b:
                return (a, b)
        return None


class Game(object):
    def __init__(self, scr, session):
        self.scr = scr
        self.session = session
        self.rng = random.Random()
        self.best = load_best()
        self.score = 0
        self.lives = START_LIVES
        self.level = 1
        self.bays = [False] * BAYS
        self.state = "thinking"
        self.manual_play = False
        self.idle_drawn = False
        self.msg = ""
        self.msg_t = 0.0
        self.die_t = 0.0
        self.hop_cool = 0.0
        self.flash = 0
        self.bay_pop = 0.0        # seconds left on a landing celebration
        self.bay_pop_i = -1
        self.bay_pop_txt = ""
        self.board_pulse = 0.0   # seconds left on the 747 full-board beat
        self.star_t = 0.0
        self.layout()
        self.build_lanes()
        self.reset_frog()

    # ---- geometry ---------------------------------------------------------
    def layout(self):
        self.h, self.w = self.scr.getmaxyx()
        self.small = self.w < MIN_W or self.h < MIN_H
        if self.small:
            return
        self.hud_y = self.h - 1
        self.bay_y = 0                      # home bays across the top
        self.curb_y = self.hud_y - 1        # ...and the curb sits ON the HUD.
        # The board FILLS the pane: bays(1) + river + median(1) + road + curb(1).
        # Stacking from the top instead left the bottom half of the pane empty,
        # which reads as a broken screen, not as a road.
        body = self.curb_y - self.bay_y - 2  # rows available to river + road
        if body < 4:
            self.small = True
            return
        # roughly half river, half road — an 11-to-5 split reads as a mistake,
        # not as a board. Frogger's balance is the point: two distinct halves.
        rl = max(1, body // 2)
        rd = max(2, body - rl)
        self.river_rows = list(range(self.bay_y + 1, self.bay_y + 1 + rl))
        self.median_y = self.river_rows[-1] + 1
        self.road_rows = list(range(self.median_y + 1, self.median_y + 1 + rd))
        # anything left over (odd heights) becomes extra road nearest the curb
        while self.road_rows and self.road_rows[-1] >= self.curb_y:
            self.road_rows.pop()
        if not self.road_rows:
            self.small = True
            return
        self.river_set = set(self.river_rows)
        self.road_set = set(self.road_rows)

    def build_lanes(self):
        """THE FROGGER RAMP, in three dials and one invariant.

        The invariant first, because it was the actual bug: lane speed used to be
        `base + 1.6 * i`, indexed off the ROW NUMBER. In a 22-row pane that is
        thirteen road lanes, so the top lane ran at 24 cells/sec — the board got
        harder the TALLER the terminal, which is not a difficulty curve, it is a
        geometry accident. Speed now interpolates across the band as a FRACTION,
        so an 80x8 split and a 110x30 pane play the same game.

        And the fraction runs from the side you ENTER: road_rows[-1] is the lane
        you step into off the curb and river_rows[-1] is the one you step onto off
        the median, so both are frac 0 = slowest. The old indexing had the very
        first lane you touched as the fastest on the board, which is why level 1
        read as a coin-flip instead of as a warm-up.

        Then the three dials, per level: everything gets faster, road holes get
        tighter, logs get shorter and further apart, and trucks get more common."""
        if self.small:
            self.lanes = []
            return
        rng = self.rng
        lv = self.level
        self.lanes = []
        # 1. SPEED. Capped, or level 8 in a think-wait is unplayable rather than hard.
        ramp = min(1.0 + 0.13 * (lv - 1), 1.85)
        # 2. HOLES. Road gaps close; logs shorten and separate.
        road_gap = max(7, 16 - 2 * lv)          # L1 14 -> L5 7, then held
        log_size = max(4, 8 - (lv - 1))         # L1 8  -> L5 4, then held
        log_gap = min(3 + (lv - 1), 8)          # L1 3  -> L6 8, then held
        # 3. EXTRA HAZARD. Trucks are size-3 hazards; more of the road becomes
        #    truck as the levels climb. Every 4th lane at L1, every 2nd by L4.
        heavy_every = 4 if lv <= 1 else (3 if lv <= 3 else 2)
        nrd = len(self.road_rows)
        # 4. HOW MUCH OF THE ROAD IS LIVE. The board fills the pane, so a 30-row
        #    terminal lays down THIRTEEN road rows — and putting traffic in all
        #    thirteen is not a hard level, it is an unfair one: there is nowhere
        #    to stand still. Every death in a 100-second instrumented run at
        #    110x30 was a car, not once the river, and not one crossing completed
        #    in 27 lives. Frogger's answer has always been a lane you can REST in.
        #    So ROAD_LANES stops being a decorative constant and becomes the
        #    number of LIVE lanes at level 1 — the "4" of 7-4-7, exactly as this
        #    file already claimed at the top. The rest of the road is empty
        #    asphalt: same charcoal wash, same dashes, no cars. Each level lights
        #    one more lane until the whole road is live, which IS the extra
        #    hazard lane the ramp wants — and on a short pane (80x8 has two road
        #    rows) min() means nothing changes at all.
        nact = max(1, min(nrd, ROAD_LANES + (lv - 1)))
        if nact >= nrd:
            live = set(range(nrd))
        elif nact == 1:
            live = set([nrd - 1])               # the one you step into off the curb
        else:
            live = set(int(round(k * (nrd - 1) / float(nact - 1)))
                       for k in range(nact))
        for i, y in enumerate(self.road_rows):
            if i not in live:
                continue                        # empty asphalt: a row you can rest on
            frac = (nrd - 1 - i) / float(nrd - 1) if nrd > 1 else 0.0
            heavy = (i % heavy_every == heavy_every - 1)
            speed = (4.2 + 3.6 * frac) * ramp * (1 if i % 2 == 0 else -1)
            self.lanes.append(Lane(y, "road", speed,
                                   gap=road_gap, size=3 if heavy else 2,
                                   rng=rng))
        nrv = len(self.river_rows)
        for i, y in enumerate(self.river_rows):
            frac = (nrv - 1 - i) / float(nrv - 1) if nrv > 1 else 0.0
            speed = (2.6 + 2.4 * frac) * ramp * (1 if i % 2 else -1)
            self.lanes.append(Lane(y, "river", speed,
                                   gap=log_gap, size=log_size,
                                   rng=rng))
        self.lane_by_y = dict((l.y, l) for l in self.lanes)
        self.build_water_field()

    def build_water_field(self):
        """A seeded, non-tiling ripple field per river row. Sparse by construction
        (~7% dim, ~2% bright) so it reads as shimmer, not a dither wall — and
        seeded, never arithmetic modulo, so it never forms diagonal moire rulers."""
        self.water_field = {}
        w = getattr(self, "w", 100)
        for y in self.river_rows:
            r = random.Random((hash(("jw-water", y, self.level)) & 0x7fffffff))
            fw = w + 48
            field = []
            for _ in range(fw):
                v = r.random()
                field.append(2 if v > 0.975 else (1 if v > 0.905 else 0))
            self.water_field[y] = field

    def bay_cols(self):
        """Seven bays evenly spaced across the top. The MIDDLE one is gold —
        that is the hidden 747: 7 bays, 4 road lanes, 7 total crossings."""
        step = self.w / float(BAYS + 1)
        return [int(step * (i + 1)) for i in range(BAYS)]

    def reset_frog(self):
        self.fx = float(self.w // 2)
        self.fy = self.curb_y
        self.riding = None
        self.die_t = 0.0

    # ---- input ------------------------------------------------------------
    def hop(self, dx, dy):
        if self.hop_cool > 0.0 or self.die_t > 0.0:
            return
        self.hop_cool = STEP_COOLDOWN
        nx = self.fx + dx
        ny = self.fy + dy
        if ny > self.curb_y:
            ny = self.curb_y
        if ny < self.bay_y:
            ny = self.bay_y
        self.fx = min(max(0.0, nx), float(self.w - 1))
        self.fy = ny
        if dy < 0:
            self.score += 10          # forward progress always pays
        if self.fy == self.bay_y:
            self.try_bay()

    def try_bay(self):
        cols = self.bay_cols()
        for i, cx in enumerate(cols):
            if abs(self.fx - cx) <= 1 and not self.bays[i]:
                self.bays[i] = True
                mid = (BAYS // 2)
                gain = 747 if i == mid else 100
                self.score += gain
                self.msg = txt("BAY %d/%d  +%d" % (sum(self.bays), BAYS, gain))
                self.msg_t = 1.4
                # LANDING JUICE. The bay itself pulses, the score flies off it,
                # and the hidden 747 additionally strobes the whole board.
                self.bay_pop = BAY_POP
                self.bay_pop_i = i
                self.bay_pop_txt = txt("+%d" % gain)
                if i == mid:
                    self.board_pulse = BAY_POP
                if all(self.bays):
                    self.level += 1
                    self.bays = [False] * BAYS
                    self.lives = min(5, self.lives + 1)
                    self.score += 500
                    self.msg = txt("ROAD %d CLEARED  +500" % (self.level - 1))
                    self.msg_t = 1.8
                    self.board_pulse = BAY_POP   # the biggest beat gets the beat
                    self.build_lanes()
                self.reset_frog()
                return
        self.die("no bay there")

    def die(self, why=""):
        if self.die_t > 0.0:
            return
        self.lives -= 1
        self.die_t = 0.7
        self.flash = 2
        self.msg = txt(why)
        self.msg_t = 1.0

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        if self.msg_t > 0.0:
            self.msg_t -= dt
        if self.flash > 0:
            self.flash -= 1
        if self.bay_pop > 0.0:
            self.bay_pop = max(0.0, self.bay_pop - dt)
        if self.board_pulse > 0.0:
            self.board_pulse = max(0.0, self.board_pulse - dt)
        if self.hop_cool > 0.0:
            self.hop_cool -= dt
        self.star_t += dt
        for l in self.lanes:
            l.step(dt)
        if self.die_t > 0.0:
            self.die_t -= dt
            if self.die_t <= 0.0:
                if self.lives <= 0:
                    return "over"
                self.reset_frog()
            return None
        self.collide(dt)
        return "over" if self.lives <= 0 and self.die_t <= 0.0 else None

    def collide(self, dt):
        y = int(self.fy)
        lane = self.lane_by_y.get(y)
        if lane is None:
            self.riding = None
            return
        if lane.kind == "road":
            self.riding = None
            hit = lane.occupied_near(self.fx, self.w)
            if hit is not None:
                self.die("hit")
            return
        # river: you must be ON something, and you drift with it
        hit = lane.occupied_near(self.fx, self.w)
        if hit is None:
            self.riding = None
            self.die("water")
            return
        self.riding = lane
        self.fx += lane.speed * dt
        if self.fx < 0 or self.fx > self.w - 1:
            self.die("swept away")

    def on_rejoin(self):
        """Back from the ghost pane. Never simulate across the gap, and never
        kill the player for time they could not see."""
        self.die_t = max(self.die_t, 0.0)
        self.hop_cool = 0.0
        # a celebration nobody could see must not strobe on the way back in
        self.bay_pop = 0.0
        self.board_pulse = 0.0

    def commit_stats(self):
        b = self.best
        b["runs"] = b.get("runs", 0) + 1
        b["best"] = max(b.get("best", 0), self.score)
        save_best(b)

    # ---- draw -------------------------------------------------------------
    def zone_bg(self, y):
        """The background wash for a screen row — the material the cell is made of."""
        C = self.pal.C
        if y == self.bay_y:
            return C["land"]
        if y in self.river_set:
            return C["water"]
        if y == self.median_y or y == self.curb_y:
            return C["land"]
        if y in self.road_set:
            return C["road"]
        if y == self.hud_y:
            return C["hud"]
        return C["land"]

    def zpair(self, y, fg, extra=0):
        """The ONE way a board actor gets an attribute: hue on top of whatever
        that row is MADE OF. Every draw_* below routes through this, so "no cell
        ever loses its wash" is a property of the code and not a property of
        twenty individual call sites remembering to pass a background."""
        return self.pal.pair(fg, self.zone_bg(y)) | extra

    def pulse_attr(self):
        """THE 747 BEAT. Landing the gold centre bay strobes the WHOLE board —
        the same full-field flash Breakout fires on a 747 brick. Reverse video
        does it, which means it costs no new hue (the reserved gold stays spent
        only on the bay itself) and it lands identically on 256 colours and on a
        terminal with none. Three hard 60ms strobes, then gone."""
        if self.board_pulse <= 0.0:
            return 0
        return curses.A_REVERSE if int(self.board_pulse * 15.0) % 2 else 0

    def draw(self, playing):
        scr = self.scr
        scr.erase()
        if self.small:
            try:
                scr.addstr(0, 0, txt("JAYWALK — pane too small")[:max(0, self.w - 1)])
            except curses.error:
                pass
            scr.refresh()
            return
        # 1. lay the world down as background washes — land / water / road.
        for y in range(self.h):
            self.wash(y, self.zone_bg(y))
        # 2. surface texture — sparse, animated, never a full glyph field.
        self.draw_banks()
        self.draw_water()
        self.draw_road_marks()
        # 3. the actors, riding on top by value contrast.
        self.draw_lanes()
        self.draw_bays()
        self.draw_player()
        self.draw_pop()
        # 4. HUD + pause overlay.
        self.hud(playing)
        if not playing:
            self.pause_overlay()
        scr.refresh()

    def draw_banks(self):
        """Sparse grass tufts on the safe green banks — texture, not a field."""
        C = self.pal.C
        for y in (self.bay_y, self.median_y, self.curb_y):
            at = self.zpair(y, C["land_hi"])
            r = random.Random((hash(("jw-grass", y)) & 0x7fffffff))
            for x in range(self.w):
                if r.random() > 0.86:
                    self.put(y, x, GRASS, at)

    def draw_water(self):
        """Dim ripples + rare bright crests, drifting in each lane's current."""
        C = self.pal.C
        for y in self.river_rows:
            a_dim = self.zpair(y, C["water_g"])
            a_hi = self.zpair(y, C["water_hi"], curses.A_BOLD)
            lane = self.lane_by_y.get(y)
            d = 1 if (lane and lane.speed > 0) else -1
            field = self.water_field.get(y)
            if not field:
                continue
            off = int(self.star_t * 4.5) * d
            n = len(field)
            for x in range(self.w):
                v = field[(x + off) % n]
                if v == 1:
                    self.put(y, x, GLINT_A, a_dim)
                elif v == 2:
                    self.put(y, x, GLINT_B, a_hi)

    def draw_road_marks(self):
        """Faint lane dashes, phase-shifted per row so they never column up."""
        C = self.pal.C
        for i, y in enumerate(self.road_rows):
            at = self.zpair(y, C["road_dash"])
            for x in range((i * 3) % 8, self.w, 8):
                self.put(y, x, DASH, at)

    def draw_lanes(self):
        C = self.pal.C
        for l in self.lanes:
            river = (l.kind == "river")
            heavy = l.size >= 3
            if river:
                body_at = self.zpair(l.y, C["log"])
                hi_at = self.zpair(l.y, C["log_hi"])   # quiet grain, not headlights
                lo_at = self.zpair(l.y, C["log_lo"])
            else:
                col = C["truck"] if heavy else C["car"]
                body_at = self.zpair(l.y, col, curses.A_BOLD)
            for a, b in l.spans(self.w):
                x0, x1 = int(a), int(b)
                for x in range(x0, x1):
                    if not (0 <= x < self.w):
                        continue
                    if river:
                        if x == x0 or x == x1 - 1:
                            self.put(l.y, x, LOG_END, lo_at)   # shadowed ends
                        else:
                            at = hi_at if ((x - x0) % 4 == 2) else body_at
                            self.put(l.y, x, LOG_BODY, at)     # lit wood grain
                    else:
                        body = TRUCK_BODY if heavy else CAR_BODY
                        if l.speed > 0:
                            ch = NOSE_R if x == x1 - 1 else body
                        else:
                            ch = NOSE_L if x == x0 else body
                        self.put(l.y, x, ch, body_at)

    def draw_bays(self):
        C = self.pal.C
        by = self.bay_y
        mid = BAYS // 2
        # ~12Hz strobe over the life of the pop. int()%2 is the whole trick: it
        # needs no frame counter, so it survives a variable dt and a ghost pane.
        popping = self.bay_pop > 0.0
        strobe = popping and (int(self.bay_pop * 24.0) % 2 == 1)
        for i, cx in enumerate(self.bay_cols()):
            full = self.bays[i]
            if i == mid:
                at = self.zpair(by, C["gold"], curses.A_BOLD)
                ch = BAY_FULL if full else BAY_EMPTY
                if not full:                      # the 747 beckons — reserved gold halo
                    g = self.zpair(by, C["gold"], curses.A_DIM)
                    self.put(by, cx - 1, GOLD_GLOW, g)
                    self.put(by, cx + 1, GOLD_GLOW, g)
            elif full:
                at = self.zpair(by, C["bay_done"], curses.A_BOLD)
                ch = BAY_FULL
            else:
                at = self.zpair(by, C["bay_empty"])   # visible open target
                ch = BAY_EMPTY
            if popping and i == self.bay_pop_i:
                # THE BAY YOU JUST LANDED pulses for BAY_POP seconds. A_REVERSE
                # carries the beat on a terminal with no colour at all, so the
                # celebration is never colour-dependent.
                at = self.zpair(by, C["hud_fg"], curses.A_BOLD)
                if strobe:
                    at |= curses.A_REVERSE
                ch = BAY_FULL
            self.put(by, cx, ch, at)

    def draw_pop(self):
        """The score flies off the bay you just filled. Bright + strobing, over
        the water immediately below the bay, rising one row as it fades — the
        arcade's oldest bit of feedback, and the cheapest."""
        if self.bay_pop <= 0.0 or not self.bay_pop_txt:
            return
        cols = self.bay_cols()
        i = self.bay_pop_i
        if not (0 <= i < len(cols)):
            return
        s = self.bay_pop_txt
        # rises toward the bay as the pop expires
        y = self.bay_y + (1 if self.bay_pop < BAY_POP * 0.5 else 2)
        y = min(max(self.bay_y + 1, y), self.curb_y)
        hue = self.pal.C["gold"] if i == (BAYS // 2) else self.pal.C["player"]
        at = self.zpair(y, hue, curses.A_BOLD)
        if int(self.bay_pop * 24.0) % 2 == 1:
            at |= curses.A_REVERSE
        x = max(0, min(self.w - len(s), cols[i] - len(s) // 2))
        self.put_str(y, x, s, at)

    def draw_player(self):
        """The one warm ember. Alive = Claude's orange, morphing like the spinner
        it is borrowed from; dead = a cold grey ash. Hitbox stays the centre cell."""
        C = self.pal.C
        dead = self.die_t > 0.0
        py, px = int(self.fy), int(round(self.fx))
        zbg = C["log"] if (self.riding is not None and not dead) else self.zone_bg(py)
        if dead and self.flash > 0:
            # 2-frame white impact spark before the ember goes cold
            core = self.pal.pair(C["hud_fg"], zbg) | curses.A_BOLD
            wing = self.pal.pair(C["gold"], zbg) | curses.A_BOLD
            fch = STAR_FRAMES[3]
        elif dead:
            core = self.pal.pair(C["dead"], zbg) | curses.A_BOLD
            wing = self.pal.pair(C["dead"], zbg) | curses.A_DIM
            fch = FROG_DEAD
        else:
            core = self.pal.pair(C["player"], zbg) | curses.A_BOLD
            wing = self.pal.pair(C["player"], zbg) | curses.A_BOLD
            fch = STAR_FRAMES[int(self.star_t / STAR_RATE) % len(STAR_FRAMES)]
        self.put(py, px - 1, WING_L, wing)
        self.put(py, px, fch, core)
        self.put(py, px + 1, WING_R, wing)

    def wash(self, y, bg):
        """Fill a whole row with a background colour — the material floor."""
        if not (0 <= y < self.h):
            return
        at = self.pal.pair(self.pal.C["hud_fg"], bg) | self.pulse_attr()
        # FULL width, in ONE call. hline() takes a chtype, writes exactly n cells,
        # never advances the cursor and never wraps — so it fills column w-1 on
        # row h-1 too, which is the one cell addstr() can never reach. The old
        # addstr(w-1) + insch + put stack was three calls to say that, and the
        # insch shifted the row it was supposed to be finishing.
        try:
            self.scr.hline(y, 0, ord(" ") | at, self.w)
            return
        except (curses.error, ValueError, TypeError):
            pass
        try:                                   # belt and braces on an odd curses
            self.scr.addstr(y, 0, " " * max(0, self.w - 1), at)
            if self.w >= 1:
                self.scr.insch(y, self.w - 1, " ", at)
        except curses.error:
            pass

    def put(self, y, x, ch, at):
        if 0 <= y < self.h and 0 <= x < self.w:
            try:
                self.scr.addstr(y, x, ch, at)
            except curses.error:
                pass

    def hud(self, playing):
        """A quiet status line: only the score number and the gold 747 bay carry
        any weight. It reports; it never competes with the board."""
        C = self.pal.C
        y = self.hud_y
        base = self.pal.pair(C["hud_fg"], C["hud"])
        dim = base | curses.A_DIM
        self.wash(y, C["hud"])
        x = 1
        self.put_str(y, x, txt("JAYWALK"), dim); x += 8
        s = "%d" % self.score
        self.put_str(y, x, s, self.pal.pair(C["player"], C["hud"]) | curses.A_BOLD)
        x += len(s) + 1
        lv = LIFE * max(0, self.lives)
        self.put_str(y, x, lv, self.pal.pair(C["life"], C["hud"]) | curses.A_BOLD)
        x += len(lv) + 1
        mid = BAYS // 2
        for i in range(BAYS):
            done = self.bays[i]
            if i == mid:
                a = self.pal.pair(C["gold"], C["hud"]) | curses.A_BOLD
            elif done:
                a = self.pal.pair(C["bay_done"], C["hud"]) | curses.A_BOLD
            else:
                a = dim
            self.put(y, x, BAY_FULL if done else BAY_EMPTY, a); x += 1
        x += 1
        self.put_str(y, x, txt("R%d" % self.level), dim)
        right = txt("[q]") if self.w < 64 else txt("[↑] cross   [q] close")
        rx = self.w - 1 - len(right)
        if rx > x + 2:
            self.put_str(y, rx, right, dim)
        # centre banner — bay/clear beats land on the median in their own hue.
        if self.msg_t > 0.0 and self.msg:
            m = self.msg[:max(0, self.w - 2)]
            gold_beat = ("747" in self.msg or "CLEAR" in self.msg)
            col = C["gold"] if gold_beat else C["player"]
            ba = self.zpair(self.median_y, col, curses.A_BOLD)
            self.put_str(self.median_y, max(0, (self.w - len(m)) // 2), m, ba)

    def put_str(self, y, x, s, at):
        for i, c in enumerate(s):
            self.put(y, x + i, c, at)

    def pause_overlay(self):
        C = self.pal.C
        s = txt("PAUSED — Claude replied    [space] keep playing    [q] close")
        y = max(0, self.h // 2)
        at = self.pal.pair(C["hud_fg"], self.zone_bg(y)) | curses.A_BOLD
        self.put_str(y, max(0, (self.w - len(s)) // 2), s, at)


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


def game_over_screen(scr, game, session):
    scr.nodelay(False)
    scr.timeout(1000)
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [
            txt("FLATTENED · %d" % game.score),
            txt("BEST %d · ROAD %d" % (max(game.best.get("best", 0), game.score), game.level)),
            "",
            txt("[r] run it again   [m] menu   [q] close") if menu_available()
            else txt("[r] run it again   [q] close"),
        ]
        for i, ln in enumerate(lines):
            try:
                scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln,
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("r"), ord("R")):
            b = game.best
            b["restarts"] = b.get("restarts", 0) + 1
            save_best(b)
            return True
        if ch in (ord("m"), ord("M")) and menu_available():
            back_to_menu(session)            # never returns
        if ch in (ord("q"), ord("Q"), 27):
            return False
        if read_state(session) == "end":
            return False


def main(stdscr, args):
    try:
        curses.curs_set(0)          # some minimal terminals cannot hide the cursor
    except curses.error:
        pass
    pal = Palette()
    while True:                                  # restart loop
        # RESTORE INPUT MODE EVERY RUN. game_over_screen() switches the screen to
        # BLOCKING with a 1000ms timeout so it can wait on a keypress. Setting
        # nodelay(True) only once, outside this loop, meant the restarted game
        # inherited that blocking mode and ran at ~1 fps — which reads as "it
        # lags when I press r", exactly as reported. Reset it per run.
        stdscr.nodelay(True)
        stdscr.timeout(0)
        game = Game(stdscr, args.session)
        game.pal = pal
        game.manual_play = args.free
        last = time.time()
        poll = 0.0
        while True:
            now = time.time()
            raw = now - last
            last = now
            poll -= raw
            if poll <= 0.0:
                game.state = read_state(args.session)
                poll = STATE_POLL if (game.manual_play or game.state == "thinking") \
                    else POLL_IDLE
            if game.state == "end":
                remove_state(args.session)
                return
            ch = stdscr.getch()
            while ch != -1:
                if ch in (ord("q"), ord("Q")):
                    remove_state(args.session)
                    return
                if ch == ord(" "):
                    game.manual_play = not game.manual_play
                    game.idle_drawn = False
                elif ch in (curses.KEY_UP, ord("w"), ord("W")):
                    game.hop(0, -1)
                elif ch in (curses.KEY_DOWN, ord("s"), ord("S")):
                    game.hop(0, 1)
                elif ch in (curses.KEY_LEFT, ord("a"), ord("A")):
                    game.hop(-1, 0)
                elif ch in (curses.KEY_RIGHT, ord("d"), ord("D")):
                    game.hop(1, 0)
                elif ch == curses.KEY_RESIZE:
                    game.layout()
                    game.build_lanes()
                ch = stdscr.getch()
            playing = game.manual_play or game.state == "thinking"
            if not playing:
                if not game.idle_drawn:
                    game.draw(False)
                    game.idle_drawn = True
                time.sleep(IDLE_SLEEP)
                continue
            if game.idle_drawn:
                game.on_rejoin()
                game.idle_drawn = False
                raw = 0.0
            dt = 0.0 if raw > DT_REJOIN else min(raw, DT_MAX)
            result = game.step(dt)
            game.draw(True)
            if result == "over":
                game.commit_stats()
                if game_over_screen(stdscr, game, args.session):
                    break
                return
            time.sleep(TICK)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    LAUNCH_ARGS = args      # module scope: back_to_menu() reads it
    if ascii_wanted():
        use_ascii()
    os.makedirs(STATE_DIR, exist_ok=True)
    set_pane_title(args.session)
    curses.wrapper(main, args)
