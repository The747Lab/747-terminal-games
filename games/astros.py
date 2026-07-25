#!/usr/bin/env python3
"""ASTROS 747 — terminal space-invaders that runs while Claude thinks.

Same pause/resume contract as Breakout 747: auto-pauses when Claude replies
(state 'idle'), resumes on the next prompt ('thinking').

The 747 is a WINK, not a billboard: a mystery ship streaks across the top now
and then, worth 747 points if you tag it. No digits are spelled in the sky.
(v0.2 idea, per Pixi: a jumbo-jet-silhouette boss wave — see the games-line note.)

Developed by The 747 Lab.
"""
import argparse
import curses
import os
import random
import time

STATE_DIR = os.environ.get("BREAKOUT747_STATE") or os.path.expanduser("~/.747-terminal-games")
TICK = 0.033
STATE_POLL = 0.2
ASK_TIMEOUT = 45


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


def remove_state(session):
    try:
        os.remove(state_path(session))
    except OSError:
        pass


def write_mode(mode):
    with open(os.path.join(STATE_DIR, "mode"), "w") as f:
        f.write(mode + "\n")


class Game:
    def __init__(self, scr, session, free):
        self.scr = scr
        self.session = session
        self.manual_play = free
        self.score = 0
        self.lives = 3
        self.wave = 1
        self.bullets = []       # player shots: [x, y]
        self.bombs = []         # enemy shots: [x, y]
        self.mystery = None     # [x, dir]
        self.mystery_cd = 14.0  # seconds until the 747 ship may appear
        self.last_poll = 0.0
        self.dir = 1
        self.layout()

    def layout(self):
        self.h, self.w = self.scr.getmaxyx()
        self.ship_x = self.w // 2
        self.ship_y = self.h - 2
        self.build_fleet()

    def build_fleet(self):
        # A classic rectangular fleet — no digits spelled in the sky. The 747 wink
        # lives entirely in the mystery ship's 747-point bounty (taste-bar: reward
        # the noticing, never advertise).
        self.cell_w = 4
        cols = max(6, min(12, (self.w - 8) // self.cell_w))
        self.rows = 5
        self.fleet = {(r, c) for r in range(self.rows) for c in range(cols)}
        self.cols = cols
        self.fleet_x0 = max(2, (self.w - cols * self.cell_w) // 2)
        self.fleet_off = 0.0
        self.fleet_row0 = 2

    def cell_xy(self, r, c):
        return self.fleet_x0 + int(self.fleet_off) + c * self.cell_w, self.fleet_row0 + r

    # --- simulation ---------------------------------------------------------
    def step(self, dt, keys):
        for k in keys:
            if k in (curses.KEY_LEFT, ord("a")):
                self.ship_x = max(2, self.ship_x - 2)
            elif k in (curses.KEY_RIGHT, ord("d")):
                self.ship_x = min(self.w - 3, self.ship_x + 2)
            elif k == ord(" "):
                if len(self.bullets) < 4:
                    self.bullets.append([self.ship_x, self.ship_y - 1])
            elif k == curses.KEY_MOUSE:
                try:
                    _, mx, _, _, _ = curses.getmouse()
                    self.ship_x = max(2, min(self.w - 3, mx))
                except curses.error:
                    pass

        # march the fleet
        speed = 2.0 + (self.wave - 1) + (12.0 / max(1, len(self.fleet)))
        self.fleet_off += self.dir * speed * dt
        if self.fleet:
            cols = [c for (_, c) in self.fleet]
            left = self.fleet_x0 + int(self.fleet_off) + min(cols) * self.cell_w
            right = self.fleet_x0 + int(self.fleet_off) + max(cols) * self.cell_w
            if right >= self.w - 3 and self.dir > 0:
                self.dir = -1; self.fleet_row0 += 1
            elif left <= 2 and self.dir < 0:
                self.dir = 1; self.fleet_row0 += 1

        # the 747 mystery ship
        self.mystery_cd -= dt
        if self.mystery is None and self.mystery_cd <= 0 and self.fleet:
            self.mystery = [1 if random.random() < 0.5 else self.w - 2,
                            1 if random.random() < 0.5 else -1]
            self.mystery[1] = 1 if self.mystery[0] < 2 else -1
        if self.mystery is not None:
            self.mystery[0] += self.mystery[1] * 24.0 * dt
            if not (0 < self.mystery[0] < self.w - 1):
                self.mystery = None
                self.mystery_cd = random.uniform(12.0, 22.0)

        # enemy fire
        if self.fleet and random.random() < 0.03 + 0.01 * self.wave:
            bx, by = self.cell_xy(*random.choice(list(self.fleet)))
            self.bombs.append([bx, by + 1])

        # advance shots
        for b in self.bullets:
            b[1] -= 1
        self.bullets = [b for b in self.bullets if b[1] > 0]
        for b in self.bombs:
            b[1] += 1
        self.bombs = [b for b in self.bombs if b[1] < self.h - 1]

        # player hits
        for b in list(self.bullets):
            if self.mystery is not None and b[1] <= 1 and abs(b[0] - self.mystery[0]) <= 2:
                self.bullets.remove(b)
                self.score += 747          # the wink
                self.mystery = None
                self.mystery_cd = random.uniform(12.0, 22.0)
                continue
            for (r, c) in self.fleet:
                cx, cy = self.cell_xy(r, c)
                if b[1] == cy and cx <= b[0] <= cx + 1:
                    self.fleet.discard((r, c))
                    self.bullets.remove(b)
                    self.score += 10
                    break

        if not self.fleet:
            self.wave += 1
            self.score += 100
            self.bombs.clear()
            self.build_fleet()

        for bomb in list(self.bombs):
            if bomb[1] >= self.ship_y and abs(bomb[0] - self.ship_x) <= 1:
                self.bombs.remove(bomb)
                return self.lose_life()
        if self.fleet and (self.fleet_row0 + max(r for (r, _) in self.fleet)) >= self.ship_y - 1:
            return self.lose_life(reset=True)
        return None

    def lose_life(self, reset=False):
        self.lives -= 1
        self.bombs.clear()
        self.bullets.clear()
        if reset:
            self.fleet_row0 = 2
            self.fleet_off = 0.0
        return "over" if self.lives <= 0 else None

    # --- render -------------------------------------------------------------
    def draw(self, playing):
        s = self.scr
        s.erase()
        h, w = s.getmaxyx()
        if (h, w) != (self.h, self.w):
            self.layout()
            h, w = self.h, self.w
        try:
            for (r, c) in self.fleet:
                cx, cy = self.cell_xy(r, c)
                if 0 <= cy < h - 1 and 0 <= cx < w - 2:
                    s.addstr(cy, cx, "▼▼", curses.color_pair((r % 5) + 1) | curses.A_BOLD)
            if self.mystery is not None:
                mx = int(self.mystery[0])
                if 0 <= mx < w - 3:
                    s.addstr(1, mx, "◄▓►", curses.color_pair(2) | curses.A_BOLD)
            for b in self.bullets:
                if 0 <= b[1] < h - 1:
                    s.addstr(b[1], b[0], "│", curses.color_pair(3) | curses.A_BOLD)
            for bomb in self.bombs:
                if 0 <= bomb[1] < h - 1:
                    s.addstr(bomb[1], bomb[0], "!", curses.color_pair(1) | curses.A_BOLD)
            if playing:
                s.addstr(self.ship_y, self.ship_x, "▲", curses.color_pair(6) | curses.A_BOLD)
            hud = f" ASTROS · SCORE {self.score} · LIVES {'▲' * self.lives} · WAVE {self.wave} "
            keys = " ←/→ move · [space] fire · [q] quit "
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
            try:
                self.scr.addstr(h // 2 + i, max(0, (w - len(ln)) // 2), ln[: w - 1], curses.A_BOLD)
            except curses.error:
                pass


def ask_screen(scr, session):
    scr.nodelay(False)
    scr.timeout(1000)
    start = time.time()
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        lines = ["PLAY ASTROS WHILE CLAUDE THINKS?", "",
                 "[y] yes   [n] not now   [a] always auto-open   [o] never ask again"]
        for i, ln in enumerate(lines):
            try:
                scr.addstr(h // 2 - 2 + i, max(0, (w - len(ln)) // 2), ln,
                           curses.A_BOLD if i == 0 else curses.A_NORMAL)
            except curses.error:
                pass
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("a"), ord("A")):
            write_mode("auto"); return True
        if ch in (ord("n"), ord("N")):
            if session:
                open(os.path.join(STATE_DIR, f"declined-{session}"), "w").close()
            return False
        if ch in (ord("o"), ord("O")):
            write_mode("off"); return False
        if ch in (ord("q"), ord("Q")):
            return False
        if time.time() - start > ASK_TIMEOUT or read_state(session) == "end":
            return False


def game_over(scr, game, session):
    scr.nodelay(False)
    scr.timeout(1000)
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        for i, ln in enumerate([f"GAME OVER · SCORE {game.score}", "", "[r] again · [q] close"]):
            try:
                scr.addstr(h // 2 - 1 + i, max(0, (w - len(ln)) // 2), ln, curses.A_BOLD)
            except curses.error:
                pass
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("r"), ord("R")):
            return True
        if ch in (ord("q"), ord("Q")) or read_state(session) == "end":
            return False


def main(scr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.start_color()
    for i, col in enumerate([curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
                             curses.COLOR_CYAN, curses.COLOR_MAGENTA], start=1):
        curses.init_pair(i, col, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    print("\033[?1003h", end="", flush=True)

    if args.ask and not ask_screen(scr, args.session):
        return

    while True:
        game = Game(scr, args.session, args.free)
        scr.nodelay(True)
        last = time.time()
        state = "thinking"
        while True:
            now = time.time()
            dt, last = min(now - last, 0.1), now
            if now - game.last_poll > STATE_POLL:
                game.last_poll = now
                state = read_state(args.session)
            if state == "end":
                remove_state(args.session)
                return
            playing = state == "thinking" or game.manual_play

            keys = []
            ch = scr.getch()
            while ch != -1:
                if ch in (ord("q"), ord("Q")):
                    return
                keys.append(ch)
                ch = scr.getch()

            result = game.step(dt, keys) if playing else None
            game.draw(playing)
            if result == "over":
                if game_over(scr, game, args.session):
                    break
                return
            time.sleep(TICK)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ask", action="store_true")
    p.add_argument("--free", action="store_true")
    p.add_argument("--session", default="")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    import sys
    sys.stdout.write("\033]2;BREAKOUT747\033\\")
    sys.stdout.flush()
    try:
        curses.wrapper(main, args)
    finally:
        print("\033[?1003l", end="", flush=True)
