#!/usr/bin/env python3
"""System-font rasterizer shared by the desktop sign tools (no GUI).

Pillow-based 1-bit text rendering plus the LED-dot mask math. Has no
tkinter/Qt in it, so both sign-designer.py (tkinter) and
sign-studio.py (Qt) use this instead of duplicating the code. The BDF
engine itself lives in engine.py and is passed in as `eng`.
"""

import math
import os

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:  # system fonts simply unavailable without Pillow
    PIL_OK = False

SYS_SS = 4  # rasterize 4x, then snap to 1-bit (kills antialiasing)
FACES = {}  # family -> {style: (path, ttc_index)}
FONTCACHE = {}
_DOTMASKS = {}


def dotmask(z):
    """z*z bool grid shaped like one LED dot (dark corners), cached."""
    m = _DOTMASKS.get(z)
    if m is None:
        c = (z - 1) / 2
        r = z / 2 - 0.4
        m = [[(dx - c) ** 2 + (dy - c) ** 2 <= r * r
              for dx in range(z)] for dy in range(z)]
        _DOTMASKS[z] = m
    return m


def preferred_default(fams):
    """Sensible default family: Arial when present, else first sorted."""
    if "Arial" in fams:
        return "Arial"
    for f in fams:
        if "arial" in f.lower():
            return f
    return fams[0] if fams else ""


def scan_system_fonts():
    """Map installed font families to files. Fast (~0.5s, runs once)."""
    import glob
    dirs = [os.path.expanduser("~/Library/Fonts"),
            "/Library/Fonts",
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental"]
    files = []
    for d in dirs:
        files += glob.glob(os.path.join(d, "*.ttf"))
        files += glob.glob(os.path.join(d, "*.otf"))
        files += glob.glob(os.path.join(d, "*.ttc"))
    out = {}
    for p in sorted(set(files)):
        try:
            if p.endswith(".ttc"):
                for i in range(32):
                    try:
                        f = ImageFont.truetype(p, 20, index=i)
                    except Exception:
                        break
                    fam, sty = f.getname()
                    out.setdefault(fam, {})[sty or "Regular"] = (p, i)
            else:
                f = ImageFont.truetype(p, 20)
                fam, sty = f.getname()
                out.setdefault(fam, {})[sty or "Regular"] = (p, 0)
        except Exception:
            continue
    # hide private UI fonts and colour emoji (no monochrome outlines)
    return {k: v for k, v in out.items()
            if not k.startswith(".") and "emoji" not in k.lower()}


def resolve_face(family, bold):
    """(path, index) for a family, preferring a Bold/Regular cut."""
    styles = FACES.get(family or "")
    if not styles:
        fams = sorted(FACES)
        if not fams:
            raise ValueError("no system fonts found")
        styles = FACES[fams[0]]
    if bold:
        if "Bold" in styles:
            return styles["Bold"]
        for k in sorted(styles):
            if "bold" in k.lower():
                return styles[k]
    else:
        for k in ("Regular", "Plain", "Book", "Roman", "Medium"):
            if k in styles:
                return styles[k]
        plain = [(k, v) for k, v in styles.items()
                 if "bold" not in k.lower()]
        if plain:
            return sorted(plain)[0][1]
    return styles[sorted(styles)[0]]


def cached_truetype(path, index, size):
    key = (path, index, size)
    f = FONTCACHE.get(key)
    if f is None:
        f = ImageFont.truetype(path, size, index=index)
        if len(FONTCACHE) > 64:
            FONTCACHE.clear()
        FONTCACHE[key] = f
    return f


def render_system(eng, job, spec):
    """Render one blind with system fonts (Pillow, 1-bit, no AA).

    spec maps field name -> (path, ttc_index, pixel_height). Layout
    (cells, centring, warnings) is the exact shared engine math, so a
    blind looks the same whichever font source drew it. Returns
    (frame, info) like eng.render.
    """
    style = str(job.get("style", "top")).lower()
    route, dest, via = eng.normalize(job)
    gap, pad, vfract = eng.job_geometry(job)
    fg = eng.parse_colour(job.get("fg", eng.DEFAULTS["fg"]))
    warnings = []
    fields = {}

    # rasterize each line at 4x on its own scratch canvas
    ink = {}   # name -> (cropped_img, adv_1x) ; None when blank
    advs = {}
    for name, text in (("route", route), ("dest", dest), ("via", via)):
        if not text:
            advs[name], ink[name] = 0, None
            continue
        path, idx, px = spec[name]
        px = max(6, min(120, int(px)))
        f4 = cached_truetype(path, idx, px * SYS_SS)
        f1 = cached_truetype(path, idx, px)
        advs[name] = max(1, int(math.ceil(f1.getlength(text))))
        w = int(f4.getlength(text)) + 32
        asc, desc = f4.getmetrics()
        scratch = Image.new("1", (w, asc + desc + 32), 0)
        ImageDraw.Draw(scratch).text((16, 16), text, font=f4, fill=1)
        box = scratch.getbbox()
        ink[name] = (scratch.crop(box), advs[name]) if box else None

    route_cell, jobs, route_over = eng.layout_cells(
        route, dest, via, advs["route"], advs["dest"], advs["via"],
        style, gap, pad, vfract)
    if route_over:
        warnings.append(f"route number too wide by {route_over}px "
                        f"-- shorten it or use a smaller size")

    main = Image.new("1", (eng.W * SYS_SS, eng.H * SYS_SS), 0)
    for name, cell in jobs + ([("route", route_cell)] if route else []):
        cell4 = tuple(v * SYS_SS for v in cell)
        adv4 = advs[name] * SYS_SS
        wide = advs[name] - (cell[2] - cell[0] + 1)
        if wide > 0:
            warnings.append(f"{name} too wide by {wide}px in this "
                            f"style -- smaller size or fewer characters")
        if ink[name] is None:
            continue
        crop, _ = ink[name]
        ix1, iy1 = crop.size[0] - 1, crop.size[1] - 1
        ox, oy = eng.place_centered(cell4, adv4, (0, 0, ix1, iy1))
        main.paste(crop, (ox, oy))
        fields[name] = {"advance": advs[name], "lit": 0,
                        "cell": list(cell), "origin": [ox, oy]}

    # snap the 4x render into 1-bit target pixels (note: mode "1"
    # pixels read back as 0/1, so the cut is 8 of 16 subpixels lit)
    frame = bytearray(eng.W * eng.H * 3)
    px = main.load()
    lit = 0
    for y in range(eng.H):
        for x in range(eng.W):
            s = 0
            for dy in range(SYS_SS):
                for dx in range(SYS_SS):
                    s += px[x * SYS_SS + dx, y * SYS_SS + dy]
            if s >= 8:
                o = (y * eng.W + x) * 3
                frame[o], frame[o + 1], frame[o + 2] = fg
                lit += 1
    for f in fields.values():
        x0, y0, x1, y1 = f["cell"]
        n = 0
        for yy in range(max(0, y0), min(eng.H, y1 + 1)):
            o = (yy * eng.W + max(0, x0)) * 3
            for _ in range(max(0, x0), min(eng.W, x1 + 1)):
                if frame[o] or frame[o + 1] or frame[o + 2]:
                    n += 1
                o += 3
        f["lit"] = n
    info = {"w": eng.W, "h": eng.H, "style": style, "route": route,
            "dest": dest, "via": via, "fg": eng.colour_hex(fg),
            "fields": fields, "lit": lit, "warnings": warnings,
            "mode": "system"}
    return frame, info
