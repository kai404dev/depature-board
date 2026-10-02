#!/usr/bin/env python3
"""
Font tester for the RGB matrix.

Two modes:

1. JSON layout mode -- draw custom text in custom fonts at custom
   positions described by a JSON file:

     python3 font-test.py --json layout.json

   layout.json format (y is the text baseline, like DrawText):

     {
       "items": [
         {"font": "5x7.bdf", "text": "10:00 Matlock", "x": 1, "y": 7,
          "color": "255,140,0"},
         {"font": "4x6.bdf", "text": "Calling at Darley Dale", "x": 1,
          "y": 15, "color": "255,255,0"}
       ]
     }

   "font" is resolved in ./fonts (a full path also works).
   "color" is R,G,B and defaults to orange (255,140,0).

2. Gallery mode (default, no --json) -- cycle through every .bdf in
   ./fonts showing sample text, to quickly compare fonts:

     python3 font-test.py --text "Matlock 10:00" --dwell 3

Validate a JSON file without hardware:

     python3 font-test.py --json layout.json --mock
"""

import argparse
import json
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))


def find_font(name):
    if os.path.isabs(name) and os.path.exists(name):
        return name
    p = os.path.join(THIS_DIR, "fonts", os.path.basename(name))
    if os.path.exists(p):
        return p
    if os.path.exists(name):
        return os.path.abspath(name)
    return p  # best guess, error will show if missing


def parse_color(s):
    try:
        r, g, b = (int(v) for v in s.split(","))
        assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255
        return r, g, b
    except Exception:
        raise ValueError(f"bad color {s!r}, want R,G,B e.g. 255,140,0")


def load_layout(path):
    with open(path) as f:
        data = json.load(f)
    items = data.get("items", data) if isinstance(data, dict) else data
    norm = []
    for n, it in enumerate(items):
        if "text" not in it:
            sys.exit(f"layout item {n} has no 'text': {it}")
        font = find_font(it.get("font", "5x7.bdf"))
        if not os.path.exists(font):
            sys.exit(f"layout item {n}: font not found: {it.get('font')}")
        try:
            color = parse_color(it.get("color", "255,140,0"))
        except ValueError as e:
            sys.exit(f"layout item {n}: {e}")
        norm.append({
            "font": font,
            "name": os.path.basename(font),
            "text": str(it["text"]),
            "x": int(it.get("x", 1)),
            "y": int(it.get("y", 7)),
            "color": color,
        })
    return norm


def add_matrix_args(p):
    p.add_argument("--led-rows", type=int, default=40)
    p.add_argument("--led-cols", type=int, default=80)
    p.add_argument("--led-chain", type=int, default=3)
    p.add_argument("--led-parallel", type=int, default=1)
    p.add_argument("--led-gpio-mapping", default="regular")
    p.add_argument("--led-brightness", type=int, default=100)
    p.add_argument("--led-pwm-bits", type=int, default=8)
    p.add_argument("--led-limit-refresh", type=int, default=0)
    p.add_argument("--led-slowdown-gpio", type=int, default=1)
    p.add_argument("--led-rgb-sequence", default="RGB")
    p.add_argument("--led-pixel-mapper", default="")
    p.add_argument("--led-show-refresh", action="store_true")
    p.add_argument("--led-no-drop-privs", action="store_true")
    p.add_argument("--led-no-hardware-pulse", action="store_true")
    p.add_argument("--led-rp1-rio", type=int, default=0, choices=[0, 1])
    p.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=130)
    p.add_argument("--led-pwm-dither-bits", type=int, default=0)
    p.add_argument("--led-row-addr-type", type=int, default=0)
    p.add_argument("--led-multiplexing", type=int, default=0)
    p.add_argument("--led-panel-type", default="")
    p.add_argument("--led-inverse", action="store_true")
    return p


def make_matrix(args):
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
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
    return RGBMatrix(options=options)


def load_fonts(graphics, items, extra=()):
    """Load each distinct font once, keyed by path.

    Must run BEFORE make_matrix(): matrix init drops root privileges
    to 'daemon', which may not be able to read files under /home.
    Fonts that fail to load are skipped with a warning (gallery mode
    must not die on one bad file).
    """
    fonts = {}
    for path in list({it["font"] for it in items}) + list(extra):
        if path in fonts or not path or not os.path.exists(path):
            continue
        try:
            f = graphics.Font()
            f.LoadFont(path)
            fonts[path] = f
        except Exception as e:
            print(f"warning: skipping {os.path.basename(path)}: {e}",
                  file=sys.stderr)
    return fonts


def main():
    p = argparse.ArgumentParser(description="Preview BDF fonts / JSON text layouts")
    p.add_argument("--json", default=None,
                   help="Layout file (see docstring). Omit for font gallery.")
    p.add_argument("--text", default="Matlock 10:00 AaBbYy",
                   help="Sample text in gallery mode")
    p.add_argument("--x", type=int, default=1)
    p.add_argument("--y", type=int, default=12)
    p.add_argument("--color", default="255,140,0")
    p.add_argument("--dwell", type=float, default=3,
                   help="Seconds per font in gallery mode")
    p.add_argument("--once", action="store_true",
                   help="Draw once, then exit")
    p.add_argument("--mock", action="store_true",
                   help="Validate + list what would be drawn, no hardware")
    add_matrix_args(p)
    args = p.parse_args()

    if args.json:
        items = load_layout(args.json)
    else:
        font_dir = os.path.join(THIS_DIR, "fonts")
        try:
            names = sorted(f for f in os.listdir(font_dir)
                           if f.endswith(".bdf"))
        except FileNotFoundError:
            sys.exit(f"no fonts dir: {font_dir}")
        if not names:
            sys.exit(f"no .bdf files in {font_dir}")
        try:
            color = parse_color(args.color)
        except ValueError as e:
            sys.exit(str(e))
        items = [{"font": os.path.join(font_dir, n), "name": n,
                  "text": args.text, "x": args.x, "y": args.y,
                  "color": color, "gallery": True} for n in names]

    if args.mock:
        for it in items:
            r, g, b = it["color"]
            print(f"{it['name']:18s} ({it['x']},{it['y']}) "
                  f"#{r:02x}{g:02x}{b:02x} {it['text']}")
        return

    from rgbmatrix import graphics
    gallery = not args.json
    # fonts BEFORE matrix: init drops privileges, hiding /home files
    tiny_path = find_font("tom-thumb.bdf") if gallery else None
    fonts = load_fonts(graphics, items,
                       extra=[tiny_path] if tiny_path else [])
    # drop items whose font failed to load
    items = [it for it in items if it["font"] in fonts]
    if not items:
        sys.exit("no fonts could be loaded")
    tiny = fonts.get(tiny_path, fonts[items[0]["font"]])
    matrix = make_matrix(args)
    offscreen = matrix.CreateFrameCanvas()

    i = 0
    try:
        while True:
            offscreen.Fill(0, 0, 0)
            if gallery:
                it = items[i % len(items)]
                print(f"[{i % len(items) + 1}/{len(items)}] {it['name']}")
                f = fonts[it["font"]]
                r, g, b = it["color"]
                graphics.DrawText(offscreen, f, it["x"], it["y"],
                                  graphics.Color(r, g, b), it["text"])
                # label the font name underneath in a tiny font if it fits
                if it["y"] + 8 < offscreen.height:
                    graphics.DrawText(offscreen, tiny, 1, offscreen.height - 1,
                                      graphics.Color(255, 255, 0), it["name"])
                offscreen = matrix.SwapOnVSync(offscreen)
                if args.once:
                    break
                time.sleep(args.dwell)
                i += 1
            else:
                for it in items:
                    f = fonts[it["font"]]
                    r, g, b = it["color"]
                    w = graphics.DrawText(offscreen, f, it["x"], it["y"],
                                          graphics.Color(r, g, b), it["text"])
                    print(f"{it['name']:18s} width={w}px {it['text']}")
                offscreen = matrix.SwapOnVSync(offscreen)
                if args.once:
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
