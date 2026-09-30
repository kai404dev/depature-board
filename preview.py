#!/usr/bin/env python3
"""Hardware-free layout preview for the departures board.

Provides a fake `rgbmatrix` module (same call surface as the real
bindings: RGBMatrix, RGBMatrixOptions, graphics.Font/Color/DrawText/
DrawLine/DrawCircle + canvas Fill/SetPixel/width/height) backed by a
pixel grid, plus a minimal BDF reader for real font metrics
(baseline/height/advances -- same formulas as lib/bdf-font.cc).

Install into sys.modules, run one frame, then dump an ASCII map of the
192x32 (or configured) screen plus per-element bounds and any rows
where elements overlap.
"""

import os
import sys

PX_COLS = 3  # LED pixels per ASCII column
PX_ROWS = 2  # LED pixels per ASCII row
GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789#*+=@"


class BDFMeta:
    """Baseline/height/advances parsed from a .bdf file."""

    def __init__(self, path):
        box_w, box_h, yoff = 0, 0, 0
        advances = {}
        enc = dwidth = None
        with open(path, errors="replace") as f:
            for line in f:
                p = line.split()
                if not p:
                    continue
                if p[0] == "FONTBOUNDINGBOX" and len(p) >= 5:
                    box_w, box_h, _, yoff = (int(x) for x in p[1:5])
                elif p[0] == "ENCODING" and len(p) >= 2:
                    try:
                        enc = int(p[1])
                    except ValueError:
                        enc = None
                elif p[0] == "DWIDTH" and len(p) >= 2:
                    try:
                        dwidth = int(p[1])
                    except ValueError:
                        dwidth = None
                elif p[0] == "ENDCHAR":
                    if enc is not None and enc >= 0 and dwidth is not None:
                        advances[enc] = dwidth + 2  # +kBorder, as rendered
                    enc = dwidth = None
        self.baseline = yoff + box_h + 1
        self.height = box_h + 2
        self.advances = advances
        self.default_advance = box_w + 2

    def text_width(self, text):
        return sum(self.advances.get(ord(c), self.default_advance)
                   for c in text)


class FakeColor:
    def __init__(self, red=0, green=0, blue=0):
        self.red, self.green, self.blue = red, green, blue


class FakeFont:
    def __init__(self):
        self._m = None
        self.path = None

    def LoadFont(self, path):
        if isinstance(path, bytes):
            path = path.decode("utf-8")
        if not path or not os.path.exists(path):
            raise Exception(f"Couldn't load font {path}")
        self._m = BDFMeta(path)
        self.path = path

    @property
    def baseline(self):
        return self._m.baseline

    @property
    def height(self):
        return self._m.height

    def text_width(self, text):
        return self._m.text_width(text)


class FakeCanvas:
    def __init__(self, width, height, rec):
        self.width = width
        self.height = height
        self._rec = rec

    def Fill(self, r, g, b):
        pass

    def Clear(self):
        pass

    def SetPixel(self, x, y, r, g, b):
        self._rec.pixel(x, y, structural=True)


class Recorder:
    """Collects draw ops + a pixel grid with per-pixel op ids.

    Pixels are tagged (op id, structural?) so the overlap check only
    considers text-vs-text collisions (fills/outlines layering under
    text is intentional, not a bug).
    """

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.grid = {}  # (x, y) -> set((op id, structural))
        self.ops = []   # (id, kind, label, rect)
        self._n = 0
        self._cur = ("*", True)

    def _next_id(self):
        c = GLYPHS[self._n % len(GLYPHS)]
        self._n += 1
        return c

    def pixel(self, x, y, structural=None):
        if 0 <= x < self.width and 0 <= y < self.height:
            oid, struct = self._cur
            if structural is not None:
                struct = structural
            self.grid.setdefault((x, y), set()).add((oid, struct))

    def line(self, x0, y0, x1, y1):
        oid = self._next_id()
        self._cur = (oid, True)
        self.ops.append((oid, "line", f"{x0},{y0}->{x1},{y1}",
                         (min(x0, x1), min(y0, y1),
                          max(x0, x1), max(y0, y1))))
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        n = max(dx, dy)
        for i in range(n + 1):
            t = i / max(1, n)
            self.pixel(round(x0 + (x1 - x0) * t),
                       round(y0 + (y1 - y0) * t))

    def circle(self, x, y, r):
        oid = self._next_id()
        self._cur = (oid, True)
        self.ops.append((oid, "circle", f"c{x},{y} r{r}",
                         (x - r, y - r, x + r, y + r)))
        import math
        for a in range(0, 360, 5):
            self.pixel(round(x + r * math.cos(math.radians(a))),
                       round(y + r * math.sin(math.radians(a))))

    def text(self, font, x, y, s):
        # Ink rows approximated as caps + one descender row (drops the
        # empty top border row of the glyph box).
        w = font.text_width(s)
        y0, y1 = y - font.height + 3, y + 1
        for xx in range(x, x + w):
            for yy in range(y0, y1 + 1):
                self.pixel(xx, yy)
        return w

    def text_op(self, font, x, y, s):
        oid = self._next_id()
        self._cur = (oid, False)
        w = font.text_width(s)
        self.ops.append((oid, "text", f"{os.path.basename(font.path or '?')} "
                                      f"'{s[:24]}'",
                         (x, y - font.height + 3, x + w - 1, y + 1)))

    def report(self, out=None):
        out = out or sys.stdout
        W = (self.width + PX_COLS - 1) // PX_COLS
        H = (self.height + PX_ROWS - 1) // PX_ROWS
        cells = [[" "] * W for _ in range(H)]
        owners = [[set() for _ in range(W)] for _ in range(H)]
        for (x, y), ids in self.grid.items():
            cx, cy = x // PX_COLS, y // PX_ROWS
            if 0 <= cx < W and 0 <= cy < H:
                owners[cy][cx] |= {i for i, _ in ids}
        for cy in range(H):
            for cx in range(W):
                ids = owners[cy][cx] - {"*"}
                if len(ids) == 1:
                    cells[cy][cx] = next(iter(ids))
                elif len(ids) > 1:
                    cells[cy][cx] = "!"
        print("+" + "-" * W + "+", file=out)
        for row in cells:
            print("|" + "".join(row) + "|", file=out)
        print("+" + "-" * W + "+", file=out)
        for oid, kind, label, rect in self.ops:
            x0, y0, x1, y1 = rect
            print(f"  {oid} [{kind}] {label} x{x0}..{x1} y{y0}..{y1}",
                  file=out)
        # row-level overlap check on real pixel rows, text-vs-text only.
        # Disjoint collisions are reported as separate ranges.
        by_row = {}
        for (x, y), ids in self.grid.items():
            ids = {i for i, structural in ids if not structural}
            if len(ids) > 1:
                by_row.setdefault(y, set()).update(ids)
        if by_row:
            rows = sorted(by_row)
            start = prev = rows[0]
            for r in rows[1:] + [None]:
                if r is None or r != prev + 1:
                    who = sorted(set().union(
                        *(by_row[rr] for rr in range(start, prev + 1))))
                    if start == prev:
                        print(f"  OVERLAP row {start}: {','.join(who)}",
                              file=out)
                    else:
                        print(f"  OVERLAP rows {start}..{prev}: "
                              f"{','.join(who)}", file=out)
                    start = r
                prev = r if r is not None else prev
        else:
            print("  no overlaps", file=out)


class FakeGraphics:
    Font = FakeFont
    Color = FakeColor

    def __init__(self, rec):
        self._rec = rec

    def DrawText(self, canvas, font, x, y, color, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if y < 0:
            return font.text_width(text)  # offscreen measure, don't record
        self._rec.text_op(font, x, y, text)
        self._rec.text(font, x, y, text)
        return font.text_width(text)

    def DrawLine(self, canvas, x0, y0, x1, y1, color):
        self._rec.line(x0, y0, x1, y1)

    def DrawCircle(self, canvas, x, y, r, color):
        self._rec.circle(x, y, r)


class FakeMatrix:
    def __init__(self, options, rec):
        w = getattr(options, "cols", 64) * getattr(options, "chain_length", 1)
        h = getattr(options, "rows", 32)
        self._canvas = FakeCanvas(w, h, rec)

    def CreateFrameCanvas(self):
        return self._canvas

    def SwapOnVSync(self, canvas):
        return canvas


class FakeOptions:
    pass


def install(width, height):
    """Install fake rgbmatrix into sys.modules. Returns the Recorder."""
    import types
    rec = Recorder(width, height)
    mod = types.ModuleType("rgbmatrix")
    mod.RGBMatrix = lambda options: FakeMatrix(options, rec)
    mod.RGBMatrixOptions = FakeOptions
    mod.graphics = FakeGraphics(rec)
    sys.modules["rgbmatrix"] = mod
    return rec
