#!/usr/bin/env python3
"""
Bus front destination blind replica for RGB LED matrix.

Replicates the amber destination display on the front of a UK bus:

  +------------------------------------------------+
  | 43   SHEFFIELD                                 |
  |      via Dronfield, Chesterfield               |
  +------------------------------------------------+

Route number huge on the left, filling the panel bottom-to-top
(pixel-doubled font), destination and via stacked in the remaining
space to its right, both centred and much bigger than before. All
amber, like the real blinds. Give one service to hold it, or several
to rotate like a bus cycling through displays.

  python3 bus-board.py --mock --once \
    --service "43|Sheffield|Dronfield, Chesterfield"

  python3 bus-board.py --mock --once \
    --service "43|Sheffield|Dronfield, Chesterfield" \
    --service "X17|Matlock|Rowsley, Darley Dale"

Service shape: "ROUTE|DESTINATION|VIA" (VIA optional).
Shorthand also works: "43: Sheffield via Dronfield".
Or a JSON file: [{"route": "43", "destination": "Sheffield",
"via": "Dronfield"}, ...] ("dest" accepted for "destination").

Image mode: --image bitmap/fluffynet.png shows a PNG scaled to the
panel instead of blinds (--image-fit fit|fill|stretch, default fit).
Repeatable, rotates like services. PNG decoding is stdlib only.

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

import tempfile

FONTS = {
    "route": "10x20.bdf",  # pixel-doubled at load -> fills panel height
    "dest": "10x20.bdf",   # destination, pinned to the top
    "via": "6x13B.bdf",    # via points, smaller, bottom half
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


def via_text(svc, prefix=True):
    via = (svc.get("via") or "").strip()
    if not via:
        return ""
    return f"via {via}" if prefix else via


def format_console(services, show_via=True):
    out = []
    for svc in services:
        route = (svc.get("route") or "").strip()
        dest = (svc.get("destination") or "").strip().upper()
        via = via_text(svc, prefix=show_via)
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


def load_fonts(graphics, route_scale=2):
    F = {}
    for role, name in FONTS.items():
        path = find_font(name)
        if role == "route" and route_scale != 1:
            path = scale_bdf(path, route_scale)
        f = graphics.Font()
        f.LoadFont(path)
        F[role] = f
    # doubled/tripled destination for the no-via case (grown to fill)
    base = find_font(FONTS["dest"])
    for s in (2, 3):
        f = graphics.Font()
        f.LoadFont(scale_bdf(base, s))
        F[f"dest{s}"] = f
    # one step smaller route for long numbers (>4 chars)
    small = max(1, route_scale - 1)
    path = find_font(FONTS["route"])
    if small != 1:
        path = scale_bdf(path, small)
    f = graphics.Font()
    f.LoadFont(path)
    F["route_sm"] = f
    return F


ROUTE_SHRINK_AFTER = 4  # route numbers longer than this drop one size


def scale_bdf(src, scale):
    """Pixel-double (or triple) a BDF font, return the scaled file path.

    Each source pixel becomes scale x scale pixels: BBX/DWIDTH extents
    and every bitmap row are expanded, so e.g. 10x20 @2x renders ~40px
    tall and the route number fills the panel bottom-to-top. The
    scaled file lives in the OS temp dir (regenerated every run).
    """
    if scale < 2:
        return src
    with open(src, errors="replace") as f:
        lines = f.read().splitlines()
    out = []
    in_bitmap = False
    bbx_w = 0
    for line in lines:
        p = line.split()
        if not p:
            out.append(line)
            continue
        if p[0] == "FONTBOUNDINGBOX" and len(p) >= 5:
            w, h, xo, yo = (int(x) for x in p[1:5])
            out.append(f"FONTBOUNDINGBOX {w * scale} {h * scale} "
                       f"{xo * scale} {yo * scale}")
        elif p[0] == "BBX" and len(p) >= 5:
            bbx_w, h, xo, yo = (int(x) for x in p[1:5])
            bbx_w *= scale
            out.append(f"BBX {bbx_w} {h * scale} {xo * scale} {yo * scale}")
        elif p[0] == "DWIDTH" and len(p) >= 3:
            out.append(f"DWIDTH {int(p[1]) * scale} {p[2]}")
        elif p[0] == "BITMAP":
            in_bitmap = True
            out.append(line)
        elif p[0] == "ENDCHAR":
            in_bitmap = False
            bbx_w = 0
            out.append(line)
        elif in_bitmap and bbx_w:
            row = line.strip()
            nbytes = (bbx_w // scale + 7) // 8
            bits = "".join(f"{int(row[i:i + 2] or '00', 16):08b}"
                            for i in range(0, nbytes * 2, 2))
            bits = bits[:bbx_w // scale]
            big = "".join(b * scale for b in bits)
            big += "0" * ((-len(big)) % 8)
            out.append("".join(f"{int(big[i:i + 8], 2):02X}"
                               for i in range(0, len(big), 8)))
        else:
            out.append(line)
    # repeat each bitmap row vertically: second pass over the rows
    final = []
    in_bitmap = False
    for line in out:
        final.append(line)
        if line == "BITMAP":
            in_bitmap = True
        elif line == "ENDCHAR":
            in_bitmap = False
        elif in_bitmap:
            for _ in range(scale - 1):
                final.append(line)
    fd, dst = None, os.path.join(
        tempfile.gettempdir(),
        f"busroute-{os.path.basename(src)}-{scale}x.bdf")
    with open(dst, "w") as f:
        f.write("\n".join(final) + "\n")
    return dst


def text_width(graphics, canvas, font, color, text):
    return graphics.DrawText(canvas, font, 0, -100, color, text)


# ---------------------------------------------------------------------------
# PNG images (stdlib only): decode + scale to the panel
# ---------------------------------------------------------------------------

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(path):
    """Minimal PNG decoder, stdlib only. Returns (w, h, RGB bytearray).

    Handles non-interlaced colour types 0/2/3/4/6 at bit depths
    1/2/4/8 (16-bit keeps the high byte). Alpha is composited onto
    black. Anything fancier (interlaced, weird chunks) exits with a
    plain message telling you to re-export the file.
    """
    import struct
    import zlib
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        sys.exit(f"image {path}: {e}")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit(f"image {path}: not a PNG file "
                 "(convert it to .png first)")
    pos = 8
    width = height = bitd = ctype = None
    idat = bytearray()
    palette = None
    pal_alpha = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            break
        if typ == b"IHDR":
            width, height, bitd, ctype, _, _, inter = \
                struct.unpack(">IIBBBBB", body)
            if inter != 0:
                sys.exit(f"image {path}: interlaced PNGs are not "
                         f"supported (re-export without interlacing)")
            if ctype not in (0, 2, 3, 4, 6) or bitd not in \
                    (1, 2, 4, 8, 16):
                sys.exit(f"image {path}: unsupported PNG "
                         f"(type {ctype}, {bitd}-bit)")
        elif typ == b"PLTE":
            palette = body
        elif typ == b"tRNS":
            pal_alpha = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + length
    if width is None:
        sys.exit(f"image {path}: no IHDR found, file is corrupt?")
    try:
        raw = zlib.decompress(bytes(idat))
    except Exception:
        sys.exit(f"image {path}: corrupt image data")
    spp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    if bitd == 16:
        stride = width * spp * 2
        fbpp = spp * 2
    elif bitd >= 8:
        stride = width * spp
        fbpp = spp
    else:
        stride = (width * bitd * spp + 7) // 8
        fbpp = 1
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(fbpp, stride):
                line[i] = (line[i] + line[i - fbpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - fbpp] if i >= fbpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - fbpp] if i >= fbpp else 0
                b = prev[i]
                c = prev[i - fbpp] if i >= fbpp else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 255
        elif f != 0:
            sys.exit(f"image {path}: bad filter {f} on row {y}")
        prev = line
        o = y * width * 3
        if bitd == 16:
            s = 0
            for x in range(width):
                v = line[s:s + spp * 2:2]
                s += spp * 2
                if ctype == 0:
                    out[o:o + 3] = bytes((v[0], v[0], v[0]))
                elif ctype == 2:
                    out[o:o + 3] = bytes(v[:3])
                elif ctype == 4:
                    g, a = v[0], v[1]
                    g = (g * a + 127) // 255
                    out[o:o + 3] = bytes((g, g, g))
                else:
                    r, g, b, a = v[0], v[1], v[2], v[3]
                    out[o:o + 3] = bytes(((r * a + 127) // 255,
                                          (g * a + 127) // 255,
                                          (b * a + 127) // 255))
                o += 3
        elif bitd == 8:
            s = 0
            for x in range(width):
                if ctype == 0:
                    g = line[s]
                    s += 1
                    out[o:o + 3] = bytes((g, g, g))
                elif ctype == 2:
                    out[o:o + 3] = bytes(line[s:s + 3])
                    s += 3
                elif ctype == 3:
                    i = line[s]
                    s += 1
                    r, g, b = (palette[3 * i:3 * i + 3]
                               if palette and 3 * i + 2 < len(palette)
                               else b"\x00\x00\x00")
                    a = pal_alpha[i] if pal_alpha and i < len(
                        pal_alpha) else 255
                    out[o:o + 3] = bytes(((r * a + 127) // 255,
                                          (g * a + 127) // 255,
                                          (b * a + 127) // 255))
                elif ctype == 4:
                    g, a = line[s], line[s + 1]
                    s += 2
                    g = (g * a + 127) // 255
                    out[o:o + 3] = bytes((g, g, g))
                else:
                    r, g, b, a = line[s:s + 4]
                    s += 4
                    out[o:o + 3] = bytes(((r * a + 127) // 255,
                                          (g * a + 127) // 255,
                                          (b * a + 127) // 255))
                o += 3
        else:
            acc, bits = 0, 0
            s = 0
            for x in range(width):
                if bits == 0:
                    acc, bits = line[s], 8
                    s += 1
                bits -= bitd
                i = (acc >> bits) & ((1 << bitd) - 1)
                if bitd < 8:
                    i = (i * 255 + ((1 << bitd) - 1) // 2) // \
                        ((1 << bitd) - 1)
                if ctype == 3:
                    r, g, b = (palette[3 * i:3 * i + 3]
                               if palette and 3 * i + 2 < len(palette)
                               else b"\x00\x00\x00")
                    a = pal_alpha[i] if pal_alpha and i < len(
                        pal_alpha) else 255
                    out[o:o + 3] = bytes(((r * a + 127) // 255,
                                          (g * a + 127) // 255,
                                          (b * a + 127) // 255))
                else:
                    out[o:o + 3] = bytes((i, i, i))
                o += 3
    return width, height, out


def scale_pixels(src, sw, sh, dw, dh, mode):
    """Nearest-neighbour scale of an RGB bytearray to dw x dh.

    fit: whole image visible, centred on black. fill: cover the
    panel, cropping the middle. stretch: exact size, aspect ignored.
    """
    dst = bytearray(dw * dh * 3)  # black bars by default
    if mode == "stretch":
        sx, sy, ox, oy, tw, th = sw / dw, sh / dh, 0, 0, dw, dh
    else:
        s = min(dw / sw, dh / sh) if mode == "fit" else \
            max(dw / sw, dh / sh)
        tw, th = max(1, int(sw * s)), max(1, int(sh * s))
        ox, oy = (dw - tw) // 2, (dh - th) // 2
        sx, sy = sw / tw, sh / th
    for y in range(th):
        if not 0 <= oy + y < dh:
            continue
        srow = int(y * sy) * sw * 3
        drow = ((oy + y) * dw + ox) * 3
        for x in range(tw):
            if not 0 <= ox + x < dw:
                continue
            o = srow + int(x * sx) * 3
            dst[drow:drow + 3] = src[o:o + 3]
            drow += 3
    return dst


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

    F = load_fonts(graphics, route_scale=args.route_scale)
    matrix = RGBMatrix(options=options)
    C = {name: graphics.Color(*rgb) for name, rgb in COLORS.items()}

    offscreen = matrix.CreateFrameCanvas()
    W, H = offscreen.width, offscreen.height

    # image mode: preload every PNG scaled to the panel once
    frames = []
    for ip in (args.image or []):
        sw, sh, rgb = decode_png(ip)
        frames.append((ip, sw, sh,
                       scale_pixels(rgb, sw, sh, W, H, args.image_fit)))
        print(f"image {ip}: {sw}x{sh} -> {args.image_fit} {W}x{H}",
              file=sys.stderr, flush=True)
    if frames:
        print(f"bus board {W}x{H} images={len(frames)}",
              file=sys.stderr, flush=True)
    else:
        print(f"bus board {W}x{H} services={len(services)}",
              file=sys.stderr, flush=True)

    # Vertical geometry: route digits vertically centred (their lit
    # band sits in the upper part of the doubled glyph box, so the
    # baseline sits above panel bottom), dest pinned to the top, via
    # centred in the bottom half (all overridable for other heights).
    route_y = args.main_y if args.main_y is not None else H - 7
    dest_y = args.dest_y if args.dest_y is not None else H // 2 - 3
    via_y = args.via_y if args.via_y is not None else H - 5

    scroll_w = {}
    page_since = time.time()
    idx = 0
    idx_since = time.time()
    scroll_need = 0.0

    def draw_scroll(fnt, col, text, y_base, max_x, xspec="left", x0=1,
                      x1=None):
        nonlocal scroll_need
        key = (id(fnt), text)
        tw = scroll_w.get(key)
        if tw is None:
            if len(scroll_w) > 20:
                scroll_w.clear()
            tw = scroll_w[key] = text_width(
                graphics, offscreen, fnt, col, text)
        if tw <= max_x - x0:
            if x1 is not None and xspec == "center":
                # centre inside the cell [x0..x1], not the whole panel
                x = x0 + max(0, (x1 - x0 + 1 - tw) // 2)
            elif x0 > 1:
                x = x0
            else:
                x = resolve_x(xspec, tw, W)
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

    def dest_baseline(scale):
        """Baseline that vertically centres the caps band.

        Caps light bitmap rows 3..15 of 20, scaled: the band centre
        sits (13*s+1)/2 above the baseline, so offset from mid-panel.
        """
        return int((H - 1) / 2 + (13 * scale + 1) / 2)

    def draw_blind(svc):
        """One front-blind: huge route number left, vertically
        centred; destination and via stacked and centred in the space
        to its right. With no via, the destination grows (up to 3x)
        to fill the cell and centres vertically instead. Route stays
        put; dest and via scroll inside their cells when too long."""
        route = (svc.get("route") or "").strip()
        dest = (svc.get("destination") or "").strip()
        if args.dest_upper:
            dest = dest.upper()
        via = via_text(svc, prefix=args.via)

        # route width claims the left of the panel; dest/via share the
        # cell to its right. Scrolling rows are painted first, the
        # route last, so scroll edge-blanking can never chew into it.
        # Numbers longer than ROUTE_SHRINK_AFTER chars drop one size
        # so they still fit, keeping their own vertical centring.
        w_route = 0
        rfont, ry = F["route"], route_y
        if route:
            if len(route) > ROUTE_SHRINK_AFTER:
                small = max(1, args.route_scale - 1)
                rfont = F["route_sm"]
                ry = dest_baseline(small) if args.main_y is None \
                    else route_y
            w_route = text_width(graphics, offscreen, rfont,
                                 C["route"], route)
        cell_x0 = 1 if not route else 1 + w_route + args.gap
        if via:
            draw_scroll(F["via"], C["via"], via, via_y, W - 1,
                        xspec="center", x0=cell_x0, x1=W - 1)
            draw_scroll(F["dest"], C["dest"], dest, dest_y, W - 1,
                        xspec="center", x0=cell_x0, x1=W - 1)
        else:
            # no via: grow the destination to fill the cell (largest
            # doubling that fits without scrolling), centred
            # vertically; falls back to scrolling 1x when even that
            # is too wide.
            grown = None
            if args.dest_grow:
                for s in (3, 2):
                    fnt = F[f"dest{s}"]
                    tw = text_width(graphics, offscreen, fnt,
                                    C["dest"], dest)
                    if tw <= W - 1 - cell_x0:
                        grown = (fnt, dest_baseline(s))
                        break
            if grown is None:
                grown = (F["dest"], dest_baseline(1))
            fnt, y = grown
            draw_scroll(fnt, C["dest"], dest, y, W - 1,
                        xspec="center", x0=cell_x0, x1=W - 1)
        if route:
            graphics.DrawText(offscreen, rfont, 1, ry,
                              C["route"], route)

    def blit(frame):
        for y in range(H):
            o = y * W * 3
            for x in range(W):
                offscreen.SetPixel(x, y, frame[o], frame[o + 1],
                                   frame[o + 2])
                o += 3

    while True:
        now = time.time()
        if frames:
            # image mode: static pictures, rotate if several
            if len(frames) > 1 and now - idx_since >= args.rotate_seconds:
                idx = (idx + 1) % len(frames)
                idx_since = now
            scroll_need = 0.0
            offscreen.Fill(0, 0, 0)
            blit(frames[idx % len(frames)][3])
            offscreen = matrix.SwapOnVSync(offscreen)
            if args.once:
                break
            time.sleep(0.5)
            continue
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
    p.add_argument("--image", action="append", default=[],
                   help="Show a PNG image scaled to the panel instead of "
                        "blinds, e.g. --image bitmap/fluffynet.png. "
                        "Repeatable: several images rotate. Images take "
                        "over when given; services are ignored.")
    p.add_argument("--image-fit", default="fit",
                   choices=["fit", "fill", "stretch"],
                   help="How images map to the panel: fit (whole image, "
                        "centred on black), fill (cover, cropping), "
                        "stretch (exact panel size)")
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
    p.add_argument("--via", "--no-via",
                   action=argparse.BooleanOptionalAction, default=True,
                   help='Prefix the via line with "via " (--no-via shows '
                        'just the places, e.g. "Dronfield" instead of '
                        '"via Dronfield")')
    p.add_argument("--dest-grow", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="With no via, grow the destination (up to 3x) to "
                        "fill the space instead of scrolling "
                        "(--no-dest-grow to keep it standard size)")
    p.add_argument("--route-scale", type=int, default=2,
                   help="Pixel-scaling of the route number font "
                        "(2 = double size, fills the 40px panel top to "
                        "bottom; 1 = unscaled)")
    p.add_argument("--main-y", type=int, default=None,
                   help="Baseline of the route number "
                        "(default: digits vertically centred)")
    p.add_argument("--dest-y", type=int, default=None,
                   help="Baseline of the destination "
                        "(default: pinned to the top)")
    p.add_argument("--via-y", type=int, default=None,
                   help="Baseline of the via row "
                        "(default: centred in the bottom half)")
    p.add_argument("--gap", type=int, default=6,
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

    if args.image:
        if args.mock:
            for ip in args.image:
                sw, sh, _ = decode_png(ip)
                print(f"{ip}: {sw}x{sh} -> {args.image_fit} "
                      f"{args.led_cols * args.led_chain}"
                      f"x{args.led_rows}")
            return
        if args.preview:
            import preview
            W = args.led_cols * args.led_chain
            H = args.led_rows
            rec = preview.install(W, H)
            run_board(args, [])
            print(f"--- bus image preview ({W}x{H}) ---")
            # images paint raw pixels: count non-black ones
            lit = sum(1 for v in rec.paint.values() if v != (0, 0, 0))
            print(f"  {lit} lit pixels of {W * H}")
            return
        run_board(args, [])
        return

    if args.mock:
        print(format_console(services, show_via=args.via))
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
