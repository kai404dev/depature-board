#!/usr/bin/env python3
"""Sign Studio -- Qt LED destination editor with pixel touch-up.

A PySide6 rewrite of the sign-designer workflow: message list (route +
destination + via), bitmap or system fonts, the four via layouts,
colour dots, and a zoomable canvas with an LED-dot simulation plus
pixel-grid / field-box overlays. Output is the same crisp 240x40 PNG
(bitmap pixels only, no antialiasing) for programs/*.json.

The pixel tool (paint / erase, 1-3px brush) lets you hand-fix glyphs:
touch-ups sit on top of the rendered text, travel with the message,
and are baked into the saved PNG.

  python3 sign-studio.py
  QT_QPA_PLATFORM=offscreen python3 sign-studio.py --smoke  # self-test

Needs: pip install PySide6   (Pillow too, for system fonts)
Messages live in sign-messages.json, shared with sign-designer.py.
"""

import argparse
import json
import os
import re
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)
MSG_FILE = os.path.join(THIS_DIR, "sign-messages.json")

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QImage, QKeySequence, QPainter,
                           QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QCompleter, QDialog,
                               QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMainWindow, QMessageBox, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox,
                               QSplitter, QStatusBar, QVBoxLayout, QWidget)

import sysfonts
import program as programs_model
import engine as ENG
W, H = ENG.W, ENG.H
STYLES = ("top", "bottom", "left", "right")
STYLE_TAG = {
    "top": "top via -- via over dest",
    "bottom": "bottom via -- dest over via",
    "left": "left via -- via | dest side by side",
    "right": "right via -- dest | via side by side",
}
COLOURS = [
    ("white", "#ffffff"),
    ("amber", "#ff8c00"),
    ("red", "#ff2828"),
    ("green", "#28ff5a"),
    ("blue", "#3c8cff"),
]
CELL_COLOURS = {"route": "#ffe14d", "dest": "#4dd2ff",
                "via": "#ff4dd2"}
SEED = [
    {"name": "43 Sheffield", "route": "43", "destination": "Sheffield",
     "via": "Dronfield, Chesterfield", "style": "top",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
    {"name": "X12 Burton", "route": "X12", "destination": "Burton",
     "via": "Lichfield", "style": "bottom",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
    {"name": "Not In Service", "route": "", "destination": "Not In Service",
     "via": "", "style": "top",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
]


def load_messages():
    try:
        with open(MSG_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [m for m in data if isinstance(m, dict)]
    except (OSError, ValueError):
        pass
    return [dict(m) for m in SEED]


def save_messages(msgs):
    tmp = MSG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(msgs, f, indent=2)
        f.write("\n")
    os.replace(tmp, MSG_FILE)


def touch_of(msg):
    """(add_set, del_set) of (x, y) touch-ups stored on a message."""
    t = msg.get("touch") if isinstance(msg.get("touch"), dict) else {}
    try:
        add = {(int(x), int(y)) for x, y in t.get("add", [])}
        dele = {(int(x), int(y)) for x, y in t.get("del", [])}
    except (TypeError, ValueError):
        add, dele = set(), set()
    return ({p for p in add if 0 <= p[0] < W and 0 <= p[1] < H},
            {p for p in dele if 0 <= p[0] < W and 0 <= p[1] < H})


def touch_of(msg):
    """(add_set, del_set) of (x, y) touch-ups stored on a message."""
    t = msg.get("touch") if isinstance(msg.get("touch"), dict) else {}
    try:
        add = {(int(x), int(y)) for x, y in t.get("add", [])}
        dele = {(int(x), int(y)) for x, y in t.get("del", [])}
    except (TypeError, ValueError):
        add, dele = set(), set()
    return ({p for p in add if 0 <= p[0] < W and 0 <= p[1] < H},
            {p for p in dele if 0 <= p[0] < W and 0 <= p[1] < H})


def slug(s, fallback="untitled"):
    return (re.sub(r"[^a-z0-9]+", "-",
                   str(s or "").strip().lower()).strip("-")
            or fallback)


def page_paths(program, name_route, name_dest, n):
    """Repo-relative PNG paths for n pages, 1-based and stable:

      bitmap/destinations/<program>/<route>/<route>-<dest>-<page>.png

    e.g. bitmap/destinations/401/401/401-burton-1.png
    """
    p = slug(program, "custom")
    r = slug(name_route, "noroute")
    d = slug(name_dest, "untitled")
    base = f"{r}-{d}" if r != d else r
    return [os.path.join("bitmap", "destinations", p, r,
                          f"{base}-{i}.png").replace(os.sep, "/")
            for i in range(1, n + 1)]


def page_job(page, fg_hex):
    """Engine job dict from a page snapshot (fields + rules)."""
    dest = page.get("destination", page.get("dest", ""))
    return {
        "route": page.get("route", ""), "dest": dest,
        "via": page.get("via", ""), "style": page.get("style", "top"),
        "route_font": page.get("route_font", "10x20.bdf"),
        "dest_font": page.get("dest_font", "10x20.bdf"),
        "via_font": page.get("via_font", "6x13B.bdf"),
        "route_scale": page.get("route_scale", 2),
        "dest_scale": page.get("dest_scale", 1),
        "via_scale": page.get("via_scale", 1),
        "fg": fg_hex,
        "upper_dest": page.get("upper_dest", True),
        "via_prefix": page.get("via_prefix", True),
    }


def page_spec(page):
    """(path, index, px) per field for a page snapshot (system fonts)."""
    sysm = page.get("sys") if isinstance(page.get("sys"), dict) else {}
    out = {}
    for name, default_px in (("route", 34), ("dest", 20), ("via", 12)):
        s = sysm.get(name, {}) if isinstance(sysm.get(name), dict) \
            else {}
        fam = str(s.get("family", "") or "")
        try:
            px = max(6, min(120, int(s.get("px", default_px))))
        except (TypeError, ValueError):
            px = default_px
        bold = bool(s.get("bold", False))
        out[name] = (*sysfonts.resolve_face(fam, bold), px)
    return out


def page_is_empty(page):
    for k in ("route", "destination", "dest", "via"):
        if str(page.get(k, "") or "").strip():
            return False
    t = page.get("touch")
    return not (isinstance(t, dict) and (t.get("add") or t.get("del")))


def _touch_lists(t):
    """Touch-ups as sorted [[x, y]] lists (JSON-safe)."""
    add, dele = touch_of({"touch": t})
    return {"add": sorted(add), "del": sorted(dele)}


def normalize_page(p):
    """Fill defaults so every page has the full editor state."""
    sysm = p.get("sys") if isinstance(p.get("sys"), dict) else {}
    sysn = {}
    for name, default_px in (("route", 34), ("dest", 20), ("via", 12)):
        s = sysm.get(name, {}) if isinstance(sysm.get(name), dict) \
            else {}
        try:
            px = max(6, min(120, int(s.get("px", default_px))))
        except (TypeError, ValueError):
            px = default_px
        sysn[name] = {"family": str(s.get("family", "") or ""),
                      "px": px, "bold": bool(s.get("bold", False))}
    try:
        secs = float(p.get("seconds", 0) or 0)
    except (TypeError, ValueError):
        secs = 0
    return {
        "route": str(p.get("route", "") or ""),
        "destination": str(p.get("destination", p.get("dest", ""))
                           or ""),
        "via": str(p.get("via", "") or ""),
        "style": p.get("style", "top") if p.get("style") in STYLES
        else "top",
        "fsrc": p.get("fsrc", "bdf"),
        "route_font": str(p.get("route_font", "10x20.bdf") or ""),
        "dest_font": str(p.get("dest_font", "10x20.bdf") or ""),
        "via_font": str(p.get("via_font", "6x13B.bdf") or ""),
        "route_scale": p.get("route_scale", 2),
        "dest_scale": p.get("dest_scale", 1),
        "via_scale": p.get("via_scale", 1),
        "upper_dest": p.get("upper_dest", False),
        "via_prefix": p.get("via_prefix", False),
        "sys": sysn,
        "touch": _touch_lists(p.get("touch")),
        "seconds": secs if secs > 0 else 0,
    }


def page_from_flat(m):
    """Legacy flat message (tk format) -> single-page snapshot."""
    p = {"route": m.get("route", ""),
         "destination": m.get("destination", m.get("dest", "")),
         "via": m.get("via", ""), "style": m.get("style", "top"),
         "fsrc": m.get("fsrc", "bdf"),
         "route_font": m.get("route_font", "10x20.bdf"),
         "dest_font": m.get("dest_font", "10x20.bdf"),
         "via_font": m.get("via_font", "6x13B.bdf"),
         "route_scale": m.get("route_scale", 2),
         "dest_scale": m.get("dest_scale", 1),
         "via_scale": m.get("via_scale", 1),
         "sys": m.get("sys") if isinstance(m.get("sys"), dict) else {},
         "touch": m.get("touch") if isinstance(m.get("touch"), dict)
         else {"add": [], "del": []}}
    return normalize_page(p)


def pages_from_message(m):
    pages = m.get("pages")
    if isinstance(pages, list) and pages and all(
            isinstance(p, dict) for p in pages):
        return [normalize_page(dict(p)) for p in pages]
    return [page_from_flat(m)]


def program_files():
    """Repo-relative programs/*.json paths, sorted."""
    base = os.path.join(THIS_DIR, "programs")
    try:
        names = sorted(f for f in os.listdir(base)
                       if f.lower().endswith(".json"))
    except OSError:
        return []
    return [os.path.join("programs", f) for f in names]


def program_index(path):
    """{program: {'route': r, 'destinations': [names]}} for a file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    progs = data.get("programs") if isinstance(data, dict) else None
    if not isinstance(progs, dict):
        return {}
    for name, raw in progs.items():
        if not isinstance(raw, dict):
            continue
        dests = raw.get("destinations", [])
        if isinstance(dests, dict):
            names = list(dests)
        elif isinstance(dests, str):
            names = [dests]
        elif isinstance(dests, list):
            names = [str(d) for d in dests if str(d).strip()]
        else:
            names = []
        out[str(name)] = {"route": str(raw.get("route", "") or ""),
                          "destinations": names}
    return out


def _screen_image(entry):
    return entry.get("image") if isinstance(entry, dict) else entry


def send_pages_to_program(pages, fg_hex, invert, name_route, name_dest,
                          routing):
    """Render pages and append them to a programs file.

    pages: page snapshots (fields + touch + seconds). routing:
    {"file": programs/*.json (repo-relative or absolute), "program":
    name, "destination": name, "route": route for a new program}.
    Blank pages are skipped; already-listed images are not duplicated.
    Returns a summary dict. Raises ValueError with a plain message;
    the JSON file is only replaced once the result validates (a
    backup is restored otherwise).
    """
    if not pages:
        raise ValueError("no pages to send -- add a page first")
    rel = str(routing.get("file", "") or "")
    full = rel if os.path.isabs(rel) else os.path.normpath(
        os.path.join(THIS_DIR, rel))
    if not full.lower().endswith(".json"):
        raise ValueError("program file must be a *.json file")
    if os.path.isfile(full):
        try:
            with open(full) as f:
                original = f.read()
            data = json.loads(original)
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read {rel or full}: {e}")
        if not isinstance(data, dict):
            raise ValueError(f"{rel or full}: top level must be an "
                             f"object")
        try:
            programs_model.load_programs_file(full)
        except SystemExit as e:
            raise ValueError(f"{rel or full} is already broken: {e}")
    else:
        # brand-new program file in house style (midlandclassic
        # defaults: amber blinds). New files must live under
        # programs/ so a typo can't spray files around the repo.
        progs_dir = os.path.join(THIS_DIR, "programs") + os.sep
        if not full.startswith(progs_dir) or os.sep in os.path.basename(
                full):
            raise ValueError("new program files must live under "
                             "programs/, e.g. programs/mine.json")
        if not os.path.isdir(os.path.dirname(full)):
            raise ValueError(f"folder does not exist: "
                             f"{os.path.dirname(full)}")
        original = None
        data = {"rotate_seconds": 5, "image_fit": "fit",
                "colour": "#ff8000", "programs": {}}
    pname = str(routing.get("program", "") or "").strip()
    dname = str(routing.get("destination", "") or "").strip()
    if not pname:
        raise ValueError("name a program (route)")
    if not dname:
        raise ValueError("name a destination")

    relpaths = page_paths(pname, name_route, name_dest, len(pages))
    fg = ENG.parse_colour(fg_hex)
    # phase 1: prepare every page (spec resolution fails here, before
    # any PNG is written)
    planned = []  # (page, job, spec-or-None, png_full_path, secs)
    skipped = 0
    for i, page in enumerate(pages):
        if page_is_empty(page):
            skipped += 1
            continue
        job = page_job(page, fg_hex)
        spec = None
        if str(page.get("fsrc", "bdf")) == "sys" and sysfonts.PIL_OK:
            spec = page_spec(page)  # raises ValueError without fonts
        try:
            secs = float(page.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            secs = 0
        planned.append((page, job, spec,
                        os.path.join(THIS_DIR, relpaths[i]),
                        secs if secs > 0 else None))
    if not planned:
        raise ValueError("every page is blank -- nothing sent")
    # phase 2: render + write PNGs
    rendered = []  # (repo-rel-posix-path, seconds-or-None)
    for full_png in {p[3] for p in planned}:
        os.makedirs(os.path.dirname(full_png), exist_ok=True)
    for page, job, spec, full_png, secs in planned:
        if spec is not None:
            frame, _info = sysfonts.render_system(ENG, job, spec)
        else:
            frame, _info = ENG.render(job)
        if invert:
            out = bytearray(len(frame))
            for j in range(0, len(frame), 3):
                if not (frame[j] or frame[j + 1] or frame[j + 2]):
                    out[j], out[j + 1], out[j + 2] = fg
            frame = out
        # touch-ups bake in
        t = page.get("touch") if isinstance(page.get("touch"), dict) \
            else {}
        for x, y in t.get("add", []) or []:
            try:
                o = (int(y) * W + int(x)) * 3
                frame[o], frame[o + 1], frame[o + 2] = fg
            except (TypeError, ValueError, IndexError):
                continue
        for x, y in t.get("del", []) or []:
            try:
                o = (int(y) * W + int(x)) * 3
                frame[o], frame[o + 1], frame[o + 2] = 0, 0, 0
            except (TypeError, ValueError, IndexError):
                continue
        tmp = full_png + ".tmp"
        with open(tmp, "wb") as f:
            f.write(ENG.encode_png(frame))
        os.replace(tmp, full_png)
        try:
            secs = float(page.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            secs = 0
        rendered.append((os.path.relpath(full_png, THIS_DIR).replace(
            os.sep, "/"), secs if secs > 0 else None))
    if not rendered:
        raise ValueError("every page is blank -- nothing sent")

    progs = data.get("programs")
    if not isinstance(progs, dict):
        progs = data["programs"] = {}
    prog = progs.get(pname)
    if not isinstance(prog, dict):
        prog = {"route": str(routing.get("route", "") or "").strip()
                or name_route.strip() or pname,
                "destinations": {}}
        progs[pname] = prog
    dests = prog.get("destinations")
    if isinstance(dests, str):
        dests = prog["destinations"] = [dests]
    if isinstance(dests, dict):
        entry = dests.get(dname)
        if isinstance(entry, dict):
            key = "images" if "images" in entry else (
                "screens" if "screens" in entry else "images")
            lst = entry.get(key)
            if not isinstance(lst, list):
                lst = entry[key] = []
        elif isinstance(entry, list):
            lst = entry
        else:
            lst = dests[dname] = []
        existing = [_screen_image(s) for s in lst]
        added = 0
        for relp, secs in rendered:
            if relp in existing:
                continue
            lst.append({"image": relp, "seconds": secs} if secs
                       else relp)
            added += 1
    elif isinstance(dests, list):
        if dname not in [str(d) for d in dests]:
            dests.append(dname)
        screens = prog.get("screens")
        if not isinstance(screens, list):
            screens = prog["screens"] = []
        existing = [_screen_image(s) for s in screens]
        added = 0
        for relp, secs in rendered:
            if relp in existing:
                continue
            screens.append({"image": relp, "seconds": secs} if secs
                           else relp)
            added += 1
    else:
        raise ValueError(f"program '{pname}' has an odd 'destinations' "
                         f"shape -- edit it in the program editor first")
    try:
        tmp = full + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, full)
        programs_model.load_programs_file(full)
    except SystemExit as e:
        if original is None:
            try:
                os.remove(full)  # our own half-written new file: remove
            except OSError:
                pass
        else:
            with open(full, "w") as f:
                f.write(original)
        raise ValueError(f"write failed validation ({e}) -- restored")
    except OSError as e:
        raise ValueError(str(e))
    return {"paths": [p for p, _ in rendered], "added": added,
            "skipped": skipped, "file": rel or full,
            "program": pname, "destination": dname,
            "created_file": original is None}


class SendDialog(QDialog):
    """Pick where pages land: program file > program > destination."""

    def __init__(self, parent, routing, message_route, message_dest,
                 pages, fg_hex, invert):
        super().__init__(parent)
        self.setWindowTitle("Send pages to program")
        self._pages = pages
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.c_file = QComboBox()
        self.c_file.setEditable(True)
        self.c_file.setInsertPolicy(QComboBox.NoInsert)
        files = program_files()
        self.c_file.addItems(files)
        if routing.get("file"):
            # editable: an existing file or a new programs/*.json path
            self.c_file.setCurrentText(routing["file"])
        self.c_file.currentIndexChanged.connect(self._file_changed)
        self.c_file.lineEdit().editingFinished.connect(self._file_changed)
        form.addRow("Program file", self.c_file)
        self.c_prog = QComboBox()
        self.c_prog.setEditable(True)
        self.c_prog.setInsertPolicy(QComboBox.NoInsert)
        self.c_prog.currentTextChanged.connect(self._prog_changed)
        form.addRow("Program", self.c_prog)
        self.e_route = QLineEdit()
        self.e_route.textChanged.connect(self._update_summary)
        form.addRow("Route (new program)", self.e_route)
        self.c_dest = QComboBox()
        self.c_dest.setEditable(True)
        self.c_dest.setInsertPolicy(QComboBox.NoInsert)
        self.c_dest.currentTextChanged.connect(self._update_summary)
        form.addRow("Destination", self.c_dest)
        lay.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        lay.addWidget(QLabel(
            "Saves one PNG per page into bitmap/destinations/custom/ "
            "and appends them to the destination (new programs and "
            "destinations are created as needed). Global colour and "
            "invert apply to every page; blank pages are skipped; "
            "images already listed are not duplicated."))
        btns = QDialogButtonBox(QDialogButtonBox.Ok
                                | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Send")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._index = {}
        self._file_changed()
        # prefill (after _file_changed so combos exist)
        if routing.get("program"):
            self.c_prog.setCurrentText(routing["program"])
        elif message_route:
            self.c_prog.setCurrentText(message_route)
        if routing.get("destination"):
            self.c_dest.setCurrentText(routing["destination"])
        elif message_dest:
            self.c_dest.setCurrentText(message_dest)
        if routing.get("route"):
            self.e_route.setText(routing["route"])
        elif message_route:
            self.e_route.setText(message_route)
        self._update_summary()

    def _full(self):
        rel = self.c_file.currentText().strip()
        return rel if os.path.isabs(rel) else os.path.normpath(
            os.path.join(THIS_DIR, rel))

    def _file_changed(self):
        self._index = program_index(self._full())
        cur = self.c_prog.currentText()
        self.c_prog.blockSignals(True)
        try:
            self.c_prog.clear()
            self.c_prog.addItems(sorted(self._index))
        finally:
            self.c_prog.blockSignals(False)
        if cur:
            self.c_prog.setCurrentText(cur)
        self._prog_changed()

    def _prog_changed(self):
        prog = self.c_prog.currentText().strip()
        info = self._index.get(prog, {})
        if info.get("route") and not self.e_route.text().strip():
            self.e_route.setText(info["route"])
        cur = self.c_dest.currentText()
        self.c_dest.blockSignals(True)
        try:
            self.c_dest.clear()
            self.c_dest.addItems(info.get("destinations", []))
        finally:
            self.c_dest.blockSignals(False)
        if cur:
            self.c_dest.setCurrentText(cur)
        self._update_summary()

    def _is_new_file(self):
        full = self._full()
        return bool(full) and not os.path.isfile(full)

    def _update_summary(self):
        n = len(self._pages)
        paths = page_paths(self.c_prog.currentText(),
                           self.e_route.text(),
                           self.c_dest.currentText(), n)
        secs = []
        for p in self._pages:
            try:
                s = float(p.get("seconds", 0) or 0)
            except (TypeError, ValueError):
                s = 0
            secs.append(f"{s:g}s" if s > 0 else "file default")
        new = " (new file — will be created)" if self._is_new_file() \
            else ""
        self.summary.setText(
            f"{n} page(s) → {self.c_file.currentText() or '?'} › "
            f"{self.c_prog.currentText() or '?'} › "
            f"{self.c_dest.currentText() or '?'}{new}\n"
            + ", ".join(f"{f} ({s})"
                        for f, s in zip(paths, secs)))

    def routing(self):
        return {"file": self.c_file.currentText().strip(),
                "program": self.c_prog.currentText().strip(),
                "destination": self.c_dest.currentText().strip(),
                "route": self.e_route.text().strip()}


class PixelCanvas(QLabel):
    """Zoomed 240x40 canvas: left button paints, right button erases,
    drag strokes interpolate so fast moves leave no gaps."""

    def __init__(self, studio):
        super().__init__()
        self.studio = studio
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self._last = None

    def pixel_from_pos(self, pos):
        z = self.studio.zoom()
        x, y = int(pos.x() // z), int(pos.y() // z)
        if 0 <= x < W and 0 <= y < H:
            return (x, y)
        return None

    def mousePressEvent(self, event):
        p = self.pixel_from_pos(event.position().toPoint())
        if p is None:
            return
        erase = (event.button() == Qt.RightButton or
                 self.studio.tool() == "erase")
        self._last = p
        self.studio.paint_stroke([p], erase)

    def mouseMoveEvent(self, event):
        p = self.pixel_from_pos(event.position().toPoint())
        self.studio.hover_pixel(p)
        if p is None or self._last is None:
            if p is None:
                self._last = None
            return
        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            erase = (bool(event.buttons() & Qt.RightButton) or
                     self.studio.tool() == "erase")
            self.studio.paint_stroke(self._line(self._last, p), erase)
            self._last = p

    def mouseReleaseEvent(self, _event):
        self._last = None

    def leaveEvent(self, _event):
        self._last = None
        self.studio.hover_pixel(None)

    @staticmethod
    def _line(a, b):
        """Pixel walk between two grid points (no gaps on fast drags)."""
        x0, y0, x1, y1 = a[0], a[1], b[0], b[1]
        pts, dx, dy = [], abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, x, y = dx - dy, x0, y0
        while True:
            pts.append((x, y))
            if x == x1 and y == y1:
                return pts
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy


class Studio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sign Studio -- 240x40")
        self.fonts = ENG.available_fonts() or ["10x20.bdf"]
        self.messages = load_messages()
        self.fg_hex = "#ffffff"
        self.touch_add = set()
        self.touch_del = set()
        self.base = bytearray(W * H * 3)   # engine render, no touch-ups
        self.frame = bytearray(W * H * 3)  # base + touch-ups (== saved)
        self.info = {"lit": 0, "warnings": [], "fg": "#ffffff",
                     "fields": {}}
        self._last_sig = None
        self._path_touched = False
        self.pages = []
        self.page_idx = 0
        self.program_routing = {}
        self._deb = QTimer(self)
        self._deb.setSingleShot(True)
        self._deb.timeout.connect(self.refresh)
        self._faces_ready = False
        self._build()
        self._reload_list()
        self.refresh()

    # -- layout ------------------------------------------------------
    def _build(self):
        men = self.menuBar()
        filem = men.addMenu("File")
        filem.addAction("Save PNG", QKeySequence.StandardKey.Save,
                        self.save_png)
        filem.addAction("Send to program…", self.send_to_program)
        filem.addSeparator()
        filem.addAction("Quit", QKeySequence.StandardKey.Quit, self.close)
        msgm = men.addMenu("Message")
        msgm.addAction("New from fields", self.msg_new)
        msgm.addAction("Update selected", self.msg_update)
        msgm.addAction("Delete", self.msg_delete)
        helpm = men.addMenu("Help")
        helpm.addAction("About", self.about)

        split = QSplitter()
        self.setCentralWidget(split)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 8, 8)

        text_box = self._group("Text", sl)
        tl = QVBoxLayout(text_box)
        self.e_route = self._field(tl, "Route no.", "43")
        self.e_dest = self._field(tl, "Destination", "Sheffield")
        self.e_via = self._field(tl, "Via", "Dronfield, Chesterfield")
        brow = QHBoxLayout()
        bprev, bdrop = QPushButton("Preview"), QPushButton("Drop")
        bprev.clicked.connect(self.refresh)
        bdrop.clicked.connect(self.drop)
        brow.addWidget(bprev)
        brow.addWidget(bdrop)
        brow.addStretch(1)
        brow.addWidget(QLabel("Sign 240x40"))
        tl.addLayout(brow)

        lay_box = self._group("Layout  (keys 1-4)", sl)
        ll = QVBoxLayout(lay_box)
        self.style_btns = QButtonGroup(self)
        self.style_radios = {}
        for i, s in enumerate(STYLES):
            rb = QRadioButton(STYLE_TAG[s])
            rb.setChecked(i == 0)
            rb.clicked.connect(self.refresh)
            self.style_btns.addButton(rb)
            self.style_radios[s] = rb
            ll.addWidget(rb)

        font_box = self._group("Font Selection", sl)
        fl = QVBoxLayout(font_box)
        srcrow = QHBoxLayout()
        self.rb_bdf = QRadioButton("bitmap BDF")
        self.rb_sys = QRadioButton("system")
        self.rb_bdf.setChecked(True)
        if not sysfonts.PIL_OK:
            self.rb_sys.setEnabled(False)
            self.rb_sys.setToolTip("needs Pillow: pip install pillow")
        self.rb_bdf.clicked.connect(self.refresh)
        self.rb_sys.clicked.connect(self.refresh)
        srcrow.addWidget(self.rb_bdf)
        srcrow.addWidget(self.rb_sys)
        srcrow.addStretch(1)
        self.face_count = QLabel("")
        srcrow.addWidget(self.face_count)
        fl.addLayout(srcrow)
        self.bdf_rows = QWidget()
        bl = QVBoxLayout(self.bdf_rows)
        bl.setContentsMargins(0, 0, 0, 0)
        self.c_rf, self.s_rs = self._bdf_row(bl, "route",
                                              "johnston100-40.bdf", 1)
        self.c_df, self.s_ds = self._bdf_row(bl, "dest",
                                             "johnston100-32.bdf", 1)
        self.c_vf, self.s_vs = self._bdf_row(bl, "via",
                                             "johnston100-20.bdf", 1)
        fl.addWidget(self.bdf_rows)
        self.sys_rows = QWidget()
        yl = QVBoxLayout(self.sys_rows)
        yl.setContentsMargins(0, 0, 0, 0)
        self.c_srf, self.s_srp, self.b_srb = self._sys_row(
            yl, "route", "Arial", 34, True)
        self.c_sfd, self.s_sdp, self.b_sdb = self._sys_row(
            yl, "dest", "Arial", 20, True)
        self.c_sfv, self.s_svp, self.b_svb = self._sys_row(
            yl, "via", "Arial", 12, False)
        self.sys_rows.setVisible(False)
        fl.addWidget(self.sys_rows)

        col_box = self._group("Colour", sl)
        cl = QHBoxLayout(col_box)
        self.col_btns = QButtonGroup(self)
        for i, (name, hexv) in enumerate(COLOURS):
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(30, 22)
            b.setToolTip(name)
            b.setStyleSheet(f"background-color: {hexv}; border: 1px "
                            f"solid #888; border-radius: 4px;")
            b.clicked.connect(
                lambda _c=False, h=hexv: self.set_fg(h))
            self.col_btns.addButton(b, i)
            cl.addWidget(b)
        self.col_btns.button(0).setChecked(True)
        self.e_custom = QLineEdit()
        self.e_custom.setPlaceholderText("#rrggbb")
        self.e_custom.setMaximumWidth(80)
        self.e_custom.textChanged.connect(self._custom_live)
        cl.addWidget(self.e_custom)

        opt_box = self._group("Options", sl)
        ol = QHBoxLayout(opt_box)
        self.ck_invert = QCheckBox("Invert")
        self.ck_invert.stateChanged.connect(lambda _s: self.refresh())
        ol.addWidget(self.ck_invert)

        msg_box = self._group("Messages", sl)
        ml = QVBoxLayout(msg_box)
        self.listbox = QListWidget()
        self.listbox.setMaximumHeight(90)
        self.listbox.currentRowChanged.connect(self._on_select)
        ml.addWidget(self.listbox)
        mbr = QHBoxLayout()
        for label, fn in (("New", self.msg_new), ("Update", self.msg_update),
                          ("Delete", self.msg_delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            mbr.addWidget(b)
        ml.addLayout(mbr)

        self.pages_box = self._group("Pages (0)", sl)
        pl = QVBoxLayout(self.pages_box)
        self.pagelist = QListWidget()
        self.pagelist.setMaximumHeight(70)
        self.pagelist.currentRowChanged.connect(self._select_page)
        pl.addWidget(self.pagelist)
        pbr = QHBoxLayout()
        for label, fn in (("Add", self.page_add),
                          ("Dupe", self.page_dupe),
                          ("Del", self.page_delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            pbr.addWidget(b)
        for label, fn in (("▲", lambda: self.page_move(-1)),
                          ("▼", lambda: self.page_move(1))):
            b = QPushButton(label)
            b.setMaximumWidth(34)
            b.clicked.connect(fn)
            pbr.addWidget(b)
        pl.addLayout(pbr)
        secrow = QHBoxLayout()
        secrow.addWidget(QLabel("secs/page"))
        self.s_secs = QSpinBox()
        self.s_secs.setRange(0, 120)
        self.s_secs.setToolTip("dwell per page on the panel "
                               "(0 = file default)")
        self.s_secs.valueChanged.connect(self._secs_changed)
        secrow.addWidget(self.s_secs)
        secrow.addStretch(1)
        bsend = QPushButton("Send to program…")
        bsend.clicked.connect(self.send_to_program)
        secrow.addWidget(bsend)
        pl.addLayout(secrow)

        save_box = self._group("File", sl)
        vl = QVBoxLayout(save_box)
        self.e_path = QLineEdit()
        self.e_path.textEdited.connect(self._mark_path_touched)
        vl.addWidget(self.e_path)
        sr = QHBoxLayout()
        bs, bse = QPushButton("Save PNG"), QPushButton("Save && Exit")
        bs.clicked.connect(self.save_png)
        bse.clicked.connect(self.save_and_exit)
        sr.addWidget(bs)
        sr.addWidget(bse)
        vl.addLayout(sr)
        sl.addStretch(1)

        mid = QWidget()
        mml = QVBoxLayout(mid)
        mml.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("zoom"))
        self.c_zoom = QComboBox()
        self.c_zoom.addItems(["fit", "2", "3", "4", "6", "8"])
        self.c_zoom.setCurrentText("fit")
        self.c_zoom.currentIndexChanged.connect(self.refresh_display)
        toolbar.addWidget(self.c_zoom)
        self.ck_dots = QCheckBox("LED dots")
        self.ck_dots.setChecked(True)
        self.ck_dots.stateChanged.connect(lambda _s: self.refresh_display())
        self.ck_grid = QCheckBox("overlay")
        self.ck_grid.stateChanged.connect(lambda _s: self.refresh_display())
        toolbar.addWidget(self.ck_dots)
        toolbar.addWidget(self.ck_grid)
        toolbar.addSpacing(20)
        self.tool_btns = QButtonGroup(self)
        self.b_paint = QPushButton("Paint")
        self.b_erase = QPushButton("Erase")
        for i, b in enumerate((self.b_paint, self.b_erase)):
            b.setCheckable(True)
            self.tool_btns.addButton(b, i)
        self.b_paint.setChecked(True)
        toolbar.addWidget(self.b_paint)
        toolbar.addWidget(self.b_erase)
        toolbar.addWidget(QLabel("brush"))
        self.s_brush = QSpinBox()
        self.s_brush.setRange(1, 3)
        toolbar.addWidget(self.s_brush)
        bclear = QPushButton("Clear touch-ups")
        bclear.clicked.connect(self.clear_touch)
        toolbar.addWidget(bclear)
        bprev1 = QPushButton("Preview 1:1")
        bprev1.clicked.connect(self.open_preview)
        toolbar.addWidget(bprev1)
        toolbar.addStretch(1)
        mml.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setBackgroundRole(self.scroll.backgroundRole())
        self.canvas = PixelCanvas(self)
        self.scroll.setWidget(self.canvas)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.viewport().installEventFilter(self)
        mml.addWidget(self.scroll, 1)
        mml.addWidget(QLabel(
            "left paints · right erases · B/E tools · touch-ups stick to "
            "coordinates and bake into the PNG"))

        split.addWidget(side)
        split.addWidget(mid)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.coord_label = QLabel("x -, y -")
        self.status.addPermanentWidget(self.coord_label)
        self.status.showMessage("keys: 1-4 layout · B/E tools · "
                                "Ctrl+S save · left paints · right erases")
        for key, style in (("1", "top"), ("2", "bottom"), ("3", "left"),
                           ("4", "right")):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(
                lambda s=style: self._quick_style(s))
        for key, tool in (("B", "paint"), ("E", "erase")):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(
                lambda t=tool: self._set_tool(t))

    def _typing(self):
        return isinstance(QApplication.focusWidget(),
                          (QLineEdit, QComboBox, QSpinBox))

    def _mark_path_touched(self, _text=""):
        self._path_touched = True

    def _set_tool(self, tool):
        if self._typing():
            return
        (self.b_paint if tool == "paint" else self.b_erase).setChecked(
            True)

    def _quick_style(self, style):
        if self._typing():
            return  # typing "43" must not flip layouts
        self.style_radios[style].setChecked(True)
        self.refresh()

        for key, style in (("1", "top"), ("2", "bottom"), ("3", "left"),
                           ("4", "right")):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(
                lambda s=style: self._quick_style(s))

    # -- widget helpers ----------------------------------------------
    @staticmethod
    def _group(title, parent):
        g = QGroupBox(title)
        parent.addWidget(g)
        return g

    def _field(self, parent, label, default):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        e = QLineEdit(default)
        e.textChanged.connect(lambda _t: self._deb.start(120))
        e.returnPressed.connect(self.refresh)
        row.addWidget(e, 1)
        parent.addLayout(row)
        return e

    def _bdf_row(self, parent, which, default_font, default_scale):
        row = QHBoxLayout()
        row.addWidget(QLabel(which))
        c = QComboBox()
        c.addItems(self.fonts)
        c.setCurrentText(default_font if default_font in self.fonts
                         else self.fonts[0])
        c.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(c, 1)
        s = QSpinBox()
        s.setRange(1, 8)
        s.setValue(default_scale)
        s.valueChanged.connect(lambda _v: self.refresh())
        row.addWidget(s)
        parent.addLayout(row)
        return c, s

    def _sys_row(self, parent, which, default_fam, default_px,
                 default_bold):
        row = QHBoxLayout()
        row.addWidget(QLabel(which))
        c = QComboBox()
        c.setEditable(True)
        c.setInsertPolicy(QComboBox.NoInsert)
        comp = c.completer()
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setFilterMode(Qt.MatchContains)
        c.setCompleter(comp)
        c.setCurrentText(default_fam)
        c.activated.connect(lambda _i: self.refresh())
        c.lineEdit().returnPressed.connect(self.refresh)
        row.addWidget(c, 1)
        s = QSpinBox()
        s.setRange(6, 120)
        s.setValue(default_px)
        s.valueChanged.connect(lambda _v: self.refresh())
        row.addWidget(s)
        b = QCheckBox("B")
        b.setChecked(default_bold)
        b.setToolTip("bold")
        b.stateChanged.connect(lambda _s: self.refresh())
        row.addWidget(b)
        parent.addLayout(row)
        return c, s, b

    # -- state ---------------------------------------------------------
    def zoom(self):
        text = self.c_zoom.currentText()
        if text == "fit":
            try:
                avail = self.scroll.viewport().width()
            except Exception:
                avail = 0
            if avail and avail >= W:
                return max(1, min(8, avail // W))
            return 3
        try:
            return max(1, min(8, int(text)))
        except (TypeError, ValueError):
            return 3

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and \
                event.type() == QEvent.Type.Resize and \
                self.c_zoom.currentText() == "fit":
            self.refresh_display()
        return super().eventFilter(obj, event)

    def tool(self):
        return "erase" if self.b_erase.isChecked() else "paint"

    def style(self):
        for s, rb in self.style_radios.items():
            if rb.isChecked():
                return s
        return "top"

    def set_fg(self, hexv):
        self.fg_hex = hexv
        self.refresh()

    def _custom_live(self, text):
        v = text.strip()
        s = v[1:] if v.startswith("#") else v
        if len(s) in (3, 6) and all(
                c in "0123456789abcdefABCDEF" for c in s):
            self.fg_hex = "#" + s
            for b in self.col_btns.buttons():
                b.setChecked(False)
            self.refresh()

    def _quick_style(self, style):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QComboBox, QSpinBox)):
            return  # typing "43" must not flip layouts
        self.style_radios[style].setChecked(True)
        self.refresh()

    def drop(self):
        for e in (self.e_route, self.e_dest, self.e_via):
            e.clear()
        self.refresh()

    def job(self):
        cur = self.pages[self.page_idx] \
            if 0 <= self.page_idx < len(self.pages) else {}
        return {
            "route": self.e_route.text(), "dest": self.e_dest.text(),
            "via": self.e_via.text(), "style": self.style(),
            "route_font": self.c_rf.currentText(),
            "dest_font": self.c_df.currentText(),
            "via_font": self.c_vf.currentText(),
            "route_scale": self.s_rs.value(),
            "dest_scale": self.s_ds.value(),
            "via_scale": self.s_vs.value(),
            "fg": self.fg_hex,
            "upper_dest": cur.get("upper_dest", False),
            "via_prefix": cur.get("via_prefix", False),
        }

    def ensure_faces(self):
        if sysfonts.FACES:
            return True
        for _ in range(2):
            try:
                found = sysfonts.scan_system_fonts()
            except Exception:
                found = {}
            if found:
                sysfonts.FACES.update(found)
                break
        fams = sorted(sysfonts.FACES)
        for combo in (self.c_srf, self.c_sfd, self.c_sfv):
            cur = combo.currentText()
            combo.clear()
            combo.addItems(fams)
            combo.setCurrentText(cur if cur in fams else
                                 sysfonts.preferred_default(fams))
        self.face_count.setText(
            f"{len(fams)} families" if fams else "scan found nothing")
        return bool(fams)

    def _sys_spec(self):
        out = {}
        for name, combo, spin, bold, default in (
                ("route", self.c_srf, self.s_srp, self.b_srb, 34),
                ("dest", self.c_sfd, self.s_sdp, self.b_sdb, 20),
                ("via", self.c_sfv, self.s_svp, self.b_svb, 12)):
            fam = combo.currentText().strip()
            px = spin.value()
            out[name] = (*sysfonts.resolve_face(fam, bold.isChecked()),
                         px)
        return out

    def fsrc(self):
        return "sys" if (self.rb_sys.isChecked() and sysfonts.PIL_OK) \
            else "bdf"

    # -- render --------------------------------------------------------
    def refresh(self):
        t0 = time.perf_counter()
        sys_mode = self.fsrc() == "sys" and self.ensure_faces()
        self.bdf_rows.setVisible(not sys_mode)
        self.sys_rows.setVisible(sys_mode)
        job = self.job()
        try:
            if sys_mode:
                spec = self._sys_spec()
                sig = ("sys", repr(sorted(job.items())), repr(spec),
                       self.ck_invert.isChecked())
                if sig == self._last_sig:
                    return
                frame, info = sysfonts.render_system(ENG, job, spec)
            else:
                sig = ("bdf", repr(sorted(job.items())),
                       self.ck_invert.isChecked())
                if sig == self._last_sig:
                    return
                frame, info = ENG.render(job)
        except (ValueError, OSError) as e:
            self.status.showMessage(str(e))
            return
        self._last_sig = sig
        if self.ck_invert.isChecked():
            fg = ENG.parse_colour(self.fg_hex)
            out = bytearray(len(frame))
            for i in range(0, len(frame), 3):
                if not (frame[i] or frame[i + 1] or frame[i + 2]):
                    out[i], out[i + 1], out[i + 2] = fg
            frame = out
            info = dict(info, lit=sum(
                1 for i in range(0, len(frame), 3)
                if frame[i] or frame[i + 1] or frame[i + 2]))
        self.base = frame
        self.info = info
        if not self._path_touched:
            self.e_path.setText(ENG.default_filename(
                info.get("route", ""), self.e_dest.text()))
        self._apply_touch()
        ms = (time.perf_counter() - t0) * 1000
        self._show_status(ms)

    def _apply_touch(self):
        fg = ENG.parse_colour(self.fg_hex)
        frame = bytearray(self.base)
        for x, y in self.touch_add:
            o = (y * W + x) * 3
            frame[o], frame[o + 1], frame[o + 2] = fg
        for x, y in self.touch_del:
            o = (y * W + x) * 3
            frame[o], frame[o + 1], frame[o + 2] = 0, 0, 0
        self.frame = frame
        self.info = dict(
            self.info, lit=sum(
                1 for i in range(0, len(frame), 3)
                if frame[i] or frame[i + 1] or frame[i + 2]))
        self.refresh_display()

    def _show_status(self, ms=None):
        i = self.info
        touch = ""
        if self.touch_add or self.touch_del:
            touch = (f"  touch +{len(self.touch_add)} "
                     f"-{len(self.touch_del)}")
        warn = ("  ! " + " / ".join(i.get("warnings", ""))
                if i.get("warnings") else "")
        timing = f"  {ms:.0f}ms" if ms is not None else ""
        self.status.showMessage(
            f"{i.get('w', W)}x{i.get('h', H)}  {i.get('lit', 0)} lit "
            f"pixels  {i.get('fg', '')}"
            f"{'  system' if i.get('mode') == 'system' else ''}"
            f"{'  inverted' if self.ck_invert.isChecked() else ''}"
            f"{touch}{warn}{timing}")

    # -- display ---------------------------------------------------------
    def _frame_image(self):
        data = bytes(self.frame)
        img = QImage(data, W, H, W * 3, QImage.Format.Format_RGB888)
        img._keep = data  # QImage borrows; keep the bytes alive
        return img

    def refresh_display(self):
        z = self.zoom()
        img = self._frame_image()
        if self.ck_dots.isChecked() and z >= 3:
            big = QImage(W * z, H * z, QImage.Format.Format_RGB888)
            big.fill(Qt.black)
            fg = QColor(self.info.get("fg", "#ffffff"))
            p = QPainter(big)
            try:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(fg))
                fr = self.frame
                for y in range(H):
                    o = y * W * 3
                    for x in range(W):
                        if fr[o] or fr[o + 1] or fr[o + 2]:
                            p.drawEllipse(x * z + 1, y * z + 1,
                                          z - 2, z - 2)
                        o += 3
            finally:
                p.end()
            shown = big
        else:
            shown = img.scaled(W * z, H * z, Qt.IgnoreAspectRatio,
                               Qt.FastTransformation)
        if self.ck_grid.isChecked():
            shown = QImage(shown)
            p = QPainter(shown)
            try:
                for x in range(W + 1):
                    p.setPen(QColor("#3d3d3d" if x % 10 == 0
                                    else "#242424"))
                    p.drawLine(x * z, 0, x * z, H * z)
                for y in range(H + 1):
                    p.setPen(QColor("#3d3d3d" if y % 10 == 0
                                    else "#242424"))
                    p.drawLine(0, y * z, W * z, y * z)
                for name, f in self.info.get("fields", {}).items():
                    c = f.get("cell")
                    if not c:
                        continue
                    p.setPen(QColor(CELL_COLOURS.get(name, "#ffffff")))
                    p.drawRect(c[0] * z, c[1] * z,
                               (c[2] - c[0] + 1) * z - 1,
                               (c[3] - c[1] + 1) * z - 1)
            finally:
                p.end()
        pm = QPixmap.fromImage(shown)
        self.canvas.setPixmap(pm)
        self.canvas.setFixedSize(pm.size())
        self._show_status()

    # -- pixel tool --------------------------------------------------------
    def paint_stroke(self, pts, erase):
        s = self.s_brush.value()
        changed = False
        for x, y in pts:
            for dy in range(s):
                for dx in range(s):
                    px, py = x + dx, y + dy
                    if not (0 <= px < W and 0 <= py < H):
                        continue
                    if erase:
                        if (px, py) in self.touch_add:
                            self.touch_add.discard((px, py))
                            changed = True
                        if (px, py) not in self.touch_del:
                            self.touch_del.add((px, py))
                            changed = True
                    else:
                        if (px, py) in self.touch_del:
                            self.touch_del.discard((px, py))
                            changed = True
                        if (px, py) not in self.touch_add:
                            self.touch_add.add((px, py))
                            changed = True
        if changed:
            self._apply_touch()
            self._show_status()

    def clear_touch(self):
        self.touch_add.clear()
        self.touch_del.clear()
        self._apply_touch()
        self._show_status()

    def hover_pixel(self, p):
        self.coord_label.setText(
            f"x {p[0]}, y {p[1]}" if p else "x -, y -")

    # -- messages ----------------------------------------------------------
    def _reload_list(self):
        self.listbox.clear()
        for m in self.messages:
            self.listbox.addItem(
                m.get("name") or
                f"{m.get('route', '')} {m.get('destination', '')}".strip())

    def _on_select(self, row):
        if not 0 <= row < len(self.messages):
            return
        m = self.messages[row]
        self.pages = pages_from_message(m)
        self.page_idx = 0
        prog = m.get("program")
        self.program_routing = dict(prog) if isinstance(prog, dict) \
            else {}
        self._rebuild_pages()
        self._load_page(self.pages[0])

    def _current_message(self, single=False):
        if single or not self.pages:
            pages = [self._snapshot_page()]
        else:
            self._flush_page()
            pages = [dict(p) for p in self.pages]
        cur = pages[self.page_idx] \
            if 0 <= self.page_idx < len(pages) else pages[0]
        route = cur.get("route", "").strip()
        dest = cur.get("destination", "").strip()
        msg = {
            "name": f"{route} {dest}".strip() or "(empty)",
            "route": route, "destination": dest,
            "via": cur.get("via", ""), "style": cur.get("style", "top"),
            "fsrc": cur.get("fsrc", "bdf"),
            "route_font": cur.get("route_font", "10x20.bdf"),
            "dest_font": cur.get("dest_font", "10x20.bdf"),
            "via_font": cur.get("via_font", "6x13B.bdf"),
            "route_scale": cur.get("route_scale", 2),
            "dest_scale": cur.get("dest_scale", 1),
            "via_scale": cur.get("via_scale", 1),
            "sys": cur.get("sys", {}),
            "touch": cur.get("touch", {"add": [], "del": []}),
            "program": dict(self.program_routing),
            "pages": pages,
        }
        return msg

    def msg_new(self):
        self.messages.append(self._current_message(single=True))
        save_messages(self.messages)
        self._reload_list()
        self.listbox.setCurrentRow(len(self.messages) - 1)
        self.status.showMessage(f"message {len(self.messages)} added")

    def msg_update(self):
        row = self.listbox.currentRow()
        if row < 0:
            self.status.showMessage("select a message first")
            return
        self.messages[row] = self._current_message()
        save_messages(self.messages)
        self._reload_list()
        self.listbox.setCurrentRow(row)

    def msg_delete(self):
        row = self.listbox.currentRow()
        if row < 0:
            return
        del self.messages[row]
        if not self.messages:
            self.messages = [self._current_message()]
        save_messages(self.messages)
        self._reload_list()

    # -- pages -----------------------------------------------------------
    def _snapshot_page(self):
        sys_spec = {}
        for name, combo, spin, bold, default in (
                ("route", self.c_srf, self.s_srp, self.b_srb, 34),
                ("dest", self.c_sfd, self.s_sdp, self.b_sdb, 20),
                ("via", self.c_sfv, self.s_svp, self.b_svb, 12)):
            sys_spec[name] = {"family": combo.currentText().strip(),
                              "px": spin.value(),
                              "bold": bold.isChecked()}
        # case/prefix rules live on the page (no editor UI): keep the
        # current page's values so editing never flips them; fresh
        # pages default to off (type casing manually)
        cur = self.pages[self.page_idx] \
            if 0 <= self.page_idx < len(self.pages) else {}
        return normalize_page({
            "route": self.e_route.text(), "destination": self.e_dest.text(),
            "via": self.e_via.text(), "style": self.style(),
            "fsrc": self.fsrc(),
            "route_font": self.c_rf.currentText(),
            "dest_font": self.c_df.currentText(),
            "via_font": self.c_vf.currentText(),
            "route_scale": self.s_rs.value(),
            "dest_scale": self.s_ds.value(),
            "via_scale": self.s_vs.value(),
            "upper_dest": cur.get("upper_dest", False),
            "via_prefix": cur.get("via_prefix", False),
            "sys": sys_spec,
            "touch": {"add": sorted(self.touch_add),
                      "del": sorted(self.touch_del)},
            "seconds": self.s_secs.value(),
        })

    def _load_page(self, page):
        page = normalize_page(page)
        for w in (self.e_route, self.e_dest, self.e_via, self.c_rf,
                  self.c_df, self.c_vf, self.s_rs, self.s_ds, self.s_vs,
                  self.c_srf, self.c_sfd, self.c_sfv, self.s_srp,
                  self.s_sdp, self.s_svp, self.s_secs):
            w.blockSignals(True)
        try:
            self.e_route.setText(page["route"])
            self.e_dest.setText(page["destination"])
            self.e_via.setText(page["via"])
            self.style_radios[page["style"]].setChecked(True)
            for combo, k in ((self.c_rf, "route_font"),
                             (self.c_df, "dest_font"),
                             (self.c_vf, "via_font")):
                if page[k] in self.fonts:
                    combo.setCurrentText(page[k])
            self.s_rs.setValue(max(1, min(8, int(page["route_scale"]))))
            self.s_ds.setValue(max(1, min(8, int(page["dest_scale"]))))
            self.s_vs.setValue(max(1, min(8, int(page["via_scale"]))))
            if sysfonts.PIL_OK and page.get("fsrc") == "sys":
                self.rb_sys.setChecked(True)
            else:
                self.rb_bdf.setChecked(True)
            for combo, spin, bold, k in (
                    (self.c_srf, self.s_srp, self.b_srb, "route"),
                    (self.c_sfd, self.s_sdp, self.b_sdb, "dest"),
                    (self.c_sfv, self.s_svp, self.b_svb, "via")):
                s = page["sys"][k]
                if s["family"] in sysfonts.FACES:
                    combo.setCurrentText(s["family"])
                spin.setValue(s["px"])
                bold.setChecked(s["bold"])
            self.s_secs.setValue(int(page["seconds"] or 0))
            self.touch_add, self.touch_del = touch_of(
                {"touch": page["touch"]})
        finally:
            for w in (self.e_route, self.e_dest, self.e_via, self.c_rf,
                      self.c_df, self.c_vf, self.s_rs, self.s_ds,
                      self.s_vs, self.c_srf, self.c_sfd, self.c_sfv,
                      self.s_srp, self.s_sdp, self.s_svp, self.s_secs):
                w.blockSignals(False)
        self._last_sig = None
        self.refresh()

    def _flush_page(self):
        if 0 <= self.page_idx < len(self.pages):
            self.pages[self.page_idx] = self._snapshot_page()

    def _rebuild_pages(self):
        self.pagelist.blockSignals(True)
        try:
            self.pagelist.clear()
            for i, p in enumerate(self.pages):
                label = f"{p.get('route', '')} {p.get('destination', '')}" \
                    .strip() or "(blank)"
                self.pagelist.addItem(f"Page {i + 1} -- {label}")
            self.pages_box.setTitle(f"Pages ({len(self.pages)})")
            if 0 <= self.page_idx < len(self.pages):
                self.pagelist.setCurrentRow(self.page_idx)
        finally:
            self.pagelist.blockSignals(False)

    def _select_page(self, row):
        if not 0 <= row < len(self.pages) or row == self.page_idx:
            if 0 <= row < len(self.pages):
                self.page_idx = row
            return
        self._flush_page()
        self.page_idx = row
        self._load_page(self.pages[row])

    def _blank_page(self):
        cur = self._snapshot_page()
        cur.update({"destination": "", "via": "",
                    "touch": {"add": [], "del": []}, "seconds": 0})
        return normalize_page(cur)

    def page_add(self):
        self._flush_page()
        self.pages.append(self._blank_page())
        self.page_idx = len(self.pages) - 1
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_dupe(self):
        self._flush_page()
        self.pages.append(self._snapshot_page())
        self.page_idx = len(self.pages) - 1
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_delete(self):
        if not self.pages:
            return
        del self.pages[self.page_idx]
        if not self.pages:
            self.pages = [self._blank_page()]
        self.page_idx = min(self.page_idx, len(self.pages) - 1)
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_move(self, direction):
        j = self.page_idx + direction
        if not (0 <= self.page_idx < len(self.pages) and
                0 <= j < len(self.pages)):
            return
        self._flush_page()
        self.pages[self.page_idx], self.pages[j] = \
            self.pages[j], self.pages[self.page_idx]
        self.page_idx = j
        self._rebuild_pages()

    def _secs_changed(self, value):
        if 0 <= self.page_idx < len(self.pages):
            self.pages[self.page_idx]["seconds"] = value

    # -- file / windows ------------------------------------------------------
    def _resolve_path(self):
        rel = self.e_path.text().strip()
        if not rel:
            rel = ENG.default_filename(self.info.get("route", ""),
                                       self.e_dest.text())
            self.e_path.setText(rel)
        full = ENG.resolve_save_path(rel)
        if full is None:
            self.status.showMessage("path must be a bitmap/*.png file")
        return full

    def save_png(self):
        full = self._resolve_path()
        if full is None:
            return False
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "wb") as f:
                f.write(ENG.encode_png(self.frame))
            os.replace(tmp, full)
        except OSError as e:
            self.status.showMessage(str(e))
            return False
        rel = os.path.relpath(full, THIS_DIR)
        self.status.showMessage(
            f"saved {rel} ({self.info.get('lit', 0)} lit pixels)")
        return True

    def save_and_exit(self):
        if self.save_png():
            self.close()

    def _send_names(self):
        """(route, dest) text for PNG filenames: editor first, then
        the first non-blank page."""
        route = self.e_route.text().strip()
        dest = self.e_dest.text().strip()
        if not route or not dest:
            for p in self.pages:
                if not route and str(p.get("route", "")).strip():
                    route = str(p["route"]).strip()
                if not dest and str(p.get("destination", "")).strip():
                    dest = str(p["destination"]).strip()
                if route and dest:
                    break
        return route, dest

    def send_to_program(self):
        self._flush_page()
        if not self.pages:
            self.pages = [self._snapshot_page()]
            self.page_idx = 0
            self._rebuild_pages()
        if sysfonts.PIL_OK and any(
                str(p.get("fsrc", "bdf")) == "sys" for p in self.pages):
            if not self.ensure_faces():
                self.status.showMessage(
                    "system font scan found nothing -- cannot render "
                    "system-font pages")
                return False
        name_route, name_dest = self._send_names()
        dlg = SendDialog(self, self.program_routing, name_route,
                         name_dest, self.pages, self.fg_hex,
                         self.ck_invert.isChecked())
        if dlg.exec() != QDialog.Accepted:
            return False
        routing = dlg.routing()
        try:
            summary = send_pages_to_program(
                self.pages, self.fg_hex, self.ck_invert.isChecked(),
                name_route, name_dest, routing)
        except ValueError as e:
            self.status.showMessage(f"send failed: {e}")
            return False
        self.program_routing = {k: routing[k] for k in
                                ("file", "program", "destination",
                                 "route")}
        row = self.listbox.currentRow()
        if 0 <= row < len(self.messages):
            self.messages[row]["program"] = dict(self.program_routing)
            save_messages(self.messages)
        skip = (f" (+{summary['skipped']} blank skipped)"
                if summary["skipped"] else "")
        dup = summary["added"] < len(summary["paths"])
        note = " (already-listed images skipped)" if dup else ""
        new = " (new file created)" if summary.get("created_file") else ""
        self.status.showMessage(
            f"sent {len(summary['paths'])} page(s) to "
            f"{summary['program']}/{summary['destination']} in "
            f"{summary['file']}{skip}{note}{new}")
        return True

    def open_preview(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Preview 1:1")
        lab = QLabel()
        lab.setPixmap(QPixmap.fromImage(self._frame_image()))
        box = QVBoxLayout(dlg)
        box.addWidget(lab)
        box.addWidget(QLabel("exact panel pixels"))
        dlg.exec()

    def about(self):
        QMessageBox.about(
            self, "About Sign Studio",
            "Sign Studio -- 240x40 LED destination editor.\n\n"
            "Type route + destination + via, pick bitmap or system "
            "fonts and a via layout, hand-fix pixels with the paint "
            "tool, then Save PNG into bitmap/ and attach it in the "
            "program editor.\n\nBitmap pixels only -- no antialiasing.")

    def closeEvent(self, event):
        event.accept()


def smoke():
    """Offscreen self-test: prints results, returns exit code."""
    print("faces...", flush=True)
    t0 = time.time()
    found = sysfonts.scan_system_fonts()
    sysfonts.FACES.update(found)
    print(f"  {len(found)} families in {time.time() - t0:.1f}s",
          flush=True)
    w = Studio()
    fails = []

    def check(name, cond, extra=""):
        print(f"  {'ok' if cond else 'FAIL'} {name} {extra}", flush=True)
        if not cond:
            fails.append(name)

    check("bdf render", w.info.get("lit", 0) > 0,
          f"lit={w.info.get('lit')}")
    for s in STYLES:
        w.style_radios[s].setChecked(True)
        w.refresh()
        check(f"style {s}", w.info.get("lit", 0) > 0)
    w.style_radios["top"].setChecked(True)
    w.rb_sys.setChecked(True)
    w.refresh()
    check("sys render", w.info.get("mode") == "system"
          and w.info.get("lit", 0) > 0, f"lit={w.info.get('lit')}")
    colours = {(w.frame[i], w.frame[i + 1], w.frame[i + 2])
               for i in range(0, len(w.frame), 3)}
    check("two colours only", len(colours) == 2, f"{colours}")
    # pixel tool: paint then erase the same pixel, plus a brush-2 dab
    w.rb_bdf.setChecked(True)
    w.refresh()
    lit0 = w.info["lit"]
    w.paint_stroke([(5, 5)], erase=False)
    check("paint adds", (5, 5) in w.touch_add
          and w.info["lit"] >= lit0)
    w.paint_stroke([(5, 5)], erase=True)
    check("erase removes", (5, 5) not in w.touch_add
          and (5, 5) in w.touch_del)
    w.s_brush.setValue(2)
    w.paint_stroke([(10, 10)], erase=False)
    check("brush 2x2", len(w.touch_add) == 4, f"{sorted(w.touch_add)}")
    # coordinate mapping incl. clipping (at explicit 3x: fit varies)
    w.c_zoom.setCurrentText("3")
    check("map inside", w.canvas.pixel_from_pos(QPoint(3 * 10 + 1,
                                                        3 * 20 + 2))
          == (10, 20))
    check("map clipped", w.canvas.pixel_from_pos(QPoint(-5, 999)) is None)
    # save + decode (clean up our own artifact afterwards)
    w.e_path.setText("bitmap/destinations/custom/qt-smoke.png")
    check("save", w.save_png())
    try:
        from images import decode_png
        sw, sh, _ = decode_png(
            "bitmap/destinations/custom/qt-smoke.png")
        check("png 240x40", (sw, sh) == (240, 40))
    except SystemExit as e:
        check("png 240x40", False, str(e))
    finally:
        try:
            os.remove("bitmap/destinations/custom/qt-smoke.png")
        except OSError:
            pass
    # messages round-trip (backup + restore the real file)
    bak = None
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE) as f:
            bak = f.read()
    try:
        n0 = len(w.messages)
        w.msg_new()
        check("msg new", len(w.messages) == n0 + 1)
        w.listbox.setCurrentRow(n0)
        w.msg_update()
        w.listbox.setCurrentRow(0)
        w.msg_delete()
        check("msg update/delete", len(w.messages) == n0)
        disk = json.load(open(MSG_FILE))
        check("msg persisted", len(disk) == n0)
    finally:
        if bak is None:
            if os.path.exists(MSG_FILE):
                os.remove(MSG_FILE)
        else:
            with open(MSG_FILE, "w") as f:
                f.write(bak)
    print("SMOKE " + ("OK" if not fails else f"FAILED: {fails}"),
          flush=True)
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Sign Studio (Qt)")
    ap.add_argument("--smoke", action="store_true",
                    help="Offscreen self-test, prints results, exits")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    if args.smoke:
        sys.exit(smoke())
    w = Studio()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
