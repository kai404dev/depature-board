#!/usr/bin/env python3
"""Mobitec ICU 602 replica web portal for program.py (stdlib only).

Hosts the controller UI: F1 enters the route, F2 picks the
destination (arrows or numeric id), the keypad takes full
route+dest codes. Drives program.py's live selection; the matrix
follows whatever is picked here.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import program as model


def numeric_route(route):
    """Digits of a route number: X12 -> 12."""
    return "".join(c for c in str(route) if c.isdigit())


def has_letter(route):
    return any(c.isalpha() for c in str(route))


def match_route(buf, routes):
    """Match a typed route against [(program, route)]. Letter routes
    can be typed with a leading 1 (112 -> X12 when digits match)."""
    b = str(buf).strip().upper()
    for n, r in routes:
        if r and str(r).upper() == b:
            return n
    if len(b) > 1 and b.startswith("1"):
        rest = b[1:]
        for n, r in routes:
            if r and str(r).upper() == rest:
                return n
        hits = [n for n, r in routes
                if r and has_letter(r) and numeric_route(r) == rest]
        if len(hits) == 1:
            return hits[0]
    hits = [n for n, r in routes
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
    """Live selection shared between the portal and the matrix loop."""

    def __init__(self, path, program=None, dest=None):
        self.path = path
        self.lock = threading.Lock()
        self.mtime = None
        self.data = None
        self._gen = 0
        self.message = ""
        self.program_name = None
        self.dest_name = None
        if not self._refresh_locked():
            sys.exit(f"portal: cannot load {path}")
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
        except SystemExit as e:
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
        return [(n, r.get("route", "") if isinstance(r, dict) else "")
                for n, r in self.data[1].items()]

    def _ids(self):
        raw = self.data[1].get(self.program_name)
        if not isinstance(raw, dict):
            return []
        return model.destination_ids(raw, self.program_name)

    def _match_dest(self, dest):
        for d, _ in self._ids():
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
            k = str(k)
            if k == "F1":
                self.field, self.buffer = "line", ""
                self.message = "Enter line number"
            elif k in ("F2", "dest"):
                self.field = "dest"
                self.buffer = ""
                self.hi = self._dest_index()
                self.message = "Select destination"
            elif k in ("F3", "F4", "F5"):
                self.message = f"{k} not used"
            elif k in ("home", "clearall"):
                self.program_name, self.dest_name = self._clamp(
                    *self.initial)
                self.field, self.buffer = "line", ""
                self.hi = self._dest_index()
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
            else:
                self.message = f"Unknown key {k}"
            return self._snapshot_locked()

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

    def _digit(self, k):
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
        if self.field != "dest":
            self.message = "F2 for destination"
            return
        ids = self._ids()
        if not ids:
            self.message = "No destinations"
            return
        step = 1 if k in ("down", "right") else -1
        self.hi = (self.hi + step) % len(ids)
        self.buffer = ""
        self.message = ""

    def _confirm(self):
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
        if self.buffer:
            big = self.buffer
        elif self.dest_name:
            big = f"{route} {self.dest_name}"
        else:
            big = f"{route} ALL" if route else "---"
        return {
            "program": self.program_name,
            "route": route,
            "dest": self.dest_name,
            "dest_id": cur_id,
            "big": big,
            "field": self.field,
            "buffer": self.buffer,
            "hi": self.hi,
            "message": self.message,
            "routes": [{"program": n, "route": r or n}
                       for n, r in self._routes()],
            "destinations": [{"name": d, "id": di} for d, di in ids],
        }


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICU 602</title>
<style>
body{background:#222;margin:0;padding:16px;font-family:Arial,sans-serif;
color:#eee;display:flex;flex-direction:column;align-items:center;gap:12px}
.unit{background:linear-gradient(#2b3a58,#131a2e);border-radius:28px;
padding:22px 26px;width:760px;max-width:96vw;box-shadow:0 8px 30px #000}
.brand{color:#8fa3c7;font-size:13px;letter-spacing:1px}
.lcd{background:#d7e7f1;border-radius:6px;color:#1c3550;
padding:12px 16px;box-shadow:inset 0 2px 8px rgba(0,0,0,.35);
display:grid;grid-template-columns:1fr 150px;gap:8px;min-height:120px}
.big{font-family:"Courier New",monospace;font-weight:bold;
font-size:44px;overflow:hidden;white-space:nowrap}
.meta{font-size:17px;line-height:1.5;text-align:right}
.soft{display:flex;justify-content:space-between;margin-top:6px}
.soft button{background:#23405c;color:#fff;border:0;border-radius:4px;
padding:5px 14px;font-size:14px;cursor:pointer}
.msg{min-height:20px;font-size:14px;color:#ffd27f;margin-top:6px}
.fnrow{display:flex;gap:14px;margin:16px 0 4px;align-items:center}
.fnrow button{background:#0a0a0a;color:#ff8c1a;border:1px solid #444;
border-radius:8px;padding:8px 0;width:64px;font-size:16px;font-weight:bold;
cursor:pointer}
.fnrow button.active{outline:2px solid #ffd27f}
.home{background:#0a0a0a;color:#ff8c1a !important}
.pad{display:flex;gap:26px;margin-top:14px;align-items:flex-start}
.digits{display:grid;grid-template-columns:repeat(3,64px);gap:10px}
.digits button{background:#101010;color:#fff;border:1px solid #333;
border-radius:10px;padding:10px 0 6px;font-size:20px;cursor:pointer}
.digits button small{display:block;color:#999;font-size:10px}
.digits button:active{background:#333}
.nav{display:grid;grid-template-columns:repeat(3,54px);gap:8px;
align-content:start}
.nav button{background:#101010;color:#fff;border:1px solid #333;
border-radius:50%;width:54px;height:54px;font-size:18px;cursor:pointer}
.nav button.ok{color:#37e05a;border-radius:10px}
.nav button.no{color:#ff4444;border-radius:10px}
.dests{display:flex;flex-wrap:wrap;gap:8px;max-width:760px}
.dests span{background:#333;border-radius:6px;padding:6px 10px;font-size:14px}
.dests span.cur{background:#1d5c2e}
.dests span.hi{outline:2px solid #ffd27f}
.hint{color:#888;font-size:12px}
</style></head><body>
<div class="unit">
<div class="brand">mobitec&nbsp;&nbsp;ICU 602</div>
<div class="lcd">
<div><div class="big" id="big">---</div>
<div class="soft"><button onclick="press('dest')">Dest</button>
<button onclick="press('clearall')">Clear all</button></div></div>
<div class="meta"><div id="line">Line: -</div><div id="dest">Dest: -</div>
<div>Extr:</div></div>
</div>
<div class="msg" id="msg"></div>
<div class="fnrow">
<button class="home" onclick="press('home')">&#8962;</button>
<button id="f1" onclick="press('F1')">F1</button>
<button id="f2" onclick="press('F2')">F2</button>
<button onclick="press('F3')">F3</button>
<button onclick="press('F4')">F4</button>
<button onclick="press('F5')">F5</button>
</div>
<div class="pad">
<div class="digits">
<button onclick="press('1')">1</button>
<button onclick="press('2')">2<small>ABC</small></button>
<button onclick="press('3')">3<small>DEF</small></button>
<button onclick="press('4')">4<small>GHI</small></button>
<button onclick="press('5')">5<small>JKL</small></button>
<button onclick="press('6')">6<small>MNO</small></button>
<button onclick="press('7')">7<small>PQRS</small></button>
<button onclick="press('8')">8<small>TUV</small></button>
<button onclick="press('9')">9<small>WXYZ</small></button>
<button></button>
<button onclick="press('0')">0<small>_</small></button>
<button></button>
</div>
<div class="nav">
<button></button><button onclick="press('up')">&#8593;</button><button></button>
<button onclick="press('left')">&#8592;</button>
<button onclick="press('down')">&#8595;</button>
<button onclick="press('right')">&#8594;</button>
<button class="no" onclick="press('clear')">X</button>
<button></button>
<button class="ok" onclick="press('ok')">&#1003;</button>
</div>
</div>
</div>
<div class="dests" id="dests"></div>
<div class="hint">F1 route &middot; F2 destination (arrows or id) &middot;
keypad takes route+dest codes, e.g. 40101 &middot; keyboard: 0-9,
arrows, Enter, Backspace</div>
<script>
function update(s){
document.getElementById('big').textContent=s.big;
document.getElementById('line').textContent='Line: '+(s.route||'-');
document.getElementById('dest').textContent='Dest: '+
(s.dest_id===null||s.dest_id===undefined?'-':s.dest_id);
document.getElementById('msg').textContent=s.message||'';
document.getElementById('f1').className=s.field=='line'?'active':'';
document.getElementById('f2').className=s.field=='dest'?'active':'';
var box=document.getElementById('dests');box.innerHTML='';
s.destinations.forEach(function(d,i){
var el=document.createElement('span');el.textContent=d.id+' '+d.name;
if(d.name==s.dest)el.className='cur';
else if(s.field=='dest'&&i==s.hi)el.className='hi';
box.appendChild(el);});
}
async function press(k){
var r=await fetch('/api/key',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});
update(await r.json());
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
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
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

    print(f"portal on :{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
