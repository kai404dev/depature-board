#!/usr/bin/env python3
"""
Bus front destination blind replica for RGB LED matrix.

Replicates the amber destination display on the front of a UK bus:

  +------------------------------------------------+
  | 43   SHEFFIELD                                 |
  |      via Dronfield, Chesterfield               |
  +------------------------------------------------+

Route number big on the left, destination big next to it, via points
smaller along the bottom (scrolls when too long). All amber, like the
real blinds. Give one service to hold it, or several to rotate like a
bus cycling through displays.

  python3 bus-board.py --mock --once \
    --service "43|Sheffield|Dronfield, Chesterfield"

  python3 bus-board.py --mock --once \
    --service "43|Sheffield|Dronfield, Chesterfield" \
    --service "X17|Matlock|Rowsley, Darley Dale"

Service shape: "ROUTE|DESTINATION|VIA" (VIA optional).
Shorthand also works: "43: Sheffield via Dronfield".
Or a JSON file: [{"route": "43", "destination": "Sheffield",
"via": "Dronfield"}, ...] ("dest" accepted for "destination").

Modes:
  --mock     print to console instead of driving the LED matrix
  --preview  ASCII preview of the blind (no hardware needed)
  (default)  drive the LED matrix (needs root for GPIO on a Pi)
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
    "route": "10x20.bdf",  # route number, big left
    "dest": "7x14B.bdf",   # destination, big next to route
    "via": "5x7.bdf",      # via points along the bottom
}
AMBER = (255, 140, 0)  # bus blinds are monochrome amber
COLORS = {
    "route": AMBER,
    "dest": AMBER,
    "via": AMBER,
}

SCROLL_PAUSE0 = 2.5  # sit at the start before scrolling
SCROLL_PAUSE1 = 2.0  # sit at the end before snapping back
SCROLL_SPEED = 20.0  # px per second while scrolling

DEMO = [
    {"route": "43", "destination": "Sheffield",
     "via": "Dronfield, Chesterfield"},
    {"route": "X17", "destination": "Matlock",
     "via": "Rowsley, Darley Dale"},
]


# ---------------------------------------------------------------------------
# Service parsing
# ---------------------------------------------------------------------------

def parse_service(s):
    """Parse one --service string.

    Accepted: "route|destination|via" (via optional).
    Shorthand: "43: Sheffield via Dronfield" -> route=43,
    destination=Sheffield, via=Dronfield.
    """
    s = s.strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        while len(parts) < 3:
            parts.append("")
        route, dest, via = parts[:3]
        return {"route": route, "destination": dest, "via": via}
    # shorthand "ROUTE: rest" with optional " via VIA"
    route, rest = "", s
    if ":" in s:
        route, rest = [p.strip() for p in s.split(":", 1)]
    dest, via = rest, ""
    low = rest.lower()
    if " via " in low:
        idx = low.index(" via ")
        dest, via = rest[:idx].strip(), rest[idx + 5:].strip()
    return {"route": route, "destination": dest, "via": via}


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
        dest = (svc.get("destination") or "").strip().upper()
        via = via_text(svc)
        out.append(f"[{route}] {dest}")
        if via:
            out.append(f"  {via}")
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

    def draw_blind(svc):
        """One front-blind: big route left, big destination next to it,
        via points smaller along the bottom. Route stays put; dest and
        via scroll inside their cells when too long."""
        route = (svc.get("route") or "").strip()
        dest = (svc.get("destination") or "").strip()
        if args.dest_upper:
            dest = dest.upper()
        via = via_text(svc)

        # main row: route fixed left, destination flows after it.
        # Destination first: its scroll edge-blanking clears the row
        # outside its cell, then the route is painted fresh on top so
        # a long scrolling destination can never chew into it.
        w_route = 0
        if route:
            w_route = text_width(graphics, offscreen, F["route"],
                                 C["route"], route)
        dest_x0 = 1 if not route else 1 + w_route + args.gap
        draw_scroll(F["dest"], C["dest"], dest, args.dest_y,
                    W - 1, x0=dest_x0)
        if route:
            graphics.DrawText(offscreen, F["route"], 1, args.main_y,
                              C["route"], route)

        # via row along the bottom, full width
        if via:
            draw_scroll(F["via"], C["via"], via, args.via_y, W - 1)

    while True:
        now = time.time()
        # one blind at a time; rotate when several services are given.
        # Never leave before scrolling text has made one full pass.
        if len(services) > 1 and \
                now - idx_since >= max(args.rotate_seconds, scroll_need):
            idx = (idx + 1) % len(services)
            idx_since = now
            page_since = now

        scroll_need = 0.0
        offscreen.Fill(0, 0, 0)
        draw_blind(services[idx % len(services)])

        offscreen = matrix.SwapOnVSync(offscreen)
        if args.once:
            break
        time.sleep(0.08)


def main():
    p = argparse.ArgumentParser(description="Bus front destination blind")
    p.add_argument("--service", action="append", default=[],
                   help='One blind as "ROUTE|DEST|VIA", e.g. '
                        '"43|Sheffield|Dronfield, Chesterfield". Repeatable: '
                        'several services rotate. '
                        'Shorthand "43: Sheffield via Dronfield" also works.')
    p.add_argument("--services-file", default="",
                   help="JSON file with a list of "
                        '{"route, destination, via} objects')
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--rotate-seconds", type=float, default=5,
                   help="Seconds per blind when several services are given "
                        "(a blind with scrolling text stays until it has "
                        "scrolled once)")
    p.add_argument("--dest-upper", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Uppercase the destination like real blinds "
                        "(--no-dest-upper to keep as typed)")
    p.add_argument("--main-y", type=int, default=20,
                   help="Baseline of the route/destination row")
    p.add_argument("--dest-y", type=int, default=20,
                   help="Baseline of the destination (tweak to align "
                        "with the route font)")
    p.add_argument("--via-y", type=int, default=36,
                   help="Baseline of the via row")
    p.add_argument("--gap", type=int, default=8,
                   help="Pixels between route number and destination")
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
