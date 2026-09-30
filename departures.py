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
        mins = d.get("delay_minutes", 0)
        return f"Delayed +{mins}m" if mins else "Delayed"
    if d.get("is_tbc"):
        return "TBC"
    return "On time"


def format_departure(d):
    """
    Turn one API record into display strings.
    Returns (time_text, destination, status_text, raw_dict).
    """
    t = d.get("planned_time") or d.get("scheduled_time") or "??:??"
    dest = d.get("destination_name") or d.get("destination") or "?"
    return t, dest, departure_status(d), d


def enrich_with_timetables(departures, railway, fallback_date, timeout=10):
    """Attach a 'calling_at' string to each departure via the timetable API."""
    for d in departures:
        headcode = d.get("headcode")
        if not headcode:
            d["calling_at"] = ""
            continue
        try:
            tt = fetch_timetable(headcode,
                                 d.get("operating_date") or fallback_date,
                                 railway or d.get("railway"),
                                 timeout)
            d["calling_at"] = calling_at_text(
                tt, origin=d.get("origin") or d.get("station"))
        except Exception as e:
            print(f"Timetable fetch failed for {headcode}: {e}", file=sys.stderr)
            d["calling_at"] = ""
    return departures


def format_console(departures):
    out = []
    for d in departures:
        t, dest, status, raw = format_departure(d)
        plat = raw.get("platform") or ""
        right = ((plat + " ") if plat else "") + status
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
                     d.get("calling_at", "")))
    return json.dumps(rows, sort_keys=True)


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
                 f"Try e.g. --font-small ./fonts/4x6.bdf")
    sfont.LoadFont(args.font_small)

    matrix = RGBMatrix(options=options)

    # Classic departure-board palette: everything orange bar the status;
    # time and platform yellow.
    amber = graphics.Color(255, 140, 0)
    yellow = graphics.Color(255, 255, 0)
    red = graphics.Color(255, 30, 30)
    green = graphics.Color(60, 255, 60)

    offscreen = matrix.CreateFrameCanvas()
    width = offscreen.width
    height = offscreen.height

    board = []
    last_fetch = 0
    last_sig = None
    idx = 0
    idx_since = time.time()

    def status_colors(raw):
        if raw.get("is_cancelled"):
            return amber, red
        if raw.get("is_delayed"):
            return amber, red
        return amber, green

    def draw_row(dep, fnt, y_base, clock_x=None):
        """Service main line: time + platform yellow, destination orange,
        status green/red. clock_x caps the destination so it never runs
        under the centered clock on the last row."""
        t, dest, status, raw = format_departure(dep)
        _, sub_c = status_colors(raw)
        plat = (raw.get("platform") or "").strip()
        plat_part = (plat + " ") if plat else ""
        right_w = text_width(graphics, offscreen, fnt, amber, plat_part)
        right_w += text_width(graphics, offscreen, fnt, sub_c, status)
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

    def draw_static():
        # Top service bigger with its calling-at line; the rest compact.
        # --row-gap blank pixels between departures. The live clock shares
        # the last row, centered (a dedicated clock line does not fit in
        # 32px alongside 3 services + calling-at).
        rows = board[:args.limit]
        if not rows:
            return
        gap = args.row_gap
        tight = max(4, sfont.height - 2)  # snug pitch; glyph boxes overlap
        clock_s = time.strftime("%H:%M:%S")
        clock_w = text_width(graphics, offscreen, sfont, amber, clock_s)
        clock_x = max(1, (width - clock_w) // 2)

        y = font.baseline
        draw_row(rows[0], font, y)
        y += tight + 2  # clear the main line's descenders
        draw_calling(rows[0], y)
        rest = rows[1:]
        for n, dep in enumerate(rest):
            y += tight + gap
            if y >= height:
                break
            last = (n == len(rest) - 1)
            draw_row(dep, sfont, y,
                     clock_x=clock_x if last else None)
            if last:
                graphics.DrawText(offscreen, sfont, clock_x, y,
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
                # else: data unchanged, keep the current display as-is
            elif not board:
                board = []
            last_fetch = now

        offscreen.Fill(0, 0, 0)

        if not board:
            graphics.DrawText(offscreen, font, 2, 1 + font.baseline,
                              red, "No departures")
        elif args.layout == "static":
            draw_static()
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
    p.add_argument("--font", default=find_font("5x7.bdf"),
                   help="Path to *.bdf font for the top service line "
                        "(default 5x7)")
    p.add_argument("--font-small", default=find_font("4x6.bdf"),
                   help="Path to *.bdf font for other services, "
                        "calling-at and clock lines")
    p.add_argument("--layout", default="static", choices=["rotate", "static"],
                   help="'static': all departures at once, 2 lines each "
                        "(main + calling-at). 'rotate': one full-detail "
                        "departure at a time.")
    p.add_argument("--rotate-seconds", type=float, default=5,
                   help="Seconds per departure in rotate layout (default 5)")
    p.add_argument("--row-gap", type=int, default=2,
                   help="Blank pixels between departures in static layout")
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
