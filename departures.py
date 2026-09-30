#!/usr/bin/env python3
"""
Peak Rail departures board for Waveshare RGB-Matrix / rpi-rgb-led-matrix.

Files working together (all next to this script unless --layout-dir):
  api.py          fetching + formatting of the departures/timetable APIs
  layout/         look of the board, split per page:
                    shared.json  font roles, named colours, clock,
                                 page number, progress bar
                    page1.json   board: lead service, calling-at, rows
                    page2.json   next departure big + notes
                    page3.json   formation diagram
                  Every text line has its font, colour and position here --
                  edit + rerun, no code changes needed.
  departures.py   this file: argument parsing + LED matrix rendering
  preview.py      hardware-free ASCII preview (--preview)

Calls:
  https://peakraildepartures.com/api/departures/?railway=PR&station=RWS&limit=3&date=2026-10-04

and for each departure its timetable, e.g.:
  https://peakraildepartures.com/api/timetable/2M03/?date=2026-10-04&railway=PR

Based on the example code in:
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/text-example.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/clock.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/bindings/python/samples/runtext.py

Usage on Pi (3 panels chained, 240x40 total):
  sudo python3 departures.py --led-rows 40 --led-cols 80 --led-chain 3

Test on Mac / without hardware:
  python3 departures.py --mock --once
  python3 departures.py --mock
  python3 departures.py --preview          # ASCII map of every page
"""

import argparse
import errno
import json
import math
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

from api import (
    board_signature,
    enrich_with_timetables,
    expected_time,
    fetch_departures,
    find_font,
    format_console,
    format_departure,
    formation_of,
    live_status,
)

import rtt 

FONT_ROLES = ("top", "row", "small", "big", "tiny")
COLOR_NAMES = ("text", "time", "platform", "ok", "alert", "mark")


def _bad(path, msg):
    sys.exit(f"layout {path}: {msg}")


def _section(raw, path, name):
    sec = raw.get(name)
    if not isinstance(sec, dict):
        _bad(path, f"missing section '{name}'")
    return sec


def _num(sec, path, where, key):
    v = sec.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        _bad(path, f"{where}.{key} must be a whole number of LEDs")
    return v


def _onum(sec, path, where, key):
    """Optional whole number, None when absent."""
    v = sec.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        _bad(path, f"{where}.{key} must be a whole number of LEDs")
    return v


def _str(sec, path, where, key):
    v = sec.get(key)
    if not isinstance(v, str):
        _bad(path, f"{where}.{key} must be a string")
    return v


def _xpos(sec, path, where, key="x"):
    v = sec.get(key, "left")
    if v in ("left", "center", "right") or type(v) is int:
        return v
    _bad(path, f"{where}.{key} must be left|center|right|pixels")


def _ypos(sec, path, where, key="y"):
    v = sec.get(key)
    if type(v) is int:
        return v
    if v == "bottom" or (isinstance(v, str) and v.startswith("bottom-")
                         and v[7:].isdigit()):
        return v
    _bad(path, f"{where}.{key} must be pixels|bottom|bottom-N")


def _font(sec, path, where, fonts, key="font"):
    role = sec.get(key)
    if role not in fonts:
        _bad(path, f"{where}.{key} must be one of {sorted(fonts)}")
    return role


def _color(sec, path, where, colors, key="color"):
    name = sec.get(key)
    if name not in colors:
        _bad(path, f"{where}.{key} must be one of {sorted(colors)}")
    return name


def deep_merge(base, over):
    """Recursive overlay: dicts merge key by key, everything else
    is replaced wholesale."""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_layout(layout_dir, overlay=None):
    """Load layout/*.json with strict validation. Returns resolved dict.
    overlay: optional {filename: dict} merged over the files first --
    the untracked local.json live overrides use this."""
    overlay = overlay or {}

    def read(name):
        path = os.path.join(layout_dir, name)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            sys.exit(f"layout file not found: {path}")
        except json.JSONDecodeError as e:
            sys.exit(f"layout file {path} is not valid JSON: {e}")
        if not isinstance(data, dict):
            sys.exit(f"layout file {path}: top level must be an object")
        if isinstance(overlay.get(name), dict):
            data = deep_merge(data, overlay[name])
        return data, path

    raw, path = read("shared.json")
    fonts = {}
    fsec = _section(raw, path, "fonts")
    for role in FONT_ROLES:
        name = fsec.get(role)
        if not name or not isinstance(name, str):
            _bad(path, f"fonts.{role} must be a filename")
        p = find_font(name)
        if not os.path.exists(p):
            _bad(path, f"fonts.{role} not found: {name}")
        fonts[role] = p
    colors = {}
    csec = _section(raw, path, "colors")
    for cname in COLOR_NAMES:
        v = csec.get(cname)
        if (not isinstance(v, list) or len(v) != 3
                or any(not isinstance(x, int) or x < 0 or x > 255 for x in v)):
            _bad(path, f"colors.{cname} must be [R,G,B] with 0-255 values")
        colors[cname] = tuple(v)

    def clock_page_num(sec, path, where):
        return {"font": _font(sec, path, where, fonts),
                "color": _color(sec, path, where, colors),
                "x": _xpos(sec, path, where),
                "y": _ypos(sec, path, where)}

    L = {"fonts": fonts, "colors": colors}
    clk = _section(raw, path, "clock")
    L["clock"] = clock_page_num(clk, path, "clock")
    L["clock"]["format"] = _str(clk, path, "clock", "format")
    pgm = _section(raw, path, "page_num")
    L["page_num"] = clock_page_num(pgm, path, "page_num")
    prg = _section(raw, path, "progress")
    L["progress"] = {"color": _color(prg, path, "progress", colors),
                     "backing": _color(prg, path, "progress", colors,
                                       "backing")}

    raw1, path1 = read("page1.json")
    top = _section(raw1, path1, "top")
    seg = _section(raw1, path1, "segments")
    for k in ("time", "destination", "platform"):
        _color(seg, path1, "segments", colors, k)
    cal = _section(raw1, path1, "calling")
    if cal.get("show", "top-only") != "top-only":
        _bad(path1, "calling.show must be 'top-only'")
    rows = _section(raw1, path1, "rows")
    L["page1"] = {
        "top": {"font": _font(top, path1, "top", fonts),
                "y": _num(top, path1, "top", "y")},
        "segments": {"time": seg["time"], "destination": seg["destination"],
                     "platform": seg["platform"]},
        "calling": {"font": _font(cal, path1, "calling", fonts),
                    "color": _color(cal, path1, "calling", colors),
                    "x": _xpos(cal, path1, "calling"),
                    "show": cal.get("show", "top-only"),
                    "dy": _num(cal, path1, "calling", "dy")},
        "rows": {"font": _font(rows, path1, "rows", fonts),
                 "dy": _num(rows, path1, "rows", "dy"),
                 "pitch": _num(rows, path1, "rows", "pitch")},
    }
    exp = _section(raw1, path1, "exp")
    L["page1"]["exp"] = {"dx": _num(exp, path1, "exp", "dx"),
                         "gap": _num(exp, path1, "exp", "gap")}
    pp = _section(raw1, path1, "plat")
    plat_dx = _num(pp, path1, "plat", "dx")
    L["page1"]["plat"] = {"dx": plat_dx}
    for _secname, _sec in (("top", top), ("rows", rows)):
        _pdx = _onum(_sec, path1, _secname, "plat_dx")
        L["page1"][_secname]["plat_dx"] = \
            _pdx if _pdx is not None else plat_dx

    raw2, path2 = read("page2.json")
    hl = _section(raw2, path2, "headline")
    st = _section(raw2, path2, "status")
    nt = _section(raw2, path2, "note")
    L["page2"] = {
        "headline": {"font": _font(hl, path2, "headline", fonts),
                     "y": _num(hl, path2, "headline", "y"),
                     "time_color": _color(hl, path2, "headline", colors,
                                          "time_color"),
                     "dest_color": _color(hl, path2, "headline", colors,
                                          "dest_color"),
                     "dest_dy": _num(hl, path2, "headline", "dest_dy")},
        "status": {"font": _font(st, path2, "status", fonts),
                   "dy": _num(st, path2, "status", "dy")},
        "note": {"font": _font(nt, path2, "note", fonts),
                 "color": _color(nt, path2, "note", colors),
                 "dy": _num(nt, path2, "note", "dy")},
    }

    raw3, path3 = read("page3.json")
    hh = _section(raw3, path3, "header")
    ch = _section(raw3, path3, "coach")
    lt = _section(raw3, path3, "letters")
    L["page3"] = {
        "header": {"font": _font(hh, path3, "header", fonts),
                   "y": _num(hh, path3, "header", "y"),
                   "time_color": _color(hh, path3, "header", colors,
                                        "time_color"),
                   "dest_color": _color(hh, path3, "header", colors,
                                        "dest_color")},
        "coach": {"width": _num(ch, path3, "coach", "width"),
                  "height": _num(ch, path3, "coach", "height"),
                  "gap": _num(ch, path3, "coach", "gap"),
                  "margin": _num(ch, path3, "coach", "margin"),
                  "slant": _num(ch, path3, "coach", "slant"),
                  "dy": _num(ch, path3, "coach", "dy"),
                  "outline": _color(ch, path3, "coach", colors, "outline"),
                  "fill": _color(ch, path3, "coach", colors, "fill"),
                  "mark": _color(ch, path3, "coach", colors, "mark"),
                  "mark_off": _color(ch, path3, "coach", colors, "mark_off"),
                  "mark_dy": _num(ch, path3, "coach", "mark_dy"),
                  "default_coaches": _num(ch, path3, "coach",
                                          "default_coaches")},
        "letters": {"font": _font(lt, path3, "letters", fonts),
                    "color": _color(lt, path3, "letters", colors),
                    "dy": _num(lt, path3, "letters", "dy")},
    }
    return L


LAYOUT_FILES = ("shared.json", "page1.json", "page2.json", "page3.json",
                "local.json")


def read_local_overlay(layout_dir):
    """Untracked live overrides (layout/local.json), sparse sections."""
    lp = os.path.join(layout_dir, "local.json")
    if not os.path.exists(lp):
        return {}
    try:
        with open(lp) as f:
            local = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"layout file {lp} is not valid JSON: {e}")
    if not isinstance(local, dict):
        sys.exit(f"layout file {lp}: top level must be an object")
    return {k: v for k, v in local.items() if isinstance(v, dict)}


def load_effective_layout(layout_dir):
    """Tracked defaults overlaid with untracked local.json overrides."""
    return load_layout(layout_dir, overlay=read_local_overlay(layout_dir))


def layout_mtimes(layout_dir):
    m = {}
    for name in LAYOUT_FILES:
        try:
            m[name] = os.path.getmtime(os.path.join(layout_dir, name))
        except OSError:
            m[name] = -1
    return m


def reload_layout_files(graphics, F, C, L, layout_dir, mtimes):
    """Reload layout/*.json into F/C/L in place when files changed.

    Returns (new_mtimes, error). A bad layout keeps the old one but
    still advances mtimes (retries on the next file change, no spam).
    New font files that fail to load keep their old font.
    """
    cur = layout_mtimes(layout_dir)
    if cur == mtimes:
        return mtimes, None
    try:
        new = load_effective_layout(layout_dir)
    except SystemExit as e:
        return cur, f"layout reload failed, keeping old: {e}"
    for role in FONT_ROLES:
        try:
            f = graphics.Font()
            f.LoadFont(new["fonts"][role])
            F[role] = f
        except Exception as e:
            print(f"keep old font {role}: {e}", file=sys.stderr)
    for name, rgb in new["colors"].items():
        C[name] = graphics.Color(*rgb)
    L.clear()
    L.update(new)
    print("layout reloaded", file=sys.stderr)
    return cur, None


def resolve_x(spec, tw, W):
    if spec == "left":
        return 1
    if spec == "center":
        return max(1, (W - tw) // 2)
    if spec == "right":
        return max(1, W - tw - 1)
    return spec  # validated int


def resolve_y(spec, H):
    if type(spec) is int:
        return spec
    if spec == "bottom":
        return H - 1
    return H - int(spec[7:])  # validated bottom-N


# ---------------------------------------------------------------------------
# Matrix rendering (only imported when not in --mock mode)
# ---------------------------------------------------------------------------

def text_width(graphics, canvas, font, color, text):
    """Measure pixel width by drawing offscreen (DrawText returns width)."""
    return graphics.DrawText(canvas, font, 0, -100, color, text)


def fit_text(graphics, canvas, font, color, text, max_w):
    """Trim text until it fits max_w pixels."""
    while text and text_width(graphics, canvas, font, color, text) > max_w:
        text = text[:-1]
    return text


def run_matrix(args, L, get_board_data, layout_dir, preview_frac=None):
    from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

    options = RGBMatrixOptions()
    options.rows = args.led_rows
    options.cols = args.led_cols
    options.chain_length = args.led_chain
    options.parallel = args.led_parallel
    options.hardware_mapping = args.led_gpio_mapping
    options.brightness = args.led_brightness
    options.pwm_bits = args.led_pwm_bits
    options.limit_refresh_rate_hz = args.led_limit_refresh
    options.gpio_slowdown = args.led_slowdown_gpio
    options.led_rgb_sequence = args.led_rgb_sequence
    options.pixel_mapper_config = args.led_pixel_mapper
    options.show_refresh_rate = 1 if args.led_show_refresh else 0
    options.pwm_lsb_nanoseconds = args.led_pwm_lsb_nanoseconds
    options.pwm_dither_bits = args.led_pwm_dither_bits
    options.row_address_type = args.led_row_addr_type
    options.multiplexing = args.led_multiplexing
    options.panel_type = args.led_panel_type
    if args.led_inverse:
        options.inverse_colors = True
    if args.led_no_hardware_pulse:
        options.disable_hardware_pulsing = True
    if args.led_rp1_rio:
        options.rp1_rio = args.led_rp1_rio
    if args.led_no_drop_privs:
        options.drop_privileges = False

    # Load fonts BEFORE creating the matrix: RGBMatrix init drops
    # root privileges to 'daemon' by default, which may not be able to
    # read files under e.g. /home/kai afterwards.
    F = {}
    for role in FONT_ROLES:
        f = graphics.Font()
        f.LoadFont(L["fonts"][role])
        F[role] = f

    matrix = RGBMatrix(options=options)

    C = {name: graphics.Color(*rgb) for name, rgb in L["colors"].items()}

    offscreen = matrix.CreateFrameCanvas()
    width = offscreen.width
    height = offscreen.height
    print(f"board {width}x{height} layout={layout_dir} "
          f"control={os.path.join(THIS_DIR, 'control.json')} "
          f"pages={args.pages}", file=sys.stderr, flush=True)

    board = []
    last_fetch = 0
    last_sig = None
    idx = 0
    idx_since = time.time()
    page_idx = 0
    page_since = time.time()
    was_held = False

    control_path = os.path.join(THIS_DIR, "control.json")

    layout_mt = layout_mtimes(layout_dir)
    control_mt = -1
    paused_page = None
    last_hot_check = 0.0
    last_hot_err = None

    def check_hot():
        """Hot-reload layout/*.json + read pause state.

        Throttled, never fatal: bad files keep the old layout, bad
        control.json means keep cycling. Returns paused page or None.
        """
        nonlocal layout_mt, control_mt, paused_page, last_hot_check
        nonlocal last_hot_err
        now = time.time()
        if now - last_hot_check < 0.5:
            return paused_page
        last_hot_check = now
        layout_mt, err = reload_layout_files(
            graphics, F, C, L, layout_dir, layout_mt)
        if err != last_hot_err:
            last_hot_err = err
            if err:
                print(err, file=sys.stderr)
        if all(v == -1 for v in layout_mt.values()):
            if not getattr(check_hot, "_perm_warned", False):
                check_hot._perm_warned = True
                print("layout files unreadable after privilege drop "
                      "(running as 'daemon'?) - live editing disabled. "
                      "Run with --led-no-drop-privs or fix permissions.",
                      file=sys.stderr, flush=True)
        try:
            mt = os.path.getmtime(control_path)
        except OSError as e:
            if (e.errno in (errno.EACCES, errno.EPERM)
                    and not getattr(check_hot, "_ctl_warned", False)):
                check_hot._ctl_warned = True
                print("control.json unreadable after privilege drop "
                      "(running as 'daemon'?) - pause disabled. "
                      "Run with --led-no-drop-privs or fix permissions.",
                      file=sys.stderr, flush=True)
            paused_page = None
            control_mt = -1
            return paused_page
        if mt != control_mt:
            control_mt = mt
            paused_page = None
            ctl_source = None
            try:
                with open(control_path) as f:
                    ctl = json.load(f)
                pg = ctl.get("page")
                if pg in (1, 2, 3):
                    paused_page = pg
                src = ctl.get("source")
                if src in SOURCES:
                    ctl_source = src
            except Exception as e:
                print(f"bad control.json, ignoring: {e}", file=sys.stderr)
        if paused_page != getattr(check_hot, "_announced", "init"):
            check_hot._announced = paused_page
            if paused_page:
                print(f"control: holding page {paused_page}",
                      file=sys.stderr, flush=True)
            else:
                print("control: cycling pages", file=sys.stderr, flush=True)
        return paused_page

    def status_colors(raw):
        if raw.get("is_cancelled"):
            return C["text"], C["alert"]
        if raw.get("is_delayed"):
            return C["text"], C["alert"]
        return C["text"], C["ok"]

    def clock_geom():
        """Live clock text + centered geometry, pinned to the bottom."""
        spec = L["clock"]
        s = time.strftime(spec["format"])
        fnt = F[spec["font"]]
        w = text_width(graphics, offscreen, fnt, C[spec["color"]], s)
        return s, fnt, resolve_x(spec["x"], w, width), w

    def page_label_geom(page):
        """Measure the page indicator without drawing it."""
        spec = L["page_num"]
        lab = f"{page}/{len(args.pages)}"
        fnt = F[spec["font"]]
        w = text_width(graphics, offscreen, fnt, C["text"], lab)
        return lab, fnt, resolve_x(spec["x"], w, width), w, \
            resolve_y(spec["y"], height)

    def draw_page_chrome(page, frac):
        """Progress bar behind the page number, digits knocking it out.
        frac None hides the bar (single page); 1.0 = held page."""
        P = L["progress"]
        lab, fnt, x, w, y = page_label_geom(page)
        if frac is not None:
            back, fill = C[P["backing"]], C[P["color"]]
            x0, x1 = x - 4, x + w + 4
            y0 = y - fnt.height + 1 - 1
            for yy in range(max(0, y0), min(height, y + 1)):
                for xx in range(max(0, x0), min(width, x1 + 1)):
                    offscreen.SetPixel(xx, yy, back.red,
                                       back.green, back.blue)
            fw = int((x1 - x0 + 1) * frac)
            for yy in range(max(0, y0), min(height, y + 1)):
                for xx in range(x0, min(x1 + 1, x0 + fw)):
                    offscreen.SetPixel(xx, yy, fill.red,
                                       fill.green, fill.blue)
        graphics.DrawText(offscreen, fnt, x, y, C["mark"], lab)

    def draw_row(dep, fnt, seg, y_base, clock_x=None, right_extra=0,
                 plat_dx=0):
        """Service row: time + platform + destination + status, all in
        the row font. A flipped Exp renders as two parts -- the Exp
        label slides by layout exp.dx, the time stays put."""
        t, dest, status, raw = format_departure(dep)
        status = live_status(raw, args.flip_seconds)
        _, sub_c = status_colors(raw)
        exp_txt, time_txt = None, status
        if status.startswith("Exp ") and raw.get("is_delayed"):
            exp_txt, time_txt = "Exp", status[4:]
        exp = L["page1"]["exp"]
        plat = (raw.get("platform") or "").strip()
        plat_part = (plat + " ") if plat else ""
        plat_w = text_width(graphics, offscreen, fnt, seg["platform"],
                            plat_part)
        exp_w = text_width(graphics, offscreen, fnt, sub_c, exp_txt) \
            if exp_txt else 0
        time_w = text_width(graphics, offscreen, fnt, sub_c, time_txt)
        # right-aligned [plat][Exp][time] block; Exp slides by exp.dx
        # (0 = snug). Destination clears the leftmost ink of the block.
        right_w = plat_w + time_w + right_extra
        if exp_txt:
            right_w += exp_w + exp["gap"]
        sub_x = max(1, width - right_w - 1)
        plat_x = sub_x + plat_dx
        if plat:
            graphics.DrawText(offscreen, fnt, plat_x, y_base,
                              seg["platform"], plat_part)
        time_x = sub_x + plat_w + (exp_w + exp["gap"] if exp_txt else 0)
        left_ink = min(sub_x, plat_x)
        if exp_txt:
            exp_x = sub_x + plat_w + exp["gap"] + exp["dx"]
            left_ink = min(left_ink, exp_x)
            graphics.DrawText(offscreen, fnt, exp_x, y_base, sub_c, exp_txt)
        graphics.DrawText(offscreen, fnt, time_x, y_base, sub_c, time_txt)
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, fnt, 1, y_base,
                                   seg["time"], t_part)
        max_dest = left_ink - w_time - 2
        if clock_x is not None:
            max_dest = min(max_dest, clock_x - w_time - 3)
        dest = fit_text(graphics, offscreen, fnt, seg["destination"], dest,
                        max(0, max_dest))
        graphics.DrawText(offscreen, fnt, 1 + w_time, y_base,
                          seg["destination"], dest)

    def draw_calling(dep, spec, y_base):
        """Calling-at line, static (truncated to fit)."""
        if dep.get("calling_at"):
            fnt = F[spec["font"]]
            col = C[spec["color"]]
            graphics.DrawText(offscreen, fnt, resolve_x(spec["x"], 0, width),
                              y_base, col,
                              fit_text(graphics, offscreen, fnt, col,
                                       dep["calling_at"], width - 2))

    def draw_full(dep, y0):
        """One departure in full detail, starting at vertical offset y0."""
        seg = {k: C[v] for k, v in L["page1"]["segments"].items()}
        fnt = F[L["page1"]["top"]["font"]]
        draw_row(dep, fnt, seg, y0 + 1 + fnt.baseline,
                 plat_dx=L["page1"]["top"]["plat_dx"])
        draw_calling(dep, {"font": L["page1"]["calling"]["font"],
                           "color": L["page1"]["calling"]["color"],
                           "x": "left"},
                     y0 + 1 + fnt.baseline + F["small"].height + 1)

    def draw_page2(dep, page, frac):
        """Next departure nice and big, with API notes underneath."""
        P2 = L["page2"]
        bfont = F[P2["headline"]["font"]]
        sfont = F[P2["status"]["font"]]
        t, dest, _, raw = format_departure(dep)
        _, sub_c = status_colors(raw)

        y_big = P2["headline"]["y"]
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, bfont, 1, y_big,
                                   C[P2["headline"]["time_color"]], t_part)
        dest = fit_text(graphics, offscreen, bfont,
                        C[P2["headline"]["dest_color"]], dest,
                        width - w_time - 1)
        graphics.DrawText(offscreen, bfont, 1 + w_time,
                          y_big + P2["headline"]["dest_dy"],
                          C[P2["headline"]["dest_color"]], dest)

        status = live_status(raw, args.flip_seconds)
        line2 = status
        stfont = F[P2["status"]["font"]]
        y2 = y_big + stfont.height + P2["status"]["dy"]
        graphics.DrawText(offscreen, stfont, 1, y2, sub_c,
                          fit_text(graphics, offscreen, stfont, sub_c,
                                   line2, width - 2))

        clock_s, clock_fnt, clock_x, clock_w = clock_geom()
        page_w = page_label_geom(page)[3]
        nfont = F[P2["note"]["font"]]
        ncol = C[P2["note"]["color"]]
        notes = list(dep.get("note_lines") or [])
        if not notes and dep.get("calling_at"):
            notes = [dep["calling_at"]]
        if not notes:
            notes = [f"{raw.get('headcode', '')} "
                     f"{raw.get('service_type_name', '')}".strip()]
        y3 = y2 + nfont.height + P2["note"]["dy"]
        if notes and y3 < height + 1:
            share3 = y3 >= height - nfont.height
            cap = min(width - 2, width - page_w - 4)
            if share3:
                cap = min(cap, clock_x - 3)
            graphics.DrawText(offscreen, nfont, 1, y3, ncol,
                              fit_text(graphics, offscreen, nfont, ncol,
                                       notes[0], max(0, cap)))
        graphics.DrawText(offscreen, clock_fnt, clock_x,
                          resolve_y(L["clock"]["y"], height),
                          C[L["clock"]["color"]], clock_s)
        draw_page_chrome(page, frac)

    def draw_page3(dep, page, frac):
        """Train formation diagram: fixed-width coach cards, pointy front
        car, 1ST/wheelchair markers inside, letters underneath."""
        P3 = L["page3"]
        hfont = F[P3["header"]["font"]]
        sfont = F[P3["letters"]["font"]]
        t, dest, _, raw = format_departure(dep)
        cars, label = formation_of(
            dep, default_cars=P3["coach"]["default_coaches"])
        n = len(cars)

        # header in the main font, full width
        y_head = P3["header"]["y"]
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, hfont, 1, y_head,
                                   C[P3["header"]["time_color"]], t_part)
        graphics.DrawText(offscreen, hfont, 1 + w_time, y_head,
                          C[P3["header"]["dest_color"]],
                          fit_text(graphics, offscreen, hfont,
                                   C[P3["header"]["dest_color"]], dest,
                                   width - w_time - 1))

        # coach cards row
        margin, gap_b, bh, slant = (
            P3["coach"]["margin"], P3["coach"]["gap"],
            P3["coach"]["height"], P3["coach"]["slant"])
        bw = P3["coach"]["width"]
        outline = C[P3["coach"]["outline"]]
        fill = C[P3["coach"]["fill"]]
        tfont = F["tiny"]
        x0 = margin
        y_top = y_head + P3["coach"]["dy"]
        yb = y_top + bh - 1

        def draw_wheelchair(cx, it, col):
            """~7x8 side-view wheelchair pictogram, top row it. Fits the
            8px coach interior exactly (no outline clip)."""
            ln = lambda x0, y0, x1, y1: graphics.DrawLine(
                offscreen, x0, y0, x1, y1, col)
            px = lambda x, y: offscreen.SetPixel(
                x, y, col.red, col.green, col.blue)
            px(cx - 2, it)                    # head
            ln(cx - 2, it + 1, cx - 2, it + 3)  # backrest
            ln(cx - 2, it + 3, cx + 2, it + 3)  # seat
            ln(cx + 2, it + 3, cx + 2, it + 5)  # footrest
            graphics.DrawCircle(offscreen, cx - 1, it + 5, 2, col)  # wheel
            px(cx + 3, it + 6)                 # caster

        for i, car in enumerate(cars):
            x = x0 + i * (bw + gap_b)
            x1 = x + bw - 1
            cx = x + bw // 2 + (slant // 2 if i == 0 else 0)
            # capacity fill from the bottom (min 1px when loaded)
            fill_h = int((bh - 2) * car["capacity"])
            if car["capacity"] > 0 and fill_h < 1:
                fill_h = 1
            if fill_h > 0:
                for yy in range(max(y_top + 1, yb - fill_h), yb):
                    if i == 0:
                        frac = (yy - y_top) / max(1, bh - 1)
                        xs = x + 1 + int(slant * (1 - frac))
                    else:
                        xs = x + 1
                    for xx in range(xs, x1):
                        offscreen.SetPixel(xx, yy, fill.red,
                                           fill.green, fill.blue)
            if i == 0:
                graphics.DrawLine(offscreen, x, yb, x1, yb, outline)
                graphics.DrawLine(offscreen, x1, y_top, x1, yb, outline)
                graphics.DrawLine(offscreen, x + slant, y_top, x1, y_top,
                                  outline)
                graphics.DrawLine(offscreen, x, yb, x + slant, y_top, outline)
            else:
                graphics.DrawLine(offscreen, x, y_top, x1, y_top, outline)
                graphics.DrawLine(offscreen, x, yb, x1, yb, outline)
                graphics.DrawLine(offscreen, x, y_top, x, yb, outline)
                graphics.DrawLine(offscreen, x1, y_top, x1, yb, outline)
            # class markers sit mark_dy lower, amber with a black
            # border so the capacity fill never muddies them
            mdy = P3["coach"]["mark_dy"]
            blot = C["mark"]
            if car["first"]:
                tw = text_width(graphics, offscreen, tfont, outline, "1ST")
                tx, ty = cx - tw // 2, y_top + bh // 2 + 2 + mdy
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    graphics.DrawText(offscreen, tfont, tx + ox, ty + oy,
                                      blot, "1ST")
                graphics.DrawText(offscreen, tfont, tx, ty, outline, "1ST")
            elif car["accessible"]:
                it = y_top + 1 + mdy
                for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    draw_wheelchair(cx + ox, it + oy, blot)
                draw_wheelchair(cx, it, outline)

        clock_s, clock_fnt, clock_x, clock_w = clock_geom()
        page_w = page_label_geom(page)[3]

        # carriage letter under each car, centered; baseline may sit on
        # the last row (safe: capitals never descend, canvas clips)
        lcol = C[P3["letters"]["color"]]
        y_lab = yb + 1 + P3["letters"]["dy"]
        if y_lab <= height:
            for i, car in enumerate(cars):
                x = x0 + i * (bw + gap_b)
                letter = "ABCDEFGH"[i] if i < 8 else str(i + 1)
                lw = text_width(graphics, offscreen, sfont, lcol, letter)
                graphics.DrawText(offscreen, sfont,
                                  x + bw // 2 - lw // 2, y_lab,
                                  lcol, letter)

        graphics.DrawText(offscreen, clock_fnt, clock_x,
                          resolve_y(L["clock"]["y"], height),
                          C[L["clock"]["color"]], clock_s)
        draw_page_chrome(page, frac)

    def draw_static(page, frac):
        # Lead service + calling-at, then compact rows on a fixed pitch.
        # All positions from layout/page1.json.
        P1 = L["page1"]
        rows = board[:args.limit]
        if not rows:
            return
        seg = {k: C[v] for k, v in P1["segments"].items()}
        rfont = F[P1["rows"]["font"]]
        clock_s, clock_fnt, clock_x, clock_w = clock_geom()
        page_w = page_label_geom(page)[3]

        y = P1["top"]["y"]
        draw_row(rows[0], F[P1["top"]["font"]], seg, y)
        cal = P1["calling"]
        cy = y + cal["dy"]
        if cal["show"] == "top-only":
            draw_calling(rows[0], cal, cy)
        y = cy + P1["rows"]["dy"]
        # second slot rotates through the remaining services
        # (2nd/3rd swap every --rotate-seconds): two visible max
        slot = rows[1:]
        clock_drawn = False
        if slot:
            dep = slot[int(time.time() // args.rotate_seconds) % len(slot)]
            yy = y
            if yy >= height:
                if time.time() - getattr(draw_static, "_warned", 0) > 60:
                    print("warning: second service row off-screen "
                          "- check layout/page1.json rows/dy?",
                          file=sys.stderr)
                    draw_static._warned = time.time()
            else:
                # share the row only if a separate clock line would hit
                # it (threshold from the clock font, not the row font)
                share = yy >= height - clock_fnt.height + 1
                draw_row(dep, rfont, seg, yy,
                         clock_x=clock_x if share else None,
                         right_extra=page_w + 2,
                         plat_dx=L["page1"]["rows"]["plat_dx"])
                # clock pinned to the bottom (shares the row on 32px)
                graphics.DrawText(offscreen, clock_fnt, clock_x,
                                  yy if share else resolve_y(
                                      L["clock"]["y"], height),
                                  C[L["clock"]["color"]], clock_s)
                clock_drawn = True
        if not clock_drawn:
            # single service (or rows off-screen) - clock still shows
            graphics.DrawText(offscreen, clock_fnt, clock_x,
                              resolve_y(L["clock"]["y"], height),
                              C[L["clock"]["color"]], clock_s)
        draw_page_chrome(page, frac)

    while True:
        now = time.time()
        paused = check_hot()
        if now - last_fetch >= args.refresh or not board:
          source = ctl_source or args.source
          if source != last_source:
              if last_source is not None:
                  print(f"data source: {source}", file=sys.stderr, flush=True)
              last_source = source
              board, last_sig, last_fetch = [], None, 0
          interval = (args.refresh if source == "htrs"
                      else max(args.refresh, args.rtt_refresh))
          if not board and source == "htrs":
              interval = min(interval, 5)  # quick retry after a failed pull
          if now - last_fetch >= interval:
              try:
                  fresh = get_board_data(source)
              except Exception as e:  # keep old data, show error briefly
                  print(f"Fetch failed ({source}): {e}", file=sys.stderr)
                  fresh = None
              if fresh is not None:
                  sig = board_signature(fresh)
                  if sig != last_sig:
                      board = fresh
                      last_sig = sig
                      idx = 0
                      idx_since = now
              last_fetch = now

        held = paused in (1, 2, 3) and not getattr(
            args, "ignore_control", False)
        if held:
            # held from the web UI (control.json): stay put
            cur_page = paused
            was_held = True
        else:
            if was_held:
                # fresh dwell on unpause
                was_held = False
                page_since = now
            if len(args.pages) > 1 and now - page_since >= args.page_seconds:
                page_idx = (page_idx + 1) % len(args.pages)
                page_since = now
            cur_page = args.pages[page_idx % len(args.pages)]

        if preview_frac is not None:
            frac = preview_frac
        elif len(args.pages) < 2:
            frac = None
        elif held:
            frac = 1.0
        else:
            frac = max(0.0, min(1.0, (now - page_since)
                                / max(0.1, args.page_seconds)))

        offscreen.Fill(0, 0, 0)

        if not board:
            graphics.DrawText(offscreen, F["top"], 2, 1 + F["top"].baseline,
                  C["alert"], f"No departures [{source.upper()}]")
            draw_page_chrome(cur_page, frac)
        elif cur_page == 2:
            draw_page2(board[0], cur_page, frac)
        elif cur_page == 3:
            draw_page3(board[0], cur_page, frac)
        elif args.layout == "static":
            draw_static(cur_page, frac)
        else:
            if len(board) > 1 and now - idx_since >= args.rotate_seconds:
                idx = (idx + 1) % len(board)
                idx_since = now
            draw_full(board[idx % len(board)], 0)
            draw_page_chrome(cur_page, frac)

        offscreen = matrix.SwapOnVSync(offscreen)

        if args.once:
            break
        time.sleep(0.08)


def main():
    p = argparse.ArgumentParser(description="Peak Rail departure board")
    p.add_argument("--railway", default="PR")
    p.add_argument("--station", default="RWS")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--date", default="2026-10-04",
                   help='Operating date YYYY-MM-DD. Use "" for live/next-from-now.')
    p.add_argument("--refresh", type=int, default=20,
                   help="Seconds between API pulls (default 20). The display "
                        "only updates when the data actually changes.")
    p.add_argument("--layout-dir", default=os.path.join(THIS_DIR, "layout"),
                   help="Fonts, colours, text lines and positions "
                        "(default layout/ next to the script)")
    p.add_argument("--layout", default="static", choices=["rotate", "static"],
                   help="'static': all departures at once, 2 lines each "
                        "(main + calling-at). 'rotate': one full-detail "
                        "departure at a time.")
    p.add_argument("--rotate-seconds", type=float, default=5,
                   help="Seconds per departure in rotate layout (default 5)")
    p.add_argument("--flip-seconds", type=float, default=3,
                   help="Seconds per side when flipping Delayed/expected time")
    p.add_argument("--pages", default="1,2,3",
                   help="Comma-separated pages to cycle, e.g. '1,2,3' or '1'. "
                        "Page 1 = board, 2 = next departure big, "
                        "3 = formation diagram.")
    p.add_argument("--page-seconds", type=float, default=10,
                   help="Seconds per page when cycling")
    p.add_argument("--mock", action="store_true",
                   help="Print to console instead of driving the LED matrix")
    p.add_argument("--once", action="store_true",
                   help="Fetch and draw once, then exit")
    p.add_argument("--preview", action="store_true",
                   help="ASCII preview of every page (no hardware needed)")
    # Matrix flags (mirrors SampleBase from the examples)
    p.add_argument("--led-rows", type=int, default=40)
    p.add_argument("--led-cols", type=int, default=80)
    p.add_argument("--led-chain", type=int, default=3,
                   help="You have 3 panels chained, so default is 3")
    p.add_argument("--led-parallel", type=int, default=1)
    p.add_argument("--led-gpio-mapping", default="regular")
    p.add_argument("--led-brightness", type=int, default=100)
    p.add_argument("--led-pwm-bits", type=int, default=11)
    p.add_argument("--led-limit-refresh", type=int, default=0)
    p.add_argument("--led-slowdown-gpio", type=int, default=1)
    p.add_argument("--led-rgb-sequence", default="RGB")
    p.add_argument("--led-pixel-mapper", default="")
    p.add_argument("--led-show-refresh", action="store_true")
    p.add_argument("--led-no-drop-privs", action="store_true")
    p.add_argument("--led-no-hardware-pulse", action="store_true",
                   help="Don't use hardware pin-pulse generation. "
                        "Avoids the snd_bcm2835 sound-module conflict, "
                        "but with more flicker.")
    p.add_argument("--led-rp1-rio", type=int, default=0, choices=[0, 1],
                   help="On Pi 5, use experimental RP1 RIO backend instead of PIO. 0=PIO, 1=RIO.")
    p.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=130)
    p.add_argument("--led-pwm-dither-bits", type=int, default=0)
    p.add_argument("--led-row-addr-type", type=int, default=0)
    p.add_argument("--led-multiplexing", type=int, default=0)
    p.add_argument("--led-panel-type", default="")
    p.add_argument("--led-inverse", action="store_true",
                   help="Switch if your matrix has inverse colors on.")
    p.add_argument("--source", default="htrs", choices=SOURCES,
                   help="Data source at startup; switch live with "
                        '{"source": "rtt"} in control.json')
    p.add_argument("--rtt-station", default="MAT",
                   help="Network Rail station code for RTT mode")
    p.add_argument("--rtt-refresh", type=int, default=60,
                   help="Minimum seconds between RTT pulls (default 60)")
    args = p.parse_args()

    try:
        pages = [int(x) for x in args.pages.split(",") if x.strip()]
    except ValueError:
        sys.exit("--pages must be comma-separated numbers, e.g. '1,2'")
    if not pages or any(x not in (1, 2, 3) for x in pages):
        sys.exit("--pages must be a combination of 1, 2 and 3")
    args.pages = pages

    L = load_effective_layout(args.layout_dir)

    if args.date == "":
        args.date = None

    rtt.set_token(rtt.load_token())

    def get_board_data(source=None):
        if (source or args.source) == "rtt":
            return rtt.get_departures(args.rtt_station, args.limit)
        deps = fetch_departures(args.railway, args.station,
                                args.limit, args.date)
        return enrich_with_timetables(deps, args.railway, args.date)
    if args.preview:
        import copy
        import preview
        try:
            board = get_board_data()
        except Exception as e:
            sys.exit(f"API fetch failed: {e}")
        W = args.led_cols * args.led_chain
        H = args.led_rows
        for pg in args.pages:
            rec = preview.install(W, H)
            a2 = copy.copy(args)
            a2.pages = [pg]
            a2.once = True
            a2.ignore_control = True
            run_matrix(a2, L, lambda: board, args.layout_dir,
                       preview_frac=0.5)
            print(f"--- page {pg} preview ({W}x{H}) ---")
            rec.report()
        return

    if args.mock:
        try:
            board = get_board_data()
        except Exception as e:
            sys.exit(f"API fetch failed: {e}")
        print(f"# {args.railway}/{args.station} date={args.date or 'live'}")
        print(format_console(board))
        if 2 in args.pages and board:
            d = board[0]
            t, dest, status, raw = format_departure(d)
            print("--- page 2 ---")
            print(f"{t} {dest}")
            plat = (raw.get("platform") or "").strip()
            print(f"{('Plat ' + plat + ' ') if plat else ''}{status}", end="")
            if raw.get("is_delayed"):
                print(f" (Exp {expected_time(raw)})")
            else:
                print()
            notes = list(d.get("note_lines") or [])
            if not notes and d.get("calling_at"):
                notes = [d["calling_at"]]
            for n in notes[:2]:
                print(f"  {n}")
        if 3 in args.pages and board:
            d = board[0]
            t, dest, _, _ = format_departure(d)
            cars, label = formation_of(
                d, default_cars=L["page3"]["default_coaches"])
            print("--- page 3 ---")
            print(f"{t} {dest} ({label})")
            cells = []
            for i, c in enumerate(cars):
                letter = "ABCDEFGH"[i] if i < 8 else str(i + 1)
                tag = ("1ST " if c["first"] else "") + \
                      ("WCHR " if c["accessible"] else "")
                cells.append(f"{letter}[{tag}{c['capacity']:.0%}]")
            print("FRONT>" + "".join(cells))
        if not args.once:
            # keep polling in mock mode so you can watch it update
            try:
                while True:
                    time.sleep(args.refresh)
                    board = get_board_data()
                    print("---")
                    print(format_console(board))
            except KeyboardInterrupt:
                pass
        return

    run_matrix(args, L, get_board_data, args.layout_dir)


if __name__ == "__main__":
    main()
