#!/usr/bin/env python3
"""BREAK-IN — in-terminal game that runs while Claude thinks.

You never clear the wall. You punch a hole through the ceiling and climb into
the chamber above. Forever.

Lives in a tmux pane split below the Claude Code session. Auto-pauses when
Claude finishes a turn (Stop hook writes 'idle'), resumes on the next prompt
(UserPromptSubmit writes 'thinking'). Exits when the session ends.

The file key stays `breakout` on purpose: the hook derives the tmux pane title
from it (BREAKOUT747-<sid>), and that string is how a ghost-paned run is found,
rejoined and killed. BREAK-IN is a display-layer name. Nothing on the wire moves.

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
# paused-then-resumed run would hand the next title a --free it never had.
LAUNCH_ARGS = None
TICK = 0.033          # ~30 fps
STATE_POLL = 0.2      # how often to re-read the state file while playing
POLL_IDLE = 0.15      # ...and while ghosted. Must stay < 0.25 or session end hangs a pane.
IDLE_SLEEP = 0.05     # idle loop nap: keeps [space]/[q] responsive, writes zero bytes
ASK_TIMEOUT = 45      # ask screen auto-closes after this many seconds
DT_MAX = 0.05         # fixed-timestep clamp: a stalled pane may never teleport the ball
DT_REJOIN = 0.35      # a gap longer than this is a ghost-pane return, not a slow frame

MIN_W, MIN_H = 80, 8  # below this we idle politely with a message — never crash, never exit


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
# pass on 3.9, which is precisely how this shipped. Catch both, always, and a
# missing colour capability costs colour instead of costing the game.
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

BRICK_W = 6
GLYPH_ROWS = 5        # the 747 bitmap is 5 rows; the wall never starts shorter than its own egg


# ---------------------------------------------------------------------------
# GLYPHS — one silhouette family per role, and an honest ASCII fallback.
# Module globals so use_ascii() can swap the whole vocabulary in one call.
# ---------------------------------------------------------------------------
G_BRICK = "▄"          # BRICK      — half block, 5 of a 6-col cell, so bricks never merge
G_EGG = "█"            # 747 BRICK  — the egg hides in TEXTURE, never in colour
G_RIVET = "▒"          # RIVET      — grey means "never breaks"
G_CEIL = "▔"           # CEILING    — STRUCT girder between the hatches
G_BALL = "●"           # BALL       — the only round object
G_PADDLE = "▀"         # PADDLE     — same hue as the ball on purpose: these two are you
G_SENTRY = "◆"         # SENTRY     — the only thing up there you do not want to touch
G_CRAWL = "·"          # CRAWLSPACE — the darkness is literally the hole you made
G_DEBRIS = "·"
G_ON, G_OFF = "▰", "▱"  # the BREACH meter
G_HEART = "♥"
G_TICK = "▌"           # the studio tick, col 0 of the bar
G_SEP = "·"
G_PAUSE = "⏸"
G_MULT = "×"
G_ARROWS = "←→"
G_DASH = "—"
# HATCH erosion — less ink = more damage. Indexed by remaining hit points.
HATCH = ["▖ ", "▞▖", "▛▞", "▛▜"]
# the welcome flyby's own fill ramp (it predates the ASCII switch and would
# mojibake without it)
F_BLK, F_SH3, F_SH2, F_SH1, F_DOT, F_BAR = "█", "▓", "▒", "░", "·", "─"


def ascii_wanted():
    """ASCII when the terminal cannot encode UTF-8, or when forced for the mono test."""
    # `747_ASCII=1 python3 ...` is not a legal shell assignment prefix — a POSIX
    # variable name may not start with a digit, so zsh and bash both reject it and
    # only `env 747_ASCII=1 ...` works. Honour the documented name AND a
    # shell-settable alias, so the mono test is actually runnable either way.
    if os.environ.get("747_ASCII") == "1" or os.environ.get("LAB747_ASCII") == "1":
        return True
    return "utf" not in (sys.stdout.encoding or "").lower()


def use_ascii():
    """The mono fallback. Every object class must still be unambiguous by glyph
    alone: `-` brick vs `#` egg vs `[]` hatch vs `%` rivet vs `O` ball vs `*` sentry."""
    global G_BRICK, G_EGG, G_RIVET, G_CEIL, G_BALL, G_PADDLE, G_SENTRY
    global G_CRAWL, G_DEBRIS, G_ON, G_OFF, G_HEART, G_TICK, G_SEP, G_PAUSE, HATCH
    global G_MULT, G_ARROWS, G_DASH, F_BLK, F_SH3, F_SH2, F_SH1, F_DOT, F_BAR
    G_DASH = "-"
    G_BRICK, G_EGG, G_RIVET, G_CEIL = "-", "#", "%", "_"
    G_BALL, G_PADDLE, G_SENTRY, G_CRAWL, G_DEBRIS = "O", "=", "*", ".", "."
    G_ON, G_OFF, G_HEART, G_TICK, G_SEP, G_PAUSE = "#", ".", "*", "|", "-", "||"
    G_MULT, G_ARROWS = "x", "<>"
    HATCH = [". ", ": ", "[.", "[]"]   # 2 cells wide, always — same footprint as UTF-8
    F_BLK, F_SH3, F_SH2, F_SH1, F_DOT, F_BAR = "#", "#", "*", ":", ".", "-"


# ---------------------------------------------------------------------------
# PALETTE — pairs 100-139 are the reserved shared band (the welcome flyby owns
# 30-56, skyrun owns 60+, and pairs 1-8 stay exactly as they were because the
# flyby's 16-colour branch still reads them).
#
# One hue, one job: RED = do not touch · CYAN = destroy this · GREY = never breaks
# · WHITE = you · GOLD = the 747 and nothing else.
# ---------------------------------------------------------------------------

# One hue per chamber, a 5-step luminance ramp down the rows, so colour answers
# the question the mechanic asks: WHICH CHAMBER AM I IN. Never red, cyan, white,
# grey or gold — all four are reserved in-title.
STRATA_256 = [
    (141, 135,  99,  97,  61),   # C1  violet — the studio hue, chamber one
    (180, 173, 137,  94,  58),   # C2  bronze — NOT gold (226/220 is 747-only)
    (211, 175, 168, 132,  89),   # C3  rose
    (110, 109, 103,  67,  60),   # C4  slate
    (150, 114,  71,  65,  22),   # C5  moss
]


class Palette:
    def __init__(self):
        self._i = 100
        try:
            has256 = curses.COLORS >= 256
        except (curses.error, AttributeError):
            # AttributeError, not just curses.error: on a terminal where
            # start_color() failed, curses.COLORS is never DEFINED at all.
            has256 = False
        if has256:
            self.hazard = self._mk(210, curses.A_BOLD)   # sentry: DO NOT TOUCH
            self.target = self._mk(195, curses.A_BOLD)   # hatch: DESTROY THIS
            self.gold = self._mk(226, curses.A_BOLD)     # the 747 and nothing else
            self.player = self._mk(231, curses.A_BOLD)   # paddle + ball: these two are you
            self.damage = self._mk(203, curses.A_BOLD)
            self.struct = self._mk(250)                  # ceiling girder, rivets
            self.text_hi = self._mk(231, curses.A_BOLD)
            self.text = self._mk(189)
            self.text_dim = self._mk(103)
            self.accent = self._mk(141)
            self.strata = [[self._mk(c) for c in row] for row in STRATA_256]
        else:
            R, C, Y = curses.COLOR_RED, curses.COLOR_CYAN, curses.COLOR_YELLOW
            W, M, B, G = (curses.COLOR_WHITE, curses.COLOR_MAGENTA,
                          curses.COLOR_BLUE, curses.COLOR_GREEN)
            self.hazard = self._mk(R, curses.A_BOLD)
            self.target = self._mk(C, curses.A_BOLD)
            self.gold = self._mk(Y, curses.A_BOLD)
            self.player = self._mk(W, curses.A_BOLD)
            self.damage = self._mk(R, curses.A_BOLD)
            self.struct = self._mk(W, curses.A_DIM)
            self.text_hi = self._mk(W, curses.A_BOLD)
            self.text = self._mk(W)
            self.text_dim = self._mk(W, curses.A_DIM)
            self.accent = self._mk(M)
            # the luminance ramp becomes BOLD -> normal -> DIM. Bronze is never
            # bold: bold yellow is gold, and gold is the 747's channel alone.
            D, N, X = curses.A_BOLD, 0, curses.A_DIM
            self.strata = [
                [self._mk(M, a) for a in (D, D, N, X, X)],   # C1 violet
                [self._mk(Y, a) for a in (N, N, X, X, X)],   # C2 bronze
                [self._mk(M, a) for a in (N, N, X, X, X)],   # C3 rose
                [self._mk(B, a) for a in (D, D, N, X, X)],   # C4 slate
                [self._mk(G, a) for a in (N, N, X, X, X)],   # C5 moss
            ]

    def _mk(self, fg, attr=0):
        i = self._i
        self._i += 1
        return ipair(i, fg) | attr


# ---------------------------------------------------------------------------
# state protocol — the seamless contract. Unchanged, deliberately: these four
# functions and the exact OSC-2 string are how the hook finds this pane.
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
    sys.stdout.write(f"\033]2;BREAKOUT747-{session or 'free'}\033\\")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# persisted stats — LOCAL ONLY. There is no code path that could transmit this,
# and CI proves it. Delete ~/.747-terminal-games/stats-breakout.json any time.
# ---------------------------------------------------------------------------
STATS_ZERO = {"v": 1, "best_stage": 0, "best_score": 0, "runs": 0, "cleared": False, "eggs": 0}


def stats_path():
    return os.path.join(STATE_DIR, "stats-breakout.json")


def stats_off():
    return os.path.exists(os.path.join(STATE_DIR, "no-stats"))


def load_stats():
    if stats_off():
        return dict(STATS_ZERO)
    try:
        with open(stats_path()) as f:
            d = json.load(f)
        out = dict(STATS_ZERO)
        for k in out:
            if k in d and isinstance(d[k], type(out[k])):
                out[k] = d[k]
        return out
    except Exception:
        return dict(STATS_ZERO)   # corrupt or absent -> zeros, never a traceback


def save_stats(d):
    """Atomic, and only ever on run end — never per frame."""
    if stats_off():
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = stats_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, stats_path())
    except Exception:
        pass


class Game:
    """BREAK-IN.

    Geometry, top to bottom: the CRAWLSPACE band (the chamber above), the
    CEILING (a grey girder studded with cyan hatches), the brick WALL hanging
    below it, open air, the paddle. Destroy a hatch to open a hole, then thread
    the ball back up THROUGH that hole and out the top: that is the BREACH, and
    the whole playfield scrolls down to reveal the next chamber. Forever.

    WORST-CASE TELEGRAPH (§2.7, the translated rule — nothing spawns here, so the
    guarantee is PADDLE REACHABILITY, not reaction time):

      required_traverse / (paddle_speed x window), where the window starts at the
      ball's LAST deflection (the first instant the landing point is knowable) and
      the paddle is wherever it was at that instant. 20,000 flights per chamber,
      chambers 1-20, 400,000 samples, run against this exact step():

        WORST p99 = 0.647 (chamber 7)  ·  GLOBAL MAX = 0.941  ·  GATE p99 <= 0.85

      PASS, 1.31x headroom on p99 — and the single worst flight in 400,000 still
      came in under 1.0, so nothing in the sample was physically unreachable.
      Holds at the ×2.2 speed cap, so the cap stands.
      The paddle's keyboard speed rides the SAME curve as the ball
      (paddle_cps = 70 x (1.0 + 0.08*(c-1))), which is why the ratio does not
      drift as the game accelerates. If a future speed increase broke the number,
      the speed would not increase — the cap is derived, not tuned.
    """

    def __init__(self, stdscr, session, pal):
        self.scr = stdscr
        self.session = session
        self.pal = pal
        self.rng = random.Random()
        self.score = 0
        self.shown = 0.0          # the count-up display value (a number that jumps is
        self.lives = 3            # a number nobody attributes)
        self.chamber = 1
        self.streak = 1           # CLEAN BREACH streak, cap x5
        self.eggs = 0
        self.clean = True         # this chamber taken without losing a life?
        self.manual_play = False  # space overrides the idle pause
        self.idle_drawn = False   # latch: draw the paused overlay ONCE, then zero bytes
        self.last_poll = 0.0
        self.stats = load_stats()
        # juice
        self.hitstop = 0.0
        self.shake = 0.0
        self.shake_x = self.shake_y = 0
        self.debris = []          # [x, y, vx, vy, ttl]
        self.floats = []          # [x, y, text, ttl, attr]
        self.flash = []           # [y, x0, x1, frames]
        self.ceil_flash = 0
        self.ascent = None
        self.reseal_t = 0.0
        self.reseal_x = None
        self.sentry_x = 0.0
        self.sentry_v = 0.0
        self.layout()
        if not self.too_small:
            self.build_chamber()

    # ---- layout -----------------------------------------------------------
    def layout(self):
        self.h, self.w = self.scr.getmaxyx()
        self.too_small = self.h < MIN_H or self.w < MIN_W
        # set before the early return: nothing downstream should ever be able to
        # trip over a half-built geometry
        self.paddle_w = max(5, self.w // 10 - (self.chamber - 1) // 2)
        self.paddle_x = max(0, (self.w - self.paddle_w) // 2)
        if self.too_small:
            return
        self.pf_top = 1             # row 0 is the bar and row h-1 the footer: the
        self.paddle_y = self.h - 2  # HUD owns those two rows and nothing else
        self.pf_h = self.paddle_y - self.pf_top + 1
        self.crawl_h = 2 if self.h >= 16 else 1
        self.crawl_y = self.pf_top
        self.ceil_y = self.crawl_y + self.crawl_h
        self.brick_top = self.ceil_y + 1
        # always leave two clear rows above the paddle so the ball has air
        self.brick_space = max(1, self.paddle_y - self.brick_top - 2)
        self.sentry_y = self.crawl_y + self.crawl_h // 2
        mult = self.speed_mult()
        # the paddle rides the same acceleration curve as the ball (§2.7)
        self.paddle_step = max(3, int(round(70.0 * mult * TICK)))

    def speed_mult(self):
        return min(2.2, 1.0 + 0.08 * (self.chamber - 1))   # cap x2.2 at chamber 16

    # ---- the chamber ------------------------------------------------------
    def build_chamber(self, restore_holes=0):
        c = self.chamber
        self.paddle_w = max(5, self.w // 10 - (c - 1) // 2)
        self.paddle_x = min(max(1, self.paddle_x), max(1, self.w - self.paddle_w - 1))
        self.bricks = {}
        cols = max(1, (self.w - 2) // BRICK_W)
        # Rows start at 5, not 4: the 747 spelled into the wall is a 5-row bitmap
        # and it is the oldest thing in this game. The wall never starts shorter
        # than its own egg.
        want = min(7, GLYPH_ROWS + (c - 1) // 3)
        rows = max(1, min(want, self.brick_space))
        self.brick_rows = rows
        glyph = self.glyph_cells(cols, rows)
        strata = self.pal.strata[(c - 1) % 5]
        for r in range(rows):
            for cc in range(cols):
                x = 1 + cc * BRICK_W
                if x + BRICK_W - 1 >= self.w:
                    continue
                # glyph bricks inherit their ROW colour — the 747 hides in texture,
                # not a loud colour. Camouflage, discovered on a second look.
                self.bricks[(self.brick_top + r, x)] = [strata[min(r, 4)],
                                                        (r, cc) in glyph, False]

        # CHAMBER 1 IS A TUTORIAL DISGUISED AS A LEVEL — hand-authored, never
        # procedural, and no other chamber uses this layout. A chimney is already
        # open above the ball, with a hatch panel at the top of it: the first serve
        # flies straight up it and cracks the ceiling before the player has touched
        # a key. That is the whole game taught without a word.
        #
        # The chimney is cut in the glyph-free column NEAREST the centre, never
        # the centre column itself. The 747 spelled into the wall is centred too,
        # and punching the middle out of it would eat a whole stroke of the "4" —
        # in the one chamber every single player sees. The egg outranks the
        # symmetry: it is the oldest thing in this game.
        chim = None
        if c == 1 and cols >= 3:
            taken = set(cc for (_r, cc) in glyph)
            mid = (self.w // 2 - 1) // BRICK_W
            free = [cc for cc in range(cols) if cc not in taken]
            chim = min(free, key=lambda cc: abs(cc - mid)) if free else mid
            cx = 1 + chim * BRICK_W
            for r in range(rows):
                self.bricks.pop((self.brick_top + r, cx), None)
            # and the paddle starts under the chimney, so the authored serve is
            # exact rather than approximately vertical
            self.paddle_x = max(1, min(self.w - self.paddle_w - 1,
                                       cx + (BRICK_W - 1) // 2 - self.paddle_w // 2))

        # RIVETS from chamber 4: one indestructible brick per row forces angle play.
        if c >= 4 and cols >= 4:
            for r in range(rows):
                for k in range(cols):
                    cc = (r * 3 + c + k) % cols
                    key = (self.brick_top + r, 1 + cc * BRICK_W)
                    b = self.bricks.get(key)
                    if b is not None and not b[1]:     # never eat a 747 brick
                        b[2] = True
                        break

        # THE CEILING: hatch PANELS, and a girder everywhere else.
        # A hatch is a panel, not a speck. Two cells in a hundred-column ceiling
        # is a 10% target and it made the ceiling unhittable — measured, not
        # guessed: an autopilot rallied for 55 s and destroyed nothing. Difficulty
        # comes from FEWER panels as you climb, never from smaller ones.
        hw = max(2, min(8, ((self.w // 12) // 2) * 2))
        self.hatch_w = hw
        n = max(1, 5 - (c - 1) // 2)                   # floor 1 -> c>=9 is an aiming game
        n = min(n, max(1, (self.w - 6) // (hw + 2)))   # >=2 clear cells between panels
        hp = min(4, 1 + (c - 1) // 3)
        if c == 1:
            hp = 2      # authored: crack on the first pass, destroy on the second
        self.hatch_hp = hp
        span = self.w - 6 - hw
        if n > 1:
            xs = [3 + int(span * i / (n - 1)) for i in range(n)]
        else:
            xs = [(self.w - hw) // 2]
        if chim is not None:
            # the centre panel sits exactly on top of the chimney, so the authored
            # first serve cannot miss it
            xs[n // 2] = 1 + chim * BRICK_W + (BRICK_W - 1) // 2 - hw // 2
            xs.sort()
        self.hatches = {}
        for x in xs:
            self.hatches[max(1, min(self.w - hw - 2, x))] = hp
        self.hatch_total = len(self.hatches)
        self.holes = set()
        for _ in range(min(restore_holes, len(self.hatches) - 1)):
            k = sorted(self.hatches)[0]
            del self.hatches[k]
            self.holes.add(k)
        self.reseal_t = 0.0
        self.reseal_x = None
        self.sentry_x = float(self.w // 2)
        self.sentry_v = 13.0 * (1 if c % 2 else -1)
        self.serve(hold=1.2 if c == 1 else 0.6, straight=(c == 1))

    @staticmethod
    def glyph_cells(cols, rows):
        # 5-row bitmap font, "7 4 7" with a 1-col gap between digits (11 cols wide).
        if rows < GLYPH_ROWS:
            return set()          # too short to hold the egg — plain wall
        seven = ["###", "..#", ".#.", ".#.", ".#."]
        four = ["#.#", "#.#", "###", "..#", "..#"]
        grid = [seven[i] + "." + four[i] + "." + seven[i] for i in range(GLYPH_ROWS)]
        gw = len(grid[0])
        start = (cols - gw) // 2
        if start < 0:
            return set()          # too narrow to render the glyph — plain wall
        return {(r, start + i) for r in range(GLYPH_ROWS)
                for i, ch in enumerate(grid[r]) if ch == "#"}

    # ---- serving ----------------------------------------------------------
    def serve(self, hold=0.8, straight=False):
        self.ball_x = self.paddle_x + self.paddle_w / 2.0
        self.ball_y = self.paddle_y - 1.0
        self.vx = self.vy = 0.0
        self.serve_hold = hold
        self.serve_straight = straight
        self.in_crawl = False

    def launch(self):
        m = self.speed_mult()
        if self.serve_straight:
            # the authored opening: straight up the chimney, and fast enough that
            # the ceiling cracks inside the first two seconds
            self.vx, self.vy = 0.0, -16.0
        else:
            self.vx = 11.0 * m * (1 if int(self.ball_x) % 2 else -1)
            self.vy = -7.0 * m
        self.serve_hold = 0.0
        self.serve_straight = False

    # ---- juice ------------------------------------------------------------
    def burst(self, y, x, n=4, attr=None):
        """Debris that PERSISTS 0.4s. A flash you can miss by blinking is not
        feedback; debris still on screen half a second later is proof the world
        reacted to you."""
        a = attr if attr is not None else self.pal.text_dim
        for _ in range(n):
            self.debris.append([float(x), float(y),
                                self.rng.uniform(-9.0, 9.0),
                                self.rng.uniform(-7.0, -1.0), 0.4, a])

    def floater(self, y, x, text, attr=None, ttl=0.45):
        # SLIDE THE ORIGIN, NEVER THE TAIL. A floater is a payoff message, and
        # the payoff is the whole string: "+500 CLEAN BREACH" clipped to
        # "+500 CLEAN BRE" is the title's biggest moment printed as a bug.
        # Clamping here (and again in put(), belt and braces) means no floater
        # in this file can clip, wherever it is fired from.
        x = max(0, min(int(x), self.w - 1 - len(text)))
        self.floats.append([float(x), float(y), text, ttl,
                            attr if attr is not None else self.pal.text_hi])

    def kick(self, amount, frames=4):
        self.shake = max(self.shake, frames * TICK)
        self.shake_amp = amount

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        if self.too_small:
            return None
        self.tick_effects(dt)

        if self.ascent is not None:
            # THE ASCENT. The sim is frozen for the whole scroll — pure payoff.
            # If the state file flips to idle mid-scroll it freezes with everything
            # else, exactly like the rest of the world.
            a = self.ascent
            a["t"] += dt
            if a["t"] >= a["dur"]:
                self.ascent = None
                self.drop_in()
            return None

        if self.hitstop > 0.0:
            self.hitstop -= dt
            return None                      # world stops; effects above kept ticking

        if self.serve_hold > 0.0:
            self.serve_hold -= dt
            self.ball_x = self.paddle_x + self.paddle_w / 2.0
            self.ball_y = self.paddle_y - 1.0
            if self.serve_hold <= 0.0:
                self.launch()
            return None

        self.ball_x += self.vx * dt
        self.ball_y += self.vy * dt

        # side walls
        if self.ball_x <= 1:
            self.ball_x = 1.0
            self.vx = abs(self.vx)
        elif self.ball_x >= self.w - 2:
            self.ball_x = float(self.w - 2)
            self.vx = -abs(self.vx)

        r = self.ceiling(dt)
        if r:
            return r
        self.paddle_bounce()
        self.brick_hits()

        # dropped ball — the ONLY way to lose a life, so every death is legibly
        # the player's own
        if self.ball_y > self.paddle_y:
            self.lives -= 1
            self.streak = 1
            self.clean = False
            self.hitstop = 0.10
            self.kick(2, 6)
            self.burst(self.paddle_y, int(self.ball_x), 5, self.pal.damage)
            if self.lives <= 0:
                return "over"
            self.serve(hold=0.8)
        return None

    def tick_effects(self, dt):
        if self.shake > 0.0:
            self.shake -= dt
            amp = getattr(self, "shake_amp", 1)
            self.shake_x = self.rng.randint(-amp, amp)
            self.shake_y = self.rng.randint(-amp, amp) if amp > 1 else 0
        else:
            self.shake_x = self.shake_y = 0
        if self.ceil_flash > 0:
            self.ceil_flash -= 1
        for f in self.flash:
            f[3] -= 1
        self.flash = [f for f in self.flash if f[3] > 0]
        for d in self.debris:
            d[4] -= dt
            d[0] += d[2] * dt
            d[3] += 34.0 * dt          # gravity
            d[1] += d[3] * dt
        self.debris = [d for d in self.debris if d[4] > 0]
        for f in self.floats:
            f[3] -= dt
            f[1] -= 3.0 * dt           # floats up ~3 rows over its life
        self.floats = [f for f in self.floats if f[3] > 0]
        # the score COUNTS UP so the player can see which action paid
        if self.shown < self.score:
            self.shown = min(float(self.score), self.shown + 400.0 * dt)
        elif self.shown > self.score:
            self.shown = float(self.score)
        # the SENTRY (c>=10) drifts across the crawlspace: movement itself is the tell
        if self.chamber >= 10:
            self.sentry_x += self.sentry_v * dt
            if self.sentry_x < 2:
                self.sentry_x, self.sentry_v = 2.0, abs(self.sentry_v)
            elif self.sentry_x > self.w - 3:
                self.sentry_x, self.sentry_v = float(self.w - 3), -abs(self.sentry_v)
        # the ceiling RESEALS from chamber 7: 12s without touching a hatch and one
        # hole closes, with a full second of blink first so it is never a surprise
        if self.chamber >= 7 and self.holes and self.hatches:
            self.reseal_t += dt
            if self.reseal_t >= 11.0 and self.reseal_x is None:
                self.reseal_x = sorted(self.holes)[0]
            if self.reseal_t >= 12.0 and self.reseal_x is not None:
                self.holes.discard(self.reseal_x)
                self.hatches[self.reseal_x] = self.hatch_hp
                self.floater(self.ceil_y, self.reseal_x, "sealed", self.pal.hazard, 0.7)
                self.reseal_x = None
                self.reseal_t = 0.0

    def hatch_at(self, bx):
        for x in self.hatches:
            if x <= bx < x + self.hatch_w:
                return x
        return None

    def hole_at(self, bx):
        for x in self.holes:
            if x <= bx < x + self.hatch_w:
                return True
        return False

    def ceiling(self, dt):
        """The ceiling, the hole, and the breach. This is the whole title."""
        bx = int(round(self.ball_x))
        if self.in_crawl:
            if self.chamber >= 10 and abs(self.ball_x - self.sentry_x) < 1.6 \
                    and abs(self.ball_y - self.sentry_y) < 0.9:
                # the sentry costs you tempo, never a life
                self.vy = abs(self.vy)
                self.ball_y = self.sentry_y + 0.9
                self.in_crawl = False
                self.kick(1, 3)
                self.burst(self.sentry_y, int(self.sentry_x), 3, self.pal.hazard)
                return None
            if self.ball_y <= self.pf_top - 0.4:
                return self.breach()
            if self.vy > 0 and self.ball_y >= self.ceil_y - 0.4:
                self.in_crawl = False       # fell back out of the hole
            return None
        if self.vy < 0 and self.ball_y <= self.ceil_y + 0.4:
            hx = self.hatch_at(bx)
            if hx is not None:
                self.hit_hatch(hx)
                self.vy = abs(self.vy)
                self.ball_y = self.ceil_y + 0.5
            elif self.hole_at(bx):
                self.in_crawl = True        # BREAKING IN
            else:
                self.vy = abs(self.vy)      # solid girder
                self.ball_y = self.ceil_y + 0.5
        return None

    def hit_hatch(self, x):
        self.reseal_t = 0.0
        self.reseal_x = None
        hp = self.hatches[x] - 1
        self.score += 25 * self.streak
        self.flash.append([self.ceil_y, x, x + self.hatch_w, 2])
        if hp <= 0:
            del self.hatches[x]
            self.holes.add(x)
            self.score += 100 * self.streak
            self.hitstop = 0.07
            self.kick(1, 4)
            self.burst(self.ceil_y, x + self.hatch_w // 2, 5, self.pal.target)
            self.floater(self.ceil_y + 1, x, "+%d" % (100 * self.streak), self.pal.target)
        else:
            self.hatches[x] = hp
            self.burst(self.ceil_y, x + self.hatch_w // 2, 2, self.pal.target)

    def breach(self):
        """You are in. 250, doubled for a clean chamber, +747 every seventh."""
        c = self.chamber
        gain = 250
        label = "BREACH"
        if self.clean:
            gain *= 2
            self.streak = min(5, self.streak + 1)
            label = "CLEAN BREACH"
        if c % 7 == 0:                       # VAULT — the quiet wink, never a banner
            gain += 747
            label = "VAULT"
        self.score += gain
        if c % 5 == 0 and self.lives < 5:
            self.lives += 1
        self.hitstop = 0.08
        self.ceil_flash = 2                  # the flash is on the CEILING ROW only
        self.kick(2, 3)
        for x in list(self.holes) + list(self.hatches):
            self.burst(self.ceil_y, x, 3, self.pal.target)
        self.floater(self.ceil_y + 1, max(0, int(self.ball_x) - 6),
                     "+%d %s" % (gain, label), self.pal.gold, 0.9)
        self.ball_y = float(self.pf_top)     # park it on the row it holds through the scroll
        self.start_ascent()
        return None

    def start_ascent(self):
        old = {"bricks": self.bricks, "hatches": dict(self.hatches),
               "holes": set(self.holes)}
        held_x = self.ball_x
        self.chamber += 1
        self.clean = True
        self.layout()
        self.build_chamber()
        # build_chamber() re-serves; the ball must NOT snap to the paddle here.
        # It holds the row it breached on, all the way through the scroll.
        self.ball_x = min(max(2.0, held_x), float(self.w - 3))
        self.ball_y = float(self.pf_top)
        self.ascent = {"t": 0.0, "dur": 0.6 if self.h >= 12 else 0.3, "old": old,
                       # how far the NEW chamber's drawn content has to fall to
                       # land. The old chamber drops a whole pane height and the
                       # new one only its own height, so the world you are leaving
                       # accelerates away beneath the world you are entering.
                       "ch": self.brick_top + self.brick_rows + 2 - self.pf_top,
                       "ghost": big_text(str(self.chamber))[0]}
        self.serve_hold = 9.9                # held until the scroll lands

    def drop_in(self):
        """The scroll is over — and you START THE NEW CHAMBER FROM THE BOTTOM.

        FOUNDER CALL 2026-07-27, and he is right: the earlier version parked the
        ball high in the fresh chamber, "so the ball never falls down — makes it
        so easy". Arriving at the top hands you the whole wall for free and kills
        the tension at the exact moment it should spike. Instead the climb
        REPEATS: the paddle takes the ball back at the floor and you work your way
        up through the new wall, chamber after chamber. Same verb, higher stakes,
        forever — which is the whole promise of the name."""
        self.serve(hold=0.55)
        self.in_crawl = False

    def paddle_bounce(self):
        if (self.vy > 0 and self.paddle_y - 0.5 <= self.ball_y <= self.paddle_y + 0.6
                and self.paddle_x - 1 <= self.ball_x <= self.paddle_x + self.paddle_w):
            self.vy = -abs(self.vy)
            # english: hit position steers the ball. The greedy read is to angle
            # the return at a hatch instead of the nearest brick.
            rel = (self.ball_x - (self.paddle_x + self.paddle_w / 2)) / (self.paddle_w / 2)
            self.vx = 16.0 * rel + self.vx * 0.25
            if abs(self.vx) < 4:
                self.vx = 4.0 if rel >= 0 else -4.0
            self.ball_y = self.paddle_y - 1.0

    def brick_hits(self):
        by, bx = int(self.ball_y), int(self.ball_x)
        for (r, c) in list(self.bricks):
            if r == by and c <= bx < c + BRICK_W - 1:
                attr, is_glyph, is_rivet = self.bricks[(r, c)]
                if is_rivet:
                    self.vy = -self.vy       # grey never breaks: learned once, never twice
                    self.ball_y = float(r) + (0.6 if self.vy > 0 else -0.6)
                    self.kick(1, 2)
                    return
                del self.bricks[(r, c)]
                # 747 bricks pay 47 (the quiet wink) — no flash, no fanfare.
                self.score += (47 if is_glyph else 10) * self.streak
                if is_glyph:
                    self.eggs += 1
                self.hitstop = max(self.hitstop, 0.03)
                self.flash.append([r, c, c + BRICK_W - 1, 2])
                self.burst(r, c + 2, 4, attr)
                self.vy = -self.vy
                return

    # ---- ghost-pane return ------------------------------------------------
    def on_rejoin(self):
        """A ghosted pane can be gone for minutes. Reset every accumulator a
        wall-clock gap would corrupt, and never resume mid-effect."""
        self.hitstop = 0.0
        self.shake = 0.0
        self.shake_x = self.shake_y = 0
        self.debris = []
        self.floats = []
        self.flash = []
        self.ceil_flash = 0
        self.reseal_t = 0.0
        self.reseal_x = None
        self.shown = float(self.score)
        if self.ascent is None and self.serve_hold <= 0.0:
            self.serve_hold = 0.0
        elif self.ascent is None:
            self.serve_hold = max(self.serve_hold, 0.5)

    # ---- rendering --------------------------------------------------------
    def put(self, y, x, text, attr):
        """Clipped to the playfield. The HUD owns rows 0 and h-1 and nothing
        else may ever write there."""
        if not (self.pf_top <= y <= self.paddle_y):
            return
        # SLIDE THE ORIGIN BEFORE CUTTING THE STRING. The old order clamped
        # only the tail, so anything drawn near the right edge lost its end —
        # which is how the breach payoff shipped as "+500 CLEAN BRE".
        # A string that genuinely cannot fit still gets cut, but only then.
        if len(text) < self.w - 1:
            x = min(x, self.w - 1 - len(text))
        if x < 0:
            text = text[-x:]
            x = 0
        if x >= self.w - 1:
            return
        text = text[: self.w - 1 - x]
        if not text:
            return
        try:
            self.scr.addstr(y, x, text, attr)
        except (curses.error, UnicodeEncodeError):
            pass

    def draw(self, playing):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if (h, w) != (self.h, self.w):
            holes = len(getattr(self, "holes", ()))
            self.layout()
            if not self.too_small:
                self.build_chamber(restore_holes=holes)
            h, w = self.h, self.w
        if self.too_small:
            self.draw_too_small(h, w)
            s.refresh()
            return
        try:
            self.draw_hud(w)
            if self.ascent is not None:
                a = self.ascent
                u = min(1.0, a["t"] / a["dur"])
                self.draw_field(a["old"], int(round(self.pf_h * u)), ghost=None)
                self.draw_field(None, -int(round(a["ch"] * (1.0 - u))), ghost=a["ghost"])
                self.draw_actors(True, world=False)
            else:
                self.draw_field(None, 0, ghost=None)
                self.draw_actors(playing)
            self.draw_effects()
            self.draw_footer(h, w)
            if not playing:
                self.overlay([G_PAUSE + "  CLAUDE'S DONE " + G_DASH + " READING TIME",
                              "resumes on your next prompt %s [space] play anyway" % G_SEP])
        except curses.error:
            pass
        s.refresh()

    def draw_too_small(self, h, w):
        msg = "TERMINAL TOO SMALL %s 80x8 MINIMUM" % G_DASH
        try:
            self.scr.addstr(max(0, h // 2), max(0, (w - len(msg)) // 2),
                            msg[: max(0, w - 1)], curses.A_BOLD)
        except (curses.error, UnicodeEncodeError):
            pass

    def draw_field(self, snap, dy, ghost):
        """One field — bricks, ceiling, hatches, crawlspace — drawn at a row
        offset. dy is what makes the ascent scroll possible: the old chamber
        slides off the bottom while the new one slides in from the top."""
        p = self.pal
        bricks = self.bricks if snap is None else snap["bricks"]
        hatches = self.hatches if snap is None else snap["hatches"]
        holes = self.holes if snap is None else snap["holes"]
        sx, sy = self.shake_x, self.shake_y + dy

        if ghost is not None:
            # a giant dim chamber number ghosting behind the new bricks, in the
            # same 5-row bitmap the wall spells its 747 in
            gw = len(ghost[0])
            gx = (self.w - gw) // 2
            for i, row in enumerate(ghost):
                y = self.ceil_y + 2 + i + sy
                for j, ch in enumerate(row):
                    if ch == "#":
                        self.put(y, gx + j + sx, F_BLK, p.text_dim)

        # the ceiling girder, then the hatches on top of it. Emitted as runs
        # between the openings, not cell by cell — 5 addstr calls, not 98.
        cy = self.ceil_y + sy
        ceil_attr = p.damage if self.ceil_flash > 0 else p.struct
        gap = set()
        for x in list(hatches) + list(holes):
            for k in range(self.hatch_w):
                gap.add(x + k)
        run = 1
        for x in range(1, self.w):
            if x in gap or x == self.w - 1:
                if x > run:
                    self.put(cy, run + sx, G_CEIL * (x - run), ceil_attr)
                run = x + 1

        for x, hp in hatches.items():
            blink = (self.reseal_x == x and snap is None
                     and int(self.reseal_t * 6) % 2 == 0)
            # LESS INK = MORE DAMAGE. The frame is picked by the FRACTION of health
            # left, not by the raw count, so a full hatch always reads full whether
            # this chamber's hatches take one hit or four.
            f = hp / float(max(1, self.hatch_hp))
            idx = 3 if f > 0.75 else 2 if f > 0.5 else 1 if f > 0.25 else 0
            self.put(cy, x + sx, HATCH[idx] * (self.hatch_w // 2),
                     p.damage if blink else p.target)
        # THE DARKNESS IS THE HOLE YOU MADE — crawlspace is drawn only where a
        # hatch has been destroyed
        for x in holes:
            for r in range(self.crawl_y, self.ceil_y):
                self.put(r + sy, x + sx, G_CRAWL * self.hatch_w, p.text_dim)

        for (r, c), (attr, is_glyph, is_rivet) in bricks.items():
            if is_rivet:
                self.put(r + sy, c + sx, G_RIVET * (BRICK_W - 1), p.struct)
            else:
                # the 747 hides in TEXTURE: glyph bricks are full blocks in their
                # row's own colour — a second-look discovery, never a banner
                self.put(r + sy, c + sx, (G_EGG if is_glyph else G_BRICK) * (BRICK_W - 1),
                         attr | curses.A_BOLD)

        for (y, x0, x1, _f) in self.flash:
            self.put(y + sy, x0 + sx, " " * max(1, x1 - x0), curses.A_REVERSE)

    def draw_actors(self, playing, world=True):
        """The paddle NEVER scrolls — it stays glued to the bottom through the
        ascent, and the ball holds its screen position, so the only thing that
        moves during the breach is the world."""
        p = self.pal
        sx, sy = self.shake_x, self.shake_y
        if world and self.chamber >= 10:
            self.put(self.sentry_y + sy, int(self.sentry_x) + sx, G_SENTRY, p.hazard)
        self.put(self.paddle_y + sy, max(0, self.paddle_x + sx),
                 G_PADDLE * self.paddle_w, p.player)
        if playing or self.serve_hold > 0:
            self.put(int(self.ball_y) + sy, int(self.ball_x) + sx, G_BALL, p.player)

    def draw_effects(self):
        for d in self.debris:
            self.put(int(d[1]), int(d[0]), G_DEBRIS, d[5])
        for f in self.floats:
            self.put(int(f[1]), int(f[0]), f[2], f[4] | curses.A_BOLD)

    # ---- HUD --------------------------------------------------------------
    def draw_hud(self, w):
        """Two rows, forever. Fixed columns so the eye lands in the same place
        every time, and the primary integer is a fixed-width field: a score whose
        digits reflow makes the whole bar shudder on every 10x."""
        p, s = self.pal, self.scr
        R = curses.A_REVERSE

        def put(x, txt, attr=0):
            if 0 <= x < w - 1 and txt:
                try:
                    s.addstr(0, x, txt[: w - 1 - x], attr | R)
                except (curses.error, UnicodeEncodeError):
                    pass

        try:
            s.addstr(0, 0, " " * (w - 1), R)
        except curses.error:
            pass
        score = "%7d" % int(self.shown)
        hearts = (G_HEART * self.lives) if self.lives <= 5 else (G_HEART + "x%d" % self.lives)
        if w < 46:
            # narrowest rung: the number you are chasing, and your lives
            put(0, score, p.text_hi)
            put(8, hearts, p.player)
            return
        put(0, G_TICK, p.accent)
        put(2, "BREAK-IN" if w >= 62 else "BRK", p.text_hi)
        put(12, G_SEP, p.text_dim)
        put(14, score, p.text_hi)
        put(22, hearts, p.player)
        put(28, "C %-2d" % self.chamber, p.text)
        done = len(self.holes)
        total = max(1, self.hatch_total)
        if w >= 62 and total <= 7:
            put(35, "BREACH ", p.text_dim)
            put(42, G_ON * done + G_OFF * max(0, total - done), p.target)
        else:
            put(35, "B %d/%d" % (done, total), p.target)
        if self.streak > 1:
            put(50, G_MULT + "%d" % self.streak, p.gold)
        keys = " %s %s [space] %s [q] quit " % (G_ARROWS, G_SEP, G_SEP)
        if w - len(keys) - 1 >= 57:          # 62-77 cols: the keys block is the first
            put(w - len(keys) - 1, keys, p.text_dim)   # thing to go

    def draw_footer(self, h, w):
        p = self.pal
        best = max(self.stats["best_stage"], self.chamber)
        left = " CHAMBER %d %s BEST %d" % (self.chamber, G_SEP, best)
        tag = "THE 747 LAB "
        try:
            self.scr.addstr(h - 1, 0, left[: max(0, w - len(tag) - 2)], p.text_dim)
            self.scr.addstr(h - 1, max(0, w - len(tag) - 1), tag, p.text_dim)
        except (curses.error, UnicodeEncodeError):
            pass

    def overlay(self, lines):
        h, w = self.scr.getmaxyx()
        for i, ln in enumerate(lines):
            y = h // 2 - 1 + i
            x = max(0, (w - len(ln)) // 2)
            try:
                self.scr.addstr(y, x, ln[: w - 1], curses.A_BOLD)
            except (curses.error, UnicodeEncodeError):
                pass

    # ---- input ------------------------------------------------------------
    def handle_key(self, ch):
        if ch in (ord("q"), ord("Q")):
            return "quit"
        if ch == ord(" "):
            self.manual_play = not self.manual_play
            self.idle_drawn = False
        elif self.too_small:
            return None
        elif ch in (curses.KEY_LEFT, ord("a")):
            self.paddle_x = max(1, self.paddle_x - self.paddle_step)
        elif ch in (curses.KEY_RIGHT, ord("d")):
            self.paddle_x = min(self.w - self.paddle_w - 1,
                                self.paddle_x + self.paddle_step)
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, _, _, _ = curses.getmouse()
                self.paddle_x = max(1, min(self.w - self.paddle_w - 1,
                                           mx - self.paddle_w // 2))
            except curses.error:
                pass
        return None

    # ---- run end ----------------------------------------------------------
    def commit_stats(self):
        st = self.stats
        st["runs"] += 1
        st["best_stage"] = max(st["best_stage"], self.chamber)
        st["best_score"] = max(st["best_score"], self.score)
        st["eggs"] += self.eggs
        st["cleared"] = st["cleared"] or self.chamber > 1
        save_stats(st)


def game_over_screen(scr, game, session):
    """There is no end — winning is DEPTH. So the game-over screen states the
    depth flatly, and the number is the only thing on it that matters."""
    scr.nodelay(False)
    scr.timeout(200)          # every blocking screen polls for 'end'
    best = max(game.stats["best_stage"], game.chamber)
    lines = ["CHAMBER %d REACHED %s BEST %d" % (game.chamber, G_SEP, best),
             "SCORE %d" % game.score,
             "",
             # [m] is only on the line when it is actually wired (see
             # menu_available): an offered key that does nothing is worse than
             # no key at all.
             ("[r] play again %s [m] menu %s [q] close" % (G_SEP, G_SEP))
             if menu_available() else "[r] play again %s [q] close" % G_SEP]
    dirty = True
    while True:
        if dirty:
            scr.erase()
            h, w = scr.getmaxyx()
            for i, ln in enumerate(lines):
                try:
                    scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln[: w - 1],
                               curses.A_BOLD if i == 0 else curses.A_NORMAL)
                except (curses.error, UnicodeEncodeError):
                    pass
            scr.refresh()
            dirty = False
        ch = scr.getch()
        if ch in (ord("r"), ord("R")):
            return True
        if ch in (ord("m"), ord("M")) and menu_available():
            back_to_menu(session)                 # never returns
        if ch in (ord("q"), ord("Q")):
            return False
        if ch != -1:
            dirty = True
        if read_state(session) == "end":
            return False


# Same 5-row bitmap font the brick wall uses for its 747 — the welcome, the wall
# and the ascent all speak one language. Digits beyond 7 and 4 exist so the
# chamber number can ghost in behind the next wall.
FLYBY_FONT = {
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "E": ["###", "#..", "###", "#..", "###"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "A": [".#.", "#.#", "###", "#.#", "#.#"],
    "B": ["##.", "#.#", "##.", "#.#", "##."],
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", "###", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    " ": [".", ".", ".", ".", "."],
}


def big_text(s):
    """Render s in the 5-row font. Returns (rows, per-char column spans for coloring)."""
    rows, spans, x = ["", "", "", "", ""], [], 0
    for ch in s:
        g = FLYBY_FONT.get(ch, FLYBY_FONT[" "])
        spans.append((ch, x, x + len(g[0])))
        for i in range(5):
            rows[i] += g[i] + "."
        x += len(g[0]) + 1
    return [r[:-1] for r in rows], spans


def welcome_flyby(scr, session=""):
    """First-run title flyby — a 7.47s three-act cinematic in the donut.c /
    ANSI-textmode lineage. You are the CAMERA: the letters of THE 747 LAB are
    giant monuments spaced out in z-depth along a flight path, and you fly POV
    straight through the corridor. Letters loom out of a vanishing point, swing
    past on the left and right, and occlude each other via a per-cell z-buffer
    (nearer wins) — real perspective, in a terminal. Stars warp radially; a
    dawn sky evolves indigo->amber across the run; then THE 747 LAB resolves
    dead-ahead like the Fox monument, a glint crosses it, "welcome to the lab",
    hard cut to the ask. Any key skips instantly at any frame. Tiny panes skip
    (reliable-or-silent). 256->16 color, never crashes. No full-field flash."""
    rows, spans = big_text("THE 747 LAB")
    sw, sh = len(rows[0]), 5
    h, w = scr.getmaxyx()
    if h < 10 or w < sw + 8:                    # reliable-or-silent
        return

    try:
        has256 = curses.COLORS >= 256
    except (curses.error, AttributeError):
        has256 = False

    _pi = [30]

    def mk(fg, bold=False):
        i = _pi[0]
        _pi[0] += 1
        a = ipair(i, fg)
        return a | curses.A_BOLD if bold else a

    if has256:
        # ROYAL: a deep violet/indigo sky; GOLD is reserved for the 747 and the
        # ceremony beats. Rose/sunset bands dropped — regal, not cute.
        SKY = [mk(c) for c in (16, 17, 18, 54, 55, 56, 92, 60, 97, 137, 178)]
        STAR = [mk(255, True), mk(251), mk(60)]            # bright / mid / dim-violet
        SUN = mk(220, True)                                # the crown light
        # letters by DEPTH: near = platinum, far cools into violet (ramp = depth)
        CHROME = [mk(231, True), mk(253), mk(146), mk(60)]
        GOLD = [mk(226, True), mk(220), mk(178), mk(136)]  # the 747 + ceremony
        GLINT_W, GLINT_G = mk(231, True), mk(226, True)
        BAR = mk(236)                                      # cinema letterbox
        TAG_DIM, TAG = mk(97), mk(189)                     # quiet violet -> pale lilac
    else:
        white, gold, mag = cpair(6), cpair(8), cpair(5)
        SKY = [curses.A_DIM] * 11
        STAR = [curses.A_NORMAL, curses.A_DIM, curses.A_DIM]
        SUN = gold | curses.A_BOLD
        CHROME = [white | curses.A_BOLD, white, white | curses.A_DIM,
                  mag | curses.A_DIM]
        GOLD = [gold | curses.A_BOLD, gold, gold | curses.A_DIM,
                gold | curses.A_DIM]
        GLINT_W, GLINT_G = white | curses.A_BOLD, gold | curses.A_BOLD
        BAR = curses.A_DIM
        TAG_DIM, TAG = curses.A_DIM, curses.A_NORMAL

    # ---- geometry ----
    bar = 2 if h >= 18 else 1
    sky_top, horizon = bar, h - bar - 1
    cx = (w - sw) // 2                           # resolve-sign left edge
    top = sky_top + max(1, ((horizon - sky_top) - sh) // 2 - 1)
    if top + sh + 1 > horizon:
        top = max(sky_top, horizon - sh - 1)
    tag_row = min(horizon - 1, top + sh + 1)
    vcx, vcy = w // 2, (sky_top + horizon) // 2  # vanishing point (camera axis)

    rng = random.Random(747)
    # starfield: sparse points drifting radially (2001-void — NO line debris).
    # each: (cos, sin*aspect, phase, speed, base-brightness-tier)
    NST = 16
    warp = []
    for _ in range(NST):
        th = rng.uniform(0, 2 * math.pi)
        bt = 2 if rng.random() < 0.62 else (1 if rng.random() < 0.6 else 0)
        warp.append((math.cos(th), math.sin(th) * 0.5, rng.random(),
                     0.6 + rng.random() * 0.9, bt))

    # ---- the letter corridor: each glyph a billboard at (Xw, Zw) in world space,
    #      alternating sides (BOTH walls) so the frame reads as a tunnel of monuments.
    LETTERS = [c for c in "THE 747 LAB" if c != " "]
    NL = len(LETTERS)
    Z0, DZ = 14.0, 5.0
    Zw = [Z0 + i * DZ for i in range(NL)]
    Xw = [(-1 if i % 2 == 0 else 1) * (4.0 + (i % 3)) for i in range(NL)]
    FOCAL = 15.0
    NEAR = 1.6
    ZMAX = Zw[-1] + NEAR + 0.5                    # camera z where last letter has passed

    # ---- per-cell z-buffer compositor (nearer z wins) ----
    INF = 1e9
    zb = [[INF] * w for _ in range(h)]
    cb = [[None] * w for _ in range(h)]
    ab = [[0] * w for _ in range(h)]

    def put(y, x, ch, attr, z):
        if 0 <= y < h and 0 <= x < w and z < zb[y][x]:
            zb[y][x] = z
            cb[y][x] = ch
            ab[y][x] = attr

    def depth_tier(depth):
        return 0 if depth < 6 else 1 if depth < 15 else 2 if depth < 28 else 3

    # Each monument lives in exactly TWO clean states — a dim POINT far away, or
    # a crisp k>=2 MONUMENT up close. It fades through the awkward mid-scale band
    # as a point (never as a fragment of floating strokes). No "alien text".
    S_LO, S_HI = 1.9, 2.7                        # monument threshold / full-brightness
    POINT_MAX_DEPTH = 14.0                       # only the next 1-2 letters glint as points

    def draw_letter(ch, scx, scy, depth):
        pal = GOLD if ch in "74" else CHROME
        s = FOCAL / depth                        # continuous scale
        gx0, gy0 = int(round(scx)), int(round(scy))
        if s < S_LO:                             # FAR: a single clean point, or nothing
            if depth <= POINT_MAX_DEPTH:
                put(gy0, gx0, F_DOT, pal[2] if s > S_LO * 0.7 else pal[3], depth)
            return
        # NEAR: a crisp monument. Emerge dim->bright across [S_LO,S_HI] (no pop).
        if s < S_HI:
            attr = pal[3] if (s - S_LO) < (S_HI - S_LO) * 0.5 else pal[2]
        else:
            attr = pal[depth_tier(depth)]
        g = FLYBY_FONT.get(ch, FLYBY_FONT[" "])
        gw = len(g[0])
        W, H = gw * s, 5 * s                      # float cell extent -> grows 1 cell/step
        ox, oy = scx - W / 2.0, scy - H / 2.0
        for yy in range(int(math.floor(oy)), int(math.ceil(oy + H))):
            sry = (yy + 0.5 - oy) / s
            if sry < 0 or sry >= 5:
                continue
            line = g[int(sry)]
            for xx in range(int(math.floor(ox)), int(math.ceil(ox + W))):
                srx = (xx + 0.5 - ox) / s
                if 0 <= srx < gw and line[int(srx)] == "#":
                    put(yy, xx, F_BLK, attr, depth)

    FLYBY_T = 7.47          # the runtime is part of the signature (7-4-7)
    A1_END, A2_END = 1.5, 5.0                     # approach / fly-through / resolve
    scr.nodelay(True)
    t0 = time.time()
    z_prev = 0.0
    frame_n = 0
    while True:
        now = time.time() - t0
        if scr.getch() != -1:                     # any key -> INSTANT skip, every frame
            break
        if now >= FLYBY_T:
            break
        frame_n += 1
        if frame_n % 25 == 0 and read_state(session) == "end":
            return                                # session over mid-intro -> exit now, not in 7s

        # ONE velocity spline for the whole flight — drift -> surge -> glide-to-rest
        # as a single breath. smootherstep has zero velocity AND accel at both
        # ends, so there is no seam at any act boundary; act 3 parks seamlessly.
        u = min(1.0, now / A2_END)
        z_cam = ZMAX * (u * u * u * (u * (u * 6 - 15) + 10))
        cam_v = max(0.0, z_cam - z_prev)
        z_prev = z_cam
        dawn = min(1.0, now / (FLYBY_T * 0.8))    # sky warms across the whole run
        skyfade = min(1.0, now / 0.8)

        for y in range(h):                        # reset buffers
            zb[y] = [INF] * w
            cb[y] = [None] * w
            ab[y] = [0] * w

        # --- the void is BLACK to the horizon. ONE clean warm glow sits low and
        #     centred — a smooth, coherent band (NO speckle), the single light in
        #     the void, rising across the run. Interstellar, not a particle storm. ---
        nb = 1 + int(dawn * 1.5)
        for r in range(max(sky_top, horizon - nb), horizon + 1):
            rowfac = 1.0 - (horizon - r) * 0.40
            for x in range(w):
                d = abs(x - vcx) / (w * 0.34)            # centred, soft falloff
                it = rowfac * max(0.0, 1.0 - d * d) * (0.35 + 0.65 * dawn) * skyfade
                if it > 0.62:
                    put(r, x, F_SH3, SUN, 110)
                elif it > 0.36:
                    put(r, x, F_SH2, SKY[-1], 110)
                elif it > 0.16:
                    put(r, x, F_SH1, SKY[-2], 110)

        # --- starfield: SPARSE points drifting radially outward. Pure '·' — one
        #     consistent, elegant language, no line debris. Fades in at the centre
        #     and out at the rim; at peak velocity a single trailing dot = motion. ---
        surge = cam_v > 0.33
        for ux, uy, base, spd, bt in warp:
            rr = (base + z_cam * 0.045 * spd) % 1.0
            if rr < 0.08 or rr > 0.92:            # unseen at spawn and before wrap
                continue
            rr2 = rr * rr                          # accelerate toward the rim
            sx = vcx + ux * rr2 * (w * 0.62)
            sy = vcy + uy * rr2 * (h * 1.15)
            tier = 2 if (rr < 0.2 or rr > 0.82) else bt
            put(int(round(sy)), int(round(sx)), F_DOT, STAR[tier], 90)
            if surge and rr > 0.45:                # one fading trailing dot (same char)
                put(int(round(sy - uy * 1.6)), int(round(sx - ux * 1.6)),
                    F_DOT, STAR[2], 91)

        # --- ACT 1-2: fly the POV corridor (letters loom, pass, occlude) ---
        if now < A2_END:
            for i in range(NL):
                depth = Zw[i] - z_cam
                if depth <= NEAR:                 # this monument is now behind us
                    continue
                scx = vcx + (Xw[i] * FOCAL) / depth
                scy = vcy
                draw_letter(LETTERS[i], scx, scy, depth)

        # --- ACT 3: the monument GLIDES into its lockup, then holds as ceremony ---
        if now >= A2_END:
            asm = min(1.0, (now - A2_END) / 0.55)
            asm = asm * asm * (3 - 2 * asm)                 # eased converge
            # one slow, singular glint — light crossing a crown (soft 3-cell band)
            gcol = None
            g0, g1 = A2_END + 0.75, FLYBY_T - 0.25
            if g0 <= now <= g1:
                gcol = ((now - g0) / (g1 - g0)) * (sw + 2) - 1
            for i, row in enumerate(rows):
                ry = top + i
                if not (sky_top <= ry <= horizon):
                    continue
                for ch_, x0, x1 in spans:
                    is_gold = ch_ in "74"
                    pal = GOLD if is_gold else CHROME
                    attr = pal[0] if asm > 0.6 else pal[2]  # dim -> full as it seats
                    off = ((x0 + (x1 - x0) / 2.0) - sw / 2.0) * 0.7 * (1 - asm)
                    for j, c in enumerate(row[x0:x1]):
                        if c != "#":
                            continue
                        xp = int(round(cx + x0 + j + off))   # letters fan in, eased
                        a = attr
                        if gcol is not None and abs((x0 + j) - gcol) <= 1:
                            a = GLINT_G if is_gold else GLINT_W
                        put(ry, xp, F_BLK, a, -10.0)
            if asm >= 1.0:                                   # seat opaque; kill bleed
                for i in range(sh):
                    ry = top + i
                    if sky_top <= ry <= horizon:
                        for bx in range(max(0, cx), min(w, cx + sw)):
                            if zb[ry][bx] > -9.0:
                                put(ry, bx, " ", curses.A_NORMAL, -9.0)
            if now >= A2_END + 0.8:                          # tagline: quiet fade in
                sub = "welcome to the lab"
                sx0 = max(0, (w - len(sub)) // 2)
                tg = TAG if now >= A2_END + 1.15 else TAG_DIM
                for k, chc in enumerate(sub):
                    put(tag_row, sx0 + k, chc, tg, -12.0)

        # --- cinema letterbox: thin dim bars through the flight, RELEASED at the
        #     resolve. Only drawn where they can't crowd the sign/tagline rows. ---
        if now < A2_END:
            for rb in (sky_top - 1, h - 1):
                if 0 <= rb < h and (rb < top - 1 or rb > tag_row + 1):
                    for bx in range(w):
                        put(rb, bx, F_BAR, BAR, 130)

        # --- blit the z-buffer (run-length per row for speed) ---
        scr.erase()
        for y in range(h):
            rc, raw = cb[y], ab[y]
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
                if y == h - 1 and j >= w:          # avoid bottom-right cell error
                    s = s[:-1]
                if s:
                    try:
                        scr.addstr(y, x, s, a)
                    except (curses.error, UnicodeEncodeError):
                        pass
                x = j
        scr.refresh()
        time.sleep(0.02)                          # ~50fps for a smoother glide

    scr.nodelay(False)                            # hard cut straight to the ask


# ---------------------------------------------------------------------------
# game picker — shown right after the intro. Founder: "on launch after the intro
# we need to allow the user to SELECT the game, and give them the available
# options". Titles are DISCOVERED from the games/ directory, so a new title
# appears here automatically the day it lands.
# ---------------------------------------------------------------------------
# THE LINE. Display names carry NO "747" suffix — the 747 lives INSIDE each game
# (texture, structure, egg), never on the label. The key column is the file key AND
# the pane-title key, and it is frozen: renaming a key orphans any pane already
# ghosted under the old OSC-2 title. Order is the line order.
# Blurbs carry the separator as the token {sep}, never a literal "·": CATALOGUE is
# built at import time, but use_ascii() rebinds G_SEP later, so a baked-in glyph
# would survive into the mono/ASCII render as the one non-ASCII byte on screen.
CATALOGUE = [
    ("breakout", "BREAK-IN", "endless ascent {sep} break into the chamber above"),
    # Blurbs name SHAPES and verbs, never hues: the picker is the first thing a
    # mono / 16-colour pane draws, and "shoot cyan, dodge red" on a screen with
    # no cyan and no red teaches the game in a vocabulary it is not speaking.
    ("skyrun",   "SKYRUN",   "POV space run {sep} 7 sectors, shoot craft, dodge rock"),
    ("jetwash",  "JETWASH",  "side-on sky runner {sep} one button, 7,470 m"),
    ("astros",   "ASTROS",   "invaders {sep} 7 waves, then the big one"),
    ("jaywalk",  "JAYWALK",  "cross the road {sep} ride the river, fill all seven bays"),
]


def available_titles():
    """Only offer titles that actually exist next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for key, name, blurb in CATALOGUE:
        if os.path.exists(os.path.join(here, key + ".py")):
            out.append((key, name, blurb))
    return out


def picker_screen(scr, session):
    """Returns a title key to play, or None if the user declined.

    Also REMEMBERS the choice in the state dir, so the hook auto-opens the game
    they actually like next time instead of always defaulting to breakout."""
    titles = available_titles()
    if len(titles) < 2:
        return titles[0][0] if titles else "breakout"
    scr.nodelay(False)
    scr.timeout(1000)
    sel = 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        # ROW h-1 BELONGS TO THE BRAND TAG, so the menu may never use it.
        # At exactly 80x8 the old layout put the help line on row 7 and the tag
        # on row 7, and "[o] never ask" came out as "THE 747 LAB r ask" — at the
        # geometry a real tmux split lands at most often. Rows are tagged by
        # ROLE, never by index, so dropping a spacer can never shift the
        # selection highlight onto the wrong line.
        head = [("CHOOSE YOUR GAME", "head")]
        items = []
        for i, (key, name, blurb) in enumerate(titles):
            mark = ">" if i == sel else " "
            # .replace, not %/format: it cannot raise on a blurb that has no token.
            items.append(("%s [%d]  %-10s %s"
                          % (mark, i + 1, name, blurb.replace("{sep}", G_SEP)),
                          "sel" if i == sel else "item"))
        # The help line is FUNCTIONAL, not flavour: it is the only place "[o]
        # never ask" is ever offered. Pick the longest wording that fits whole
        # rather than let the last option get clipped off a narrow pane.
        hs = ["[1-%d] or arrows + enter   %s   [a] always auto-open   %s   [o] never ask"
              % (len(titles), G_SEP, G_SEP),
              "[1-%d] or arrows %s [a] always %s [o] never ask" % (len(titles), G_SEP, G_SEP),
              "[1-%d] %s [a] always %s [o] never" % (len(titles), G_SEP, G_SEP),
              "[1-%d] [a] [o]" % len(titles)]
        help_ln = [(next((s for s in hs if len(s) <= w - 1), hs[-1]), "help")]
        avail = max(1, h - 1)
        rows = head + [("", "gap")] + items + [("", "gap")] + help_ln
        if len(rows) > avail:                      # 80x8: drop the spacers first
            rows = head + items + help_ln
        if len(rows) > avail:                      # still short: the help goes
            rows = head + items
        rows = rows[:avail]
        top = max(0, min((avail - len(rows)) // 2, avail - len(rows)))
        # ONE common left margin for the whole block. Centring each row by its own
        # length makes the menu stagger, which reads as sloppy.
        block = max(len(r[0]) for r in rows)
        left = max(0, (w - block) // 2)
        for i, (ln, kind) in enumerate(rows):
            try:
                at = curses.A_BOLD if kind in ("head", "sel") else curses.A_NORMAL
                x = max(0, (w - len(ln)) // 2) if kind == "head" else left
                room = max(0, w - 1 - x)
                if len(ln) > room and " " in ln[:room]:
                    ln = ln[:room].rsplit(" ", 1)[0]   # never clip a word in half
                scr.addstr(top + i, x, ln[:room], at)
            except curses.error:
                pass
        try:
            scr.addstr(h - 1, max(0, w - 22), "THE 747 LAB ", curses.A_DIM)
        except curses.error:
            pass
        scr.refresh()
        ch = scr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(titles)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(titles)
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            return remember_title(titles[sel][0])
        elif ord("1") <= ch <= ord("9"):
            i = ch - ord("1")
            if i < len(titles):
                return remember_title(titles[i][0])
        elif ch in (ord("a"), ord("A")):
            write_mode("auto")
            return remember_title(titles[sel][0])
        elif ch in (ord("o"), ord("O")):
            write_mode("off")
            return None
        elif ch in (ord("n"), ord("N"), ord("q"), ord("Q"), 27):
            return None
        elif read_state(session) == "end":
            return None


def remember_title(key):
    """Persist the pick so the hook opens THIS game next time."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "game"), "w") as f:
            f.write(key + "\n")
    except Exception:
        pass
    return key


def launch_title(key, session, picker=False):
    """Hand off to another title in THIS pane — same process slot, so the ghost
    pane / pause / resume contract keeps working exactly as before.

    EVERY flag this process was launched with has to survive the trip, or the
    handoff silently changes the next title's contract: a --free pane that
    forgets --free starts obeying Claude's state and freezes on the first reply.
    `picker` re-enters the menu directly, skipping the 7.47s intro."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, key + ".py")
    argv = [sys.executable, path]
    if picker:
        argv.append("--picker")
    # --session stays ahead of any trailing flag: the hook's pgrep/pkill pattern
    # is "<title>\.py.*--session <key>", and the pane it banishes is matched on it.
    argv += ["--session", session]
    if getattr(LAUNCH_ARGS, "free", False):
        argv.append("--free")
    curses.endwin()
    # execv never returns, so __main__'s `finally` that turns mouse reporting back
    # off never runs. Titles that use no mouse at all (jetwash) would then inherit
    # ?1003h and leave it enabled in the user's shell on exit. Disarm it here.
    try:
        sys.stdout.write("\033[?1003l")
        sys.stdout.flush()
    except Exception:
        pass
    os.execv(sys.executable, argv)


def menu_available():
    """Only offer [m] when there is actually a menu to go back to. A one-title
    install has nothing to choose between, and picker_screen short-circuits."""
    return len(available_titles()) > 1


def back_to_menu(session):
    """[m] on the game-over screen: hand this pane back to the picker. Same
    os.execv slot as any other title handoff — never returns."""
    launch_title("breakout", session, picker=True)


def ask_screen(scr, session):
    """Returns True to play. Handles n (this session), a (always), o (off)."""
    scr.nodelay(False)
    scr.timeout(1000)
    start = time.time()
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [
            "PLAY WHILE CLAUDE THINKS?",
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
                open(os.path.join(STATE_DIR, f"declined-{session}"), "w").close()
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
                open(os.path.join(STATE_DIR, f"declined-{session}"), "w").close()
            return False


def main(stdscr, args):
    try:
        run(stdscr, args)
    finally:
        # THE GAME OWNS ITS OWN STATE FILE. Every exit path has to clean it up,
        # not just the one in the play loop — 'end' can also land on the intro,
        # the picker, the ask screen or the game-over screen, and each of those
        # returns straight out. Leaving the file behind makes the next launch
        # think a session is still running.
        if read_state(args.session) == "end":
            remove_state(args.session)


def run(stdscr, args):
    # EVERY terminal-capability call is guarded INDIVIDUALLY. On a terminfo entry
    # without a cursor-visibility or colour capability (TERM=vt100 — which is
    # exactly the mono/16-colour case the ASCII fallback exists to serve) these
    # raise, and an unguarded one is a hard crash on launch. One failing
    # capability must cost that capability, never the game.
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()
        curses.use_default_colors()
    except (curses.error, ValueError):
        pass
    # pairs 1-8 are the legacy set; the welcome flyby's 16-colour branch reads
    # them by index, so they stay exactly where they were. ipair() is guarded
    # per pair, so a terminal that runs out of pairs halfway keeps the ones it
    # managed to allocate instead of losing the whole set.
    for i, col in enumerate([curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                             curses.COLOR_CYAN, curses.COLOR_MAGENTA], start=1):
        ipair(i, col)
    ipair(6, curses.COLOR_WHITE)
    ipair(7, curses.COLOR_YELLOW)
    ipair(8, curses.COLOR_YELLOW)                 # gold — the 747 glyph
    pal = Palette()                               # roles live in the 100-139 band
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1003h", end="", flush=True)  # mouse motion tracking
    except (curses.error, OSError):
        pass

    # --picker is the RETURNING player's door: straight to the menu, no flyby.
    # Somebody who just finished a run has already sat through the 7.47s intro,
    # and making them sit it again to switch games is the whole reason [m] exists.
    if args.ask or args.picker:
        if args.ask:
            welcome_flyby(stdscr, args.session)
        pick = picker_screen(stdscr, args.session)
        if pick is None:
            return
        if pick != "breakout":
            launch_title(pick, args.session)      # never returns

    while True:  # restart loop
        game = Game(stdscr, args.session, pal)
        game.manual_play = args.free  # manual launch: play regardless of Claude's state
        stdscr.nodelay(True)
        last = time.time()
        state = "thinking"
        while True:
            now = time.time()
            raw, last = now - last, now
            poll_every = STATE_POLL if not game.idle_drawn else POLL_IDLE
            if now - game.last_poll > poll_every:
                game.last_poll = now
                state = read_state(args.session)
            if state == "end":
                game.commit_stats()
                remove_state(args.session)  # session over — clean up our own state file
                return
            playing = state == "thinking" or game.manual_play

            ch = stdscr.getch()
            while ch != -1:
                if game.handle_key(ch) == "quit":
                    game.commit_stats()
                    return
                ch = stdscr.getch()
                playing = state == "thinking" or game.manual_play

            if not playing:
                # PAUSED. Freeze the sim on this frame, draw the overlay ONCE, then
                # zero bytes on the wire — a ghost pane that keeps redrawing is a
                # bug even though nobody can see it.
                if not game.idle_drawn:
                    game.draw(False)
                    game.idle_drawn = True
                time.sleep(IDLE_SLEEP)
                continue
            if game.idle_drawn:
                # back from the ghost: never simulate across the gap
                game.on_rejoin()
                game.idle_drawn = False
                raw = 0.0

            dt = 0.0 if raw > DT_REJOIN else min(raw, DT_MAX)
            result = game.step(dt)
            game.draw(True)
            if result == "over":
                game.commit_stats()
                if game_over_screen(stdscr, game, args.session):
                    break  # restart
                return
            time.sleep(TICK)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ask", action="store_true")
    p.add_argument("--picker", action="store_true",
                   help="go straight to the game menu, skipping the intro")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    LAUNCH_ARGS = args          # module scope: back_to_menu()/launch_title() read it
    if ascii_wanted():
        use_ascii()
    os.makedirs(STATE_DIR, exist_ok=True)
    set_pane_title(args.session)
    try:
        curses.wrapper(main, args)
    finally:
        print("\033[?1003l", end="", flush=True)
