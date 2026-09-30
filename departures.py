#!/usr/bin/env python3
"""
Peak Rail departures board for Waveshare RGB-Matrix / rpi-rgb-led-matrix.

Calls:
  https://peakraildepartures.com/api/departures/?railway=PR&station=RWS&limit=3&date=2026-10-04

and for each departure its timetable, e.g.:
  https://peakraildepartures.com/api/timetable/2M03/?date=2026-10-04&railway=PR

to show "Calling at ..." under the destination, plus live status
(On time / Delayed / Cancelled).

Based on the example code in:
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/text-example.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/examples-api-use/clock.cc
  RGB-Matrix-Px-xx/example/Raspberry-Pi/bindings/python/samples/runtext.py

Usage on Pi (3 panels chained):
  sudo python3 departures.py --led-rows 32 --led-cols 64 --led-chain 3

Test on Mac / without hardware:
  python3 departures.py --mock --once
  python3 departures.py --mock
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API_BASE = "https://peakraildepartures.com/api/departures/"
TIMETABLE_BASE = "https://peakraildepartures.com/api/timetable/"

THIS_DIR = os.path.abspath(os.path.dirname(__file__))


def find_font(name):
    p = os.path.join(THIS_DIR, "fonts", name)
    if os.path.exists(p):
        return p
    p2 = os.path.join(THIS_DIR, "..", "RGB-Matrix-Px-xx", "example",
                      "Raspberry-Pi", "fonts", name)
    if os.path.exists(p2):
        return p2
    return p  # best guess, error will show if missing


def api_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "departure-display/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_departures(railway="PR", station="RWS", limit=3, date="2026-10-04", timeout=10):
    """Call the Peak Rail departures API. Returns list of departure dicts."""
    params = {"railway": railway, "station": station, "limit": limit}
    if date:  # allow --date "" for live (API defaults to next from now)
        params["date"] = date
    url = API_BASE + "?" + urllib.parse.urlencode(params)
    data = api_get(url, timeout)
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def fetch_timetable(headcode, date, railway, timeout=10):
    """Call the timetable API for one service. Returns the timetable dict."""
    params = {}
    if date:
        params["date"] = date
    if railway:
        params["railway"] = railway
    url = (TIMETABLE_BASE + urllib.parse.quote(headcode) + "/?"
           + urllib.parse.urlencode(params))
    data = api_get(url, timeout)
    return data if isinstance(data, dict) else {}


def calling_at_text(timetable, origin=None):
    """Build 'Calling at A, B, C' from timetable movements.

    Only real stops count (STOP/DEST); pass-through (PASS) and the
    origin movement are skipped.
    """
    stops = []
    for m in timetable.get("movements") or []:
        if m.get("removed"):
            continue
        if m.get("movement_type") not in ("STOP", "DEST"):
            continue
        code = m.get("station") or ""
        if origin and code == origin and m.get("movement_type") != "DEST":
            continue
        name = m.get("station_name") or code
        if name and name not in stops:
            stops.append(name)
    if not stops:
        return ""
    return "Calling at " + ", ".join(stops)


def departure_status(d):
    """Status text: delay / on-time info (no platform)."""
    if d.get("is_cancelled"):
        return "Cancelled"
    if d.get("is_delayed"):
        return "Delayed"
    if d.get("is_tbc"):
        return "TBC"
    return "On time"


def expected_time(d):
    """Expected departure HH:MM for a delayed service.

    Prefers planned_time when it differs from the schedule (amended
    working), otherwise adds the delay onto the scheduled time.
    """
    sched = d.get("scheduled_time") or "??:??"
    planned = d.get("planned_time") or ""
    if planned and planned != sched:
        return planned
    mins = d.get("delay_minutes", 0) or 0
    try:
        h, m = int(sched[0:2]), int(sched[3:5])
        m += mins
        h = (h + m // 60) % 24
        m %= 60
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return sched


def format_departure(d):
    """
    Turn one API record into display strings.
    Returns (time_text, destination, status_text, raw_dict).
    """
    t = d.get("planned_time") or d.get("scheduled_time") or "??:??"
    dest = d.get("destination_name") or d.get("destination") or "?"
    return t, dest, departure_status(d), d


def enrich_with_timetables(departures, railway, fallback_date, timeout=10):
    """Attach 'calling_at' + 'note_lines' to each departure.

    note_lines holds free-text info from the APIs: cancellation /
    delay / amendment reasons plus the timetable's operational notes.
    """
    for d in departures:
        d["calling_at"] = ""
        d["note_lines"] = []
        headcode = d.get("headcode")
        if not headcode:
            continue
        try:
            tt = fetch_timetable(headcode,
                                 d.get("operating_date") or fallback_date,
                                 railway or d.get("railway"),
                                 timeout)
            d["calling_at"] = calling_at_text(
                tt, origin=d.get("origin") or d.get("station"))
            notes = []
            if d.get("is_cancelled") and d.get("cancellation_reason"):
                notes.append(d["cancellation_reason"])
            if d.get("is_delayed") and d.get("delay_reason"):
                notes.append(d["delay_reason"])
            if d.get("is_amended") and d.get("amendment_note"):
                notes.append(d["amendment_note"])
            for n in tt.get("operational_notes") or []:
                if n and str(n) not in notes:
                    notes.append(str(n))
            d["note_lines"] = notes
        except Exception as e:
            print(f"Timetable fetch failed for {headcode}: {e}", file=sys.stderr)
    return departures


def live_status(raw, flip_seconds):
    """Status text, flipping Delayed <-> expected time like real boards."""
    base = departure_status(raw)
    if raw.get("is_delayed") and not raw.get("is_cancelled"):
        if int(time.time() // flip_seconds) % 2 == 1:
            return f"Exp {expected_time(raw)}"
    return base


def format_console(departures):
    out = []
    for d in departures:
        t, dest, status, raw = format_departure(d)
        plat = raw.get("platform") or ""
        right = ((plat + " ") if plat else "") + status
        if raw.get("is_delayed") and not raw.get("is_cancelled"):
            right += f" (Exp {expected_time(raw)})"
        out.append(f"{t} {dest} [{right}]")
        if d.get("calling_at"):
            out.append(f"  {d['calling_at']}")
    return "\n".join(out)


def board_signature(departures):
    """Fingerprint of everything shown, to skip redraws when unchanged."""
    rows = []
    for d in departures:
        t, dest, status, raw = format_departure(d)
        rows.append((t, dest, status, raw.get("platform"),
                     d.get("calling_at", ""),
                     tuple(d.get("note_lines") or []),
                     json.dumps(d.get("formation", {}), sort_keys=True),
                     d.get("coaches"), json.dumps(d.get("units", []))))
    return json.dumps(rows, sort_keys=True)


def car_capacity(c):
    """Load factor 0..1 for one car. Defaults to 10% when the API
    sends nothing. Accepts fraction or percent under various keys."""
    for k in ("capacity", "load", "occupancy", "occupancy_percent",
              "percent", "loading"):
        if not isinstance(c, dict) or c.get(k) is None:
            continue
        try:
            v = float(c[k])
        except (TypeError, ValueError):
            continue
        if v > 1:
            v /= 100.0
        return max(0.0, min(1.0, v))
    return 0.10


def formation_of(dep, default_cars=4):
    """Coach formation for the diagram page.

    Custom API bits (optional) per departure:
      "formation": {"cars": [{"first": true, "accessible": false,
                              "capacity": 0.35}, ...],
                    "label": "4 coaches"}   # label optional
    or simply:
      "coaches": 4

    Car capacity defaults to 10% when absent (fraction or percent).
    Default: 4 cards, first class at the front (first card),
    accessible at the last car. Returns (cars, label).
    """
    f = dep.get("formation") or {}
    if isinstance(f, dict) and isinstance(f.get("cars"), list) and f["cars"]:
        cars = [{"first": bool(c.get("first") or c.get("first_class")),
                 "accessible": bool(c.get("accessible")),
                 "capacity": car_capacity(c)}
                for c in f["cars"] if isinstance(c, dict)]
        if cars:
            n = len(cars)
            label = f.get("label") or f"{n} coach" + ("" if n == 1 else "es")
            return cars, label
    n = dep.get("coaches") if isinstance(dep.get("coaches"), int) else 0
    if not n or n < 1:
        n = default_cars
    cars = [{"first": i == 0, "accessible": i == n - 1,
             "capacity": car_capacity({})} for i in range(n)]
    return cars, f"{n} coach" + ("" if n == 1 else "es")


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


def run_matrix(args, get_board_data):
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
    font = graphics.Font()
    if not os.path.exists(args.font):
        sys.exit(f"Font not found: {args.font}\n"
                 f"Try e.g. --font ./fonts/7x13.bdf")
    font.LoadFont(args.font)
    sfont = graphics.Font()
    if not os.path.exists(args.font_small):
        sys.exit(f"Small font not found: {args.font_small}\n"
                 f"Try e.g. --font-small ./fonts/5x7.bdf")
    sfont.LoadFont(args.font_small)
    mfont = graphics.Font()
    if not os.path.exists(args.font_mid):
        sys.exit(f"Mid font not found: {args.font_mid}\n"
                 f"Try e.g. --font-mid ./fonts/6x12.bdf")
    mfont.LoadFont(args.font_mid)
    bfont = graphics.Font()
    if not os.path.exists(args.font_big):
        sys.exit(f"Big font not found: {args.font_big}\n"
                 f"Try e.g. --font-big ./fonts/7x13.bdf")
    bfont.LoadFont(args.font_big)
    tfont = graphics.Font()
    if not os.path.exists(args.font_tiny):
        sys.exit(f"Tiny font not found: {args.font_tiny}\n"
                 f"Try e.g. --font-tiny ./fonts/tom-thumb.bdf")
    tfont.LoadFont(args.font_tiny)

    matrix = RGBMatrix(options=options)

    # Classic departure-board palette: everything orange bar the status;
    # time and platform yellow.
    amber = graphics.Color(255, 140, 0)
    yellow = graphics.Color(255, 255, 0)
    red = graphics.Color(255, 30, 30)
    green = graphics.Color(60, 255, 60)
    black = graphics.Color(0, 0, 0)

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
        s = time.strftime("%H:%M:%S")
        w = text_width(graphics, offscreen, sfont, amber, s)
        return s, max(1, (width - w) // 2), w

    def draw_page_num(n):
        """Page indicator bottom-right. Returns its pixel width."""
        lab = f"{n}/{len(args.pages)}"
        w = text_width(graphics, offscreen, tfont, amber, lab)
        graphics.DrawText(offscreen, tfont, width - w - 1, height - 1,
                          amber, lab)
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
                              yellow, plat_part)
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
        graphics.DrawText(offscreen, bfont, 1 + w_time, y_big,
                          amber, dest)

        status = live_status(raw, args.flip_seconds)
        plat_part = (f"Plat {plat} " if plat else "")
        line2 = plat_part + status
        # tight pitches tuned for 10x20 + 5x7 on a 32px panel:
        # big(18) / status(24) / note(31, shares line with clock)
        y2 = y_big + sfont.height - 3
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
        y3 = y2 + sfont.height - 2
        if notes and y3 < height + 1:
            share3 = y3 >= height - sfont.height
            cap = min(width - 2, width - page_w - 4)
            if share3:
                cap = min(cap, clock_x - 3)
            graphics.DrawText(offscreen, sfont, 1, y3, amber,
                              fit_text(graphics, offscreen, sfont, amber,
                                       notes[0], max(0, cap)))
        graphics.DrawText(offscreen, sfont, clock_x, height - 1,
                          amber, clock_s)

    def draw_page3(dep, page):
        """Train formation diagram: fixed-width coach cards, pointy front
        car, 1ST/wheelchair markers inside, letters underneath.
        Live clock bottom-middle, page number bottom-right."""
        t, dest, _, raw = format_departure(dep)
        cars, label = formation_of(dep, default_cars=args.coaches)
        n = len(cars)

        # header: "10:00 Matlock Town" left, "4 coaches" right
        y_head = sfont.baseline
        lab_w = text_width(graphics, offscreen, sfont, amber, label)
        graphics.DrawText(offscreen, sfont, max(1, width - lab_w - 1),
                          y_head, amber, label)
        head = fit_text(graphics, offscreen, sfont, amber, f"{t} {dest}",
                        width - lab_w - 4)
        graphics.DrawText(offscreen, sfont, 1, y_head, yellow, head)

        # coach cards row: fixed width, left-aligned so the centered
        # clock and page number never collide with them. First car gets
        # a pointy (slanted) front. All outlines amber, all fills yellow
        # at capacity height; class markers inside auto-contrast.
        margin, gap_b, bh, slant = 2, 3, 12, 5
        bw = args.coach_width
        x0 = margin
        y_top = y_head + 3
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
            # capacity fill from the bottom
            fill_h = int((bh - 2) * car["capacity"])
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

        # carriage letter under each car, centered
        y_lab = y_top + bh + sfont.height - 1
        if y_lab < height:
            for i, car in enumerate(cars):
                x = x0 + i * (bw + gap_b)
                letter = "ABCDEFGH"[i] if i < 8 else str(i + 1)
                lw = text_width(graphics, offscreen, sfont, amber, letter)
                graphics.DrawText(offscreen, sfont,
                                  x + bw // 2 - lw // 2, y_lab,
                                  amber, letter)

        graphics.DrawText(offscreen, sfont, clock_x, height - 1,
                          amber, clock_s)

    def draw_static(page):
        # Top service bigger with its calling-at line; the rest compact.
        # --row-gap blank pixels between departures. Live clock pinned
        # bottom-middle, page number bottom-right.
        rows = board[:args.limit]
        if not rows:
            return
        gap = args.row_gap
        tight = max(4, sfont.height - 2)  # calling-at pitch base
        tight_mid = max(4, mfont.height - 2)  # compact row pitch base
        clock_s, clock_x, clock_w = clock_geom()
        page_w = draw_page_num(page)

        y = font.baseline
        draw_row(rows[0], font, y)
        y += tight + 2  # clear the main line's descenders
        draw_calling(rows[0], y)
        rest = rows[1:]
        clock_drawn = False
        for n, dep in enumerate(rest):
            y += tight_mid + gap
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
                                  y if share else height - 1,
                                  amber, clock_s)
                clock_drawn = True
        if not clock_drawn:
            # rows ran off-screen (fonts too big?) - clock still shows
            graphics.DrawText(offscreen, sfont, clock_x, height - 1,
                              amber, clock_s)

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
        elif (page := args.pages[page_idx % len(args.pages)]) == 2:
            draw_page2(board[0], page)
        elif page == 3:
            draw_page3(board[0], page)
        elif args.layout == "static":
            draw_static(page)
        else:
            if len(board) > 1 and now - idx_since >= args.rotate_seconds:
                idx = (idx + 1) % len(board)
                idx_since = now
            draw_full(board[idx % len(board)], 0)

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
    p.add_argument("--font", default=find_font("7x14B.bdf"),
                   help="Path to *.bdf font for the top service line "
                        "(default 7x14B)")
    p.add_argument("--font-mid", default=find_font("6x12.bdf"),
                   help="Path to *.bdf font for the other service lines "
                        "(default 6x12)")
    p.add_argument("--font-small", default=find_font("5x7.bdf"),
                   help="Path to *.bdf font for calling-at, notes "
                        "and clock lines (default 5x7)")
    p.add_argument("--font-big", default=find_font("10x20.bdf"),
                   help="Path to *.bdf font for the page-2 headline")
    p.add_argument("--font-tiny", default=find_font("tom-thumb.bdf"),
                   help="Path to *.bdf font for the page numbers")
    p.add_argument("--layout", default="static", choices=["rotate", "static"],
                   help="'static': all departures at once, 2 lines each "
                        "(main + calling-at). 'rotate': one full-detail "
                        "departure at a time.")
    p.add_argument("--rotate-seconds", type=float, default=5,
                   help="Seconds per departure in rotate layout (default 5)")
    p.add_argument("--row-gap", type=int, default=2,
                   help="Blank pixels between departures in static layout")
    p.add_argument("--flip-seconds", type=float, default=3,
                   help="Seconds per side when flipping Delayed/expected time")
    p.add_argument("--pages", default="1,2,3",
                   help="Comma-separated pages to cycle, e.g. '1,2,3' or '1'. "
                        "Page 1 = board, 2 = next departure big, "
                        "3 = formation diagram.")
    p.add_argument("--coaches", type=int, default=4,
                   help="Default coach count for the page-3 diagram "
                        "(API formation/coaches overrides it)")
    p.add_argument("--coach-width", type=int, default=16,
                   help="Coach card width in LEDs on page 3 (default 16)")
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
            cars, label = formation_of(d, default_cars=args.coaches)
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

    run_matrix(args, get_board_data)


if __name__ == "__main__":
    main()
