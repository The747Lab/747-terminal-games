#!/usr/bin/env python3
"""record-clip — play a title for real and cut it into a GIF and an MP4.

    python3 scripts/record-clip.py breakin
    python3 scripts/record-clip.py all --out ../launch-assets

What it does, per title: opens the game in a detached tmux session at the pane
geometry the plugin actually uses (16 rows — see `want` in
hooks/breakout-hook.sh), then *plays it*. A bot reads the pane every frame and
sends real keys back, so the clip is a run with movement, hits and scoring in it,
not idle attract footage. Frames go through scripts/ansi2png.py and out to ffmpeg.

Three details that are not negotiable:

* `tmux capture-pane -p -e -N`. The `-e` keeps the colour; the `-N` keeps
  trailing blanks. Without `-N` tmux strips the end of every line, the row comes
  back short, and the render tears down the right-hand edge.
* One capture serves both the bot and the frame. Two captures per tick would put
  the bot half a frame behind what it is reacting to.
* The encode fps is measured, not assumed. Capture jitters; the frames get
  resampled onto an even grid across the real elapsed time, so a 15-second run
  plays back in 15 seconds.

Needs: tmux, ffmpeg, Pillow. Developed by The 747 Lab.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import ansi2png  # noqa: E402

COLS, ROWS = 100, 16
FPS = 12          # capture + MP4 rate; also the bot's reaction rate
GIF_FPS = 10      # see the encode block: GIF delays are whole centiseconds
FONT_SIZE = 15
GIF_MAX = 3 * 1024 * 1024

ESC = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


# ---------------------------------------------------------------------------
# bots. Each takes the plain-text pane (list of rows, already right-padded) and
# a scratch dict, and returns the keys to send this frame.
# ---------------------------------------------------------------------------
def _find(grid, chars):
    out = []
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch in chars:
                out.append((y, x))
    return out


def _nudge(err, step, cap=6):
    """Turn a column error into a run of arrow presses."""
    n = min(cap, max(1, int(round(abs(err) / float(step)))))
    return ["Right" if err > 0 else "Left"] * n


def bot_breakin(grid, st):
    ball = _find(grid, "●")
    paddle = None
    for y in range(len(grid) - 1, 0, -1):
        start = grid[y].find("▀▀▀")
        if start >= 0:
            paddle = start + grid[y].count("▀") // 2
            break
    if paddle is None or not ball:
        return []
    # Chase the LOWEST ball: that is the one about to arrive.
    by, bx = max(ball)
    err = bx - paddle
    if abs(err) <= 1:
        return []
    return _nudge(err, 4)


def bot_astros(grid, st):
    keys = []
    ship = None
    for y in range(len(grid) - 1, 0, -1):
        x = grid[y].find("▲")
        if x >= 0 and grid[y].count("▲") == 1:
            ship = (y, x)
            break
    if ship is None:
        return ["Space"]
    sy, sx = ship
    bombs = [(y, x) for y, x in _find(grid, "!") if y > 1]
    close = [x for y, x in bombs if y >= sy - 5 and abs(x - sx) <= 2]
    if close:
        # Dodge first, always: a bomb inside two columns is the only thing that
        # can end the run, and a shot fired from under one is a shot wasted.
        away = 1 if sum(close) / len(close) <= sx else -1
        return _nudge(away * 4, 2, cap=3) + ["Space"]
    aliens = [x for y, x in _find(grid, "▼") if y < sy - 1]
    if aliens:
        # Lowest-then-nearest column: pick off the front rank.
        target = min(aliens, key=lambda x: abs(x - sx))
        err = target - sx
        if abs(err) > 1:
            keys += _nudge(err, 2, cap=4)
    keys.append("Space")
    return keys


def bot_jetwash(grid, st):
    pos = None
    for y, row in enumerate(grid):
        for ch in "►»▼":
            x = row.find(ch)
            if x >= 0:
                pos = (y, x)
                break
        if pos:
            break
    if pos is None:
        return []
    py, px = pos
    lo, hi = px + 2, px + 11              # the commit window
    band = range(max(0, py - 1), min(len(grid), py + 2))
    brittle = solid = fuel = False
    for y in band:
        seg = grid[y][lo:hi]
        if "▒" in seg:
            brittle = True
        if any(c in seg for c in "█▟▙"):
            solid = True
    for y in range(max(0, py - 3), py):
        if "◈" in grid[y][lo:hi]:
            fuel = True
    if brittle and not solid:
        return ["Down"]                   # slam straight through the dither
    if solid:
        return ["Up"]
    if fuel:
        return ["Up"]                     # thrust is speed; it is worth the hop
    return []


def bot_skyrun(grid, st):
    keys = []
    nose = None
    for y in range(len(grid) - 2, 0, -1):
        x = grid[y].find("▲")
        if x >= 0:
            nose = (y, x)
            break
    if nose is None:
        return ["Space"]
    ny, nx = nose
    # Rocks are dodge-only and they arrive fast, so start leaving early and give
    # them a wide berth. Anything less and the run spends the clip on one shield.
    rocks = [x for y, x in _find(grid, "█▓▒░") if 1 < y < ny]
    near = [x for x in rocks if abs(x - nx) <= 7]
    if near:
        away = 1 if sum(near) / len(near) <= nx else -1
        return _nudge(away * 8, 2, cap=4)
    aliens = [x for y, x in _find(grid, "☩") if 1 < y < ny]
    coins = [x for y, x in _find(grid, "◈◆") if 1 < y < ny]
    aim = aliens or coins
    if aim:
        err = min(aim, key=lambda x: abs(x - nx)) - nx
        if abs(err) > 1:
            keys += _nudge(err, 2, cap=3)
    # Fire only on a lined-up alien. Spraying empties the magazine and parks
    # "dry · thread a rock to reload" on the HUD for the rest of the clip.
    if aliens and min(abs(x - nx) for x in aliens) <= 1:
        keys.append("Space")
    return keys


CARS = "▬█▶◀"


def bot_jaywalk(grid, st):
    pos = None
    for y, row in enumerate(grid):
        m = re.search(r"▌.▐", row)
        if m:
            pos = (y, m.start() + 1)
            break
    if pos is None:
        return []
    py, px = pos
    if py == 0:
        return []                          # landed in a bay; the game resets us
    ahead = grid[py - 1]
    if px <= 3:
        return ["Right", "Right"]
    if px >= len(ahead) - 4:
        return ["Left", "Left"]
    if "▄" in ahead:
        # River lane. A log is the only footing, and stepping onto its END is a
        # drowning: the log drifts a cell or so in the time between the capture
        # the decision was made from and the key landing. So require three cells
        # of plank and aim for the middle of one.
        if all(0 <= i < len(ahead) and ahead[i] == "▄" for i in (px - 1, px, px + 1)):
            return ["Up"]
        for off in (1, -1, 2, -2, 3, -3):
            i = px + off
            if all(0 <= j < len(ahead) and ahead[j] == "▄"
                   for j in (i - 1, i, i + 1)):
                return ["Right" if off > 0 else "Left"]
        return []
    # Road lane. Which way the lane runs decides which side of the player the
    # danger is on: a ▶ lane throws cars in from the left. Looking the same
    # distance both ways is what got the bot flattened — the clear cell it hops
    # into has a car in it a third of a second later.
    if "▶" in ahead and "◀" not in ahead:
        lo, hi = px - 8, px + 3
    elif "◀" in ahead and "▶" not in ahead:
        lo, hi = px - 2, px + 9
    else:
        lo, hi = px - 6, px + 7
    if any(c in ahead[max(0, lo):hi] for c in CARS):
        return []                          # let it pass
    return ["Up"]


TITLES = {
    "breakin": dict(game="breakout.py", bot=bot_breakin, warmup=2.2, secs=17),
    # SKYRUN's warmup is deliberately short: its scripted teaching rock arrives at
    # about t=2.5, and a bot that only wakes at 2.6 eats it and spends the clip on
    # one shield. Be driving before the first thing that can hit you.
    "skyrun":  dict(game="skyrun.py",   bot=bot_skyrun,  warmup=1.2, secs=17),
    "jetwash": dict(game="jetwash.py",  bot=bot_jetwash, warmup=2.2, secs=17),
    "astros":  dict(game="astros.py",   bot=bot_astros,  warmup=2.4, secs=17),
    "jaywalk": dict(game="jaywalk.py",  bot=bot_jaywalk, warmup=2.4, secs=17),
}


def tmux(*args, capture=False):
    cmd = ["tmux"] + list(args)
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    subprocess.run(cmd, capture_output=True)
    return ""


def pad(text, cols):
    """Right-pad every row to `cols` so a bot can index any column safely."""
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    return [(r + " " * cols)[:cols] for r in rows]


def record(key, out_dir, keep_frames=False):
    spec = TITLES[key]
    sess = f"clip_{key}"
    tmpdir = os.path.join(out_dir, f".frames_{key}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)

    tmux("kill-session", "-t", sess)
    game = os.path.join(ROOT, "games", spec["game"])
    tmux("new-session", "-d", "-s", sess, "-x", str(COLS), "-y", str(ROWS),
         "-e", "TERM=xterm-256color", "-e", "LANG=en_US.UTF-8",
         f"TERM=xterm-256color LANG=en_US.UTF-8 python3 {game!r} --free")
    time.sleep(spec["warmup"])

    rend = ansi2png.Renderer(FONT_SIZE)
    frames, stamps = [], []
    st = {}
    period = 1.0 / FPS
    t0 = time.monotonic()
    deadline = t0
    while time.monotonic() - t0 < spec["secs"]:
        raw = tmux("capture-pane", "-t", sess, "-p", "-e", "-N", capture=True)
        stamps.append(time.monotonic() - t0)
        frames.append(raw)
        plain = pad(ESC.sub("", raw), COLS)
        joined = "\n".join(plain)
        if "[r] run it again" in joined or "[r] again" in joined or "[r] fly again" in joined:
            # Never let a clip sit on a dead screen. Restarting also shows off the
            # thing 747-630 was about: `r` puts you back in play with no lag.
            keys = ["r"]
        else:
            keys = spec["bot"](plain, st)
        if keys:
            tmux("send-keys", "-t", sess, *keys)
        deadline += period
        nap = deadline - time.monotonic()
        if nap > 0:
            time.sleep(nap)
        else:
            deadline = time.monotonic()
    elapsed = time.monotonic() - t0
    tmux("send-keys", "-t", sess, "q")
    time.sleep(0.3)
    tmux("kill-session", "-t", sess)

    missing = ansi2png.audit(rend, "".join(ESC.sub("", f) for f in frames))
    if missing:
        raise SystemExit(f"{key}: no font in the chain can draw "
                         f"{sorted((c, hex(ord(c))) for c in missing)} — fix the "
                         f"chain before shipping the clip")

    # Real-duration timing: resample the jittery capture onto an even grid.
    n_out = max(2, int(round(elapsed * FPS)))
    written = 0
    for i in range(n_out):
        want = i * elapsed / n_out
        j = min(range(len(stamps)), key=lambda k: abs(stamps[k] - want))
        img = rend.render(ansi2png.parse(frames[j]), COLS, ROWS)
        img.save(os.path.join(tmpdir, f"f{i:05d}.png"))
        written += 1
    print(f"  {key}: {written} frames over {elapsed:.1f}s -> {FPS} fps  "
          f"({img.size[0]}x{img.size[1]})")

    mp4 = os.path.join(out_dir, f"{key}.mp4")
    gif = os.path.join(out_dir, f"{key}.gif")
    pattern = os.path.join(tmpdir, "f%05d.png")
    run = lambda a: subprocess.run(a, capture_output=True, text=True)

    r = run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", pattern,
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "faststart", mp4])
    if r.returncode:
        raise SystemExit(f"ffmpeg mp4 failed for {key}:\n{r.stderr[-1500:]}")

    # GIF: palettegen/paletteuse beats a straight encode by a wide margin on flat
    # terminal colour, and dither=none keeps the block art crisp instead of
    # speckling it. Drop colours, then fps, if the file lands over the repo budget.
    #
    # GIF_FPS is 10 and not FPS for a timing reason: a GIF frame delay is stored in
    # CENTIseconds, so 12 fps (83.3ms) gets written as 80ms and the clip plays back
    # 4% fast. 10 fps is 100ms exactly, so the GIF and the MP4 run the same 17.0s.
    for gfps, colors in ((GIF_FPS, 128), (GIF_FPS, 96), (GIF_FPS, 64), (5, 64)):
        pal = os.path.join(tmpdir, "pal.png")
        run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", pattern,
             "-vf", f"fps={gfps},palettegen=stats_mode=diff:max_colors={colors}", pal])
        r = run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", pattern, "-i", pal,
                 "-lavfi", f"fps={gfps} [x]; [x][1:v] paletteuse=dither=none:diff_mode=rectangle",
                 "-loop", "0", gif])
        if r.returncode:
            raise SystemExit(f"ffmpeg gif failed for {key}:\n{r.stderr[-1500:]}")
        if os.path.getsize(gif) <= GIF_MAX:
            break
    if not keep_frames:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return gif, mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("titles", nargs="+", help="title key(s), or 'all'")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "launch-assets"))
    ap.add_argument("--keep-frames", action="store_true")
    a = ap.parse_args()
    if not shutil.which("tmux") or not shutil.which("ffmpeg"):
        sys.exit("record-clip needs tmux and ffmpeg on PATH")
    keys = list(TITLES) if a.titles == ["all"] else a.titles
    bad = [k for k in keys if k not in TITLES]
    if bad:
        sys.exit(f"unknown title(s): {', '.join(bad)}")
    os.makedirs(a.out, exist_ok=True)
    for k in keys:
        print(f"recording {k}...")
        gif, mp4 = record(k, a.out, a.keep_frames)
        for p in (gif, mp4):
            print(f"    {os.path.basename(p)}  {os.path.getsize(p)/1024:.0f} KB")


if __name__ == "__main__":
    main()
