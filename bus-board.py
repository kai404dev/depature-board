#!/usr/bin/env python3
"""
Bus departure board for Waveshare RGB-Matrix / rpi-rgb-led-matrix.

Each service is: route number + destination + via (optional) + due (optional).

Layout per service (matches the requested format):

                destination
  route-number  via ... here ...

i.e. destination on its own line, then route number on the left with
the via text next to it. Long lines scroll; the page waits until each
visible line has scrolled once before rotating.

Provide services in any of these ways (they combine, in order):

  python3 bus-board.py --mock --once \
    --service "43|Sheffield|Dronfield, Chesterfield|12 min" \
    --service "44|Chesterfield|Dronfield"

  python3 bus-board.py --mock --once \
    --services-file buses.json

buses.json shape: [{"route": "43", "destination": "Sheffield",
"via": "Dronfield", "due": "12 min"}, ...]
("via" and "due" are optional; "dest" also accepted for "destination".)

No services given -> built-in demo data so --mock/--preview work.

Modes (same spirit as departures.py):
  --mock     print to console instead of driving the LED matrix
  --preview  ASCII preview of the board (no hardware needed)
  (default)  drive the LED matrix (needs root for GPIO on a Pi)

Examples:
  python3 bus-board.py --mock --once
  python3 bus-board.py --preview --service "43|Sheffield|Dronfield"
  sudo python3 bus-board.py --led-rows 40 --led-cols 80 --led-chain 3
"""

import argparse
import json
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

from api import find_font  # reuse font search path (fonts/ next to script)

FONTS = {
    "dest": "6x12.bdf",      # destination line
    "via": "5x7.bdf",        # "43  via ..." line
    "small": "tom-thumb.bdf",  # clock (tiny, shares the last via row)
}
COLORS = {
    "dest": (255, 140, 0),   # orange destination
    "route": (255, 255, 0),  # yellow route number
    "via": (255, 140, 0),    # orange via text
    "due": (60, 255, 60),    # green due time
    "clock": (255, 140, 0),
}

SCROLL_PAUSE0 = 2.5  # sit at the start before scrolling
SCROLL_PAUSE1 = 2.0  # sit at the end before snapping back
SCROLL_SPEED = 20.0  # px per second while scrolling

DEMO = [
    {"route": "43", "destination": "Sheffield",
     "via": "Dronfield, Chesterfield", "due": "12 min"},
    {"route": "44", "destination": "Chesterfield",
     "via": "Dronfield", "due": "25 min"},
    {"route": "X17", "destination": "Matlock",
     "via": "Rowsley, Darley Dale", "due": "35 min"},
]


# ---------------------------------------------------------------------------
# Service parsing
# ---------------------------------------------------------------------------

def parse_service(s):
    """Parse one --service string.

    Accepted: "route|destination|via|due" (via/due optional).
    Shorthand: "43: Sheffield via Dronfield" -> route=43,
    destination=Sheffield, via=Dronfield.
    """
    s = s.strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        while len(parts) < 4:
            parts.append("")
        route, dest, via, due = parts[:4]
        return {"route": route, "destination": dest,
                "via": via, "due": due}
    # shorthand "ROUTE: rest" with optional " via VIA"
    route, rest = "", s
    if ":" in s:
        route, rest = [p.strip() for p in s.split(":", 1)]
    dest, via = rest, ""
    low = rest.lower()
    if " via " in low:
        idx = low.index(" via ")
        dest, via = rest[:idx].strip(), rest[idx + 5:].strip()
    return {"route": route, "destination": dest,
            "via": via, "due": ""}


def load_services(args):
    services = []
    if args.services_file:
        with open(args.services_file) as f:
            data = json.load(f)
        if isinstance(data, dict) and "services" in data:
            data = data["services"]
        if not isinstance(data, list):
            sys.exit("--services-file must hold a list of services")
        for d in data:
            if not isinstance(d, dict):
                sys.exit("--services-file entries must be objects")
            services.append({
                "route": str(d.get("route", d.get("number", ""))),
                "destination": str(d.get("destination",
                                         d.get("dest", ""))),
                "via": str(d.get("via", "")),
                "due": str(d.get("due", d.get("time", ""))),
            })
    for s in args.service or []:
        services.append(parse_service(s))
    if not services:
        services = [dict(d) for d in DEMO]
    # drop fully empty rows
    services = [d for d in services
                if d.get("route") or d.get("destination")]
    return services[:args.limit]


def via_text(svc):
    via = (svc.get("via") or "").strip()
    return f"via {via}" if via else ""


def format_console(services):
    out = []
    for svc in services:
        route = (svc.get("route") or "").strip()
        dest = (svc.get("destination") or "").strip()
        via = via_text(svc)
        due = (svc.get("due") or "").strip()
        out.append(f"{dest}")
        line2 = f"{route}  {via}".rstrip()
        if due:
            line2 = f"{line2}  [{due}]" if line2.strip() else due
        out.append(line2)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Rendering helpers (same measurement model as departures.py / preview.py)
# ---------------------------------------------------------------------------

def resolve_x(spec, tw, W):
    if spec == "left":
        return 1
    if spec == "center":
        return max(1, (W - tw) // 2)
    if spec == "right":
        return max(1, W - tw - 1)
    return spec


def scroll_timeline(tw, max_x, x0, t):
    if tw <= max_x - x0:
        return 0, 0.0
    span = x0 + tw - max_x
    cycle = SCROLL_PAUSE0 + span / SCROLL_SPEED + SCROLL_PAUSE1
    if t < SCROLL_PAUSE0:
        return 0, cycle
    if t < SCROLL_PAUSE0 + span / SCROLL_SPEED:
        return int((t - SCROLL_PAUSE0) * SCROLL_SPEED), cycle
    return span, cycle


def load_fonts(graphics):
    F = {}
    for role, name in FONTS.items():
        f = graphics.Font()
        f.LoadFont(find_font(name))
        F[role] = f
    return F


def text_width(graphics, canvas, font, color, text):
    return graphics.DrawText(canvas, font, 0, -100, color, text)


def run_board(args, services):
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
    if args.led_no_hardware_pulse:
        options.disable_hardware_pulsing = True

    F = load_fonts(graphics)
    matrix = RGBMatrix(options=options)
    C = {name: graphics.Color(*rgb) for name, rgb in COLORS.items()}

    offscreen = matrix.CreateFrameCanvas()
    W, H = offscreen.width, offscreen.height
    print(f"bus board {W}x{H} services={len(services)}",
          file=sys.stderr, flush=True)

    scroll_w = {}
    page_since = time.time()
    idx = 0
    idx_since = time.time()
    scroll_need = 0.0

    def draw_scroll(fnt, col, text, y_base, max_x, xspec="left", x0=1):
        nonlocal scroll_need
        key = (id(fnt), text)
        tw = scroll_w.get(key)
        if tw is None:
            if len(scroll_w) > 20:
                scroll_w.clear()
            tw = scroll_w[key] = text_width(
                graphics, offscreen, fnt, col, text)
        if tw <= max_x - x0:
            x = x0 if x0 > 1 else resolve_x(xspec, tw, W)
            graphics.DrawText(offscreen, fnt, x, y_base, col, text)
            return
        _, cycle = scroll_timeline(tw, max_x, x0, 0.0)
        scroll_need = max(scroll_need, cycle)
        t = (time.time() - page_since) % cycle
        off, _ = scroll_timeline(tw, max_x, x0, t)
        graphics.DrawText(offscreen, fnt, x0 - off, y_base, col, text)
        top = y_base - fnt.baseline
        for yy in range(max(0, top), min(H, top + fnt.height)):
            for xx in range(0, min(x0, W)):
                offscreen.SetPixel(xx, yy, 0, 0, 0)
            for xx in range(max(0, max_x + 1), W):
                offscreen.SetPixel(xx, yy, 0, 0, 0)

    def draw_service(svc, y_dest, y_via, clock_band=None, clock_x=None):
        """Two lines: destination, then 'ROUTE  via ...' (+ due right)."""
        dest = (svc.get("destination") or "").strip()
        route = (svc.get("route") or "").strip()
        via = via_text(svc)
        due = (svc.get("due") or "").strip()

        # line 1: destination (aligned per --dest-align, scrolls if long)
        draw_scroll(F["dest"], C["dest"], dest, y_dest, W - 1,
                    xspec=args.dest_align)

        # line 2: route number left, via text after it, due right-aligned
        fnt = F["via"]
        route_part = (route + "  ") if route else ""
        w_route = text_width(graphics, offscreen, fnt,
                             C["route"], route_part) if route_part else 0
        due_part = (f" {due}") if due else ""
        w_due = text_width(graphics, offscreen, fnt,
                           C["due"], due_part) if due_part else 0
        max_x = W - 2 - w_due  # 1px gap before the right-aligned due time
        if clock_band is not None and clock_x is not None:
            # via row shares the clock row: keep clear of the clock
            v_top = y_via - fnt.height + 2
            v_bot = y_via + 1
            c_top, c_bot = clock_band
            if v_top <= c_bot and c_top <= v_bot:
                max_x = min(max_x, clock_x - 2)
        draw_scroll(fnt, C["via"], via, y_via, max_x, x0=1 + w_route)
        if route_part:
            graphics.DrawText(offscreen, fnt, 1, y_via,
                              C["route"], route_part)
        if due_part:
            graphics.DrawText(offscreen, fnt, W - 1 - w_due, y_via,
                              C["due"], due_part)

    per_page = max(1, args.per_page)
    while True:
        now = time.time()
        # rotate window of services
        pages = max(1, (len(services) + per_page - 1) // per_page)
        if len(services) > per_page and \
                now - idx_since >= max(args.rotate_seconds, scroll_need):
            idx = (idx + 1) % pages
            idx_since = now
            page_since = now
        window = services[idx * per_page:(idx + 1) * per_page]

        scroll_need = 0.0
        offscreen.Fill(0, 0, 0)

        # clock geometry first so the last via row can keep clear of it
        clk = time.strftime("%H:%M:%S")
        cw = text_width(graphics, offscreen, F["small"],
                        C["clock"], clk)
        clock_x = resolve_x("center", cw, W)
        clock_y = H - 1
        cfont = F["small"]
        clock_band = (clock_y - cfont.height + 2, clock_y + 1)

        y = args.top_y
        for svc in window:
            draw_service(svc, y, y + args.via_dy,
                         clock_band=clock_band, clock_x=clock_x)
            y += args.pitch

        graphics.DrawText(offscreen, F["small"], clock_x, clock_y,
                          C["clock"], clk)

        offscreen = matrix.SwapOnVSync(offscreen)
        if args.once:
            break
        time.sleep(0.08)


def main():
    p = argparse.ArgumentParser(description="Bus departure board")
    p.add_argument("--service", action="append", default=[],
                   help='One service as "ROUTE|DEST|VIA|DUE", e.g. '
                        '"43|Sheffield|Dronfield|12 min". Repeatable. '
                        'Shorthand "43: Sheffield via Dronfield" also works.')
    p.add_argument("--services-file", default="",
                   help="JSON file with a list of "
                        '{"route, destination, via, due} objects')
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--per-page", type=int, default=2,
                   help="Services shown at once (each takes 2 lines)")
    p.add_argument("--rotate-seconds", type=float, default=5,
                   help="Seconds per window when services exceed --per-page")
    p.add_argument("--dest-align", default="right",
                   choices=["left", "center", "right"],
                   help="Alignment of the destination line (default right, "
                        "as in the '              destinations' sketch)")
    p.add_argument("--top-y", type=int, default=8,
                   help="Baseline of the first destination line")
    p.add_argument("--via-dy", type=int, default=9,
                   help="Pixels from destination baseline to via baseline")
    p.add_argument("--pitch", type=int, default=22,
                   help="Pixels per service (destination + via block)")
    p.add_argument("--mock", action="store_true",
                   help="Print to console instead of driving the matrix")
    p.add_argument("--once", action="store_true",
                   help="Fetch and draw once, then exit")
    p.add_argument("--preview", action="store_true",
                   help="ASCII preview of the board (no hardware needed)")
    p.add_argument("--led-rows", type=int, default=40)
    p.add_argument("--led-cols", type=int, default=80)
    p.add_argument("--led-chain", type=int, default=3)
    p.add_argument("--led-parallel", type=int, default=1)
    p.add_argument("--led-gpio-mapping", default="regular")
    p.add_argument("--led-brightness", type=int, default=70)
    p.add_argument("--led-pwm-bits", type=int, default=11)
    p.add_argument("--led-limit-refresh", type=int, default=0)
    p.add_argument("--led-slowdown-gpio", type=int, default=2)
    p.add_argument("--led-rgb-sequence", default="RGB")
    p.add_argument("--led-pixel-mapper", default="")
    p.add_argument("--led-show-refresh", action="store_true")
    p.add_argument("--led-no-hardware-pulse", action="store_true",
                   default=True)
    args = p.parse_args()

    services = load_services(args)

    if args.mock:
        print(format_console(services))
        if not args.once:
            print("(bus-board mock: static list, nothing to poll. "
                  "Re-run to update.)", file=sys.stderr)
        return

    if args.preview:
        import preview
        W = args.led_cols * args.led_chain
        H = args.led_rows
        rec = preview.install(W, H)
        run_board(args, services)
        print(f"--- bus board preview ({W}x{H}) ---")
        rec.report()
        return

    run_board(args, services)


if __name__ == "__main__":
    main()
