#!/usr/bin/env python3
"""Live layout tweaker for the departures board. Stdlib only.

Serves an ASCII preview of every page (real API data + real BDF
metrics, via preview.py) with a form for every value in layout/.
Editing a value rewrites the JSON and re-renders immediately.

  python3 webui.py                      # http://localhost:4000
  python3 webui.py --port 4001 --layout-dir mylayout

This never touches the LED matrix -- it only edits layout files and
previews them. Run departures.py on the Pi to see the real thing.
"""

import argparse
import copy
import html
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

import api
import preview
import departures
from departures import load_layout

FILES = ("shared.json", "page1.json", "page2.json", "page3.json")
CACHE_SECS = 20

_lock = threading.Lock()
_board = {"at": 0, "data": []}
_layout_dir = None


def board_now(railway, station, limit, date):
    with _lock:
        now = time.time()
        if now - _board["at"] > CACHE_SECS or not _board["data"]:
            deps = api.fetch_departures(railway, station, limit, date)
            _board["data"] = api.enrich_with_timetables(deps, railway, date)
            _board["at"] = now
        return _board["data"]


def render_page(L, board, page, args):
    """Render one page to ASCII via the fake matrix. Returns report str."""
    import departures
    W = args.led_cols * args.led_chain
    H = args.led_rows
    with _lock:
        rec = preview.install(W, H)
        a2 = copy.copy(args)
        a2.pages = [page]
        a2.once = True
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            departures.run_matrix(a2, L, lambda: board)
        finally:
            sys.stdout = old
        out = io.StringIO()
        rec.report(out)
        return out.getvalue()


def read_layout_files(layout_dir):
    data = {}
    for name in FILES:
        with open(os.path.join(layout_dir, name)) as f:
            data[name] = json.load(f)
    return data


def dump_layout(content):
    """Serialize like the hand-written files: indent 2, short int
    arrays on one line, trailing newline."""
    raw = json.dumps(content, indent=2)
    raw = re.sub(r"\[\n((?:\s*-?\d+,?\n)+)\s*\]",
                 lambda m: "[" + ", ".join(
                     x.strip().rstrip(",")
                     for x in m.group(1).strip().split("\n")) + "]",
                 raw)
    return raw + "\n"


def fonts_available(layout_dir):
    d = os.path.join(layout_dir, "fonts")
    try:
        return sorted(f for f in os.listdir(d) if f.endswith(".bdf"))
    except FileNotFoundError:
        return []


def esc(s):
    return html.escape(str(s), quote=True)


def field_html(fname, sec, key, val, fonts):
    """One form row for a layout value. Returns (label, control) html."""
    name = f"{fname}|{sec}|{key}"
    if fname == "shared.json" and sec == "colors" and isinstance(val, list):
        hexv = "#%02x%02x%02x" % tuple(val)
        ctl = (f'<input type="color" name="{esc(name)}" value="{hexv}"> '
               f'<code>{esc(val)}</code>')
    elif key == "font":
        opts = "".join(
            f'<option value="{esc(f)}"{" selected" if f == val else ""}>'
            f'{esc(f)}</option>' for f in fonts)
        ctl = f'<select name="{esc(name)}">{opts}</select>'
    elif isinstance(val, bool):
        sel = lambda b, t: f'<option {"selected" if b else ""}>{t}</option>'
        ctl = (f'<select name="{esc(name)}">'
               f'{sel(val, "true")}{sel(not val, "false")}</select>')
    elif isinstance(val, int):
        ctl = (f'<input type="number" step="1" name="{esc(name)}" '
               f'value="{val}" style="width:5em">')
    elif isinstance(val, float):
        ctl = (f'<input type="number" step="any" name="{esc(name)}" '
               f'value="{val}" style="width:5em">')
    else:
        hint = ""
        if key in ("x",):
            hint = ' <small>left|center|right|px</small>'
        elif key in ("y",):
            hint = ' <small>px|bottom|bottom-N</small>'
        ctl = (f'<input type="text" name="{esc(name)}" value="{esc(val)}" '
               f'size="12">{hint}')
    return f"<label>{esc(key)}</label>", ctl


def page_html(title, body):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>departures layout</title>
<style>
body{{background:#111;color:#ddd;font-family:monospace;margin:1em}}
pre{{background:#000;color:#ffb000;padding:.5em;overflow-x:auto;font-size:13px;line-height:1.25}}
form{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:.7em}}
fieldset{{border:1px solid #444;padding:.5em}}
legend{{color:#ff0}}
label{{display:inline-block;min-width:9em;color:#8cf}}
input,select{{background:#222;color:#fff;border:1px solid #555}}
button{{font-size:1.1em;margin:.4em .4em .4em 0;padding:.3em 1em}}
.err{{color:#f66}} .ok{{color:#6f6}} small{{color:#888}}
#live{{color:#6f6;border:1px solid #6f6;padding:0 .4em;font-size:.8em}}
.topbar{{display:flex;gap:1em;align-items:center;flex-wrap:wrap}}
</style></head><body>{body}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BoardTweaker/1.0"

    def log_message(self, *a):
        pass

    def _args(self):
        return self.server.board_args

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/preview":
            return self._preview(urllib.parse.parse_qs(parsed.query))
        L = load_layout(_layout_dir)
        args = self._args()
        try:
            board = board_now(args.railway, args.station, args.limit,
                              args.date or None)
        except Exception as e:
            body = f'<h1>departures layout</h1><p class="err">API error: {esc(e)}</p>'
            return self._send(page_html("layout", body))
        previews = []
        for p in (1, 2, 3):
            try:
                rep = render_page(L, board, p, args)
            except Exception as e:
                rep = f"render error: {e}"
            previews.append(f"<h2>page {p} <small id=\"st{p}\"></small></h2>"
                            f"<pre id=\"pv{p}\">{esc(rep)}</pre>")
        data = read_layout_files(_layout_dir)
        fonts = fonts_available(_layout_dir)
        if not fonts:
            fonts = sorted({v for f in data.values() for s in f.values()
                            if isinstance(s, dict) for k, v in s.items()
                            if k == "font"})
        forms = []
        for fname in FILES:
            for sec, items in data[fname].items():
                if sec.startswith("_") or not isinstance(items, dict):
                    continue
                rows = []
                for key, val in items.items():
                    if key.startswith("_"):
                        continue
                    if isinstance(val, (dict, list)) and not (
                            fname == "shared.json" and sec == "colors"):
                        rows.append(
                            f"<label>{esc(key)}</label><small>edit file "
                            f"directly</small>")
                        continue
                    lab, ctl = field_html(fname, sec, key, val, fonts)
                    rows.append(f"<div>{lab} {ctl}</div>")
                forms.append(f"<fieldset><legend>{esc(fname)} › {esc(sec)}"
                             f"</legend>{''.join(rows)}</fieldset>")
        body = (f'<div class="topbar"><h1>departures layout</h1>'
                f'<span id="live">live</span> <span id="msg"></span>'
                f'<label><input type="checkbox" id="auto" checked> '
                f'auto-refresh previews</label></div>'
                f'{"".join(previews)}'
                f'<h2>tweak (saves as you type)</h2>'
                f'<form method="post" action="/save" id="tweak">'
                f'{"".join(forms)}'
                f'</form>'
                f'<script>'
                f'const msg=document.getElementById("msg");'
                f'let timer=null;'
                f'async function poll(){{'
                f' if(!document.getElementById("auto").checked)return;'
                f' for(const p of [1,2,3]){{'
                f'  try{{const r=await fetch("/preview?page="+p);'
                f'   if(r.ok)document.getElementById("pv"+p).textContent'
                f'    =await r.text();}}catch(e){{}}'
                f' }}'
                f'}}'
                f'setInterval(poll,3000);'
                f'function collect(){{'
                f' const fd=new FormData(document.getElementById("tweak"));'
                f' const sp=new URLSearchParams();'
                f' for(const [k,v] of fd.entries())sp.append(k,v);'
                f' return sp.toString();'
                f'}}'
                f'async function save(){{'
                f' msg.textContent="saving…";msg.className="";'
                f' try{{const r=await fetch("/save?json=1",{{method:"POST",'
                f'  headers:{{"Content-Type":"application/x-www-form-urlencoded"}},'
                f'  body:collect()}});'
                f'  const j=await r.json();'
                f'  if(j.ok){{msg.textContent="saved "+new Date().'
                f'   toLocaleTimeString();msg.className="ok";poll();}}'
                f'  else{{msg.textContent=j.error||"save failed";'
                f'   msg.className="err";}}'
                f' }}catch(e){{msg.textContent="server unreachable";'
                f'  msg.className="err";}}'
                f'}}'
                f'clearTimeout(timer);'
                f'document.getElementById("tweak").addEventListener("input",e=>{{'
                f' clearTimeout(timer);timer=setTimeout(save,600);}});'
                f'document.getElementById("tweak").addEventListener("change",e=>{{'
                f' clearTimeout(timer);save();}});'
                f'</script>')
        self._send(page_html("layout", body))

    def _preview(self, qs):
        try:
            page = int((qs.get("page") or ["1"])[0])
        except ValueError:
            page = 1
        if page not in (1, 2, 3):
            page = 1
        L = load_layout(_layout_dir)
        args = self._args()
        try:
            board = board_now(args.railway, args.station, args.limit,
                              args.date or None)
            rep = render_page(L, board, page, args)
        except Exception as e:
            rep = f"render error: {e}"
        raw = rep.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        global _board
        if self.path == "/refresh":
            with _lock:
                _board = {"at": 0, "data": []}
            return self._redirect("/?saved")
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/save":
            return self._send("not found", 404)
        want_json = "json" in urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0))
        post = urllib.parse.parse_qs(self.rfile.read(length).decode(),
                                     keep_blank_values=True)
        with _lock:
            ok, err = self._apply_save(post)
            if ok:
                _board = {"at": 0, "data": []}
        if want_json:
            payload = json.dumps({"ok": ok, "error": err}).encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if not ok:
            return self._send(
                page_html("layout", f'<p class="err">{esc(err)}</p>'
                                    f'<p><a href="/">back</a></p>'), 400)
        self._redirect("/?saved")

    def _apply_save(self, post):
        """Apply posted fields to the JSON files. Returns (ok, error)."""
        data = read_layout_files(_layout_dir)
        for key, vals in post.items():
            if "|" not in key:
                continue
            fname, sec, field = key.split("|", 2)
            if fname not in data or sec not in data[fname]:
                continue
            cur = data[fname][sec].get(field, "")
            v = vals[0]
            if isinstance(cur, bool):
                data[fname][sec][field] = (v == "true")
            elif isinstance(cur, int):
                try:
                    data[fname][sec][field] = int(v)
                except ValueError:
                    return False, f"{field} must be a whole number"
            elif isinstance(cur, float):
                try:
                    data[fname][sec][field] = float(v)
                except ValueError:
                    return False, f"{field} must be a number"
            elif (fname == "shared.json" and sec == "colors"
                    and v.startswith("#") and len(v) == 7):
                try:
                    data[fname][sec][field] = [
                        int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)]
                except ValueError:
                    return False, "bad colour"
            else:
                data[fname][sec][field] = v

        # write to temp copies, validate, then swap in
        tmp = _layout_dir + ".tmp"
        os.makedirs(tmp, exist_ok=True)
        for fname, content in data.items():
            with open(os.path.join(tmp, fname), "w") as f:
                f.write(dump_layout(content))
        # fonts live next to the real layout dir, link them in so the
        # validator can resolve filenames
        link = os.path.join(tmp, "fonts")
        try:
            if not os.path.exists(link):
                os.symlink(os.path.join(_layout_dir, "fonts"), link)
        except OSError:
            pass
        try:
            departures.load_layout(tmp)
        except SystemExit as e:
            return False, str(e)
        for fname in data:
            os.replace(os.path.join(tmp, fname),
                       os.path.join(_layout_dir, fname))
        return True, ""

    def _send(self, body, code=200):
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()


def main():
    global _layout_dir
    ap = argparse.ArgumentParser(description="Live layout tweaker")
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--layout-dir", default=os.path.join(THIS_DIR, "layout"))
    ap.add_argument("--railway", default="PR")
    ap.add_argument("--station", default="RWS")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--date", default="2026-10-04")
    ap.add_argument("--layout", default="static", choices=["rotate", "static"])
    ap.add_argument("--led-rows", type=int, default=32)
    ap.add_argument("--led-cols", type=int, default=64)
    ap.add_argument("--led-chain", type=int, default=3)
    ap.add_argument("--led-parallel", type=int, default=1)
    ap.add_argument("--led-gpio-mapping", default="regular")
    ap.add_argument("--led-brightness", type=int, default=100)
    ap.add_argument("--led-pwm-bits", type=int, default=11)
    ap.add_argument("--led-limit-refresh", type=int, default=0)
    ap.add_argument("--led-slowdown-gpio", type=int, default=1)
    ap.add_argument("--led-rgb-sequence", default="RGB")
    ap.add_argument("--led-pixel-mapper", default="")
    ap.add_argument("--led-show-refresh", action="store_true")
    ap.add_argument("--led-no-drop-privs", action="store_true")
    ap.add_argument("--led-no-hardware-pulse", action="store_true")
    ap.add_argument("--led-rp1-rio", type=int, default=0, choices=[0, 1])
    ap.add_argument("--led-pwm-lsb-nanoseconds", type=int, default=130)
    ap.add_argument("--led-pwm-dither-bits", type=int, default=0)
    ap.add_argument("--led-row-addr-type", type=int, default=0)
    ap.add_argument("--led-multiplexing", type=int, default=0)
    ap.add_argument("--led-panel-type", default="")
    ap.add_argument("--led-inverse", action="store_true")
    ap.add_argument("--flip-seconds", type=float, default=3)
    ap.add_argument("--rotate-seconds", type=float, default=5)
    ap.add_argument("--page-seconds", type=float, default=10)
    ap.add_argument("--refresh", type=int, default=20)
    args = ap.parse_args()
    _layout_dir = os.path.abspath(args.layout_dir)
    import departures
    departures.load_layout(_layout_dir)  # fail fast on bad layout
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.board_args = args
    print(f"tweaker on http://localhost:{args.port}  layout={_layout_dir}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
