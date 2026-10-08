#!/usr/bin/env python3
"""Mobitec ICU 602 replica web portal for program.py (stdlib only).

Hosts the controller UI: F1 enters the route, F2 picks the
destination (arrows or numeric id), the keypad takes full
route+dest codes. Drives program.py's live selection; the matrix
follows whatever is picked here.
"""

import base64
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import program as model

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
REPO_ROOT = os.path.abspath(os.path.join(THIS_DIR, os.pardir))
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, REPO_ROOT)  # engine.py (text renderer) stays at root

import engine as ENG


def slug(s, fallback="untitled"):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").strip().lower())
            .strip("-") or fallback)


TEXT_STYLES = ("top", "bottom", "left", "right")


def canonical_colour(v):
    """Validated programme colour string (#rrggbb, full or RGB)."""
    try:
        c = model.parse_colour(v, "<new>", "colour")
    except SystemExit:
        raise ValueError("colour must be #rrggbb (e.g. #ff8000), "
                         "full or RGB")
    if c == "full":
        return "full"
    if c == "rgb":
        return "RGB"
    return "#%02x%02x%02x" % c


def parse_seconds(v):
    if v in (None, ""):
        return None
    try:
        secs = float(v)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if secs <= 0:
        raise ValueError("seconds must be positive")
    return secs


def parse_dest_id(v):
    if v in (None, ""):
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v).strip())
        except (TypeError, ValueError):
            raise ValueError("destination id must be a whole number")
    if v < 0:
        raise ValueError("destination id must be 0 or more")
    return v


def render_text_png(route, dest, via, style, colour_hex):
    """One 240x40 blind PNG from typed text (Sign Studio BDF look).

    Returns (png_bytes, info). Raises ValueError with a plain message.
    """
    style = str(style or "top").lower()
    if style not in TEXT_STYLES:
        raise ValueError(f"style must be one of {TEXT_STYLES}")
    if not (str(route or "").strip() or str(dest or "").strip()):
        raise ValueError("type a route number and/or a destination")
    job = {"route": str(route or ""), "dest": str(dest or ""),
           "via": str(via or ""), "style": style,
           "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
           "via_font": "6x13B.bdf", "route_scale": 2,
           "dest_scale": 1, "via_scale": 1,
           "fg": colour_hex or "#ff8000"}
    try:
        frame, info = ENG.render(job)
    except ValueError as e:
        raise ValueError(str(e))
    return ENG.encode_png(frame), info


def numeric_route(route):
    """Digits of a route number: X12 -> 12."""
    return "".join(c for c in str(route) if c.isdigit())


def has_letter(route):
    return any(c.isalpha() for c in str(route))


def match_route(buf, routes):
    """Match a typed route against [(program, route, code)]. Letter
    routes can be typed with a leading 1 (112 -> X12 when digits
    match); any route also answers to its custom file "code"."""
    b = str(buf).strip().upper()
    for n, r, c in routes:
        if r and str(r).upper() == b:
            return n
    for n, r, c in routes:
        if c and str(c).upper() == b:
            return n
    if len(b) > 1 and b.startswith("1"):
        rest = b[1:]
        for n, r, c in routes:
            if r and str(r).upper() == rest:
                return n
        for n, r, c in routes:
            if c and str(c).upper() == rest:
                return n
        hits = [n for n, r, c in routes
                if r and has_letter(r) and numeric_route(r) == rest]
        if len(hits) == 1:
            return hits[0]
    hits = [n for n, r, c in routes
            if r and has_letter(r) and numeric_route(r) == b]
    if len(hits) == 1:
        return hits[0]
    return None


def decode_keypad(code, routes, ids_of):
    """Full route+dest code, e.g. 40101 -> (401 prog, dest id 1),
    11204 -> (X12 prog, dest id 4). Returns (program, dest|None)."""
    digits = "".join(c for c in str(code) if c.isdigit())
    if len(digits) < 3:
        return (None, None)
    ident, rp = int(digits[-2:]), digits[:-2]
    name = match_route(rp, routes)
    if name is None:
        return (None, None)
    for d, di in ids_of(name):
        if di == ident:
            return (name, d)
    return (name, None)


class Controller:
    """Live selection shared between the portal and the matrix loop.

    Confirmed picks are also written to a control file so a matrix
    run in another process follows the portal (and vice versa).
    """

    def __init__(self, path, program=None, dest=None, control=None):
        self.path = path
        self.control = control
        self.lock = threading.RLock()  # re-entrant: store/create nest
        self.mtime = None
        self.data = None
        self._gen = 0
        self.message = ""
        self.program_name = None
        self.dest_name = None
        if not self._refresh_locked():
            why = f": {self.message}" if self.message else ""
            sys.exit(f"portal: cannot load {path}{why}")
        names = list(self.data[1])
        if program is None:
            program = names[0]
        if program not in self.data[1]:
            sys.exit(f"portal: program '{program}' not in {path} "
                     f"(have: {', '.join(sorted(names))})")
        self.program_name = program
        self.dest_name = None
        if dest is not None:
            hit = self._match_dest(dest)
            if hit is None:
                sys.exit(f"portal: no destination '{dest}' in "
                         f"program '{program}'")
            self.dest_name = hit
        self.initial = (self.program_name, self.dest_name)
        self.field = "line"
        self.buffer = ""
        self.hi = self._dest_index()
        self.hi_all = self._flat_index()
        if self.message == "":
            self.message = "F1 line, F2 destination"

    # -- file ------------------------------------------------------
    def _refresh_locked(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            return False
        if self.mtime is not None and mt == self.mtime:
            return False
        try:
            data = model.load_programs_file(self.path)
            for nm, raw in data[1].items():
                if isinstance(raw, dict):
                    model.destination_ids(raw, nm)
                    code = raw.get("code", "")
                    if code not in (None, "") and \
                            not str(code).strip().isdigit():
                        raise ValueError(
                            f"program '{nm}': code must be digits")
        except SystemExit as e:
            self.message = str(e)
            return False
        except ValueError as e:
            self.message = str(e)
            return False
        self.data = data
        self.mtime = mt
        self._gen += 1
        if self.program_name is not None and \
                self.program_name not in data[1]:
            self.program_name = next(iter(data[1]))
            self.dest_name = None
            self.message = "Program file reloaded"
        return True

    def refresh(self):
        with self.lock:
            return self._refresh_locked()

    # -- selection (for the matrix loop) ---------------------------
    def key(self):
        with self.lock:
            return (self.program_name, self.dest_name, self._gen)

    def resolve(self):
        with self.lock:
            data, progs, gR, gF, gC = self.data
            pn, dn = self.program_name, self.dest_name
        return model.resolve_program(data, progs, gR, gF, gC, pn, dn,
                                     self.path)

    # -- helpers (lock held by caller) ------------------------------
    def _routes(self):
        out = []
        for n, r in self.data[1].items():
            if not isinstance(r, dict):
                continue
            out.append((n, r.get("route", ""),
                        str(r.get("code", "") or "").strip()))
        return out

    def _ids(self):
        raw = self.data[1].get(self.program_name)
        if not isinstance(raw, dict):
            return []
        return model.destination_ids(raw, self.program_name)

    def _match_dest(self, dest, program=None):
        for d, _ in self._ids_for(program or self.program_name):
            if d.lower() == str(dest).lower():
                return d
        return None

    def _dest_index(self):
        for i, (d, _) in enumerate(self._ids()):
            if d == self.dest_name:
                return i
        return 0

    def _route_of(self, name):
        raw = self.data[1].get(name, {})
        r = raw.get("route", "") if isinstance(raw, dict) else ""
        return r or name

    # -- keys --------------------------------------------------------
    def press(self, k):
        with self.lock:
            self._refresh_locked()
            sel0 = (self.program_name, self.dest_name)
            k = str(k)
            if k == "F1":
                self.field, self.buffer = "line", ""
                self.message = "Enter line number"
            elif k in ("F2", "dest"):
                self.field = "dest"
                self.buffer = ""
                self.hi = self._dest_index()
                self.message = "Select destination"
            elif k in ("F3", "F4"):
                self.message = f"{k} not used"
            elif k == "F5":
                self.field = "all"
                self.buffer = ""
                self.hi_all = self._flat_index()
                self.message = "All destinations - arrows, tick to select"
            elif k in ("home", "clearall"):
                self.program_name, self.dest_name = self._clamp(
                    *self.initial)
                self.field, self.buffer = "line", ""
                self.hi = self._dest_index()
                self.hi_all = self._flat_index()
                self.message = "Cleared"
            elif k == "clear":
                self.buffer = ""
                self.message = ""
            elif k.isdigit() and len(k) == 1:
                self._digit(k)
            elif k in ("up", "down", "left", "right"):
                self._arrow(k)
            elif k == "ok":
                self._confirm()
            elif k.startswith("pickall:"):
                try:
                    i = int(k[len("pickall:"):])
                except ValueError:
                    i = None
                flat = self._flat()
                if i is None or not 0 <= i < len(flat):
                    self.message = "Nothing to pick"
                else:
                    name, dest, _ = flat[i]
                    self._apply_program(name)
                    self.dest_name = dest
                    self.hi = self._dest_index()
                    self.message = ""
                self.buffer = ""
            elif k.startswith("pick:"):
                hit = self._match_dest(k[len("pick:"):])
                if hit is None:
                    self.message = "Nothing to pick"
                else:
                    self.dest_name = hit
                    self.message = ""
                self.buffer = ""
            else:
                self.message = f"Unknown key {k}"
            if (self.program_name, self.dest_name) != sel0:
                self._save_control()
            return self._snapshot_locked()

    def _save_control(self):
        if not self.control:
            return
        try:
            with open(self.control, "w") as f:
                json.dump({"program": self.program_name,
                           "destination": self.dest_name}, f)
        except OSError as e:
            self.message = f"Cannot write control: {e}"

    def adopt(self, program, dest):
        """Take a selection from the control file. False + message
        when it names something unknown."""
        with self.lock:
            self._refresh_locked()
            if program not in self.data[1]:
                self.message = f"Ignoring control: no program " \
                    f"'{program}'"
                return False
            if dest is not None:
                dest = self._match_dest(dest, program)
                if dest is None:
                    self.message = f"Ignoring control: no such " \
                        f"destination"
                    return False
            self.program_name = program
            self.dest_name = dest
            self.field, self.buffer = "line", ""
            self.hi = self._dest_index()
            self.message = ""
            return True

    def selection(self):
        with self.lock:
            return (self.program_name, self.dest_name)

    def known_images(self):
        with self.lock:
            self._refresh_locked()
            return self._known_images()

    def _clamp(self, program, dest):
        if program not in self.data[1]:
            program = next(iter(self.data[1]))
            return program, None
        if dest is not None:
            names = [d for d, _ in self._ids_for(program)]
            if dest not in names:
                dest = None
        return program, dest

    def _ids_for(self, program):
        raw = self.data[1].get(program)
        if not isinstance(raw, dict):
            return []
        return model.destination_ids(raw, program)

    def _first_image(self, program, dest):
        raw = self.data[1].get(program)
        if not isinstance(raw, dict):
            return None
        dests = raw.get("destinations", {})
        if isinstance(dests, dict):
            v = dests.get(dest)
            if isinstance(v, dict):
                v = v.get("images", v.get("screens", []))
            if isinstance(v, list) and v:
                s = v[0]
                return str(s.get("image") if isinstance(s, dict)
                           else s) or None
            return None
        for s in (raw.get("screens") or []):
            p = s.get("image") if isinstance(s, dict) else s
            if p:
                return str(p)
        return None

    def _known_images(self):
        imgs = set()
        for name, raw in self.data[1].items():
            if not isinstance(raw, dict):
                continue
            for d, _ in self._ids_for(name):
                for s in self._screens_of(name, d):
                    if s.get("image"):
                        imgs.add(s["image"])
        return imgs

    def _flat(self):
        """Every (program, destination, id) in file order."""
        out = []
        for name in self.data[1]:
            for d, di in self._ids_for(name):
                out.append((name, d, di))
        return out

    def _flat_index(self):
        for i, (p, d, _) in enumerate(self._flat()):
            if p == self.program_name and d == self.dest_name:
                return i
        for i, (p, d, _) in enumerate(self._flat()):
            if p == self.program_name:
                return i
        return 0

    def _digit(self, k):
        if self.field == "all":
            self.message = "Arrows to browse, tick to select"
            return
        if len(self.buffer) >= 8:
            self.message = "Buffer full - X to clear"
            return
        self.buffer += k
        self.message = ""
        if self.field == "dest":
            try:
                ident = int(self.buffer)
            except ValueError:
                return
            for i, (d, di) in enumerate(self._ids()):
                if di == ident:
                    self.hi = i
                    break

    def _arrow(self, k):
        step = 1 if k in ("down", "right") else -1
        if self.field == "all":
            flat = self._flat()
            if not flat:
                self.message = "No destinations"
                return
            self.hi_all = (self.hi_all + step) % len(flat)
            self.buffer = ""
            self.message = ""
            return
        if self.field != "dest":
            self.message = "F2 for destination"
            return
        ids = self._ids()
        if not ids:
            self.message = "No destinations"
            return
        self.hi = (self.hi + step) % len(ids)
        self.buffer = ""
        self.message = ""

    def _confirm(self):
        if self.field == "all":
            flat = self._flat()
            if not flat:
                self.message = "No destinations"
                return
            name, dest, _ = flat[self.hi_all % len(flat)]
            self._apply_program(name)
            self.dest_name = dest
            self.hi = self._dest_index()
            self.message = ""
            self.buffer = ""
            return
        if self.field == "line":
            buf = self.buffer.strip()
            if not buf:
                self.message = "Enter line number (F1)"
                return
            name = match_route(buf, self._routes())
            if name is not None:
                self._apply_program(name)
                self.message = ""
            else:
                routes = self._routes()
                name, dest = decode_keypad(
                    buf, routes,
                    lambda n: self._ids_for(n))
                if name is None:
                    self.message = f"Unknown line '{buf}'"
                elif dest is None:
                    self.message = f"No dest id {buf[-2:]} " \
                        f"on {self._route_of(name)}"
                else:
                    self._apply_program(name)
                    self.dest_name = dest
                    self.message = ""
            self.buffer = ""
            return
        ids = self._ids()
        if not ids:
            self.message = "No destinations"
        elif self.buffer:
            try:
                ident = int(self.buffer)
            except ValueError:
                ident = None
            hit = next((d for d, di in ids if di == ident), None)
            if hit is None:
                self.message = f"No dest id {self.buffer}"
            else:
                self.dest_name = hit
                self.message = ""
        else:
            self.dest_name = ids[self.hi % len(ids)][0]
            self.message = ""
        self.buffer = ""

    def _apply_program(self, name):
        if name != self.program_name:
            self.program_name = name
            if self.dest_name not in [d for d, _ in self._ids()]:
                self.dest_name = None
        self.hi = self._dest_index()

    # -- UI state ------------------------------------------------------
    def snapshot(self):
        with self.lock:
            self._refresh_locked()
            return self._snapshot_locked()

    def _snapshot_locked(self):
        route = self._route_of(self.program_name)
        ids = self._ids()
        cur_id = next((di for d, di in ids if d == self.dest_name),
                      None)
        flat = self._flat()
        if self.buffer:
            big = self.buffer
            bigimg = None
        elif self.field == "all" and flat:
            p, d, _ = flat[self.hi_all % len(flat)]
            big = f"{self._route_of(p)} {d}"
            bigimg = self._first_image(p, d)
        elif self.dest_name:
            big = f"{route} {self.dest_name}"
            bigimg = self._first_image(self.program_name,
                                       self.dest_name)
        else:
            big = f"{route} ALL" if route else "---"
            bigimg = None
        return {
            "program": self.program_name,
            "program_file": os.path.relpath(self.path, REPO_ROOT)
            if self.path.startswith(REPO_ROOT) else self.path,
            "route": route,
            "dest": self.dest_name,
            "dest_id": cur_id,
            "big": big,
            "bigimg": bigimg,
            "field": self.field,
            "buffer": self.buffer,
            "hi": self.hi,
            "hi_all": self.hi_all,
            "message": self.message,
            "routes": [{"program": n, "route": r or n, "code": c}
                       for n, r, c in self._routes()],
            "destinations": [{"name": d, "id": di,
                              "img": self._first_image(
                                  self.program_name, d),
                              "n": len(self._screens_of(
                                  self.program_name, d))}
                             for d, di in ids],
            "all": [{"program": p, "route": self._route_of(p),
                     "name": d, "id": di,
                     "img": self._first_image(p, d)}
                    for p, d, di in flat],
            "screens": self._screens_of(self.program_name,
                                        self.dest_name),
        }

    def _screens_of(self, program, dest):
        """All screens of the live pick for the preview filmstrip."""
        raw = self.data[1].get(program)
        if not isinstance(raw, dict):
            return []
        dests = raw.get("destinations", {})
        if isinstance(dests, dict):
            if dest is None:
                want = list(dests)
            else:
                want = [dest] if dest in dests else []
            out = []
            for d in want:
                v = dests[d]
                lst = (v.get("images", v.get("screens", []))
                       if isinstance(v, dict) else v)
                if isinstance(lst, list):
                    for s in lst:
                        if isinstance(s, dict):
                            out.append({"image": str(s.get("image", "")),
                                        "seconds": s.get("seconds"),
                                        "dest": d})
                        else:
                            out.append({"image": str(s), "seconds": None,
                                        "dest": d})
            return out
        screens = raw.get("screens", [])
        return [{"image": str(s.get("image") if isinstance(s, dict)
                              else s),
                 "seconds": s.get("seconds") if isinstance(s, dict)
                 else None, "dest": dest}
                for s in (screens if isinstance(screens, list) else [])]

    def add_upload(self, program, dest, filename, png, seconds=None,
                   route="", dest_id=None, colour=None):
        """Save an uploaded PNG and append it to the program file.

        Creates the program/destination when missing (new routes from
        Sign Studio land here). dest_id/colour optionally set the
        destination's numeric id and tint. Returns the repo-relative
        image path. Raises ValueError with a plain message on any
        problem; the JSON file is only replaced once the result
        validates.
        """
        program = str(program or "").strip()
        dest = str(dest or "").strip()
        if not program:
            raise ValueError("name a program (route)")
        if not dest:
            raise ValueError("name a destination")
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("not a PNG file")
        base = os.path.basename(str(filename or "").strip())
        if not base.lower().endswith(".png") or base in (".png",):
            base = f"{slug(dest)}.png"
        base = re.sub(r"[^a-zA-Z0-9._-]", "-", base).strip("-") or \
            f"{slug(dest)}.png"
        with self.lock:
            return self._store_locked(program, dest, base, png,
                                      seconds, route, dest_id, colour)

    def create_text(self, program, dest, route="", via="", style="top",
                    colour="#ff8000", seconds=None, dest_id=None):
        """Create a destination from typed text (no Sign Studio needed).

        Renders one 240x40 blind with the bitmap engine, saves it and
        selects it on screen. Returns (repo-relative path, warnings).
        """
        program = str(program or "").strip()
        dest = str(dest or "").strip()
        if not program:
            raise ValueError("name a program (route)")
        if not dest:
            raise ValueError("name a destination")
        colour = canonical_colour(colour or "#ff8000")
        if colour in ("full", "RGB"):
            raise ValueError("text blinds need a single colour, "
                             "e.g. #ff8000")
        png, info = render_text_png(route, dest, via, style, colour)
        with self.lock:
            self._refresh_locked()
            raw = self.data[1].get(program)
            route_no = str(route or "").strip()
            if isinstance(raw, dict):
                route_no = route_no or str(raw.get("route", "") or "")
            route_no = route_no or program
            taken = {s.get("image") for s in
                     self._screens_of(program, dest)}
            stem = (f"{slug(route_no, 'noroute')}-{slug(dest)}")
            k = 1
            while True:
                base = f"{stem}-{k}.png"
                rel = "/".join(["bus", "bitmap", "destinations",
                                slug(program), slug(route_no), base])
                if rel not in taken and not os.path.isfile(
                        os.path.join(REPO_ROOT, rel)):
                    break
                k += 1
            return (self._store_locked(program, dest, base, png,
                                       seconds, route_no, dest_id,
                                       colour),
                    info.get("warnings", []))

    def _store_locked(self, program, dest, base, png, seconds, route,
                      dest_id, colour):
        """Write PNG + JSON entry. Caller holds the lock."""
        with self.lock:
            self._refresh_locked()
            raw = self.data[1].get(program)
            route_no = str(route or "").strip()
            if isinstance(raw, dict):
                route_no = route_no or str(raw.get("route", "") or "")
            route_no = route_no or program
            rel = "/".join(["bus", "bitmap", "destinations",
                            slug(program), slug(route_no), base])
            full = os.path.normpath(os.path.join(REPO_ROOT, rel))
            if not full.startswith(os.path.join(
                    REPO_ROOT, "bus", "bitmap") + os.sep):
                raise ValueError("bad image path")
            try:
                with open(self.path) as f:
                    original = f.read()
                data = json.loads(original)
            except (OSError, ValueError) as e:
                raise ValueError(f"cannot read program file: {e}")
            progs = data.get("programs")
            if not isinstance(progs, dict):
                raise ValueError("program file has no 'programs' object")
            prog = progs.get(program)
            if not isinstance(prog, dict):
                prog = {"route": route_no, "destinations": {}}
                progs[program] = prog
            dests = prog.get("destinations")
            if isinstance(dests, str):
                dests = prog["destinations"] = [dests]
            if isinstance(dests, dict):
                entry = dests.get(dest)
                if isinstance(entry, dict):
                    key = "images" if "images" in entry else (
                        "screens" if "screens" in entry else "images")
                    lst = entry.get(key)
                    if not isinstance(lst, list):
                        lst = entry[key] = []
                elif isinstance(entry, list):
                    lst = entry
                else:
                    lst = dests[dest] = []
                existing = [s.get("image") if isinstance(s, dict) else s
                            for s in lst]
            elif isinstance(dests, list):
                if dest not in [str(d) for d in dests]:
                    dests.append(dest)
                screens = prog.get("screens")
                if not isinstance(screens, list):
                    screens = prog["screens"] = []
                lst, existing = screens, [
                    s.get("image") if isinstance(s, dict) else s
                    for s in screens]
            else:
                raise ValueError(f"program '{program}' has an odd "
                                 f"'destinations' shape -- edit it by hand")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp_png = full + ".tmp"
            with open(tmp_png, "wb") as f:
                f.write(png)
            os.replace(tmp_png, full)
            if rel not in existing:
                lst.append({"image": rel, "seconds": seconds}
                           if seconds else rel)
            if dest_id is not None or colour is not None:
                entry = dests.get(dest) if isinstance(dests, dict) \
                    else None
                if entry is not None and not isinstance(entry, dict):
                    entry = dests[dest] = {"images": lst}
                if isinstance(entry, dict):
                    if dest_id is not None:
                        try:
                            others = {i for d, i in
                                      model.destination_ids(prog, program)
                                      if d != dest}
                        except SystemExit as e:
                            raise ValueError(str(e))
                        if dest_id in others:
                            raise ValueError(
                                f"destination id {dest_id} is already "
                                f"used in program '{program}'")
                        entry["id"] = dest_id
                    if colour is not None:
                        entry["colour"] = colour
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                os.replace(tmp, self.path)
                model.load_programs_file(self.path)
            except SystemExit as e:
                with open(self.path, "w") as f:
                    f.write(original)
                raise ValueError(f"write failed validation ({e})")
            except OSError as e:
                raise ValueError(str(e))
            self._refresh_locked()
            if program != self.program_name or \
                    dest != self.dest_name:
                self.program_name, self.dest_name = program, dest
                self.field, self.buffer = "line", ""
                self.hi = self._dest_index()
                self._save_control()
                self.message = f"Showing {route_no} {dest}"
            else:
                self.message = f"Added {base}"
            return rel


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICU 602</title>
<style>
*{box-sizing:border-box}
::selection{background:#ff8c1a;color:#000}
body{background:radial-gradient(1100px 480px at 50% -40px,#1c1c1e,#060607 65%);
margin:0;padding:30px 14px 46px;font-family:Arial,Helvetica,sans-serif;color:#eee;
display:flex;flex-direction:column;align-items:center;gap:14px;min-height:100vh}
body::-webkit-scrollbar{width:10px}
body::-webkit-scrollbar-thumb{background:#333;border-radius:5px}
/* ---- unit ---- */
.unit{width:100%;max-width:98vw;border-radius:44px;padding:13px;
background:linear-gradient(#3a3f45,#0b0c0e 40%,#000);
box-shadow:0 34px 70px rgba(0,0,0,.85),0 6px 16px rgba(0,0,0,.9)}
.face{position:relative;border-radius:32px;padding:26px 30px 26px;
background:linear-gradient(#27325c 0%,#1d2547 14%,#151b3a 62%,#10152c 100%);
box-shadow:inset 0 2px 2px rgba(255,255,255,.25),inset 0 -4px 10px rgba(0,0,0,.65)}
.main{display:grid;grid-template-columns:66px 1fr 216px;gap:20px}
/* left rail */
.rail{border-right:2px solid rgba(0,0,0,.6);padding:8px 12px 8px 0;
display:flex;flex-direction:column;align-items:center;gap:26px;color:#97a0b8}
.rail .who{font-size:14px;text-align:center;line-height:1.3}
.rail .who b{display:block;font-size:16px;color:#c6cede;letter-spacing:.5px}
/* LCD */
.lcdwrap{background:#070a12;border-radius:10px;padding:11px;
box-shadow:inset 0 4px 12px rgba(0,0,0,.9),0 1px 0 rgba(255,255,255,.09)}
.lcd{background:linear-gradient(#d3e6f3,#b9d1e2 72%,#adc4d7);border-radius:3px;
color:#20344d;padding:14px 18px 10px;min-height:158px;
box-shadow:inset 0 0 26px rgba(70,110,140,.4);
display:grid;grid-template-columns:1fr 148px;gap:10px}
.big{font-family:"Courier New",Courier,monospace;font-weight:bold;font-size:40px;
letter-spacing:1px;white-space:nowrap;overflow:hidden;
text-shadow:0 1px 0 rgba(255,255,255,.35)}
.big span{border:2px solid #22374e;padding:1px 12px;display:inline-block}
.big img{display:none;max-width:100%;height:66px;image-rendering:pixelated;
border:2px solid #22374e;padding:2px;background:#000}
.meta{font-family:"Courier New",Courier,monospace;font-size:18px;line-height:1.6}
.soft{display:flex;justify-content:space-between;margin-top:10px;grid-column:1/-1}
.soft button{background:#1d3a4c;color:#fff;border:0;border-radius:3px;
padding:6px 20px;font-size:15px;cursor:pointer;
box-shadow:0 2px 0 rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.15)}
.soft button:active{transform:translateY(1px)}
.msg{min-height:22px;font-size:14px;color:#ffd27f;margin-top:8px}
.msg:empty:before{content:" "}
/* function keys */
.fnrow{display:flex;gap:15px;margin:16px 0 0;align-items:center}
.fn{background:linear-gradient(#1c1c22,#070708);color:#ff8c1a;
border:1px solid #33333c;border-top-color:#55555f;border-radius:9px;width:66px;
padding:9px 0;font-size:17px;font-weight:bold;cursor:pointer;
box-shadow:0 3px 0 #000,inset 0 1px 0 rgba(255,255,255,.14)}
.fn:hover{filter:brightness(1.25)}
.fn:active{transform:translateY(2px);box-shadow:0 1px 0 #000}
.fn.active{outline:2px solid #ffd27f;outline-offset:1px}
.fn:focus-visible{outline:2px solid #ffd27f;outline-offset:2px}
.dot{width:22px;height:22px;border-radius:50%;margin-left:auto;
background:radial-gradient(circle at 35% 30%,#3d3d49,#0e0e12 70%);
box-shadow:inset 0 2px 4px #000,0 1px 0 rgba(255,255,255,.1)}
/* keypad + nav */
.side{display:flex;flex-direction:column;gap:16px}
.keys{display:grid;grid-template-columns:repeat(3,62px);gap:10px;justify-content:start}
.keys button{background:linear-gradient(#1b1b21,#0a0a0d);color:#fff;
border:1px solid #34343e;border-top-color:#55555f;border-radius:10px;
padding:9px 0 5px;font-size:21px;font-weight:bold;cursor:pointer;
box-shadow:0 3px 0 #000,inset 0 1px 0 rgba(255,255,255,.12);line-height:1.05}
.keys button small{display:block;color:#9a9aa2;font-size:10px;font-weight:normal}
.keys button:hover{filter:brightness(1.3)}
.keys button:active{transform:translateY(2px);box-shadow:0 1px 0 #000}
.keys button:focus-visible{outline:2px solid #ffd27f;outline-offset:2px}
.keys .blank{background:none;border:0;box-shadow:none;cursor:default}
.nav{display:grid;grid-template-columns:repeat(3,56px);gap:9px;align-content:start}
.nav button{background:linear-gradient(#1b1b21,#0a0a0d);color:#fff;
border:1px solid #34343e;border-top-color:#55555f;border-radius:50%;
width:56px;height:56px;font-size:19px;cursor:pointer;
box-shadow:0 3px 0 #000,inset 0 1px 0 rgba(255,255,255,.12)}
.nav button:hover{filter:brightness(1.3)}
.nav button:active{transform:translateY(2px);box-shadow:0 1px 0 #000}
.nav button:focus-visible{outline:2px solid #ffd27f;outline-offset:2px}
.nav button.ok{color:#37e05a;border-radius:12px}
.nav button.no{color:#ff4444;border-radius:12px}
.nav .blank{background:none;border:0;box-shadow:none;cursor:default}
/* below the unit: functional extras, kept quiet */
.dests{display:flex;flex-wrap:wrap;gap:8px;max-width:100%;justify-content:center}
.dests span{background:#26262c;border:1px solid #3a3a42;border-radius:6px;
padding:6px 11px;font-size:14px;color:#cfcfd6;cursor:pointer}
.dests span.cur{background:#1d5c2e;border-color:#1d5c2e;color:#fff}
.dests span.hi{outline:2px solid #ffd27f}
.dests img{width:132px;height:22px;object-fit:contain;background:#000;
border-radius:4px;border:1px solid #3a3a42;cursor:pointer}
.dests img.cur{outline:2px solid #37e05a}
.dests img.hi{outline:2px solid #ffd27f}
.hint{color:#777;font-size:12px;text-align:center}
.stripwrap{max-width:100%;text-align:center}
.strip{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:6px}
.strip img{width:180px;height:30px;object-fit:contain;background:#000;
border-radius:4px;border:1px solid #3a3a42}
.strip span{background:#26262c;border:1px solid #3a3a42;border-radius:6px;
padding:6px 11px;font-size:12px;color:#cfcfd6}
.up{max-width:640px;background:#14151a;border:1px solid #33333c;
border-radius:10px;padding:10px 12px;font-size:13px;color:#cfcfd6}
.uprow{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.up input,.up button{background:#222;color:#fff;border:1px solid #555;
border-radius:6px;padding:6px 8px;font-size:13px}
.up button{background:#1d5c2e;border-color:#1d5c2e;cursor:pointer}
.tabs{display:flex;gap:8px}
.tabs button{background:#1b1b21;color:#9a9aa2;border:1px solid #34343e;
border-radius:8px;padding:8px 22px;font-size:15px;cursor:pointer}
.tabs button.on{background:#1d3a4c;color:#fff;border-color:#1d3a4c}
#pane1,#pane2{display:flex;flex-direction:column;align-items:center;gap:14px;
width:100%}
#pane2{display:none;max-width:640px}
.new{width:100%;background:#14151a;border:1px solid #33333c;
border-radius:10px;padding:12px;font-size:13px;color:#cfcfd6}
.new h3{margin:0 0 8px;font-size:14px;color:#fff}
.new .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.new label{display:flex;flex-direction:column;gap:3px;font-size:12px}
.new input,.new select,.new button{background:#222;color:#fff;
border:1px solid #555;border-radius:6px;padding:6px 8px;font-size:13px}
.new .row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.new button{background:#1d5c2e;border-color:#1d5c2e;cursor:pointer}
.new button.ghost{background:#1d3a4c;border-color:#1d3a4c}
#tpreview{width:100%;max-width:480px;height:80px;object-fit:contain;
background:#000;border-radius:6px;border:1px solid #3a3a42;display:none;
image-rendering:pixelated;margin-top:8px}
table.dtab{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
table.dtab th,table.dtab td{border-bottom:1px solid #333;padding:6px 8px;
text-align:left}
table.dtab button{background:#1d3a4c;border:1px solid #1d3a4c;color:#fff;
border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px}
button{font-family:inherit}
@media (max-width:720px){
.main{grid-template-columns:52px 1fr}
.side{grid-column:1/-1;flex-direction:row;flex-wrap:wrap}
.big{font-size:28px}
.fn{width:54px;font-size:15px}
}
</style></head><body>
<div class="tabs"><button id="tabbtn1" class="on" onclick="showtab(0)">Controller</button><button id="tabbtn2" onclick="showtab(1)">Destinations</button></div>
<div id="pane1">
<div class="unit"><div class="face"><div class="main">
<div class="rail">
<div class="who">mobitec<b>ICU 602</b></div>
<svg width="30" height="60" viewBox="0 0 30 60" fill="none" stroke="#7d86a0" stroke-width="2.6" stroke-linecap="round" aria-hidden="true">
<path d="M15 6v40"/><path d="M9 12l6-6 6 6"/>
<path d="M15 30L6 40"/><path d="M15 30l9 10"/>
<circle cx="6" cy="43" r="3.4" fill="#7d86a0" stroke="none"/>
<rect x="21" y="40" width="7" height="7" fill="#7d86a0" stroke="none"/>
</svg>
</div>
<div class="mid">
<div class="lcdwrap"><div class="lcd">
<div><div class="big"><span id="big">---</span><img id="bigimg" alt=""></div>
<div class="soft"><button onclick="press('dest')">Dest</button>
<button onclick="press('clearall')">Clear all</button></div></div>
<div class="meta"><div id="line">Line: -</div><div id="dest">Dest: -</div>
<div>Extr:</div></div>
</div></div>
<div class="msg" id="msg"></div>
<div class="fnrow">
<button class="fn home" onclick="press('home')">&#8962;</button>
<button class="fn" id="f1" onclick="press('F1')">F1</button>
<button class="fn" id="f2" onclick="press('F2')">F2</button>
<button class="fn" onclick="press('F3')">F3</button>
<button class="fn" onclick="press('F4')">F4</button>
<button class="fn" id="f5" onclick="press('F5')">F5</button>
<span class="dot"></span>
</div>
</div>
<div class="side">
<div class="keys">
<button onclick="press('1')">1</button>
<button onclick="press('2')">2<small>ABC</small></button>
<button onclick="press('3')">3<small>DEF</small></button>
<button onclick="press('4')">4<small>GHI</small></button>
<button onclick="press('5')">5<small>JKL</small></button>
<button onclick="press('6')">6<small>MNO</small></button>
<button onclick="press('7')">7<small>PQRS</small></button>
<button onclick="press('8')">8<small>TUV</small></button>
<button onclick="press('9')">9<small>WXYZ</small></button>
<span class="blank"></span>
<button onclick="press('0')">0<small>_</small></button>
<span class="blank"></span>
</div>
<div class="nav">
<span class="blank"></span><button onclick="press('up')">&#8593;</button><button class="no" onclick="press('clear')">X</button>
<button onclick="press('left')">&#8592;</button><span class="blank"></span><button onclick="press('right')">&#8594;</button>
<span class="blank"></span><button onclick="press('down')">&#8595;</button><button class="ok" onclick="press('ok')">&#1003;</button>
</div>
</div>
</div></div></div>
<div class="dests" id="dests"></div>
<div class="stripwrap"><div class="hint" id="striphead">screens</div>
<div class="strip" id="strip"></div></div>
<div class="up"><b>Upload a Sign Studio PNG</b> — saved into
bitmap/destinations/…, appended to the program file, and shown on
screen. New programs/destinations are created as needed.
<div class="uprow"><input type="file" id="upfile" accept=".png,image/png">
<input id="upprog" placeholder="program e.g. 43" size="8">
<input id="updest" placeholder="destination e.g. Sheffield" size="12">
<input id="upsecs" placeholder="secs" size="4">
<button onclick="upload()">Upload + show</button></div>
<div class="hint" id="upmsg"></div></div>
</div>
<div id="pane2">
<div class="new"><h3>Destinations in <span id="newfile">…</span></h3>
<div class="hint" id="newhead">program</div>
<table class="dtab"><thead><tr><th>id</th><th>destination</th>
<th>screens</th><th></th></tr></thead><tbody id="dtab"></tbody></table></div>
<div class="new"><h3>Create destination from text</h3>
<div class="grid">
<label>program (route group)<input id="nprog" list="progs" placeholder="e.g. 43"></label>
<label>route number<input id="nroute" placeholder="e.g. 43"></label>
<label>destination<input id="ndest" placeholder="e.g. Sheffield"></label>
<label>via (optional)<input id="nvia" placeholder="e.g. Dronfield"></label>
<label>layout<select id="nstyle"><option value="top">top via - via over dest</option><option value="bottom">bottom via - dest over via</option><option value="left">left via - via | dest</option><option value="right">right via - dest | via</option></select></label>
<label>colour<input id="ncolour" value="#ff8000"></label>
<label>destination id (optional)<input id="nid" placeholder="auto"></label>
<label>dwell secs (optional)<input id="nsecs" placeholder="file default"></label>
</div>
<datalist id="progs"></datalist>
<div class="row"><button class="ghost" onclick="previewCreate()">Preview</button><button onclick="createText()">Create + show</button></div>
<img id="tpreview" alt="preview">
<div class="hint" id="nmsg"></div></div>
</div>
<div class="hint">F1 route &middot; F2 destination (arrows or id) &middot;
F5 all destinations on arrows &middot;
keypad takes route+dest codes, e.g. 40101 &middot; keyboard: 0-9,
arrows, Enter, Backspace</div>
<script>
var lastChips='';
function showtab(i){
document.getElementById('pane1').style.display=i?'none':'flex';
document.getElementById('pane2').style.display=i?'flex':'none';
document.getElementById('tabbtn1').className=i?'':'on';
document.getElementById('tabbtn2').className=i?'on':'';
}
function update(s){
var bt=document.getElementById('big'),bi=document.getElementById('bigimg');
var src=null;
if(!s.buffer){
if(s.field=='dest'&&s.destinations[s.hi]&&s.destinations[s.hi].img)
src=s.destinations[s.hi].img;
else if(s.field!='dest'&&s.bigimg)src=s.bigimg;
}
if(src){
bt.style.display='none';bi.style.display='inline';
if(bi.getAttribute('data-p')!=src){
bi.setAttribute('data-p',src);
bi.src='/api/img?path='+encodeURIComponent(src);}
}else{
bi.style.display='none';bi.removeAttribute('data-p');
bt.style.display='inline';bt.textContent=s.big;
}
document.getElementById('line').textContent='Line: '+(s.route||'-');
document.getElementById('dest').textContent='Dest: '+
(s.dest_id===null||s.dest_id===undefined?'-':s.dest_id);
document.getElementById('msg').textContent=s.message||'';
document.getElementById('f1').className='fn'+(s.field=='line'?' active':'');
document.getElementById('f2').className='fn'+(s.field=='dest'?' active':'');
document.getElementById('f5').className='fn'+(s.field=='all'?' active':'');
var list=s.field=='all'?s.all:s.destinations;
var key=s.field+'|'+s.program+'|'+(s.dest||'')+'|'+
(s.field=='all'?s.hi_all:s.hi)+'|'+list.map(function(d){
return d.name+':'+d.id+':'+(d.img||'');}).join(',');
if(key==lastChips)return;
lastChips=key;
var box=document.getElementById('dests');box.innerHTML='';
list.forEach(function(d,i){
var isCur=s.field=='all'?(d.program==s.program&&d.name==s.dest):
(d.name==s.dest);
var isHi=!isCur&&(s.field=='all'?(i==s.hi_all):
(s.field=='dest'&&i==s.hi));
var cls=isCur?'cur':(isHi?'hi':'');
var go=(function(dd,ii){return function(){
if(s.field=='all')press('pickall:'+ii);else press('pick:'+dd.name);};})(d,i);
var el;
if(d.img){
el=document.createElement('img');
el.src='/api/img?path='+encodeURIComponent(d.img);
el.title=(s.field=='all'?d.route+' ':'')+d.name;
el.onclick=go;
}else{
el=document.createElement('span');el.textContent=d.id+' '+d.name;
el.onclick=go;
}
if(cls)el.className=cls;
box.appendChild(el);});
var sh=document.getElementById('striphead');
sh.textContent='screens — '+s.program+' / '+(s.dest||'all')+
' ('+s.screens.length+') — showing on the matrix';
var st=document.getElementById('strip');st.innerHTML='';
s.screens.forEach(function(sc){
var el;
if(sc.img){
el=document.createElement('img');
el.src='/api/img?path='+encodeURIComponent(sc.img);
el.title=(sc.dest||'')+' '+(sc.seconds||'');
}else{
el=document.createElement('span');
el.textContent=(sc.dest||'')+' '+(sc.img||'');
}
st.appendChild(el);});
var up=document.getElementById('upprog');
if(up&&!up.value)up.value=s.program||'';
var ud=document.getElementById('updest');
if(ud&&!ud.value)ud.value=s.dest||'';
document.getElementById('newfile').textContent=s.program_file||'';
document.getElementById('newhead').textContent='program '+s.program+
' — '+s.destinations.length+' destination(s), click Show to put one on screen';
var dl=document.getElementById('progs');
var rkey=s.routes.map(function(r){return r.program;}).join(',');
if(dl.getAttribute('data-k')!=rkey){
dl.setAttribute('data-k',rkey);dl.innerHTML='';
s.routes.forEach(function(r){
var o=document.createElement('option');o.value=r.program;
o.textContent=r.program+' (route '+r.route+')';dl.appendChild(o);});}
var tb=document.getElementById('dtab');tb.innerHTML='';
s.destinations.forEach(function(d){
var tr=document.createElement('tr');
var cur=d.name===s.dest?' style="color:#37e05a"':'';
tr.innerHTML='<td>'+d.id+'</td><td'+cur+'>'+d.name+'</td><td>'+
(d.n===undefined?'-':d.n)+'</td><td></td>';
var b=document.createElement('button');b.textContent='Show';
b.onclick=(function(n){return function(){press('pick:'+n);};})(d.name);
tr.lastChild.appendChild(b);tb.appendChild(tr);});
}
function formValues(){
var secs=parseFloat(document.getElementById('nsecs').value);
return {program:document.getElementById('nprog').value,
route:document.getElementById('nroute').value,
destination:document.getElementById('ndest').value,
via:document.getElementById('nvia').value,
style:document.getElementById('nstyle').value,
colour:document.getElementById('ncolour').value||'#ff8000',
id:document.getElementById('nid').value,
seconds:isNaN(secs)?null:secs};
}
async function previewCreate(){
var m=document.getElementById('nmsg');
m.textContent='rendering…';
var r=await fetch('/api/render-preview',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify(formValues())});
var j=await r.json();
if(!j.ok){m.textContent=j.error||'preview failed';return;}
var im=document.getElementById('tpreview');
im.src=j.data;im.style.display='block';
m.textContent=j.lit+' lit pixels'+(j.warnings.length?' — '+j.warnings.join('; '):'');
}
async function createText(){
var m=document.getElementById('nmsg');
m.textContent='creating…';
var r=await fetch('/api/create-text',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify(formValues())});
var j=await r.json();
if(!j.ok){m.textContent=j.error||'create failed';return;}
m.textContent='saved '+j.path+' — on screen now'+(j.warnings.length?' ('+j.warnings.join('; ')+')':'');
update(j.state);
}
async function press(k){
var r=await fetch('/api/key',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});
update(await r.json());
}
async function upload(){
var f=document.getElementById('upfile').files[0];
var m=document.getElementById('upmsg');
if(!f){m.textContent='pick a PNG file first';return;}
m.textContent='uploading…';
var rd=new FileReader();
rd.onload=function(){
var secs=parseFloat(document.getElementById('upsecs').value);
fetch('/api/upload',{method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({program:document.getElementById('upprog').value,
destination:document.getElementById('updest').value,
filename:f.name,data:rd.result,
seconds:isNaN(secs)?null:secs})}).then(function(r){return r.json();})
.then(function(j){
if(j.ok){m.textContent='saved '+j.path+' — on screen now';update(j.state);}
else m.textContent=j.error||'upload failed';})
.catch(function(){m.textContent='server unreachable';});
};
rd.readAsDataURL(f);
}
async function poll(){
try{var r=await fetch('/api/state');update(await r.json());}catch(e){}
}
document.addEventListener('keydown',function(e){
if(e.key>='0'&&e.key<='9')press(e.key);
else if(e.key=='Enter')press('ok');
else if(e.key=='Backspace'||e.key=='Escape')press('clear');
else if(e.key=='ArrowUp')press('up');
else if(e.key=='ArrowDown')press('down');
else if(e.key=='ArrowLeft')press('left');
else if(e.key=='ArrowRight')press('right');
});
setInterval(poll,500);poll();
</script></body></html>
"""


def serve(ctl, port):
    outer = ctl

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html")
            elif self.path == "/api/state":
                self._send(json.dumps(
                    outer.snapshot()).encode(), "application/json")
            elif self.path.startswith("/api/img?"):
                q = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query)
                p = (q.get("path") or [""])[0]
                if p in outer.known_images():
                    try:
                        with open(p, "rb") as f:
                            self._send(f.read(), "image/png")
                            return
                    except OSError:
                        pass
                self._send(b"not found", "text/plain", 404)
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            if self.path == "/api/upload":
                return self._upload()
            if self.path == "/api/render-preview":
                return self._render_preview()
            if self.path == "/api/create-text":
                return self._create_text()
            if self.path != "/api/key":
                self._send(b"not found", "text/plain", 404)
                return
            try:
                ln = int(self.headers.get("Content-Length", 0))
                key = json.loads(self.rfile.read(ln) or b"{}").get(
                    "key", "")
            except Exception:
                key = ""
            self._send(json.dumps(outer.press(str(key))).encode(),
                       "application/json")

        def _upload(self):
            try:
                ln = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                ln = 0
            if ln <= 0 or ln > 2 * 1024 * 1024:
                self._send(json.dumps(
                    {"ok": False,
                     "error": "empty or oversized upload (max 2MB)"}
                ).encode(), "application/json", 400)
                return
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                self._send(json.dumps(
                    {"ok": False, "error": "bad JSON"}).encode(),
                    "application/json", 400)
                return
            data = str(body.get("data", "") or "")
            if "," in data and data.startswith("data:"):
                data = data.split(",", 1)[1]
            try:
                png = base64.b64decode(data, validate=True)
            except Exception:
                self._send(json.dumps(
                    {"ok": False,
                     "error": "bad image data (need PNG dataURL)"}
                ).encode(), "application/json", 400)
                return
            try:
                secs = body.get("seconds", None)
                secs = float(secs) if secs not in (None, "") else None
                if secs is not None and secs <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._send(json.dumps(
                    {"ok": False,
                     "error": "seconds must be positive"}).encode(),
                    "application/json", 400)
                return
            try:
                rel = outer.add_upload(
                    body.get("program"), body.get("destination"),
                    body.get("filename", "upload.png"), png, secs,
                    route=body.get("route", ""))
            except ValueError as e:
                self._send(json.dumps(
                    {"ok": False, "error": str(e)}).encode(),
                    "application/json", 400)
                return
            self._send(json.dumps(
                {"ok": True, "path": rel,
                 "state": outer.snapshot()}).encode(),
                "application/json")

        def _json_body(self, max_bytes=65536):
            try:
                ln = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                ln = 0
            if ln <= 0 or ln > max_bytes:
                return None
            try:
                return json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return None

        def _render_preview(self):
            body = self._json_body()
            if body is None:
                self._send(json.dumps(
                    {"ok": False, "error": "bad JSON"}).encode(),
                    "application/json", 400)
                return
            try:
                colour = canonical_colour(
                    body.get("colour", "#ff8000") or "#ff8000")
                if colour in ("full", "RGB"):
                    raise ValueError("text blinds need a single "
                                     "colour, e.g. #ff8000")
                png, info = render_text_png(
                    body.get("route", ""), body.get("destination", ""),
                    body.get("via", ""), body.get("style", "top"),
                    colour)
            except ValueError as e:
                self._send(json.dumps(
                    {"ok": False, "error": str(e)}).encode(),
                    "application/json", 400)
                return
            self._send(json.dumps(
                {"ok": True,
                 "data": "data:image/png;base64," + base64.b64encode(
                     png).decode(),
                 "lit": info.get("lit", 0),
                 "warnings": info.get("warnings", [])}).encode(),
                "application/json")

        def _create_text(self):
            body = self._json_body()
            if body is None:
                self._send(json.dumps(
                    {"ok": False, "error": "bad JSON"}).encode(),
                    "application/json", 400)
                return
            try:
                secs = parse_seconds(body.get("seconds", None))
                ident = parse_dest_id(body.get("id", None))
                colour = canonical_colour(
                    body.get("colour", "#ff8000") or "#ff8000")
                rel, warnings = outer.create_text(
                    body.get("program"), body.get("destination"),
                    route=body.get("route", ""),
                    via=body.get("via", ""),
                    style=body.get("style", "top"),
                    colour=colour, seconds=secs, dest_id=ident)
            except ValueError as e:
                self._send(json.dumps(
                    {"ok": False, "error": str(e)}).encode(),
                    "application/json", 400)
                return
            self._send(json.dumps(
                {"ok": True, "path": rel, "warnings": warnings,
                 "state": outer.snapshot()}).encode(),
                "application/json")

    print(f"portal on :{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
