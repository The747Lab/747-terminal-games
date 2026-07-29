#!/usr/bin/env python3
"""ansi2png — turn a `tmux capture-pane -p -e -N` dump into a PNG frame.

Used by scripts/record-clip.py to build the launch clips. Not needed to play
anything; this is release tooling.

Two things in here were learned the hard way and are load-bearing:

1. tmux DELTA-ENCODES the SGR state down the dump. A colour opened on row 4 is
   still open on row 5 unless something closes it. Reset the parser per line and
   every wall in BREAK-IN turns grey. So: one parser, one state, whole frame.

2. The Block Elements range (U+2580-U+259F) is drawn as RECTANGLES, not as font
   glyphs. These games are ~90% block art, and a TrueType glyph is sized to the
   font's em box rather than to the terminal cell, so `▄▄▄` renders with hairline
   seams between the columns and `███` walls come out striped. Painting the
   rectangles ourselves makes the walls solid, which is the whole look.

Developed by The 747 Lab.
"""
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# PALETTE. A dark, high-contrast 16 that reads on both GitHub themes; the
# 240-colour tail is the ordinary xterm cube + greyscale ramp, because the games
# address 256 colours directly (38;5;N) and any hand-picked tail would lie.
# --------------------------------------------------------------------------
ANSI16 = [
    (0x16, 0x18, 0x1D), (0xFF, 0x5C, 0x57), (0x5A, 0xF7, 0x8E), (0xF3, 0xF9, 0x9D),
    (0x57, 0xC7, 0xFF), (0xFF, 0x6A, 0xC1), (0x9A, 0xED, 0xFE), (0xC7, 0xC7, 0xC7),
    (0x68, 0x68, 0x68), (0xFF, 0x5C, 0x57), (0x5A, 0xF7, 0x8E), (0xF3, 0xF9, 0x9D),
    (0x57, 0xC7, 0xFF), (0xFF, 0x6A, 0xC1), (0x9A, 0xED, 0xFE), (0xFF, 0xFF, 0xFF),
]
DEFAULT_FG = (0xE4, 0xE6, 0xEA)
DEFAULT_BG = (0x0D, 0x0F, 0x13)


def _palette():
    pal = list(ANSI16)
    steps = (0, 95, 135, 175, 215, 255)
    for r in range(6):
        for g in range(6):
            for b in range(6):
                pal.append((steps[r], steps[g], steps[b]))
    for i in range(24):
        v = 8 + i * 10
        pal.append((v, v, v))
    return pal


PALETTE = _palette()

FONT_CHAIN = [
    ("/System/Library/Fonts/Menlo.ttc", 0, 1),          # regular, bold
    ("/System/Library/Fonts/Supplemental/Courier New.ttf", 0, 0),
    ("/System/Library/Fonts/Apple Symbols.ttf", 0, 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0, 0),
]

SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
ESC_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


class Cell:
    __slots__ = ("ch", "fg", "bg", "bold", "under")

    def __init__(self, ch, fg, bg, bold, under):
        self.ch = ch
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.under = under


class Style:
    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = None      # None == terminal default
        self.bg = None
        self.bold = False
        self.dim = False
        self.rev = False
        self.under = False

    def apply(self, params):
        # An empty parameter list ("\x1b[m") means reset, same as "\x1b[0m".
        codes = [int(p) if p else 0 for p in params.replace(":", ";").split(";")] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 2:
                self.dim = True
            elif c == 4:
                self.under = True
            elif c == 7:
                self.rev = True
            elif c in (21, 22):
                self.bold = self.dim = False
            elif c == 24:
                self.under = False
            elif c == 27:
                self.rev = False
            elif 30 <= c <= 37:
                self.fg = c - 30
            elif c == 39:
                self.fg = None
            elif 40 <= c <= 47:
                self.bg = c - 40
            elif c == 49:
                self.bg = None
            elif 90 <= c <= 97:
                self.fg = c - 90 + 8
            elif 100 <= c <= 107:
                self.bg = c - 100 + 8
            elif c in (38, 48):
                # 38;5;N (indexed) or 38;2;R;G;B (truecolour)
                if i + 1 < len(codes) and codes[i + 1] == 5 and i + 2 < len(codes):
                    val = codes[i + 2]
                    i += 2
                elif i + 1 < len(codes) and codes[i + 1] == 2 and i + 4 < len(codes):
                    val = tuple(codes[i + 2:i + 5])
                    i += 4
                else:
                    break
                if c == 38:
                    self.fg = val
                else:
                    self.bg = val
            i += 1

    def resolve(self):
        fg = self._rgb(self.fg, DEFAULT_FG)
        bg = self._rgb(self.bg, DEFAULT_BG)
        if self.dim:
            fg = tuple(round(f * 0.55 + b * 0.45) for f, b in zip(fg, bg))
        if self.rev:
            fg, bg = bg, fg
        return fg, bg

    @staticmethod
    def _rgb(val, default):
        if val is None:
            return default
        if isinstance(val, tuple):
            return tuple(max(0, min(255, v)) for v in val)
        return PALETTE[val] if 0 <= val < len(PALETTE) else default


def parse(text):
    """ANSI dump -> list of rows, each a list of Cell. State carries across lines."""
    st = Style()
    rows = []
    for line in text.split("\n"):
        row = []
        pos = 0
        for m in SGR_RE.finditer(line):
            for ch in line[pos:m.start()]:
                fg, bg = st.resolve()
                row.append(Cell(ch, fg, bg, st.bold, st.under))
            st.apply(m.group(1))
            pos = m.end()
        tail = ESC_RE.sub("", line[pos:])       # any non-SGR escape: drop it
        for ch in tail:
            fg, bg = st.resolve()
            row.append(Cell(ch, fg, bg, st.bold, st.under))
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return rows


# --------------------------------------------------------------------------
# BLOCK ELEMENTS, drawn as geometry. Values are (x0, y0, x1, y1) in unit cell
# space, or a float shade for the ░▒▓ trio.
# --------------------------------------------------------------------------
EIGHTHS_UP = {0x2588: 8, 0x2587: 7, 0x2586: 6, 0x2585: 5,
              0x2584: 4, 0x2583: 3, 0x2582: 2, 0x2581: 1}
EIGHTHS_LEFT = {0x258F: 1, 0x258E: 2, 0x258D: 3, 0x258C: 4,
                0x258B: 5, 0x258A: 6, 0x2589: 7}
SHADES = {0x2591: 0.25, 0x2592: 0.5, 0x2593: 0.75}
QUADRANTS = {
    0x2596: (0, 1, 1, 0),  # lower left
    0x2597: (0, 0, 1, 1),  # lower right   (tl, tr, bl, br)
    0x2598: (1, 0, 0, 0),  # upper left
    0x2599: (1, 0, 1, 1),  # ul + lower half
    0x259A: (1, 0, 0, 1),  # ul + lr
    0x259B: (1, 1, 1, 0),  # upper half + ll
    0x259C: (1, 1, 0, 1),  # upper half + lr
    0x259D: (0, 1, 0, 0),  # upper right
    0x259E: (0, 1, 1, 0),  # ur + ll
    0x259F: (0, 1, 1, 1),  # ur + lower half
}
# 0x2596..0x2599 above use (top-left, top-right, bottom-left, bottom-right)
QUADRANTS[0x2596] = (0, 0, 1, 0)
QUADRANTS[0x2597] = (0, 0, 0, 1)
QUADRANTS[0x2599] = (1, 0, 1, 1)


def draw_block(d, cp, x, y, w, h, fg):
    """Paint one Block Elements codepoint. Returns True if handled."""
    if cp in EIGHTHS_UP:                       # bottom-anchored horizontal bars
        n = EIGHTHS_UP[cp]
        top = y + h - round(h * n / 8)
        d.rectangle([x, top, x + w - 1, y + h - 1], fill=fg)
        return True
    if cp in EIGHTHS_LEFT:                     # left-anchored vertical bars
        n = EIGHTHS_LEFT[cp]
        d.rectangle([x, y, x + round(w * n / 8) - 1, y + h - 1], fill=fg)
        return True
    if cp == 0x2590:                           # right half block
        d.rectangle([x + w // 2, y, x + w - 1, y + h - 1], fill=fg)
        return True
    if cp == 0x2595:                           # right one eighth
        d.rectangle([x + w - max(1, round(w / 8)), y, x + w - 1, y + h - 1], fill=fg)
        return True
    if cp == 0x2594:                           # upper one eighth
        d.rectangle([x, y, x + w - 1, y + max(1, round(h / 8)) - 1], fill=fg)
        return True
    if cp in SHADES:
        # A real dither, not a flat blend: the ▒ hazards in JETWASH and the ▒
        # crowd noise in JAYWALK have to look like texture at 1:1 pixels.
        step = SHADES[cp]
        for py in range(y, y + h):
            for px in range(x, x + w):
                if step >= 0.75:
                    on = not ((px + py) % 4 == 0)
                elif step >= 0.5:
                    on = (px + py) % 2 == 0
                else:
                    on = (px % 2 == 0) and (py % 2 == 0)
                if on:
                    d.point((px, py), fill=fg)
        return True
    if cp in QUADRANTS:
        tl, tr, bl, br = QUADRANTS[cp]
        mx, my = x + w // 2, y + h // 2
        if tl:
            d.rectangle([x, y, mx - 1, my - 1], fill=fg)
        if tr:
            d.rectangle([mx, y, x + w - 1, my - 1], fill=fg)
        if bl:
            d.rectangle([x, my, mx - 1, y + h - 1], fill=fg)
        if br:
            d.rectangle([mx, my, x + w - 1, y + h - 1], fill=fg)
        return True
    return False


class Renderer:
    def __init__(self, size=15):
        self.regular = []
        self.bold = []
        for path, ri, bi in FONT_CHAIN:
            if not os.path.exists(path):
                continue
            try:
                # Load as a pair or not at all: the two lists are indexed in
                # lockstep by font_for().
                reg = ImageFont.truetype(path, size, index=ri)
                bld = ImageFont.truetype(path, size, index=bi)
            except OSError:
                continue
            self.regular.append(reg)
            self.bold.append(bld)
        if not self.regular:
            raise SystemExit("ansi2png: no usable monospace font found")
        self.cw = max(1, round(self.regular[0].getlength("M")))
        asc, desc = self.regular[0].getmetrics()
        ch = asc + desc
        self.ch = ch + (ch & 1)                # even: half-blocks tile seamlessly
        self.baseline = asc
        self._pick = {}
        # Fingerprint each face's .notdef with a codepoint no font carries, so a
        # missing glyph can be detected and stepped over instead of shipping a
        # hollow box into a launch clip.
        self._notdef = {id(f): bytes(f.getmask("\ufff0", mode="L"))
                        for f in self.regular + self.bold}

    def font_for(self, ch, bold):
        """Per (char, weight) lookup, and it has to be per WEIGHT.

        Menlo Bold carries no box-drawing glyphs at all \u2014 no \u2502 \u2500 \u2550 \u2551 \u254c \u2571. Every
        one of those is used, in bold, by a title: the ASTROS shot, the JETWASH
        deck and gates, the JAYWALK lane marks. Picking the face on the regular
        weight and then drawing with the bold one puts a .notdef rectangle where
        the shot should be. So: try this family's requested weight, drop to its
        other weight before leaving the family, and only then move down the chain.
        """
        want = (ch, bold)
        f = self._pick.get(want)
        if f is None:
            order = []
            for i in range(len(self.regular)):
                first = self.bold[i] if bold else self.regular[i]
                second = self.regular[i] if bold else self.bold[i]
                order += [first, second]
            f = order[0]
            for cand in order:
                try:
                    if bytes(cand.getmask(ch, mode="L")) != self._notdef[id(cand)]:
                        f = cand
                        break
                except Exception:
                    continue
            self._pick[want] = f
        return f

    def render(self, rows, cols=None, nrows=None):
        """Rasterise. Pass cols AND nrows when the frames feed a video encoder.

        parse() drops trailing blank rows, and a game that clears the lower half
        of the pane for a transition screen then yields a shorter frame. ffmpeg
        will not accept a changing frame size mid-sequence — it aborts with
        "Internal bug, should not have happened" — so the caller pins the
        geometry and this pads to it.
        """
        cols = cols or max((len(r) for r in rows), default=1)
        rows = list(rows)
        if nrows:
            rows = rows[:nrows] + [[] for _ in range(max(0, nrows - len(rows)))]
        w, h = cols * self.cw, len(rows) * self.ch
        img = Image.new("RGB", (w, h), DEFAULT_BG)
        d = ImageDraw.Draw(img)
        for ry, row in enumerate(rows):
            y = ry * self.ch
            # Backgrounds first, run-length merged so adjacent cells share one
            # rectangle and no seam can appear inside a coloured band.
            cx = 0
            while cx < len(row):
                bg = row[cx].bg
                span = cx
                while span < len(row) and row[span].bg == bg:
                    span += 1
                if bg != DEFAULT_BG:
                    d.rectangle([cx * self.cw, y, span * self.cw - 1, y + self.ch - 1], fill=bg)
                cx = span
            for cxi, cell in enumerate(row):
                ch = cell.ch
                if ch == " " or not ch:
                    continue
                x = cxi * self.cw
                cp = ord(ch)
                if 0x2580 <= cp <= 0x259F and draw_block(d, cp, x, y, self.cw, self.ch, cell.fg):
                    pass
                else:
                    d.text((x, y + self.baseline), ch, font=self.font_for(ch, cell.bold),
                           fill=cell.fg, anchor="ls")
                if cell.under:
                    d.line([x, y + self.ch - 2, x + self.cw - 1, y + self.ch - 2], fill=cell.fg)
        return img


def audit(rend, text):
    """Report any character in `text` that no face in the chain can draw.

    Called by record-clip on every frame it captures. A hollow .notdef box in a
    launch clip is the kind of thing nobody notices until it is on the internet.
    """
    missing = set()
    for ch in set(text):
        if ch in " \n\r" or 0x2580 <= ord(ch) <= 0x259F:
            continue
        for bold in (False, True):
            f = rend.font_for(ch, bold)
            try:
                if bytes(f.getmask(ch, mode="L")) == rend._notdef[id(f)]:
                    missing.add(ch)
            except Exception:
                missing.add(ch)
    return missing


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: ansi2png.py <dump.ansi> <out.png> [font_size] [cols]")
    r = Renderer(int(sys.argv[3]) if len(sys.argv) > 3 else 15)
    rows = parse(open(sys.argv[1], encoding="utf-8", errors="replace").read())
    cols = int(sys.argv[4]) if len(sys.argv) > 4 else None
    r.render(rows, cols).save(sys.argv[2])
    print(f"{sys.argv[2]}  {len(rows)} rows  cell {r.cw}x{r.ch}")


if __name__ == "__main__":
    main()
