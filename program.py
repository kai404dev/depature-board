#!/usr/bin/env python3
"""
Image programmes for the bus LED matrix (the image part of bus-board).

A programme is a named sequence of full-panel PNG images played in
order: which route it belongs to, the destinations it serves (each
with its own images, in play order), and the images. Example
programs.json:

  {
    "rotate_seconds": 10,
    "image_fit": "fit",
    "programs": {
      "401": {
        "route": "401",
        "destinations": {
          "Burton": ["bitmap/destinations/401/burton/401-burton-1.png",
                     "bitmap/destinations/401/burton/401-burton-2.png"],
          "Tutbury": ["bitmap/destinations/401/tutbury/401-tutbury-only.png"]
        }
      }
    }
  }

Colour override: any destination (or screen) takes "colour":
"#ffbb00", multiply-tinted onto the image so black stays black and
bright pixels take the colour:

      "401": {
        "route": "401",
        "destinations": {
          "Burton": {"colour": "#ffbb00",
                     "images": ["bitmap/destinations/401/burton/401-burton-1.png",
                                "bitmap/destinations/401/burton/401-burton-2.png"]}
        }
      }

("color" also accepted.) Colour cascades screen -> destination ->
route (programme) -> file default; a screen's own colour wins, then
its destination's, then its route's, then the file default.

Each image is a path, or {"image": path, "seconds": N,
"fit": fit|fill|stretch, "colour": "#ffbb00"} to override the dwell /
fit / tint for that screen. Without --destination every destination
plays in file order; with it, only that destination's screens play.
Defaults cascade: screen -> programme -> file -> built-in (10s, fit).

  python3 program.py programs.json --program 401 --mock --once
  python3 program.py programs.json --program 401 --destination Burton --preview
  python3 program.py programs.json --list
  sudo python3 program.py programs.json --program 401

Web portal (Mobitec ICU 602 replica) on :4040: F1 enters the route,
F2 picks the destination (arrows or numeric id), the keypad takes
route+dest codes. Destination ids default to file order (0, 1, 2)
and are overridable per destination with {"id": N}:

  sudo python3 program.py programs.json --program 401 --portal
  python3 program.py programs.json --serve --port 4040
"""

import argparse
import json
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

from images import load_frame, tint_frame

DEFAULT_ROTATE = 10
DEFAULT_FIT = "fit"
FITS = ("fit", "fill", "stretch")
CONTROL_FILE = os.path.join(THIS_DIR, "program_control.json")


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
    gColour = parse_colour(data.get("colour", data.get("color")),
                           "<file>", "default colour")
    return data, progs, gRotate, gFit, gColour


def parse_colour(v, prog_name, label):
    if v is None:
        return None
    s = str(v).strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF"
                           for c in s):
        sys.exit(f"program '{prog_name}' {label}: colour must be "
                 f"#rrggbb, got '{v}'")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def colour_str(rgb):
    return "#%02x%02x%02x" % rgb


def destination_ids(raw, prog_name):
    """[(name, id)] in file order for one programme.

    The id defaults to the destination's position (0, 1, 2, ...),
    overridable per destination with {"id": N} alongside images.
    """
    dests_raw = raw.get("destinations", [])
    if isinstance(dests_raw, dict):
        if not dests_raw:
            sys.exit(f"program '{prog_name}': 'destinations' is empty")
        out, seen = [], set()
        for i, (dname, entry) in enumerate(dests_raw.items()):
            ident = i
            if isinstance(entry, dict):
                ident = entry.get("id", i)
                if isinstance(ident, bool) or not isinstance(ident, int) \
                        or ident < 0:
                    sys.exit(f"program '{prog_name}' destination "
                             f"'{dname}': id must be 0 or more")
            if ident in seen:
                sys.exit(f"program '{prog_name}': duplicate "
                         f"destination id {ident}")
            seen.add(ident)
            out.append((str(dname), ident))
        return out
    if isinstance(dests_raw, str):
        dests_raw = [dests_raw]
    dests = [str(d).strip() for d in dests_raw if str(d).strip()]
    return [(d, i) for i, d in enumerate(dests)]


def parse_screen(s, prog_name, label, pRotate, pFit, dColour):
    if isinstance(s, str):
        s = {"image": s}
    if not isinstance(s, dict) or not str(s.get("image", "")).strip():
        sys.exit(f"program '{prog_name}' {label}: must be an image "
                 f"path or {{\"image\": path, ...}}")
    sec = s.get("seconds", pRotate)
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        sys.exit(f"program '{prog_name}' {label}: seconds must be "
                 f"a number")
    if sec <= 0:
        sys.exit(f"program '{prog_name}' {label}: seconds must be "
                 f"positive")
    fit = s.get("fit", pFit)
    if fit not in FITS:
        sys.exit(f"program '{prog_name}' {label}: fit must be one "
                 f"of {FITS}")
    colour = parse_colour(s.get("colour", s.get("color")), prog_name,
                           label) or dColour
    return {"image": str(s["image"]).strip(),
            "seconds": sec, "fit": fit, "colour": colour}


def resolve_program(data, progs, gRotate, gFit, gColour, name,
                    dest_filter, path):
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
    pRotate = raw.get("rotate_seconds", gRotate)
    pFit = raw.get("image_fit", gFit)
    if pFit not in FITS:
        sys.exit(f"program '{name}': image_fit must be one of {FITS}")
    pColour = parse_colour(raw.get("colour", raw.get("color")),
                           name, "route colour") or gColour
    dests_raw = raw.get("destinations", [])
    pairs = []  # (destination, colour, screen entry), file order
    if isinstance(dests_raw, dict):
        if not dests_raw:
            sys.exit(f"program '{name}': 'destinations' is empty")
        for dname, entry in dests_raw.items():
            dcolour = None
            if isinstance(entry, dict):
                dcolour = parse_colour(
                    entry.get("colour", entry.get("color")), name,
                    f"destination '{dname}'")
                lst = entry.get("images", entry.get("screens"))
            else:
                lst = entry
            if dcolour is None:
                dcolour = pColour
            if not isinstance(lst, list) or not lst:
                sys.exit(f"program '{name}' destination '{dname}': "
                         f"need a non-empty list of images")
            for s in lst:
                pairs.append((str(dname), dcolour, s))
        dests = [str(d) for d in dests_raw]
    else:
        if isinstance(dests_raw, str):
            dests_raw = [dests_raw]
        if not isinstance(dests_raw, list) or \
                not [d for d in dests_raw if str(d).strip()]:
            sys.exit(f"program '{name}': need 'destinations' as a "
                     f"list or {{\"name\": [images...]}}")
        dests = [str(d).strip() for d in dests_raw if str(d).strip()]
        screens = raw.get("screens")
        if not isinstance(screens, list) or not screens:
            sys.exit(f"program '{name}': need a non-empty 'screens' "
                     f"list of image paths, in play order")
        pairs = [(None, pColour, s) for s in screens]
    if dest_filter is not None:
        hit = next((d for d in dests
                    if d.lower() == dest_filter.lower()), None)
        if hit is None:
            sys.exit(f"program '{name}' has no destination "
                     f"'{dest_filter}' "
                     f"(have: {', '.join(dests)})")
        pairs = [(d, c, s) for d, c, s in pairs if d == hit]
        dests = [hit]
    out = []
    for i, (d, c, s) in enumerate(pairs, 1):
        label = f"screen {i}" + (f" ({d})" if d else "")
        entry = parse_screen(s, name, label, pRotate, pFit, c)
        entry["destination"] = d
        out.append(entry)
    if not out:
        sys.exit(f"program '{name}': no screens to play")
    return {"name": name, "route": route, "destinations": dests,
            "screens": out}


def run_program(args, prog, watch=None):
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
        if s.get("colour"):
            frame = tint_frame(frame, s["colour"])
        frames.append((s, frame))
        tag = f" [{s['destination']}]" if s.get("destination") else ""
        if s.get("colour"):
            tag += f" {colour_str(s['colour'])}"
        print(f"image {s['image']}: {sw}x{sh} -> {s['fit']} {W}x{H} "
              f"({s['seconds']}s){tag}", file=sys.stderr, flush=True)
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
        if watch is not None and watch():
            break
        time.sleep(0.5)


def read_control(path):
    """Selection written by the portal: (program, destination|None).
    None when absent/unreadable -- the CLI selection stands."""
    try:
        with open(path) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict):
        return None
    p, d = c.get("program"), c.get("destination")
    if not isinstance(p, str) or not p:
        return None
    if d is not None and not isinstance(d, str):
        return None
    return (p, d)


def run_dynamic(args, ctl):
    """Matrix loop following the live selection.

    The portal (same process, or another one via program_control.json)
    drives; a broken program file keeps the old screens. The CLI
    selection wins at startup -- the control file only takes over
    when it changes afterwards.
    """
    last = None
    last_file = read_control(CONTROL_FILE)
    while True:
        ctl.refresh()
        cur_file = read_control(CONTROL_FILE)
        if cur_file != last_file:
            last_file = cur_file
            if cur_file is not None and \
                    cur_file != ctl.selection():
                if ctl.adopt(*cur_file):
                    print(f"control: showing {cur_file[0]} / "
                          f"{cur_file[1] or 'all'}",
                          file=sys.stderr, flush=True)
                else:
                    print(f"control: ignoring {cur_file}",
                          file=sys.stderr, flush=True)
        key = ctl.key()
        if key != last:
            try:
                prog = ctl.resolve()
            except SystemExit as e:
                print(f"selection failed ({e}); keeping screens",
                      file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            last = key
            run_program(args, prog,
                        watch=lambda k=key: ctl.refresh() or
                        ctl.key() != k or
                        read_control(CONTROL_FILE) != last_file)
            if args.once:
                break
        else:
            time.sleep(0.5)


def main():
    p = argparse.ArgumentParser(description="Bus image programmes")
    p.add_argument("program_file",
                   help="JSON file with programmes, e.g. programs.json")
    p.add_argument("--program", default=None,
                   help="Programme to play (unneeded when the file "
                        "holds just one)")
    p.add_argument("--destination", default=None,
                   help="Play only this destination's screens "
                        "(default: every destination in file order)")
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
    p.add_argument("--portal", action="store_true",
                   help="Host the ICU 602 web portal alongside the matrix "
                        "(default port 4040)")
    p.add_argument("--port", type=int, default=4040,
                   help="Web portal port (default 4040)")
    p.add_argument("--serve", action="store_true",
                   help="Host the web portal only, without the matrix")
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

    data, progs, gRotate, gFit, gColour = load_programs_file(
        args.program_file)

    ctl = None
    if args.serve or args.portal or \
            (not args.mock and not args.preview and not args.list):
        # every live matrix run follows the live selection: CLI seeds
        # it, the portal (here or another process via
        # program_control.json) steers it afterwards.
        from portal import Controller, serve
        ctl = Controller(args.program_file, program=args.program,
                         dest=args.destination, control=CONTROL_FILE)
        if args.serve:
            serve(ctl, args.port)  # blocking
            return
        if args.portal:
            import threading
            threading.Thread(target=serve, args=(ctl, args.port),
                             daemon=True).start()
            print(f"portal on :{args.port}", file=sys.stderr, flush=True)
        run_dynamic(args, ctl)
        return

    if args.list:
        for name in sorted(progs):
            raw = progs[name] if isinstance(progs[name], dict) else {}
            route = raw.get("route", "?")
            code = str(raw.get("code", "") or "").strip()
            ctag = f" [code {code}]" if code else ""
            dests = raw.get("destinations", [])
            if isinstance(dests, dict):
                parts = []
                for d, i in destination_ids(raw, name):
                    v = dests[d]
                    if isinstance(v, dict):
                        v = v.get("images", v.get("screens", []))
                    n = len(v) if isinstance(v, list) else 0
                    parts.append(f"{d}#{i}x{n}")
                print(f"{name}: route {route}{ctag} "
                      f"({', '.join(parts)})")
                continue
            if isinstance(dests, str):
                dests = [dests]
            n = len(raw.get("screens", [])) \
                if isinstance(raw.get("screens"), list) else 0
            print(f"{name}: route {route}{ctag} "
                  f"({', '.join(str(d) for d in dests)}) "
                  f"{n} screens")
        return

    prog = resolve_program(data, progs, gRotate, gFit, gColour,
                           args.program, args.destination,
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
        last_dest = None
        infos = {p: (w, h) for p, w, h in describe_images(
            [s["image"] for s in prog["screens"]])}
        for s in prog["screens"]:
            if s.get("destination") != last_dest:
                last_dest = s.get("destination")
                if last_dest:
                    print(f"  {last_dest}:")
            w, h = infos[s["image"]]
            ctag = f" {colour_str(s['colour'])}" if s.get("colour") \
                else ""
            print(f"    [{s['seconds']:g}s] {s['image']} ({w}x{h})"
                  f"{ctag}")
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
                  f"[{s['seconds']:g}s]"
                  f"{' ' + s['destination'] if s.get('destination') else ''}"
                  f"{' ' + colour_str(s['colour']) if s.get('colour') else ''} ---")
            lit = sum(1 for v in rec.paint.values() if v != (0, 0, 0))
            print(f"  {lit} lit pixels of {W * H}")
        return

    run_program(args, prog)


if __name__ == "__main__":
    main()
