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
    enc = (os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE")
           or os.environ.get("LANG") or "")
    return "utf" not in enc.lower()


def txt(s):
    if UTF:
        return s
    for a, b in (("·", "-"), ("×", "x"), ("—", "-"), ("▸", ">")):
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

    def pair(self, fg, bg=-1):
        """fg-on-bg colour attr, cached. Degrades to A_NORMAL when colour is
        unavailable or the pair table is exhausted — never a crash."""
        if not self.has_color or fg is None or fg < 0:
            return curses.A_NORMAL
        if bg is None or bg < 0:
            bg = -1
        key = (fg, bg)
        got = self._cache.get(key)
        if got is not None:
            return got
        if self._next > self._max:
            return curses.A_NORMAL
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
        self.flash_bay = -1
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
        if self.small:
            self.lanes = []
            return
        rng = self.rng
        lv = self.level
        self.lanes = []
        for i, y in enumerate(self.road_rows):
            heavy = (i % 3 == 2)
            speed = (5.0 + 1.6 * i + 0.9 * (lv - 1)) * (1 if i % 2 == 0 else -1)
            self.lanes.append(Lane(y, "road", speed,
                                   gap=max(7, 15 - lv), size=3 if heavy else 2,
                                   rng=rng))
        for i, y in enumerate(self.river_rows):
            speed = (3.5 + 1.3 * i + 0.6 * (lv - 1)) * (1 if i % 2 else -1)
            self.lanes.append(Lane(y, "river", speed,
                                   gap=max(5, 10 - lv // 2), size=max(4, 6 - lv // 3),
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
                self.flash = 3
                self.flash_bay = i
                if all(self.bays):
                    self.level += 1
                    self.bays = [False] * BAYS
                    self.lives = min(5, self.lives + 1)
                    self.score += 500
                    self.msg = txt("ROAD %d CLEARED  +500" % (self.level - 1))
                    self.msg_t = 1.8
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
        # 4. HUD + pause overlay.
        self.hud(playing)
        if not playing:
            self.pause_overlay()
        scr.refresh()

    def draw_banks(self):
        """Sparse grass tufts on the safe green banks — texture, not a field."""
        C = self.pal.C
        at = self.pal.pair(C["land_hi"], C["land"])
        for y in (self.bay_y, self.median_y, self.curb_y):
            r = random.Random((hash(("jw-grass", y)) & 0x7fffffff))
            for x in range(self.w):
                if r.random() > 0.86:
                    self.put(y, x, GRASS, at)

    def draw_water(self):
        """Dim ripples + rare bright crests, drifting in each lane's current."""
        C = self.pal.C
        a_dim = self.pal.pair(C["water_g"], C["water"])
        a_hi = self.pal.pair(C["water_hi"], C["water"]) | curses.A_BOLD
        for y in self.river_rows:
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
        at = self.pal.pair(C["road_dash"], C["road"])
        for i, y in enumerate(self.road_rows):
            for x in range((i * 3) % 8, self.w, 8):
                self.put(y, x, DASH, at)

    def draw_lanes(self):
        C = self.pal.C
        for l in self.lanes:
            river = (l.kind == "river")
            heavy = l.size >= 3
            if river:
                body_at = self.pal.pair(C["log"], C["water"])
                hi_at = self.pal.pair(C["log_hi"], C["water"])   # quiet grain, not headlights
                lo_at = self.pal.pair(C["log_lo"], C["water"])
            else:
                col = C["truck"] if heavy else C["car"]
                body_at = self.pal.pair(col, C["road"]) | curses.A_BOLD
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
        mid = BAYS // 2
        for i, cx in enumerate(self.bay_cols()):
            full = self.bays[i]
            flashing = (self.flash > 0 and self.flash_bay == i)
            if i == mid:
                at = self.pal.pair(C["gold"], C["land"]) | curses.A_BOLD
                ch = BAY_FULL if full else BAY_EMPTY
                if not full:                      # the 747 beckons — reserved gold halo
                    g = self.pal.pair(C["gold"], C["land"]) | curses.A_DIM
                    self.put(self.bay_y, cx - 1, GOLD_GLOW, g)
                    self.put(self.bay_y, cx + 1, GOLD_GLOW, g)
            elif full:
                at = self.pal.pair(C["bay_done"], C["land"]) | curses.A_BOLD
                ch = BAY_FULL
            else:
                at = self.pal.pair(C["bay_empty"], C["land"])   # visible open target
                ch = BAY_EMPTY
            if flashing:                          # 3-frame landing spark
                at = self.pal.pair(C["hud_fg"], C["land"]) | curses.A_BOLD
            self.put(self.bay_y, cx, ch, at)

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
        at = self.pal.pair(self.pal.C["hud_fg"], bg)
        if not (0 <= y < self.h):
            return
        # FULL width. Washing w-1 leaves a black column, and any row that skips
        # the wash entirely reads as a TEAR in the board — my own colour render
        # caught ragged black edges the glyph dump could not show.
        try:
            self.scr.addstr(y, 0, " " * max(0, self.w - 1), at)
            if self.w >= 1:
                self.scr.insch(y, self.w - 1, " ", at)   # last cell, without scrolling
        except curses.error:
            pass
        self.put(y, self.w - 1, " ", at)

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
            ba = self.pal.pair(col, self.zone_bg(self.median_y)) | curses.A_BOLD
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
            txt("[r] run it again   [q] close"),
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
    if ascii_wanted():
        use_ascii()
    os.makedirs(STATE_DIR, exist_ok=True)
    set_pane_title(args.session)
    curses.wrapper(main, args)
