#!/usr/bin/env python3
"""BREAKOUT 747 — in-terminal game that runs while Claude thinks.

Lives in a tmux pane split below the Claude Code session. Auto-pauses when
Claude finishes a turn (Stop hook writes 'idle'), resumes on the next prompt
(UserPromptSubmit writes 'thinking'). Exits when the session ends.

Developed by The 747 Lab.
"""
import argparse
import curses
import os
import sys
import time

STATE_DIR = os.environ.get("BREAKOUT747_STATE") or os.path.expanduser("~/.747-terminal-games")
TICK = 0.033          # ~30 fps
STATE_POLL = 0.2      # how often to re-read the state file
ASK_TIMEOUT = 45      # ask screen auto-closes after this many seconds

BRICK_W = 6
BRICK_ROWS = 5


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


def set_pane_title():
    # OSC 2 sets the tmux pane title so the launcher can detect a live game
    sys.stdout.write("\033]2;BREAKOUT747\033\\")
    sys.stdout.flush()


class Game:
    def __init__(self, stdscr, session):
        self.scr = stdscr
        self.session = session
        self.score = 0
        self.lives = 3
        self.level = 1
        self.paused_by_idle = False
        self.manual_play = False   # space overrides the idle pause
        self.last_poll = 0.0
        self.reset_field(full=True)

    # ---- layout -----------------------------------------------------------
    def reset_field(self, full=False):
        self.h, self.w = self.scr.getmaxyx()
        self.paddle_w = max(8, self.w // 10)
        self.paddle_x = (self.w - self.paddle_w) // 2
        self.paddle_y = self.h - 2
        self.serve()
        if full:
            self.build_bricks()

    def build_bricks(self):
        # bricks[(row,col)] = [color, is_glyph]. A "747" glyph is spelled into the wall,
        # hidden in texture (full blocks) rather than a loud color — see draw().
        self.bricks = {}
        cols = max(1, (self.w - 2) // BRICK_W)
        glyph = self.glyph_cells(cols)
        for r in range(BRICK_ROWS):
            for c in range(cols):
                is_glyph = (r, c) in glyph
                # glyph bricks inherit their ROW color — the 747 hides in texture,
                # not a loud color (see draw()). Camouflage, discovered on a 2nd look.
                color = (r % 5) + 1
                self.bricks[(r + 2, 1 + c * BRICK_W)] = [color, is_glyph]

    @staticmethod
    def glyph_cells(cols):
        # 5-row bitmap font, "7 4 7" with a 1-col gap between digits (11 cols wide).
        seven = ["###", "..#", ".#.", ".#.", ".#."]
        four = ["#.#", "#.#", "###", "..#", "..#"]
        rows = [seven[i] + "." + four[i] + "." + seven[i] for i in range(BRICK_ROWS)]
        gw = len(rows[0])
        start = (cols - gw) // 2
        if start < 0:
            return set()  # too narrow to render the glyph — plain wall
        return {(r, start + i) for r in range(BRICK_ROWS)
                for i, ch in enumerate(rows[r]) if ch == "#"}

    def serve(self):
        self.ball_x = self.w / 2.0
        self.ball_y = self.h - 4.0
        speed = 1.0 + 0.15 * (self.level - 1)
        self.vx = 11.0 * speed * (1 if int(self.ball_x) % 2 else -1)
        self.vy = -7.0 * speed

    # ---- simulation -------------------------------------------------------
    def step(self, dt):
        self.ball_x += self.vx * dt
        self.ball_y += self.vy * dt

        if self.ball_x <= 1:
            self.ball_x = 1.0
            self.vx = abs(self.vx)
        elif self.ball_x >= self.w - 2:
            self.ball_x = float(self.w - 2)
            self.vx = -abs(self.vx)
        if self.ball_y <= 1:
            self.ball_y = 1.0
            self.vy = abs(self.vy)

        # paddle
        if (self.vy > 0 and self.ball_y >= self.paddle_y - 0.5
                and self.paddle_x - 1 <= self.ball_x <= self.paddle_x + self.paddle_w):
            self.vy = -abs(self.vy)
            # english: hit position steers the ball
            rel = (self.ball_x - (self.paddle_x + self.paddle_w / 2)) / (self.paddle_w / 2)
            self.vx = 16.0 * rel + self.vx * 0.25
            if abs(self.vx) < 4:
                self.vx = 4.0 if rel >= 0 else -4.0
            self.ball_y = self.paddle_y - 1.0

        # bricks
        by, bx = int(self.ball_y), int(self.ball_x)
        for (r, c) in list(self.bricks):
            if r == by and c <= bx < c + BRICK_W - 1:
                _, is_glyph = self.bricks.pop((r, c))
                # 747 bricks pay 47 (the quiet wink) — no flash, no fanfare.
                self.score += 47 if is_glyph else 10
                self.vy = -self.vy
                break

        if not self.bricks:
            self.level += 1
            self.score += 100
            self.build_bricks()
            self.serve()

        # dropped ball
        if self.ball_y >= self.h - 1:
            self.lives -= 1
            if self.lives <= 0:
                return "over"
            self.serve()
        return None

    # ---- rendering --------------------------------------------------------
    def draw(self, playing):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if (h, w) != (self.h, self.w):
            self.reset_field(full=True)
            h, w = self.h, self.w
        try:
            for (r, c), (color, is_glyph) in self.bricks.items():
                if r < h - 1 and c + BRICK_W - 1 < w:
                    attr = curses.color_pair(color) | curses.A_BOLD
                    # the 747 hides in TEXTURE: glyph bricks are full blocks (█) in
                    # their row's own color — a second-look discovery, never a banner.
                    fill = "█" if is_glyph else "▄"
                    s.addstr(r, c, fill * (BRICK_W - 1), attr)
            s.addstr(self.paddle_y, max(0, self.paddle_x),
                     "▀" * min(self.paddle_w, w - self.paddle_x - 1),
                     curses.color_pair(6) | curses.A_BOLD)
            if playing:
                s.addstr(int(self.ball_y), int(self.ball_x), "●", curses.color_pair(7) | curses.A_BOLD)
            hud = f" BREAKOUT · SCORE {self.score} · LIVES {'♥' * self.lives} · LVL {self.level} "
            keys = " ←/→ or mouse · [space] pause · [q] quit "
            s.addstr(0, 0, hud[: w - 1], curses.A_REVERSE)
            if len(hud) + len(keys) < w:
                s.addstr(0, w - len(keys) - 1, keys, curses.A_DIM)
            if not playing:
                self.overlay(["⏸  CLAUDE'S DONE — READING TIME",
                              "resumes on your next prompt · [space] play anyway"])
            s.addstr(h - 1, max(0, w - 22), "THE 747 LAB ", curses.A_DIM)
        except curses.error:
            pass
        s.refresh()

    def overlay(self, lines):
        h, w = self.scr.getmaxyx()
        for i, ln in enumerate(lines):
            y = h // 2 - 1 + i
            x = max(0, (w - len(ln)) // 2)
            try:
                self.scr.addstr(y, x, ln[: w - 1], curses.A_BOLD)
            except curses.error:
                pass

    # ---- input ------------------------------------------------------------
    def handle_key(self, ch):
        if ch in (ord("q"), ord("Q")):
            return "quit"
        if ch == ord(" "):
            self.manual_play = not self.manual_play
        elif ch in (curses.KEY_LEFT, ord("a")):
            self.paddle_x = max(1, self.paddle_x - 3)
        elif ch in (curses.KEY_RIGHT, ord("d")):
            self.paddle_x = min(self.w - self.paddle_w - 1, self.paddle_x + 3)
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, _, _, _ = curses.getmouse()
                self.paddle_x = max(1, min(self.w - self.paddle_w - 1, mx - self.paddle_w // 2))
            except curses.error:
                pass
        return None


def game_over_screen(scr, game, session):
    scr.nodelay(False)
    scr.timeout(1000)
    scr.erase()
    h, w = scr.getmaxyx()
    lines = [f"GAME OVER · SCORE {game.score}", "", "[r] play again · [q] close"]
    for i, ln in enumerate(lines):
        try:
            scr.addstr(h // 2 - 1 + i, max(0, (w - len(ln)) // 2), ln, curses.A_BOLD)
        except curses.error:
            pass
    scr.refresh()
    while True:
        ch = scr.getch()
        if ch in (ord("r"), ord("R")):
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if read_state(session) == "end":
            return False


# Same 5-row bitmap font the brick wall uses for its 747 — the welcome speaks
# the game's own visual language.
FLYBY_FONT = {
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "E": ["###", "#..", "###", "#..", "###"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "A": [".#.", "#.#", "###", "#.#", "#.#"],
    "B": ["##.", "#.#", "##.", "#.#", "##."],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
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


def welcome_flyby(scr):
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
    except curses.error:
        has256 = False

    _pi = [30]

    def mk(fg, bold=False):
        i = _pi[0]
        _pi[0] += 1
        try:
            curses.init_pair(i, fg, -1)
            a = curses.color_pair(i)
        except curses.error:
            a = curses.A_NORMAL
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
        white, gold, mag = (curses.color_pair(6), curses.color_pair(8),
                            curses.color_pair(5))
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

    import math
    import random
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
    field = [[rng.random() for _ in range(w)] for _ in range(h)]   # horizon dither

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
                put(gy0, gx0, "·", pal[2] if s > S_LO * 0.7 else pal[3], depth)
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
                    put(yy, xx, "█", attr, depth)

    FLYBY_T = 7.47          # the runtime is part of the signature (7-4-7)
    A1_END, A2_END = 1.5, 5.0                     # approach / fly-through / resolve
    scr.nodelay(True)
    t0 = time.time()
    z_prev = 0.0
    while True:
        now = time.time() - t0
        if scr.getch() != -1:                     # any key -> INSTANT skip, every frame
            break
        if now >= FLYBY_T:
            break

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
                    put(r, x, "▓", SUN, 110)
                elif it > 0.36:
                    put(r, x, "▒", SKY[-1], 110)
                elif it > 0.16:
                    put(r, x, "░", SKY[-2], 110)

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
            put(int(round(sy)), int(round(sx)), "·", STAR[tier], 90)
            if surge and rr > 0.45:                # one fading trailing dot (same char)
                put(int(round(sy - uy * 1.6)), int(round(sx - ux * 1.6)),
                    "·", STAR[2], 91)

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
                        put(ry, xp, "█", a, -10.0)
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
                        put(rb, bx, "─", BAR, 130)

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
                    except curses.error:
                        pass
                x = j
        scr.refresh()
        time.sleep(0.02)                          # ~50fps for a smoother glide

    scr.nodelay(False)                            # hard cut straight to the ask


def ask_screen(scr, session):
    """Returns True to play. Handles n (this session), a (always), o (off)."""
    scr.nodelay(False)
    scr.timeout(1000)
    start = time.time()
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = [
            "PLAY BREAKOUT WHILE CLAUDE THINKS?",
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
        if time.time() - start > ASK_TIMEOUT or read_state(session) == "end":
            if session:
                open(os.path.join(STATE_DIR, f"declined-{session}"), "w").close()
            return False


def main(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.start_color()
    for i, col in enumerate([curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                             curses.COLOR_CYAN, curses.COLOR_MAGENTA], start=1):
        curses.init_pair(i, col, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.init_pair(7, curses.COLOR_YELLOW, -1)
    curses.init_pair(8, curses.COLOR_YELLOW, -1)  # gold — the 747 glyph
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    print("\033[?1003h", end="", flush=True)  # mouse motion tracking

    if args.ask:
        welcome_flyby(stdscr)
        if not ask_screen(stdscr, args.session):
            return

    while True:  # restart loop
        game = Game(stdscr, args.session)
        game.manual_play = args.free  # manual launch: play regardless of Claude's state
        stdscr.nodelay(True)
        last = time.time()
        state = "thinking"
        while True:
            now = time.time()
            dt, last = min(now - last, 0.1), now
            if now - game.last_poll > STATE_POLL:
                game.last_poll = now
                state = read_state(args.session)
            if state == "end":
                remove_state(args.session)  # session over — clean up our own state file
                return
            playing = state == "thinking" or game.manual_play

            ch = stdscr.getch()
            while ch != -1:
                if game.handle_key(ch) == "quit":
                    return
                ch = stdscr.getch()

            result = None
            if playing:
                result = game.step(dt)
            game.draw(playing)
            if result == "over":
                if game_over_screen(stdscr, game, args.session):
                    break  # restart
                return
            time.sleep(TICK)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    set_pane_title()
    try:
        curses.wrapper(main, args)
    finally:
        print("\033[?1003l", end="", flush=True)
