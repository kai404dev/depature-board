#!/usr/bin/env python3
"""
Image programmes for the bus LED matrix (the image part of bus-board).

A programme is a named sequence of full-panel PNG images played in
order: which route it belongs to, the destinations it serves, and
the images. Example programs.json:

  {
    "rotate_seconds": 10,
    "image_fit": "fit",
    "programs": {
      "401": {
        "route": "401",
        "destinations": ["Burton upon Trent", "Tutbury", "Uttoxeter"],
        "rotate_seconds": 10,
        "screens": [
          "bitmap/mbt.png",
          {"image": "bitmap/fluffynet.png", "seconds": 5}
        ]
      }
    }
  }

Each screen is an image path, or {"image": path, "seconds": N,
"fit": fit|fill|stretch} to override the dwell / fit for that
screen. Defaults cascade: screen -> programme -> file -> built-in
(10s, fit). Destinations may also be a single string.

  python3 program.py programs.json --program 401 --mock --once
  python3 program.py programs.json --program 401 --preview
  python3 program.py programs.json --list
  sudo python3 program.py programs.json --program 401
"""

import argparse
import json
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

from images import load_frame

DEFAULT_ROTATE = 10
DEFAULT_FIT = "fit"
FITS = ("fit", "fill", "stretch")


def load_programs_file(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        sys.exit(f"program file {path}: {e}")
    except json.JSONDecodeError as e:
        sys.exit(f"program file {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        sys.exit(f"program file {path}: top level must be an object")
    progs = data.get("programs")
    if not isinstance(progs, dict) or not progs:
        sys.exit(f"program file {path}: need a 'programs' object "
                 f"with at least one program")
    gRotate = data.get("rotate_seconds", DEFAULT_ROTATE)
    gFit = data.get("image_fit", DEFAULT_FIT)
    if gFit not in FITS:
        sys.exit(f"program file {path}: image_fit must be "
                 f"one of {FITS}")
    return data, progs, gRotate, gFit


def resolve_program(data, progs, gRotate, gFit, name, path):
    if name is None:
        if len(progs) == 1:
            name = next(iter(progs))
        else:
            names = ", ".join(sorted(progs))
            sys.exit(f"program file {path} holds several programs "
                     f"({names}): pick one with --program NAME")
    if name not in progs:
        names = ", ".join(sorted(progs))
        sys.exit(f"program '{name}' not in {path} (have: {names})")
    raw = progs[name]
    if not isinstance(raw, dict):
        sys.exit(f"program '{name}': must be an object")
    route = str(raw.get("route", "")).strip()
    if not route:
        sys.exit(f"program '{name}': need a 'route', e.g. \"401\"")
    dests = raw.get("destinations", [])
    if isinstance(dests, str):
        dests = [dests]
    if not isinstance(dests, list) or \
            not [d for d in dests if str(d).strip()]:
        sys.exit(f"program '{name}': need 'destinations' as a list, "
                 f"e.g. [\"Burton upon Trent\", \"Tutbury\"]")
    dests = [str(d).strip() for d in dests if str(d).strip()]
    pRotate = raw.get("rotate_seconds", gRotate)
    pFit = raw.get("image_fit", gFit)
    if pFit not in FITS:
        sys.exit(f"program '{name}': image_fit must be one of {FITS}")
    screens = raw.get("screens")
    if not isinstance(screens, list) or not screens:
        sys.exit(f"program '{name}': need a non-empty 'screens' list "
                 f"of image paths, in play order")
    out = []
    for i, s in enumerate(screens, 1):
        if isinstance(s, str):
            s = {"image": s}
        if not isinstance(s, dict) or not str(s.get("image", "")).strip():
            sys.exit(f"program '{name}' screen {i}: must be an image "
                     f"path or {{\"image\": path, ...}}")
        sec = s.get("seconds", pRotate)
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            sys.exit(f"program '{name}' screen {i}: seconds must be "
                     f"a number")
        if sec <= 0:
            sys.exit(f"program '{name}' screen {i}: seconds must be "
                     f"positive")
        fit = s.get("fit", pFit)
        if fit not in FITS:
            sys.exit(f"program '{name}' screen {i}: fit must be one "
                     f"of {FITS}")
        out.append({"image": str(s["image"]).strip(),
                    "seconds": sec, "fit": fit})
    return {"name": name, "route": route, "destinations": dests,
            "screens": out}


def run_program(args, prog):
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
    if args.led_no_hardware_pulse:
        options.disable_hardware_pulsing = True

    matrix = RGBMatrix(options=options)
    offscreen = matrix.CreateFrameCanvas()
    W, H = offscreen.width, offscreen.height

    frames = []
    for s in prog["screens"]:
        sw, sh, frame = load_frame(s["image"], W, H, s["fit"])
        frames.append((s, frame))
        print(f"image {s['image']}: {sw}x{sh} -> {s['fit']} {W}x{H} "
              f"({s['seconds']}s)", file=sys.stderr, flush=True)
    print(f"program '{prog['name']}' route {prog['route']} "
          f"({', '.join(prog['destinations'])}) "
          f"{W}x{H} screens={len(frames)}",
          file=sys.stderr, flush=True)

    def blit(frame):
        for y in range(H):
            o = y * W * 3
            for x in range(W):
                offscreen.SetPixel(x, y, frame[o], frame[o + 1],
                                   frame[o + 2])
                o += 3

    idx = 0
    idx_since = time.time()
    while True:
        now = time.time()
        if len(frames) > 1 and \
                now - idx_since >= frames[idx % len(frames)][0]["seconds"]:
            idx = (idx + 1) % len(frames)
            idx_since = now
        offscreen.Fill(0, 0, 0)
        blit(frames[idx % len(frames)][1])
        offscreen = matrix.SwapOnVSync(offscreen)
        if args.once:
            break
        time.sleep(0.5)


def main():
    p = argparse.ArgumentParser(description="Bus image programmes")
    p.add_argument("program_file",
                   help="JSON file with programmes, e.g. programs.json")
    p.add_argument("--program", default=None,
                   help="Programme to play (unneeded when the file "
                        "holds just one)")
    p.add_argument("--list", action="store_true",
                   help="List programmes in the file and exit")
    p.add_argument("--rotate-seconds", type=float, default=None,
                   help="Dwell per screen, overriding the file "
                        "(default 10s from the programme)")
    p.add_argument("--image-fit", default=None, choices=FITS,
                   help="Fit override for every screen")
    p.add_argument("--mock", action="store_true",
                   help="Print the programme instead of driving the matrix")
    p.add_argument("--once", action="store_true",
                   help="Show the first screen, then exit")
    p.add_argument("--preview", action="store_true",
                   help="ASCII preview of every screen (no hardware needed)")
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

    data, progs, gRotate, gFit = load_programs_file(args.program_file)

    if args.list:
        for name in sorted(progs):
            raw = progs[name] if isinstance(progs[name], dict) else {}
            route = raw.get("route", "?")
            dests = raw.get("destinations", [])
            if isinstance(dests, str):
                dests = [dests]
            n = len(raw.get("screens", [])) \
                if isinstance(raw.get("screens"), list) else 0
            print(f"{name}: route {route} "
                  f"({', '.join(str(d) for d in dests)}) "
                  f"{n} screens")
        return

    prog = resolve_program(data, progs, gRotate, gFit, args.program,
                           args.program_file)
    if args.rotate_seconds is not None:
        if args.rotate_seconds <= 0:
            sys.exit("--rotate-seconds must be positive")
        for s in prog["screens"]:
            s["seconds"] = args.rotate_seconds
    if args.image_fit is not None:
        for s in prog["screens"]:
            s["fit"] = args.image_fit

    if args.mock:
        print(f"program '{prog['name']}' route {prog['route']} "
              f"({', '.join(prog['destinations'])})")
        from images import describe_images
        for path, w, h in describe_images(
                [s["image"] for s in prog["screens"]]):
            sec = next(s["seconds"] for s in prog["screens"]
                       if s["image"] == path)
            print(f"  [{sec:g}s] {path} ({w}x{h})")
        return

    if args.preview:
        import preview
        W = args.led_cols * args.led_chain
        H = args.led_rows
        for i, s in enumerate(prog["screens"], 1):
            rec = preview.install(W, H)
            a2 = argparse.Namespace(**vars(args))
            a2.once = True
            run_program(a2, {"name": prog["name"],
                             "route": prog["route"],
                             "destinations": prog["destinations"],
                             "screens": [s]})
            print(f"--- program '{prog['name']}' screen {i}/"
                  f"{len(prog['screens'])} ({W}x{H}) {s['image']} "
                  f"[{s['seconds']:g}s] ---")
            lit = sum(1 for v in rec.paint.values() if v != (0, 0, 0))
            print(f"  {lit} lit pixels of {W * H}")
        return

    run_program(args, prog)


if __name__ == "__main__":
    main()
