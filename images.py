#!/usr/bin/env python3
"""Shared PNG image support for the bus boards (stdlib only).

decode_png + scale_pixels live here so bus-board.py (mixed text +
image playlist) and program.py (image programmes from JSON) share
one decoder. No third-party imports: works on a bare Pi or Mac.
"""

import os
import sys


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


def load_frame(path, W, H, mode):
    """Decode a PNG and scale it to the panel. Returns (sw, sh, frame)."""
    sw, sh, rgb = decode_png(path)
    return sw, sh, scale_pixels(rgb, sw, sh, W, H, mode)


def tint_frame(frame, colour):
    """Render an RGB frame in one colour at the same brightness.

    Each pixel's luminance (Rec. 601) scales the override colour, so
    even a full-colour image shows as pure monochrome: black stays
    black, and every lit pixel takes the colour at its own
    brightness. (Use "full" instead to keep the image's colours.)"""
    r, g, b = colour
    out = bytearray(len(frame))
    for i in range(len(frame) // 3):
        o = i * 3
        lum = (299 * frame[o] + 587 * frame[o + 1] +
               114 * frame[o + 2] + 500) // 1000
        out[o] = (lum * r + 127) // 255
        out[o + 1] = (lum * g + 127) // 255
        out[o + 2] = (lum * b + 127) // 255
    return out


def dim_frame(frame, pct):
    """Scale an RGB frame to pct% brightness (100 = unchanged).

    Dimming bright images cuts peak current draw, which is the most
    common cause of flicker on big lit areas."""
    if pct >= 100:
        return frame
    out = bytearray(len(frame))
    for i in range(len(frame)):
        out[i] = (frame[i] * pct) // 100
    return out


def describe_images(paths):
    """Decode (no scaling) to report dimensions. Returns [(path, w, h)]."""
    out = []
    for path in paths:
        w, h, _ = decode_png(path)
        out.append((path, w, h))
    return out
