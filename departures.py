#!/usr/bin/env python3
"""
Peak Rail departures board for Waveshare RGB-Matrix / rpi-rgb-led-matrix.

Calls:
  https://peakraildepartures.com/api/departures/?railway=PR&station=RWS&limit=3&date=2026-10-04

and displays the next 3 departures.

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

# Try to find a bundled BDF font in the example repo
THIS_DIR = os.path.abspath(os.path.dirname(__file__))
CANDIDATE_FONTS = [
    os.path.join(THIS_DIR, "fonts", "5x7.bdf"),
    os.path.join(THIS_DIR, "..", "RGB-Matrix-Px-xx", "example", "Raspberry-Pi", "fonts", "5x7.bdf"),
    os.path.join(THIS_DIR, "..", "RGB-Matrix-Px-xx", "example", "Raspberry-Pi", "fonts", "6x9.bdf"),
    os.path.join(THIS_DIR, "..", "RGB-Matrix-Px-xx", "example", "Raspberry-Pi", "fonts", "7x13.bdf"),
]


def find_default_font():
    for p in CANDIDATE_FONTS:
        if os.path.exists(p):
            return p
    return CANDIDATE_FONTS[1]  # best guess, error will show if missing


def fetch_departures(railway="PR", station="RWS", limit=3, date="2026-10-04", timeout=10):
    """Call the Peak Rail departures API. Returns list of departure dicts."""
    params = {"railway": railway, "station": station, "limit": limit}
    if date:  # allow --date "" for live (API defaults to next from now)
        params["date"] = date
    url = API_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "departure-display/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def format_departure(d):
    """
    Turn one API record into display strings.
    Returns (line_text, status_text, flags_dict)
    e.g. ("10:00 Matlock Riverside", "Plat 1", {...})
    """
    t = d.get("planned_time") or d.get("scheduled_time") or "??:??"
    dest = d.get("destination_name") or d.get("destination") or "?"
    plat = d.get("platform") or "-"

    if d.get("is_cancelled"):
        status = "CANCELLED"
    elif d.get("is_delayed"):
        mins = d.get("delay_minutes", 0)
        status = f"+{mins}min" if mins else "DELAYED"
    elif d.get("status") and d.get("status") not in ("upcoming", "scheduled", "on_time"):
        status = str(d.get("status")).upper()
    else:
        status = f"Plat {plat}" if plat != "-" else "On time"

    line = f"{t} {dest}"
    return line, status, d


def format_console(departures):
    out = []
    for d in departures:
        line, status, _ = format_departure(d)
        out.append(f"{line:28s} {status}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Matrix rendering (only imported when not in --mock mode)
# ---------------------------------------------------------------------------

def run_matrix(args, get_departures):
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

    matrix = RGBMatrix(options=options)

    font = graphics.Font()
    if not os.path.exists(args.font):
        sys.exit(f"Font not found: {args.font}\n"
                 f"Try e.g. --font ../RGB-Matrix-Px-xx/example/Raspberry-Pi/fonts/7x13.bdf")
    font.LoadFont(args.font)

    # Classic departure-board palette
    white = graphics.Color(255, 255, 255)
    yellow = graphics.Color(255, 200, 0)
    dim = graphics.Color(255, 140, 0)
    red = graphics.Color(255, 30, 30)
    green = graphics.Color(60, 255, 60)
    black = graphics.Color(0, 0, 0)

    offscreen = matrix.CreateFrameCanvas()

    departures = []
    last_fetch = 0

    # Layout: 3 rows, one per departure. Each row = 2 lines:
    #   main line: "10:00 Matlock Riverside"
    #   sub line:  status ("Plat 1" / "CANCELLED" / "+5min")
    # With 32px height and a 7px font that is 10-11px per departure.
    # For taller fonts / bigger panels the spacing adapts.
    while True:
        now = time.time()
        if now - last_fetch >= args.refresh:
            try:
                departures = get_departures()
            except Exception as e:  # keep old data, show error briefly
                print(f"Fetch failed: {e}", file=sys.stderr)
                if not departures:
                    departures = []
            last_fetch = now

        offscreen.Fill(0, 0, 0)

        width = offscreen.width
        height = offscreen.height
        n = max(1, min(len(departures), args.limit))
        row_h = height // max(1, args.limit)

        if not departures:
            graphics.DrawText(offscreen, font, 2, font.baseline(),
                              red, "No departures")
        else:
            for i, dep in enumerate(departures[:args.limit]):
                line, status, raw = format_departure(dep)
                y_top = i * row_h

                # colour logic
                if raw.get("is_cancelled"):
                    main_c, sub_c = dim, red
                elif raw.get("is_delayed"):
                    main_c, sub_c = white, red
                else:
                    main_c, sub_c = yellow, green

                # main line (time + destination), truncated to fit
                # DrawText returns width in pixels; trim until it fits.
                text = line
                while text and graphics.DrawText(offscreen, font, 0, -100,
                                                 main_c, text) > width - 2:
                    text = text[:-1]
                graphics.DrawText(offscreen, font, 1, y_top + font.baseline(),
                                  main_c, text)

                # sub/status line, smaller offset, right-aligned if room
                sub_w = graphics.DrawText(offscreen, font, 0, -100, sub_c, status)
                sub_x = max(1, width - sub_w - 1)
                # only draw sub-line if there is vertical room for it
                sub_y = y_top + font.baseline() + font.height() - 1
                if sub_y < (i + 1) * row_h + font.height() // 2:
                    graphics.DrawText(offscreen, font, sub_x, sub_y, sub_c, status)
                else:
                    # tiny panel: append status to main line instead
                    pass

        offscreen = matrix.SwapOnVSync(offscreen)

        if args.once:
            break
        time.sleep(1)


def main():
    p = argparse.ArgumentParser(description="Peak Rail departure board")
    p.add_argument("--railway", default="PR")
    p.add_argument("--station", default="RWS")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--date", default="2026-10-04",
                   help='Operating date YYYY-MM-DD. Use "" for live/next-from-now.')
    p.add_argument("--refresh", type=int, default=60,
                   help="Seconds between API calls (default 60)")
    p.add_argument("--font", default=find_default_font(),
                   help="Path to *.bdf font")
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

    def get_departures():
        return fetch_departures(args.railway, args.station,
                                args.limit, args.date)

    if args.mock:
        try:
            deps = get_departures()
        except Exception as e:
            sys.exit(f"API fetch failed: {e}")
        print(f"# {args.railway}/{args.station} date={args.date or 'live'}")
        print(format_console(deps))
        if not args.once:
            # keep polling in mock mode so you can watch it update
            try:
                while True:
                    time.sleep(args.refresh)
                    deps = get_departures()
                    print("---")
                    print(format_console(deps))
            except KeyboardInterrupt:
                pass
        return

    run_matrix(args, get_departures)


if __name__ == "__main__":
    main()
