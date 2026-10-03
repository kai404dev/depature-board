#!/usr/bin/env python3
"""Simple web UI to create and edit image programmes (program.py JSON).

Lists programmes, edits file defaults + per-programme route/code/
colour, destinations (add/rename/delete), and screens (image path,
per-screen seconds/fit/colour, reorder). An image browser lists every
PNG under bitmap/ to click-add, with thumbnails. Saves atomically;
the running matrix picks the file up within ~1s (no restart).

  python3 program_editor.py programs/jw.json --port 4050
  # http://localhost:4050

Stdlib only. Never touches the matrix -- it only edits the JSON file.
"""

import argparse
import html
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

import program as model

PROGRAM_FILE = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def program_files():
    """Sorted repo-relative *.json paths under programs/."""
    base = os.path.join(THIS_DIR, "programs")
    try:
        names = sorted(f for f in os.listdir(base)
                       if f.lower().endswith(".json"))
    except OSError:
        return []
    return [os.path.join("programs", f) for f in names]


def available_images():
    """Sorted repo-relative PNG paths under bitmap/ (for click-to-add)."""
    base = os.path.join(THIS_DIR, "bitmap")
    out = []
    for root, _dirs, files in os.walk(base):
        for f in sorted(files):
            if f.lower().endswith(".png"):
                full = os.path.join(root, f)
                out.append(os.path.relpath(full, THIS_DIR))
    return sorted(out)


def read_file():
    with open(PROGRAM_FILE) as f:
        return json.load(f)


def resolve_preview_file(rel):
    """Abspath for a programs/*.json rel path, or None when invalid."""
    if not rel:
        return PROGRAM_FILE
    full = os.path.normpath(os.path.join(THIS_DIR, rel))
    base = os.path.join(THIS_DIR, "programs")
    if not full.startswith(base + os.sep) or \
            not rel.lower().endswith(".json") or \
            not os.path.isfile(full):
        return None
    return full


def encode_png_rgb(W, H, frame):
    """True-colour PNG bytes from an RGB bytearray (stdlib only)."""
    import struct
    import zlib
    rows = [bytes(frame[y * W * 3:(y + 1) * W * 3]) for y in range(H)]

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


def render_preview_png(path, program, dest, index, W, H, dim, fit, t):
    """Render one programme screen as it appears on the panel.

    Returns (png_bytes, meta) or raises SystemExit/ValueError with a
    plain message. Applies the same fit/tint/dim/rainbow pipeline as
    program.py's matrix loop (rainbow phase from t seconds).
    """
    from images import dim_frame, load_frame, rainbow_frame, tint_frame
    data, progs, gRotate, gFit, gColour = model.load_programs_file(path)
    prog = model.resolve_program(data, progs, gRotate, gFit, gColour,
                                 program, dest or None, path)
    screens = prog["screens"]
    if not 0 <= index < len(screens):
        raise ValueError(f"screen {index} out of range "
                         f"(0-{len(screens) - 1})")
    s = dict(screens[index])
    if fit in model.FITS:
        s["fit"] = fit
    base = os.path.dirname(path)
    img_path = s["image"]
    full = img_path if os.path.isabs(img_path) else \
        os.path.normpath(os.path.join(THIS_DIR, img_path))
    if not full.startswith(THIS_DIR + os.sep):
        # absolute program files resolve relative to their own dir
        alt = os.path.normpath(os.path.join(base, img_path))
        if alt.startswith(THIS_DIR + os.sep):
            full = alt
    _sw, _sh, frame = load_frame(full, W, H, s["fit"])
    if s.get("colour") == "rgb":
        frame = rainbow_frame(frame, W, H, t)
    elif isinstance(s.get("colour"), tuple):
        frame = tint_frame(frame, s["colour"])
    frame = dim_frame(frame, dim)
    meta = {"program": prog["name"], "route": prog["route"],
            "destination": s.get("destination"),
            "image": s["image"], "seconds": s["seconds"],
            "fit": s["fit"],
            "colour": model.colour_str(s["colour"])
            if s.get("colour") else "",
            "index": index, "count": len(screens)}
    return encode_png_rgb(W, H, frame), meta


def preview_data(path):
    """JSON-able summary of every programme in a file for /preview."""
    data, progs, gRotate, gFit, gColour = model.load_programs_file(path)
    out = {}
    for name in sorted(progs):
        try:
            prog = model.resolve_program(data, progs, gRotate, gFit,
                                         gColour, name, None, path)
        except SystemExit:
            continue
        raw = progs[name] if isinstance(progs[name], dict) else {}
        try:
            ids = dict(model.destination_ids(raw, name))
        except SystemExit:
            ids = {}
        out[name] = {
            "route": prog["route"],
            "destinations": prog["destinations"],
            "ids": ids,
            "screens": [{
                "destination": s.get("destination"),
                "image": s["image"],
                "seconds": s["seconds"],
                "fit": s["fit"],
                "colour": model.colour_str(s["colour"])
                if s.get("colour") else "",
            } for s in prog["screens"]],
        }
    return out


def validate_data(data):
    """Validate posted programme data. Returns (ok, error, warnings).

    Structure errors reject the save; missing PNG files only warn (the
    user may be staging paths before copying files over).
    """
    warnings = []
    if not isinstance(data, dict):
        return False, "top level must be an object", warnings
    progs = data.get("programs")
    if not isinstance(progs, dict) or not progs:
        return False, "need a 'programs' object with at least one program", warnings
    g_fit = data.get("image_fit", "fit")
    if g_fit not in model.FITS:
        return False, f"image_fit must be one of {model.FITS}", warnings
    try:
        model.parse_colour(data.get("colour", data.get("color")),
                           "<file>", "default colour")
    except SystemExit as e:
        return False, str(e), warnings
    for name, raw in progs.items():
        if not isinstance(raw, dict):
            return False, f"program '{name}': must be an object", warnings
        if not str(raw.get("route", "")).strip():
            return False, f"program '{name}': need a 'route'", warnings
        p_fit = raw.get("image_fit", g_fit)
        if p_fit not in model.FITS:
            return False, f"program '{name}': image_fit must be one of {model.FITS}", warnings
        try:
            model.parse_colour(raw.get("colour", raw.get("color")),
                               name, "route colour")
        except SystemExit as e:
            return False, str(e), warnings
        code = raw.get("code", "")
        if code not in (None, "") and not str(code).strip().isdigit():
            return False, f"program '{name}': code must be digits", warnings
        dests = raw.get("destinations", [])
        if isinstance(dests, dict):
            if not dests:
                return False, f"program '{name}': 'destinations' is empty", warnings
            for dname, entry in dests.items():
                if isinstance(entry, dict):
                    try:
                        model.parse_colour(
                            entry.get("colour", entry.get("color")),
                            name, f"destination '{dname}'")
                    except SystemExit as e:
                        return False, str(e), warnings
                    ident = entry.get("id")
                    if ident is not None and (
                            isinstance(ident, bool) or not isinstance(ident, int)
                            or ident < 0):
                        return False, (f"program '{name}' destination "
                                       f"'{dname}': id must be 0 or more"), warnings
                    lst = entry.get("images", entry.get("screens"))
                else:
                    lst = entry
                if not isinstance(lst, list) or not lst:
                    return False, (f"program '{name}' destination "
                                   f"'{dname}': need a non-empty list of images"), warnings
                for s in lst:
                    err = check_screen(s, name, dname, raw, data)
                    if err:
                        return False, err, warnings
                    warnings += missing_warn(s)
        else:
            if isinstance(dests, str):
                dests = [dests]
            if not isinstance(dests, list) or not [d for d in dests if str(d).strip()]:
                return False, (f"program '{name}': need 'destinations' as a "
                               f"list or {{\"name\": [images...]}}"), warnings
            screens = raw.get("screens")
            if not isinstance(screens, list) or not screens:
                return False, (f"program '{name}': need a non-empty 'screens' "
                               f"list of image paths"), warnings
            for s in screens:
                err = check_screen(s, name, None, raw, data)
                if err:
                    return False, err, warnings
                warnings += missing_warn(s)
    # duplicate destination ids + duplicate codes
    for name, raw in progs.items():
        if isinstance(raw, dict):
            try:
                model.destination_ids(raw, name)
            except SystemExit as e:
                return False, str(e), warnings
    codes = {}
    for name, raw in progs.items():
        c = str(raw.get("code", "") or "").strip()
        if c:
            if c in codes:
                warnings.append(f"duplicate code {c}: {codes[c]} + {name}")
            codes[c] = name
    return True, "", warnings


def check_screen(s, prog, dest, raw, data):
    if isinstance(s, str):
        s = {"image": s}
    if not isinstance(s, dict) or not str(s.get("image", "")).strip():
        at = f"destination '{dest}'" if dest else "screens"
        return (f"program '{prog}' {at}: must be an image path or "
                f"{{\"image\": path, ...}}")
    sec = s.get("seconds", raw.get("rotate_seconds",
                                   data.get("rotate_seconds", 10)))
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return f"program '{prog}': seconds must be a number"
    if sec <= 0:
        return f"program '{prog}': seconds must be positive"
    fit = s.get("fit", raw.get("image_fit",
                               data.get("image_fit", "fit")))
    if fit not in model.FITS:
        return f"program '{prog}': fit must be one of {model.FITS}"
    try:
        model.parse_colour(s.get("colour", s.get("color")), prog, "screen")
    except SystemExit as e:
        return str(e)
    return None


def missing_warn(s):
    p = s.get("image") if isinstance(s, dict) else s
    p = str(p or "").strip()
    if p and not os.path.exists(os.path.join(THIS_DIR, p)):
        return [f"missing file: {p}"]
    return []


def dump_json(content):
    return json.dumps(content, indent=2) + "\n"


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>program editor</title>
<style>
*{box-sizing:border-box}
body{background:#0c0d10;color:#e8e8ea;font-family:Arial,Helvetica,sans-serif;margin:0;padding:16px}
h1{font-size:20px;margin:0}
h2{font-size:15px;color:#ffd27f;margin:18px 0 8px}
h3{font-size:13px;color:#9ab;margin:12px 0 6px}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.top .file{color:#9ab;font-size:13px}
button{background:#22262e;color:#fff;border:1px solid #454b56;border-radius:8px;padding:7px 13px;font-size:13px;cursor:pointer}
button:hover{filter:brightness(1.3)}
button.primary{background:#1d5c2e;border-color:#1d5c2e}
button.danger{background:#5c1d1d;border-color:#5c1d1d}
button.small{padding:3px 9px;font-size:12px}
#msg{min-height:20px;font-size:13px;color:#ffd27f}
#msg.err{color:#ff7b7b}#msg.ok{color:#7bff9e}
.main{display:grid;grid-template-columns:250px 1fr 280px;gap:14px;align-items:start}
.panel{background:#15171c;border:1px solid #2a2e36;border-radius:12px;padding:12px}
.prog{display:block;width:100%;text-align:left;margin:0 0 6px;padding:8px 10px}
.prog.cur{outline:2px solid #ffd27f}
.row{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
label{font-size:12px;color:#9ab;min-width:110px}
input[type=text],input[type=number],select{background:#0e1013;color:#fff;border:1px solid #454b56;border-radius:6px;padding:6px 8px;font-size:13px}
input[type=text]{min-width:0}
.grow{flex:1}
.dest{border:1px solid #2a2e36;border-radius:10px;padding:10px;margin:10px 0}
.dest.cur{border-color:#ffd27f}
.screen{display:flex;gap:6px;align-items:center;margin:6px 0;background:#0e1013;border:1px solid #2a2e36;border-radius:8px;padding:6px}
.screen img{width:132px;height:22px;object-fit:contain;background:#000;border-radius:4px;border:1px solid #333}
.screen img.broken,.imgs img.broken{border-color:#f66;background:repeating-linear-gradient(45deg,#200,#200 6px,#300 6px,#300 12px);cursor:pointer}
.nothumb{width:132px;height:22px;display:inline-flex;align-items:center;justify-content:center;background:#0a0b0d;border:1px dashed #454b56;border-radius:4px;font-size:11px;color:#666;flex:none}
.screen input.path{flex:1;min-width:120px}
.screen input.secs{width:56px}
.screen select{width:76px}
.screen input.col{width:86px}
.imgs img{width:100%;max-height:44px;object-fit:contain;background:#000;border-radius:6px;border:1px solid #333}
.imgs .it{border:1px solid #2a2e36;border-radius:8px;padding:6px;margin:6px 0;font-size:12px}
.imgs .it code{word-break:break-all;color:#9ab}
.imgs .it .row{margin:4px 0 0}
#filter{width:100%}
small{color:#777}
code{color:#ffd27f}
@media (max-width:1000px){.main{grid-template-columns:1fr}}
</style></head><body>
<div class="top">
<h1>program editor</h1><a href="/preview" style="color:#9ab;font-size:13px">preview →</a><select id="filesel" onchange="openFile()" title="program file"></select>
<span class="file" id="file"></span>
<button class="primary" onclick="save()">save</button>
<span id="msg"></span>
</div>
<div class="panel" style="margin-bottom:14px">
<h2 style="margin-top:0">file defaults</h2>
<div class="row"><label>rotate_seconds</label><input type="number" id="f-rot" step="any" style="width:90px">
<label style="min-width:70px">image_fit</label><select id="f-fit"><option>fit</option><option>fill</option><option>stretch</option></select>
<label style="min-width:60px">colour</label><input type="text" id="f-col" list="colours" style="width:110px" placeholder="(inherit)">
<datalist id="colours"><option value="full"></option><option value="RGB"></option><option value="#ff8000"></option></datalist>
<datalist id="imgpaths"></datalist>
</div>
<small>colour: #rrggbb, full (own colours), or RGB (scrolling rainbow). cascades file &rarr; program &rarr; destination &rarr; screen.</small>
</div>
<div class="main">
<div class="panel"><h2 style="margin-top:0">programs</h2><div id="plist"></div>
<div class="row"><input type="text" id="newprog" placeholder="new program name" class="grow"><button onclick="addProgram()">add</button></div>
</div>
<div class="panel" id="editor"><h2 style="margin-top:0">select a program</h2></div>
<div class="panel imgs"><h2 style="margin-top:0">images</h2>
<div class="row"><label>add to</label><select id="imgtarget" class="grow" onchange="renderImgs()"></select></div>
<input type="text" id="filter" placeholder="filter…" oninput="debImgs()">
<div id="ilist"></div></div>
</div>
<script>
let S=null,cur=null,curDest=null,imgs=[];
async function load(){
 const r=await fetch('/api/data');S=await r.json();
 imgs=S.images||[];
 const dl=document.getElementById('imgpaths');
 dl.innerHTML=imgs.map(p=>`<option value="${esc(p)}">`).join('');
 const fs=await (await fetch('/api/files')).json();
 const sel=document.getElementById('filesel');
 sel.innerHTML=(fs.files||[]).map(f=>`<option${f===S.rel?' selected':''}>${esc(f)}</option>`).join('');
 document.getElementById('file').textContent=S.file;
 document.getElementById('f-rot').value=S.data.rotate_seconds??'';
 document.getElementById('f-fit').value=S.data.image_fit||'fit';
 document.getElementById('f-col').value=S.data.colour??S.data.color??'';
 const names=Object.keys(S.data.programs||{});
 if(!names.includes(cur))cur=names[0]||null;
 if(cur&&!(S.data.programs[cur].destinations||{})[curDest])curDest=null;
 renderProgs();renderEditor();renderImgs();
}
function say(t,cls){const m=document.getElementById('msg');m.textContent=t;m.className=cls||'';}
function normDest(prog,d){
 const v=prog.destinations[d];
 if(Array.isArray(v))return{screens:v.map(normScreen),colour:'',id:''};
 const o=v||{};return{colour:o.colour??o.color??'',id:o.id??'',screens:((o.images||o.screens||[]).map(normScreen))};
}
function normScreen(s){if(typeof s==='string')return{image:s,seconds:'',fit:'',colour:''};
 return{image:s.image||'',seconds:s.seconds??'',fit:s.fit||'',colour:s.colour??s.color??''};}
function renderProgs(){
 const box=document.getElementById('plist');box.innerHTML='';
 for(const n of Object.keys(S.data.programs)){
  const b=document.createElement('button');b.className='prog'+(n===cur?' cur':'');
  const r=S.data.programs[n].route||n;
  b.textContent=r===n?n:`${r} (${n})`;  b.onclick=()=>{cur=n;curDest=null;renderProgs();renderEditor();renderImgs();};
  box.appendChild(b);}
}
function renderEditor(){
 const box=document.getElementById('editor');
 if(!cur){box.innerHTML='<h2>select a program</h2>';return;}
 const p=S.data.programs[cur];
 let h=`<h2 style="margin-top:0">program <code>${esc(cur)}</code></h2>
 <div class="row"><label>name</label><input type="text" id="e-name" value="${esc(cur)}" class="grow"><button class="small" onclick="renameProgram()">rename</button></div>
 <div class="row"><label>route</label><input type="text" id="e-route" value="${esc(p.route||'')}" style="width:100px">
 <label style="min-width:50px">code</label><input type="text" id="e-code" value="${esc(p.code||'')}" style="width:90px" placeholder="digits">
 <label style="min-width:60px">colour</label><input type="text" id="e-col" list="colours" value="${esc(p.colour??p.color??'')}" style="width:110px" placeholder="(inherit)"></div>
 <div class="row"><button class="danger small" onclick="delProgram()">delete program</button></div>
 <h3>destinations</h3><div id="dests"></div>
 <div class="row"><input type="text" id="newdest" placeholder="new destination" class="grow"><button onclick="addDest()">add</button></div>`;
 box.innerHTML=h;
 ['e-route','e-code','e-col'].forEach(id=>document.getElementById(id).addEventListener('input',syncProg));
 const db=document.getElementById('dests');
 for(const d of Object.keys(p.destinations||{})){
  const nd=normDest(p,d);
  const div=document.createElement('div');div.className='dest'+(d===curDest?' cur':'');
  div.innerHTML=`<div class="row"><input type="text" value="${esc(d)}" data-d="${esc(d)}" class="dname grow">
  <button class="small" onclick="pickDest('${esc(d)}')">${d===curDest?'editing':'edit'}</button>
  <button class="small danger" onclick="delDest('${esc(d)}')">x</button></div>
  <div class="row"><label>colour</label><input type="text" value="${esc(nd.colour)}" data-d="${esc(d)}" class="dcol" style="width:110px" list="colours" placeholder="(inherit)">
  <label style="min-width:30px">id</label><input type="number" value="${esc(nd.id)}" data-d="${esc(d)}" class="did" style="width:70px" placeholder="auto"></div>
  <div class="scr"></div>
  <div class="row"><button class="small" onclick="addScreen('${esc(d)}')">+ image</button></div>`;
  div.querySelector('.dname').addEventListener('change',ev=>renameDest(d,ev.target.value));
  div.querySelector('.dcol').addEventListener('input',ev=>{setDestObj(d,{colour:ev.target.value});});
  div.querySelector('.did').addEventListener('input',ev=>{setDestObj(d,{id:ev.target.value});});
  const sc=div.querySelector('.scr');
  nd.screens.forEach((s,i)=>{
   const r=document.createElement('div');r.className='screen';
   r.innerHTML=(s.image
    ?`<img loading="lazy" src="${thumbURL(s.image)}" title="${esc(s.image)}">`
    :`<span class="nothumb" title="no image yet">no image</span>`)+
   `<input class="path" type="text" list="imgpaths" value="${esc(s.image)}" placeholder="bitmap/…png" title="image path">`+
   `<input class="secs" type="text" value="${esc(s.seconds)}" placeholder="secs" title="seconds (blank = inherit)">`+
   `<select title="fit (blank = inherit)"><option value="">fit*</option><option${s.fit==='fit'?' selected':''}>fit</option><option${s.fit==='fill'?' selected':''}>fill</option><option${s.fit==='stretch'?' selected':''}>stretch</option></select>`+
   `<input class="col" type="text" value="${esc(s.colour)}" placeholder="colour" list="colours" title="colour (blank = inherit)">`+
   `<button class="small" title="up">↑</button><button class="small" title="down">↓</button><button class="small danger" title="delete">x</button>`;
   const [img,path,secs,fit,col,up,dn,del]=r.children;
   if(s.image)armThumb(img,s.image);
   path.addEventListener('input',()=>{setScreen(d,i,{image:path.value});
    let im=r.querySelector('img'),ph=r.querySelector('.nothumb');
    if(path.value){
     if(!im){im=document.createElement('img');im.setAttribute('loading','lazy');
      ph.replaceWith(im);ph=null;}
     im.classList.remove('broken');im.title=path.value;armThumb(im,path.value);im.src=thumbURL(path.value);
    }else if(im){ph=document.createElement('span');ph.className='nothumb';
     ph.title='no image yet';ph.textContent='no image';im.replaceWith(ph);}
    });
   secs.addEventListener('input',()=>setScreen(d,i,{seconds:secs.value}));
   fit.addEventListener('change',()=>setScreen(d,i,{fit:fit.value}));
   col.addEventListener('input',()=>setScreen(d,i,{colour:col.value}));
   up.onclick=()=>moveScreen(d,i,-1);dn.onclick=()=>moveScreen(d,i,1);del.onclick=()=>delScreen(d,i);
   sc.appendChild(r);});
  db.appendChild(div);}
 const nd2=document.getElementById('newdest');
 if(nd2)nd2.addEventListener('keydown',e=>{if(e.key==='Enter')addDest();});
}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function thumbURL(p){return '/api/img?path='+encodeURIComponent(p);}
function armThumb(img,path){
 // bulk renders cancel in-flight requests; retry a few times before
 // calling it missing, and never hide the path (click retries).
 img.dataset.tries='0';
 img.onerror=()=>{
  const n=+img.dataset.tries+1;img.dataset.tries=String(n);
  if(n<4){setTimeout(()=>{img.src=thumbURL(path)+'&t='+Date.now();},350*n);return;}
  img.classList.add('broken');img.title='missing: '+path+' (click to retry)';
 };
 img.onclick=()=>{if(img.classList.contains('broken')){img.classList.remove('broken');img.dataset.tries='0';img.src=thumbURL(path)+'&t='+Date.now();}};
}
function syncProg(){
 const p=S.data.programs[cur];if(!p)return;
 p.route=document.getElementById('e-route').value;
 const c=document.getElementById('e-code').value;
 if(c)p.code=c;else delete p.code;
 const col=document.getElementById('e-col').value;
 if(col)p.colour=col;else delete p.colour;
}
function getDestRaw(p,d){
 let v=p.destinations[d];
 if(Array.isArray(v)){v={images:v};p.destinations[d]=v;}
 return v;
}
function setDestObj(d,patch){
 const p=S.data.programs[cur],o=getDestRaw(p,d);
 if('colour'in patch){if(patch.colour)o.colour=patch.colour;else delete o.colour;delete o.color;}
 if('id'in patch){const n=patch.id===''?undefined:parseInt(patch.id,10);
  if(n===undefined||isNaN(n))delete o.id;else o.id=n;}
}
function setScreen(d,i,patch){
 const p=S.data.programs[cur],o=getDestRaw(p,d);
 let s=o.images[i];
 if(typeof s==='string'){s={image:s};o.images[i]=s;}
 Object.assign(s,patch);
 if(s.seconds===''||s.seconds==null)delete s.seconds;else if(!isNaN(+s.seconds))s.seconds=+s.seconds;
 if(!s.fit)delete s.fit;
 if(!s.colour)delete s.colour;delete s.color;
}
function pickDest(d){curDest=(curDest===d?null:d);renderEditor();renderImgs();}
function addProgram(){
 const n=(document.getElementById('newprog').value||'').trim();
 if(!n)return say('name a program first','err');
 if(S.data.programs[n])return say('program exists','err');
 S.data.programs[n]={route:n,code:'',destinations:{}};cur=n;curDest=null;say('');
 renderProgs();renderEditor();renderImgs();
}
function renameProgram(){
 const n=(document.getElementById('e-name').value||'').trim();
 if(!n||n===cur)return;
 if(S.data.programs[n])return say('name taken','err');
 S.data.programs[n]=S.data.programs[cur];delete S.data.programs[cur];cur=n;say('');renderProgs();renderEditor();renderImgs();
}
function delProgram(){
 if(!confirm(`delete program ${cur}?`))return;
 delete S.data.programs[cur];cur=Object.keys(S.data.programs)[0]||null;curDest=null;
 renderProgs();renderEditor();renderImgs();
}
function addDest(){
 const inp=document.getElementById('newdest'),n=(inp.value||'').trim();
 if(!n)return say('name a destination first','err');
 const p=S.data.programs[cur];
 if(p.destinations[n])return say('destination exists','err');
 p.destinations[n]=[];curDest=n;say('');renderEditor();renderImgs();
}
function renameDest(old,n){
 n=(n||'').trim();if(!n||n===old)return renderEditor();
 const p=S.data.programs[cur];
 if(p.destinations[n]){say('name taken','err');return renderEditor();}
 p.destinations[n]=p.destinations[old];delete p.destinations[old];
 if(curDest===old)curDest=n;say('');renderEditor();renderImgs();
}
function delDest(d){
 if(!confirm(`delete destination ${d}?`))return;
 delete S.data.programs[cur].destinations[d];
 if(curDest===d)curDest=null;renderEditor();renderImgs();
}
function addScreen(d){
 const p=S.data.programs[cur],o=getDestRaw(p,d);
 (o.images||(o.images=[])).push('');
 renderEditor();
}
function delScreen(d,i){getDestRaw(S.data.programs[cur],d).images.splice(i,1);renderEditor();}
function moveScreen(d,i,dir){
 const a=getDestRaw(S.data.programs[cur],d).images,j=i+dir;
 if(j<0||j>=a.length)return;
 [a[i],a[j]]=[a[j],a[i]];renderEditor();
}
let _fT=null;
function debImgs(){clearTimeout(_fT);_fT=setTimeout(renderImgs,250);}
function imgTarget(){
 // live value: the dropdown (if the dest still exists), else the
 // destination being edited, else the first destination.
 const sel=document.getElementById('imgtarget');
 const dests=cur?Object.keys(S.data.programs[cur].destinations||{}):[];
 if(sel&&dests.includes(sel.value))return sel.value;
 if(dests.includes(curDest))return curDest;
 return dests[0]||null;
}
function renderImgs(){
 const q=(document.getElementById('filter').value||'').toLowerCase();
 const box=document.getElementById('ilist');box.innerHTML='';
 const dests=cur?Object.keys(S.data.programs[cur].destinations||{}):[];
 const sel=document.getElementById('imgtarget');
 const keep=sel?sel.value:null;
 sel.innerHTML='';
 if(!dests.length)sel.innerHTML='<option value="">(no destinations)</option>';
 for(const d of dests){
  const o=document.createElement('option');o.value=d;
  o.textContent=(d===curDest?'\u25b8 ':'')+d;
  if(d===(dests.includes(keep)?keep:imgTarget()))o.selected=true;
  sel.appendChild(o);}
 let target=imgTarget();
 imgs.filter(p=>p.toLowerCase().includes(q)).slice(0,120).forEach(p=>{
  const d=document.createElement('div');d.className='it';
  d.innerHTML=`<img loading="lazy"><code>${esc(p)}</code><div class="row"></div>`;
  const im=d.querySelector('img');im.title=p;armThumb(im,p);im.src=thumbURL(p);
  const row=d.querySelector('.row');
  if(!target){row.innerHTML='<small>add a destination first</small>';}
  else{const b=document.createElement('button');b.className='small';b.textContent=`+ ${target}`;
   b.title='adds into '+target;
   b.onclick=()=>{const t=imgTarget();if(!t)return say('pick a destination first','err');
    const o=getDestRaw(S.data.programs[cur],t);(o.images||(o.images=[])).push(p);
    renderEditor();say(`added to ${t}`,'ok');};
   row.appendChild(b);}
  box.appendChild(d);});
 if(imgs.filter(p=>p.toLowerCase().includes(q)).length>120){
  const s=document.createElement('small');s.textContent='… refine the filter (first 120 shown)';box.appendChild(s);}
}
async function openFile(){
 const f=document.getElementById('filesel').value;
 if(!f||f===S.rel)return;
 say('opening '+f+'…');
 try{
  const r=await fetch('/api/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:f})});
  const j=await r.json();
  if(!j.ok)return say(j.error||'open failed','err');
  cur=null;curDest=null;document.getElementById('filter').value='';
  await load();say('','ok');
 }catch(e){say('server unreachable','err');}
}
async function save(){
 syncProg();
 S.data.rotate_seconds=+document.getElementById('f-rot').value||S.data.rotate_seconds;
 S.data.image_fit=document.getElementById('f-fit').value;
 const fc=document.getElementById('f-col').value;
 if(fc)S.data.colour=fc;else{delete S.data.colour;delete S.data.color;}
 // compact: drop empty code/colour keys
 for(const [n,p] of Object.entries(S.data.programs)){
  if(!p.code)delete p.code;
  if(!p.colour)delete p.colour;delete p.color;
  for(const [d,v] of Object.entries(p.destinations||{})){
   if(Array.isArray(v)){p.destinations[d]=v.map(compactScreen);continue;}
   if(v.id===''||v.id==null)delete v.id;
   if(!v.colour)delete v.colour;delete v.color;
   v.images=(v.images||v.screens||[]).map(compactScreen);
   delete v.screens;
   if(!v.colour&&v.id===undefined&&v.images.every(x=>typeof x==='string'))p.destinations[d]=v.images;
  }
 }
 say('saving…');
 try{
  const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data:S.data})});
  const j=await r.json();
  if(j.ok)say('saved'+(j.warnings&&j.warnings.length?' — '+j.warnings.join('; '):''),'ok');
  else say(j.error||'save failed','err');
 }catch(e){say('server unreachable','err');}
 await load();
}
function compactScreen(s){
 if(typeof s==='string')return s;
 if(s.seconds===''||s.seconds==null)delete s.seconds;
 if(!s.fit)delete s.fit;
 if(!s.colour)delete s.colour;delete s.color;
 if(s.seconds===undefined&&!s.fit&&!s.colour&&Object.keys(s).join(',')==='image')return s.image;
 return s;
}
load();
</script></body></html>
"""


PREVIEW_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>program preview</title>
<style>
*{box-sizing:border-box}
body{background:#0c0d10;color:#e8e8ea;font-family:Arial,Helvetica,sans-serif;margin:0;padding:16px}
h1{font-size:20px;margin:0}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.top a{color:#9ab;font-size:13px}
.panel{background:#15171c;border:1px solid #2a2e36;border-radius:12px;padding:12px;margin-bottom:14px}
.row{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
label{font-size:12px;color:#9ab;min-width:90px}
select,input[type=text],input[type=number]{background:#0e1013;color:#fff;border:1px solid #454b56;border-radius:6px;padding:6px 8px;font-size:13px}
input[type=range]{accent-color:#ffd27f}
button{background:#22262e;color:#fff;border:1px solid #454b56;border-radius:8px;padding:7px 13px;font-size:13px;cursor:pointer}
button.small{padding:3px 9px;font-size:12px}
#msg{min-height:20px;font-size:13px;color:#ffd27f}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:#15171c;border:1px solid #2a2e36;border-radius:12px;padding:10px}
.card h2{font-size:14px;margin:0 0 8px;color:#ffd27f}
.card h2 small{color:#9ab;font-weight:normal}
.card img{width:100%;aspect-ratio:6/1;object-fit:contain;background:#000;border-radius:8px;border:1px solid #333;image-rendering:pixelated}
.card .cap{font-size:12px;color:#9ab;margin-top:6px;word-break:break-all;min-height:30px}
.card .pager{display:flex;gap:6px;align-items:center;margin-top:8px;font-size:12px;color:#9ab}
.card .dots{letter-spacing:2px}
small{color:#777}
code{color:#ffd27f}
</style></head><body>
<div class="top">
<h1>program preview</h1>
<a href="/">← back to editor</a>
<span id="msg"></span>
</div>
<div class="panel">
<div class="row"><label>file</label><select id="file"></select></div>
<div class="row"><label>program</label><select id="prog"></select></div>
<div class="row"><label>panel</label><select id="size">
<option value="240x40" selected>240x40 (80x3 chain)</option>
<option value="160x32">160x32</option>
<option value="128x32">128x32</option>
<option value="64x32">64x32</option>
</select>
<label style="min-width:40px">dim</label><input type="range" id="dim" min="1" max="100" value="100"><span id="dimv">100%</span>
<label style="min-width:40px">fit</label><select id="fit"><option value="">file</option><option>fit</option><option>fill</option><option>stretch</option></select>
<button class="small" id="pp" onclick="togglePlay()">pause</button></div>
<small>One card per destination — playing through its pages like the LEDs do, using each page's own seconds.</small>
</div>
<div class="grid" id="grid"></div>
<script>
let D=null,playing=true,state={};
function say(t,err){const m=document.getElementById('msg');m.textContent=t||'';m.style.color=err?'#ff7b7b':'#ffd27f';}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function dims(){const v=document.getElementById('size').value.split('x');return{w:+v[0],h:+v[1]};}
function togglePlay(){playing=!playing;document.getElementById('pp').textContent=playing?'pause':'play';}
async function init(){
 const fs=await (await fetch('/api/files')).json();
 const sel=document.getElementById('file');
 sel.innerHTML=(fs.files||[]).map(f=>`<option>${esc(f)}</option>`).join('');
 const q=new URLSearchParams(location.search);
 if(q.get('file')&&[...sel.options].some(o=>o.value===q.get('file')))sel.value=q.get('file');
 sel.onchange=()=>load();
 for(const id of ['prog','size','dim','fit'])document.getElementById(id).onchange=()=>render(true);
 document.getElementById('dim').oninput=e=>{document.getElementById('dimv').textContent=e.target.value+'%';};
 await load();
 if(q.get('program')){document.getElementById('prog').value=q.get('program');render(true);}
 setInterval(tick,250);
}
async function load(){
 say('loading…');
 try{
  const r=await fetch('/api/preview-data?file='+encodeURIComponent(document.getElementById('file').value));
  const j=await r.json();
  if(!j.ok)return say(j.error||'load failed',true);
  D=j;render(true);say('');
 }catch(e){say('server unreachable',true);}
}
function pngURL(prog,idx){
 const {w,h}=dims();
 const p=new URLSearchParams({file:document.getElementById('file').value,
  program:prog,i:String(idx),w:String(w),h:String(h),
  dim:document.getElementById('dim').value,
  fit:document.getElementById('fit').value,t:(Date.now()/1000).toString()});
 return '/api/preview.png?'+p.toString();
}
function groupsFor(prog){
 // destination -> [{s,i}] in play order (global screen index kept
 // so /api/preview.png stays in sync with the matrix order).
 const screens=D.programs[prog].screens;
 const groups=new Map();
 screens.forEach((s,i)=>{
  const d=s.destination||'(all)';
  if(!groups.has(d))groups.set(d,[]);
  groups.get(d).push({s,i});
 });
 return groups;
}
function render(force){
 if(!D||!D.ok)return;
 const progs=Object.keys(D.programs);
 const psel=document.getElementById('prog');
 const keep=psel.value;
 psel.innerHTML=progs.map(n=>{const p=D.programs[n];return `<option value="${esc(n)}">${esc(p.route===n?n:p.route+' ('+n+')')}</option>`;}).join('');
 if(progs.includes(keep))psel.value=keep;
 const prog=psel.value||progs[0];
 if(prog)psel.value=prog;
 if(!prog)return;
 const groups=groupsFor(prog);
 const grid=document.getElementById('grid');
 const key=document.getElementById('file').value+'|'+prog+'|'+[...groups].map(([d,pgs])=>d+':'+pgs.length).join(',');
 if(force||grid.dataset.key!==key){
  grid.dataset.key=key;grid.innerHTML='';state={};
  for(const [d,pgs] of groups){
   const firstId=D.programs[prog].ids?.[d];
   const sub=pgs.length>1?` <small>${pgs.length} pages</small>`:(firstId!==undefined?` <small>id ${esc(firstId)}</small>`:'');
   const card=document.createElement('div');card.className='card';card.dataset.dest=d;
   card.innerHTML=`<h2>${esc(d)}${sub}</h2><img alt=""><div class="cap"></div>`
    +(pgs.length>1?`<div class="pager"><button class="small prev">‹</button><span class="dots"></span><span class="pos"></span><button class="small next">›</button></div>`:`<div class="pager"><span class="pos"></span></div>`);
   grid.appendChild(card);
   state[d]={pos:0,nextAt:Date.now()+(pgs[0].s.seconds*1000||0),loaded:-1};
   if(pgs.length>1){
    card.querySelector('.prev').onclick=()=>step(d,-1);
    card.querySelector('.next').onclick=()=>step(d,1);
   }
  }
 }
 tick(true);
}
function step(d,dir){
 const prog=document.getElementById('prog').value;
 const pgs=groupsFor(prog).get(d);
 if(!pgs)return;
 const st=state[d];
 st.pos=(st.pos+dir+pgs.length)%pgs.length;
 st.nextAt=Date.now()+(pgs[st.pos].s.seconds*1000||0);
 paint(true);
}
function paint(force){
 const prog=document.getElementById('prog').value;
 if(!prog||!D.programs[prog])return;
 const groups=groupsFor(prog);
 const {w,h}=dims();
 for(const card of document.getElementById('grid').children){
  const d=card.dataset.dest;
  const pgs=groups.get(d);
  if(!pgs)continue;
  const st=state[d];
  const {s,i}=pgs[st.pos];
  const img=card.querySelector('img');
  if(force||st.loaded!==i)img.src=pngURL(prog,i);
  else if(s.colour==='RGB')img.src=pngURL(prog,i); // rainbow shimmer
  st.loaded=i;
  const dots=card.querySelector('.dots');
  if(dots)dots.textContent=pgs.map((_,k)=>k===st.pos?'●':'○').join('');
  card.querySelector('.pos').textContent=pgs.length>1?`page ${st.pos+1}/${pgs.length} · ${s.seconds}s`:`${s.seconds}s`;
  card.querySelector('.cap').innerHTML=`${esc(s.image)}<br><code>${esc(s.fit)}${s.colour?' '+esc(s.colour):''}</code> ${w}x${h}`;
 }
}
function tick(force){
 if(!D||!D.ok)return;
 const prog=document.getElementById('prog').value;
 if(!prog||!D.programs[prog])return;
 if(!playing&&!force)return;
 const now=Date.now();
 const groups=groupsFor(prog);
 let moved=!!force;
 for(const [d,pgs] of groups){
  const st=state[d];
  if(!st)continue;
  if(st.loaded<0)moved=true;
  if(pgs.length>1&&now>=st.nextAt){
   st.pos=(st.pos+1)%pgs.length;
   st.nextAt=now+(pgs[st.pos].s.seconds*1000||0);
   moved=true;
  }
  if(pgs[st.pos].s.colour==='RGB')moved=true; // shimmer
 }
 if(moved)paint(force);
}
init();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ProgramEditor/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s %s\n" % (
            __import__("time").strftime("%H:%M:%S"), self.command,
            self.path.split("?")[0]))
        sys.stderr.flush()

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser cancelled (e.g. re-rendered thumbnails); quiet

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return  # cancelled thumbnail; not worth a traceback
        super().handle_error(request, client_address)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            return self._send(PAGE.replace(
                "<title>", f"<!-- {esc(PROGRAM_FILE)} --><title>"))
        if parsed.path == "/preview":
            return self._send(PREVIEW_PAGE.replace(
                "<title>", f"<!-- {esc(PROGRAM_FILE)} --><title>"))
        if parsed.path == "/api/preview-data":
            return self._preview_data(
                urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/preview.png":
            return self._preview_png(
                urllib.parse.parse_qs(parsed.query))
        if parsed.path == "/api/data":
            try:
                data = read_file()
            except (OSError, ValueError) as e:
                payload = json.dumps(
                    {"ok": False, "error": str(e)}).encode()
                return self._send(payload, "application/json", 500)
            payload = json.dumps({"file": PROGRAM_FILE,
                                  "rel": os.path.relpath(PROGRAM_FILE,
                                                         THIS_DIR),
                                  "data": data,
                                  "images": available_images()}).encode()
            return self._send(payload, "application/json")
        if parsed.path == "/api/files":
            return self._send(json.dumps(
                {"files": program_files()}).encode(),
                "application/json")
        if parsed.path == "/api/img":
            q = urllib.parse.parse_qs(parsed.query)
            p = (q.get("path") or [""])[0]
            full = os.path.normpath(os.path.join(THIS_DIR, p))
            if not full.startswith(THIS_DIR + os.sep) or \
                    not p.lower().endswith(".png") or \
                    not os.path.isfile(full):
                sys.stderr.write("IMG 404 %s\n" % p)
                sys.stderr.flush()
                return self._send("not found", "text/plain", 404)
            try:
                with open(full, "rb") as f:
                    return self._send(f.read(), "image/png")
            except OSError:
                return self._send("not found", "text/plain", 404)
        return self._send("not found", "text/plain", 404)

    def _preview_data(self, q):
        rel = (q.get("file") or [""])[0]
        path = resolve_preview_file(rel)
        if path is None:
            payload = json.dumps(
                {"ok": False,
                 "error": "file must be a programs/*.json file"}).encode()
            return self._send(payload, "application/json", 400)
        try:
            programs = preview_data(path)
        except SystemExit as e:
            payload = json.dumps({"ok": False, "error": str(e)}).encode()
            return self._send(payload, "application/json", 400)
        payload = json.dumps({
            "ok": True,
            "file": path,
            "rel": os.path.relpath(path, THIS_DIR),
            "programs": programs}).encode()
        return self._send(payload, "application/json")

    def _preview_png(self, q):
        rel = (q.get("file") or [""])[0]
        path = resolve_preview_file(rel)
        if path is None:
            return self._send("bad file", "text/plain", 400)
        program = (q.get("program") or [""])[0]
        dest = (q.get("dest") or [""])[0] or None
        try:
            index = int((q.get("i") or q.get("index") or ["0"])[0])
        except ValueError:
            index = 0
        try:
            W = int((q.get("w") or ["240"])[0])
            H = int((q.get("h") or ["40"])[0])
        except ValueError:
            W, H = 240, 40
        W = max(32, min(512, W))
        H = max(16, min(128, H))
        try:
            dim = int((q.get("dim") or ["100"])[0])
        except ValueError:
            dim = 100
        dim = max(1, min(100, dim))
        fit = (q.get("fit") or [""])[0] or None
        if fit not in model.FITS:
            fit = None
        try:
            t = float((q.get("t") or ["0"])[0])
        except ValueError:
            t = 0.0
        try:
            with _lock:
                img, _meta = render_preview_png(
                    path, program or None, dest, index, W, H,
                    dim, fit, t)
        except SystemExit as e:
            return self._send(str(e), "text/plain", 400)
        except (ValueError, OSError) as e:
            return self._send(str(e) or "render failed",
                              "text/plain", 400)
        except Exception as e:  # corrupt PNG etc: plain message
            return self._send(f"render failed: {e}", "text/plain", 500)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(img)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(img)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _open(self):
        """Switch the edited file. Restricted to programs/*.json."""
        global PROGRAM_FILE
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            rel = str(body.get("file", ""))
        except ValueError as e:
            payload = json.dumps({"ok": False,
                                  "error": f"bad JSON: {e}"}).encode()
            return self._send(payload, "application/json", 400)
        full = os.path.normpath(os.path.join(THIS_DIR, rel))
        base = os.path.join(THIS_DIR, "programs")
        if not full.startswith(base + os.sep) or \
                not rel.lower().endswith(".json") or \
                not os.path.isfile(full):
            payload = json.dumps(
                {"ok": False,
                 "error": "file must be a programs/*.json file"}).encode()
            return self._send(payload, "application/json", 400)
        try:
            model.load_programs_file(full)
        except SystemExit as e:
            payload = json.dumps({"ok": False, "error": str(e)}).encode()
            return self._send(payload, "application/json", 400)
        with _lock:
            PROGRAM_FILE = full
        sys.stderr.write("EDITOR open %s\n" % full)
        sys.stderr.flush()
        return self._send(b'{"ok": true}', "application/json")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/open":
            return self._open()
        if parsed.path != "/api/save":
            return self._send("not found", "text/plain", 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            data = body.get("data")
        except ValueError as e:
            payload = json.dumps({"ok": False,
                                  "error": f"bad JSON: {e}"}).encode()
            return self._send(payload, "application/json", 400)
        with _lock:
            ok, err, warnings = validate_data(data)
            if not ok:
                payload = json.dumps({"ok": False, "error": err,
                                      "warnings": warnings}).encode()
                return self._send(payload, "application/json", 400)
            tmp = PROGRAM_FILE + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(dump_json(data))
                # final check: the file we just wrote must load
                model.load_programs_file(tmp)
                os.replace(tmp, PROGRAM_FILE)
            except SystemExit as e:
                payload = json.dumps({"ok": False, "error": str(e),
                                      "warnings": warnings}).encode()
                return self._send(payload, "application/json", 400)
            except OSError as e:
                payload = json.dumps({"ok": False, "error": str(e),
                                      "warnings": warnings}).encode()
                return self._send(payload, "application/json", 500)
        sys.stderr.write("EDITOR save %s (%d programs)%s\n" % (
            PROGRAM_FILE, len(data.get("programs", {})),
            (" warnings: " + "; ".join(warnings)) if warnings else ""))
        sys.stderr.flush()
        payload = json.dumps({"ok": True, "warnings": warnings}).encode()
        return self._send(payload, "application/json")


def main():
    global PROGRAM_FILE
    ap = argparse.ArgumentParser(description="Edit image programmes in a browser")
    ap.add_argument("program_file", nargs="?",
                    default=os.path.join(THIS_DIR, "programs", "jw.json"),
                    help="JSON file to edit (default programs/jw.json)")
    ap.add_argument("--port", type=int, default=4050)
    args = ap.parse_args()
    PROGRAM_FILE = os.path.abspath(args.program_file)
    try:
        model.load_programs_file(PROGRAM_FILE)
    except SystemExit as e:
        sys.exit(str(e))
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"program editor on http://localhost:{args.port}  file={PROGRAM_FILE}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
