#!/usr/bin/env python3
"""Render a TrueType font to a 1-bit BDF bitmap font (Pillow only).

Usage:
    python3 ttf2bdf.py "Johnston100 W03 Regular.ttf" --px 16 -o fonts/johnston16.bdf
    python3 ttf2bdf.py "Johnston100 W03 Regular.ttf" --px 13,16,20 --outdir fonts

Each --px value writes <outdir>/johnston<px>.bdf (or the -o path for one size).
1-bit (mode "1") rendering, no antialiasing, so the result looks exactly
like it will on the LED matrix / engine.py blinds.

Charset defaults to Latin-1 printable (32-126, 160-255) plus common
punctuation: en/em dash, curly quotes, ellipsis, bullet, euro.
"""

import argparse
import os

DEFAULT_EXTRAS = [8211, 8212, 8216, 8217, 8220, 8221, 8230, 8226, 8364]


def charset():
    cps = list(range(32, 127)) + list(range(160, 256)) + DEFAULT_EXTRAS
    return sorted(set(cps))


def convert(ttf, px, out, res=75, family="Johnston100"):
    from PIL import ImageFont

    font = ImageFont.truetype(ttf, px)
    asc, desc = font.getmetrics()  # desc is positive
    hcell = asc + desc
    cps = charset()

    # .notdef signature: Pillow renders missing codepoints as this box.
    # Glyphs byte-identical to it (outside ASCII) are treated as missing.
    def sig(cp):
        ch = chr(cp)
        try:
            m = font.getmask(ch, mode="1")
            w, h = m.size
            bb = font.getbbox(ch, anchor="ls")
            data = bytes(m.getpixel((x, y)) for y in range(h) for x in range(w))
            return (w, h, bb, data)
        except Exception:
            return None

    notdef = sig(0xFFFF)

    glyphs = []  # (enc, dwidth, w, h, xoff, yoff, [hexrows])
    skipped = []
    for cp in cps:
        ch = chr(cp)
        try:
            adv = int(round(font.getlength(ch)))
        except Exception:
            skipped.append(cp)
            continue
        adv = max(1, adv)
        try:
            bb = font.getbbox(ch, anchor="ls")
        except Exception:
            skipped.append(cp)
            continue
        if bb is None:
            skipped.append(cp)
            continue
        x0, y0, x1, y1 = bb
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            # blank (space etc.): full-cell empty bitmap like classic BDFs
            rows = ["00" * ((adv + 7) // 8 or 1)] * hcell
            glyphs.append((cp, adv, adv, hcell, 0, -desc, rows))
            continue
        try:
            mask = font.getmask(ch, mode="1")
        except Exception:
            skipped.append(cp)
            continue
        mw, mh = mask.size
        if (mw, mh) != (w, h):
            # anchor/size mismatch: fall back to drawing the ls-anchored box
            from PIL import Image, ImageDraw

            img = Image.new("1", (w, h), 0)
            ImageDraw.Draw(img).text((-x0, -y0), ch, font=font, fill=1,
                                     anchor="lt")
            mask = img.im if hasattr(img, "im") else None
            # read back via PIL Image pixels
            px_acc = img.load()
            bits_grid = [[1 if px_acc[x, y] else 0 for x in range(w)]
                         for y in range(h)]
        else:
            bits_grid = [[1 if mask.getpixel((x, y)) else 0 for x in range(w)]
                         for y in range(h)]
        flat = bytes(v for row in bits_grid for v in row)
        if (w, h, bb, flat) == (notdef[0], notdef[1], notdef[2], notdef[3]) \
                and cp >= 127:
            skipped.append(cp)  # missing from the TTF, not a real glyph
            continue
        nbytes = (w + 7) // 8
        hexrows = []
        for row in bits_grid:
            val = 0
            for b in row:
                val = (val << 1) | b
            val <<= nbytes * 8 - w  # left-align in bytes per BDF spec
            hexrows.append("%0*X" % (nbytes * 2, val))
        glyphs.append((cp, adv, w, h, x0, -y1, hexrows))

    fbbx_w = max([g[2] for g in glyphs] + [adv for _, adv, *_ in glyphs])
    fbbx_x = min([g[4] for g in glyphs] + [0])
    xlfd = (f"-Misc-{family}-Medium-R-Normal--{px}-{px*10}-{res}-{res}-"
            f"P-{fbbx_w*10}-ISO10646-1")

    with open(out, "w") as f:
        f.write("STARTFONT 2.1\n")
        f.write(f"COMMENT Converted from {os.path.basename(ttf)} "
                f"at {px}px with ttf2bdf.py (Pillow, 1-bit, no AA)\n")
        f.write(f"FONT {xlfd}\n")
        f.write(f"SIZE {px} {res} {res}\n")
        f.write(f"FONTBOUNDINGBOX {fbbx_w} {hcell} {fbbx_x} {-desc}\n")
        f.write("STARTPROPERTIES 9\n")
        f.write(f"FOUNDRY \"Misc\"\nFAMILY_NAME \"{family}\"\n")
        f.write('WEIGHT_NAME "Medium"\nSLANT "R"\n')
        f.write(f"PIXEL_SIZE {px}\nRESOLUTION_X {res}\nRESOLUTION_Y {res}\n")
        f.write(f"FONT_ASCENT {asc}\nFONT_DESCENT {desc}\n")
        f.write(f"ENDPROPERTIES\nCHARS {len(glyphs)}\n")
        for enc, dw, w, h, xo, yo, rows in glyphs:
            ch = chr(enc)
            name = (f"U+{enc:04X}")
            if 33 <= enc <= 126 and ch not in "<>#":
                name = ch
            elif enc == 32:
                name = "space"
            sw = int(round(dw * 72000 / (res * px)))
            f.write(f"STARTCHAR {name}\nENCODING {enc}\n")
            f.write(f"SWIDTH {sw} 0\nDWIDTH {dw} 0\n")
            f.write(f"BBX {w} {h} {xo} {yo}\nBITMAP\n")
            for r in rows:
                f.write(r + "\n")
            f.write("ENDCHAR\n")
        f.write("ENDFONT\n")
    return len(glyphs), skipped


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ttf", help="Input .ttf/.otf path")
    p.add_argument("--px", default="16",
                   help="Pixel size(s), e.g. 16 or 13,16,20 (default 16)")
    p.add_argument("-o", "--output", default=None,
                   help="Output .bdf (single --px only)")
    p.add_argument("--outdir", default="fonts", help="Output dir (multi-px)")
    p.add_argument("--family", default="Johnston100", help="BDF family name")
    args = p.parse_args()
    sizes = [int(s) for s in str(args.px).split(",") if s.strip()]
    if args.output and len(sizes) == 1:
        n, skipped = convert(args.ttf, sizes[0], args.output,
                             family=args.family)
        print(f"{args.output}: {n} glyphs"
              + (f" (skipped {len(skipped)}: {skipped})" if skipped else ""))
    else:
        os.makedirs(args.outdir, exist_ok=True)
        base = "".join(c.lower() if c.isalnum() else ""
                       for c in args.family)
        for px in sizes:
            out = os.path.join(args.outdir, f"{base}{px}.bdf")
            n, skipped = convert(args.ttf, px, out, family=args.family)
            print(f"{out}: {n} glyphs"
                  + (f" (skipped {len(skipped)}: {skipped})" if skipped else ""))


if __name__ == "__main__":
    main()
