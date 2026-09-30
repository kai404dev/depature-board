#!/usr/bin/env python3
"""
Peak Rail departures board for Waveshare RGB-Matrix / rpi-rgb-led-matrix.

Three files work together:
  api.py         fetching + formatting of the departures/timetable APIs
  layout.json    all fonts, colours, text lines and positions (edit this
                 to move things -- no code changes needed)
  departures.py  this file: argument parsing + LED matrix rendering

Calls:
  https://peakraildepartures.com/api/departures/?railway=PR&station=RWS&limit=3&date=2026-10-04

and for each departure its timetable, e.g.:
  https://peakraildepartures.com/api/timetable/2M03/?date=2026-10-04&railway=PR

Based on the example code in:
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/text-example.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/clock.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/bindings/python/samples/runtext.py

Usage on Pi (3 panels chained):
  sudo python3 departures.py --led-rows 32 --led-cols 64 --led-chain 3

Test on Mac / without hardware:
  python3 departures.py --mock --once
  python3 departures.py --mock

Custom look:
  python3 departures.py --layout-file my-layout.json --mock
"""

import argparse
import json
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

FONT_ROLES = ("top", "row", "small", "big", "tiny")
COLOR_NAMES = ("text", "time", "platform", "ok", "alert", "mark")


def load_layout(path):
    """Load + validate layout.json. Exits with a clear message on error."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        sys.exit(f"layout file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"layout file {path} is not valid JSON: {e}")
    if not isinstance(raw, dict):
        sys.exit(f"layout file {path}: top level must be an object")

    def section(name):
        sec = raw.get(name)
        if not isinstance(sec, dict):
            sys.exit(f"layout file {path}: missing section '{name}'")
        return sec

    def num(sec_name, key):
        v = section(sec_name).get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            sys.exit(f"layout file {path}: {sec_name}.{key} "
                     f"must be a whole number of LEDs")
        return v

    def txt(sec_name, key):
        v = section(sec_name).get(key)
        if not isinstance(v, str):
            sys.exit(f"layout file {path}: {sec_name}.{key} must be a string")
        return v

    fonts = {}
    fsec = section("fonts")
    for role in FONT_ROLES:
        name = fsec.get(role)
        if not name or not isinstance(name, str):
            sys.exit(f"layout file {path}: fonts.{role} must be a filename")
        p = find_font(name)
        if not os.path.exists(p):
            sys.exit(f"layout file {path}: fonts.{role} not found: {name}")
        fonts[role] = p

    colors = {}
    csec = section("colors")
    for cname in COLOR_NAMES:
        v = csec.get(cname)
        if (not isinstance(v, list) or len(v) != 3
                or any(not isinstance(x, int) or x < 0 or x > 255 for x in v)):
            sys.exit(f"layout file {path}: colors.{cname} "
                     f"must be [R,G,B] with 0-255 values")
        colors[cname] = tuple(v)

    return {
        "fonts": fonts,
        "colors": colors,
        "page1": {
            "top_dy": num("page1", "top_dy"),
            "calling_dy": num("page1", "calling_dy"),
            "row_gap": num("page1", "row_gap"),
        },
        "page2": {
            "dest_dy": num("page2", "dest_dy"),
            "status_dy": num("page2", "status_dy"),
            "note_dy": num("page2", "note_dy"),
        },
        "page3": {
            "coach_width": num("page3", "coach_width"),
            "coach_height": num("page3", "coach_height"),
            "coach_gap": num("page3", "coach_gap"),
            "coach_margin": num("page3", "coach_margin"),
            "slant": num("page3", "slant"),
            "boxes_dy": num("page3", "boxes_dy"),
            "labels_dy": num("page3", "labels_dy"),
            "default_coaches": num("page3", "default_coaches"),
        },
        "clock": {
            "format": txt("clock", "format"),
            "dy": num("clock", "dy"),
        },
        "page_num": {
            "dy": num("page_num", "dy"),
        },
        "progress": {
            "width": num("progress", "width"),
            "height": num("progress", "height"),
            "dy": num("progress", "dy"),
        },
    }


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


def run_matrix(args, L, get_board_data):
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
    font, mfont, sfont, bfont, tfont = (
        F["top"], F["row"], F["small"], F["big"], F["tiny"])

    matrix = RGBMatrix(options=options)

    C = {name: graphics.Color(*rgb) for name, rgb in L["colors"].items()}
    amber, yellow, red, green, black = (
        C["text"], C["time"], C["alert"], C["ok"], C["mark"])
    platform_c = C["platform"]

    offscreen = matrix.CreateFrameCanvas()
    width = offscreen.width
    height = offscreen.height

    board = []
    last_fetch = 0
    last_sig = None
    idx = 0
    idx_since = time.time()
    page_idx = 0
    page_since = time.time()

    def status_colors(raw):
        if raw.get("is_cancelled"):
            return amber, red
        if raw.get("is_delayed"):
            return amber, red
        return amber, green

    def clock_geom():
        """Live clock text + centered geometry, pinned to the bottom."""
        s = time.strftime(L["clock"]["format"])
        w = text_width(graphics, offscreen, sfont, amber, s)
        return s, max(1, (width - w) // 2), w

    def draw_page_num(n):
        """Page indicator bottom-right. Returns its pixel width."""
        lab = f"{n}/{len(args.pages)}"
        w = text_width(graphics, offscreen, tfont, amber, lab)
        graphics.DrawText(offscreen, tfont, width - w - 1,
                          height + L["page_num"]["dy"], amber, lab)
        return w

    def draw_row(dep, fnt, y_base, clock_x=None, right_extra=0):
        """Service main line: time + platform yellow, destination orange,
        status green/red. clock_x caps the destination so it never runs
        under the centered clock; right_extra reserves pixels on the
        right (page number)."""
        t, dest, status, raw = format_departure(dep)
        status = live_status(raw, args.flip_seconds)
        _, sub_c = status_colors(raw)
        plat = (raw.get("platform") or "").strip()
        plat_part = (plat + " ") if plat else ""
        right_w = text_width(graphics, offscreen, fnt, amber, plat_part)
        right_w += text_width(graphics, offscreen, fnt, sub_c, status)
        right_w += right_extra
        sub_x = max(1, width - right_w - 1)
        if plat:
            graphics.DrawText(offscreen, fnt, sub_x, y_base,
                              platform_c, plat_part)
        graphics.DrawText(offscreen, fnt, sub_x + text_width(
            graphics, offscreen, fnt, amber, plat_part),
            y_base, sub_c, status)
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, fnt, 1, y_base,
                                   yellow, t_part)
        max_dest = sub_x - w_time - 2
        if clock_x is not None:
            max_dest = min(max_dest, clock_x - w_time - 3)
        dest = fit_text(graphics, offscreen, fnt, amber, dest,
                        max(0, max_dest))
        graphics.DrawText(offscreen, fnt, 1 + w_time, y_base,
                          amber, dest)

    def draw_calling(dep, y_base):
        """Calling-at line in the small font, static (truncated to fit)."""
        if dep.get("calling_at"):
            graphics.DrawText(offscreen, sfont, 1, y_base, amber,
                              fit_text(graphics, offscreen, sfont, amber,
                                       dep["calling_at"], width - 2))

    def draw_full(dep, y0):
        """One departure in full detail, starting at vertical offset y0."""
        draw_row(dep, font, y0 + 1 + font.baseline)
        draw_calling(dep, y0 + 1 + font.baseline + sfont.height + 1)

    def draw_page2(dep, page):
        """Next departure nice and big, with API notes underneath.
        Live clock bottom-middle, page number bottom-right."""
        t, dest, _, raw = format_departure(dep)
        _, sub_c = status_colors(raw)
        plat = (raw.get("platform") or "").strip()

        y_big = bfont.baseline + 1
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, bfont, 1, y_big,
                                   yellow, t_part)
        dest = fit_text(graphics, offscreen, bfont, amber, dest,
                        width - w_time - 1)
        graphics.DrawText(offscreen, bfont, 1 + w_time,
                          y_big + L["page2"]["dest_dy"], amber, dest)

        status = live_status(raw, args.flip_seconds)
        plat_part = (f"Plat {plat} " if plat else "")
        line2 = plat_part + status
        y2 = y_big + sfont.height + L["page2"]["status_dy"]
        graphics.DrawText(offscreen, sfont, 1, y2, sub_c,
                          fit_text(graphics, offscreen, sfont, sub_c,
                                   line2, width - 2))

        clock_s, clock_x, clock_w = clock_geom()
        page_w = draw_page_num(page)
        notes = list(dep.get("note_lines") or [])
        if not notes and dep.get("calling_at"):
            notes = [dep["calling_at"]]
        if not notes:
            notes = [f"{raw.get('headcode', '')} "
                     f"{raw.get('service_type_name', '')}".strip()]
        y3 = y2 + sfont.height + L["page2"]["note_dy"]
        if notes and y3 < height + 1:
            share3 = y3 >= height - sfont.height
            cap = min(width - 2, width - page_w - 4)
            if share3:
                cap = min(cap, clock_x - 3)
            graphics.DrawText(offscreen, sfont, 1, y3, amber,
                              fit_text(graphics, offscreen, sfont, amber,
                                       notes[0], max(0, cap)))
        graphics.DrawText(offscreen, sfont, clock_x,
                          height + L["clock"]["dy"], amber, clock_s)

    def draw_page3(dep, page):
        """Train formation diagram: fixed-width coach cards, pointy front
        car, 1ST/wheelchair markers inside, letters underneath.
        Live clock bottom-middle, page number bottom-right."""
        t, dest, _, raw = format_departure(dep)
        cars, label = formation_of(
            dep, default_cars=L["page3"]["default_coaches"])
        n = len(cars)

        # header in the standard main font, full width (the coach
        # count is visible from the cards, no room for a side label)
        y_head = font.baseline
        t_part = t + " "
        w_time = graphics.DrawText(offscreen, font, 1, y_head,
                                   yellow, t_part)
        graphics.DrawText(offscreen, font, 1 + w_time, y_head, amber,
                          fit_text(graphics, offscreen, font, amber, dest,
                                   width - w_time - 1))

        # coach cards row: fixed width, left-aligned so the centered
        # clock and page number never collide with them. First car gets
        # a pointy (slanted) front. All outlines amber, all fills yellow
        # at capacity height; class markers inside auto-contrast.
        P3 = L["page3"]
        margin, gap_b, bh, slant = (
            P3["coach_margin"], P3["coach_gap"], P3["coach_height"],
            P3["slant"])
        bw = P3["coach_width"]
        x0 = margin
        y_top = y_head + P3["boxes_dy"]
        yb = y_top + bh - 1

        def draw_wheelchair(cx, it, col):
            """~7x9 side-view wheelchair pictogram, top row it."""
            ln = lambda x0, y0, x1, y1: graphics.DrawLine(
                offscreen, x0, y0, x1, y1, col)
            px = lambda x, y: offscreen.SetPixel(
                x, y, col.red, col.green, col.blue)
            px(cx - 2, it)                    # head
            ln(cx - 2, it + 1, cx - 2, it + 4)  # backrest
            ln(cx - 2, it + 4, cx + 2, it + 4)  # seat
            ln(cx + 2, it + 4, cx + 2, it + 6)  # footrest
            graphics.DrawCircle(offscreen, cx - 1, it + 6, 2, col)  # wheel
            px(cx + 3, it + 7)                 # caster

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
                        offscreen.SetPixel(xx, yy, yellow.red,
                                           yellow.green, yellow.blue)
            if i == 0:
                graphics.DrawLine(offscreen, x, yb, x1, yb, amber)
                graphics.DrawLine(offscreen, x1, y_top, x1, yb, amber)
                graphics.DrawLine(offscreen, x + slant, y_top, x1, y_top,
                                  amber)
                graphics.DrawLine(offscreen, x, yb, x + slant, y_top, amber)
            else:
                graphics.DrawLine(offscreen, x, y_top, x1, y_top, amber)
                graphics.DrawLine(offscreen, x, yb, x1, yb, amber)
                graphics.DrawLine(offscreen, x, y_top, x, yb, amber)
                graphics.DrawLine(offscreen, x1, y_top, x1, yb, amber)
            # class marker inside (black on fill, amber off fill)
            mcol = black if fill_h >= bh // 2 else amber
            if car["first"]:
                mark, mfnt = "1ST", tfont
                tw = text_width(graphics, offscreen, mfnt, mcol, mark)
                graphics.DrawText(offscreen, mfnt, cx - tw // 2,
                                  y_top + bh // 2 + 2, mcol, mark)
            elif car["accessible"]:
                draw_wheelchair(cx, y_top + 1, mcol)

        clock_s, clock_x, clock_w = clock_geom()
        page_w = draw_page_num(page)

        # carriage letter under each car, centered; baseline may sit on
        # the last row (safe: capitals never descend, canvas clips)
        y_lab = y_top + bh + sfont.height + L["page3"]["labels_dy"]
        if y_lab <= height:
            for i, car in enumerate(cars):
                x = x0 + i * (bw + gap_b)
                letter = "ABCDEFGH"[i] if i < 8 else str(i + 1)
                lw = text_width(graphics, offscreen, sfont, amber, letter)
                graphics.DrawText(offscreen, sfont,
                                  x + bw // 2 - lw // 2, y_lab,
                                  amber, letter)

        graphics.DrawText(offscreen, sfont, clock_x,
                          height + L["clock"]["dy"], amber, clock_s)

    def draw_progress():
        """Thin progress bar above the page number: fraction of the
        page dwell elapsed. Skipped when only one page is configured."""
        if len(args.pages) < 2:
            return
        frac = (time.time() - page_since) / max(0.1, args.page_seconds)
        frac = max(0.0, min(1.0, frac))
        P = L["progress"]
        bw, bh = P["width"], P["height"]
        x1, y1 = width - 1, height + P["dy"]
        fill = int(bw * frac)
        for yy in range(y1, y1 + bh):  # black backing so it covers text
            for xx in range(x1 - bw + 1, x1 + 1):
                offscreen.SetPixel(xx, yy, 0, 0, 0)
        for yy in range(y1, y1 + bh):
            for xx in range(x1 - fill + 1, x1 + 1):
                offscreen.SetPixel(xx, yy, yellow.red,
                                   yellow.green, yellow.blue)

    def draw_static(page):
        # Top service bigger with its calling-at line; the rest compact.
        # Pitches/gaps from layout.json. Live clock pinned
        # bottom-middle, page number bottom-right.
        rows = board[:args.limit]
        if not rows:
            return
        P1 = L["page1"]
        tight = max(4, sfont.height - 2)  # calling-at pitch base
        tight_mid = max(4, mfont.height - 2)  # compact row pitch base
        clock_s, clock_x, clock_w = clock_geom()
        page_w = draw_page_num(page)

        y = font.baseline + P1["top_dy"]
        draw_row(rows[0], font, y)
        y += tight + P1["calling_dy"]
        draw_calling(rows[0], y)
        rest = rows[1:]
        clock_drawn = False
        for n, dep in enumerate(rest):
            y += tight_mid + P1["row_gap"]
            if y >= height:
                dropped = len(rest) - n
                if time.time() - getattr(draw_static, "_warned", 0) > 60:
                    print(f"warning: {dropped} service row(s) off-screen "
                          f"- fonts too big for {height}px height?",
                          file=sys.stderr)
                    draw_static._warned = time.time()
                break
            last = (n == len(rest) - 1)
            share = last and y >= height - sfont.height
            draw_row(dep, mfont, y,
                     clock_x=clock_x if share else None,
                     right_extra=(page_w + 2) if last else 0)
            if last:
                # clock pinned to the bottom (shares the row on 32px)
                graphics.DrawText(offscreen, sfont, clock_x,
                                  y if share else height + L["clock"]["dy"],
                                  amber, clock_s)
                clock_drawn = True
        if not clock_drawn:
            # rows ran off-screen (fonts too big?) - clock still shows
            graphics.DrawText(offscreen, sfont, clock_x,
                              height + L["clock"]["dy"], amber, clock_s)

    while True:
        now = time.time()
        if now - last_fetch >= args.refresh or not board:
            try:
                fresh = get_board_data()
            except Exception as e:  # keep old data, show error briefly
                print(f"Fetch failed: {e}", file=sys.stderr)
                fresh = None
            if fresh is not None:
                sig = board_signature(fresh)
                if sig != last_sig:
                    board = fresh
                    last_sig = sig
                    idx = 0
                    idx_since = now
                    page_idx = 0
                    page_since = now
                # else: data unchanged, keep the current display as-is
            elif not board:
                board = []
            last_fetch = now

        if len(args.pages) > 1 and now - page_since >= args.page_seconds:
            page_idx = (page_idx + 1) % len(args.pages)
            page_since = now

        offscreen.Fill(0, 0, 0)

        if not board:
            graphics.DrawText(offscreen, font, 2, 1 + font.baseline,
                              red, "No departures")
            draw_page_num(args.pages[page_idx % len(args.pages)])
            draw_progress()
        elif (page := args.pages[page_idx % len(args.pages)]) == 2:
            draw_page2(board[0], page)
            draw_progress()
        elif page == 3:
            draw_page3(board[0], page)
            draw_progress()
        elif args.layout == "static":
            draw_static(page)
            draw_progress()
        else:
            if len(board) > 1 and now - idx_since >= args.rotate_seconds:
                idx = (idx + 1) % len(board)
                idx_since = now
            draw_full(board[idx % len(board)], 0)
            draw_progress()

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
    p.add_argument("--layout-file", default=os.path.join(THIS_DIR, "layout.json"),
                   help="Fonts, colours, text lines and positions "
                        "(default layout.json next to the script)")
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
    # Matrix flags (mirrors SampleBase from the examples)
    p.add_argument("--led-rows", type=int, default=32)
    p.add_argument("--led-cols", type=int, default=64)
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
    args = p.parse_args()

    try:
        pages = [int(x) for x in args.pages.split(",") if x.strip()]
    except ValueError:
        sys.exit("--pages must be comma-separated numbers, e.g. '1,2'")
    if not pages or any(x not in (1, 2, 3) for x in pages):
        sys.exit("--pages must be a combination of 1, 2 and 3")
    args.pages = pages

    L = load_layout(args.layout_file)

    if args.date == "":
        args.date = None

    def get_board_data():
        deps = fetch_departures(args.railway, args.station,
                                args.limit, args.date)
        return enrich_with_timetables(deps, args.railway, args.date)

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

    run_matrix(args, L, get_board_data)


if __name__ == "__main__":
    main()
