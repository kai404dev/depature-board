#!/usr/bin/env python3
"""National Rail Darwin (LDBWS) data layer for carriage loadings.

RTT's Know Your Train gives formation + facilities but no seating
availability. Darwin is the only public source of per-coach loadings:
departure boards return FormationData with per-coach loading 0-100
wherever the train operator feeds it in (e.g. Avanti, CrossCountry).

This module fetches the Darwin departure board for a station, matches
one RTT departure (scheduled time + operator + destination) and merges
the loadings into the departure's formation cars as capacity 0..1,
which is exactly what the page-3 diagram renders.

Needs its own token (free registration at opendata.nationalrail.co.uk):
  DARWIN_TOKEN=... in the environment, in .env, or in darwin_token.txt
next to this file. Without a token every function here degrades to a
no-op and the board keeps its default loadings.

Pure standard library. No hardware needed.

Endpoints (schema 2021-11-01, ldb12.asmx):
  GetDepartureBoard  basic board; ServiceItem.formation holds the
                     loadings at this location when known
  GetServiceDetails  fallback per serviceID; ServiceDetails.formation
"""

import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

THIS_DIR = os.path.abspath(os.path.dirname(__file__))

ENDPOINT = ("https://realtime.nationalrail.co.uk:443/OpenLDBWS/ldb12.asmx")
LDB_NS = "http://thalesgroup.com/RTTI/2021-11-01/ldb/"
TOKEN_NS = "http://thalesgroup.com/RTTI/2013-11-28/Token/types"
ACT_BOARD = ("http://thalesgroup.com/RTTI/2012-01-13/ldb/GetDepartureBoard")
ACT_DETAILS = ("http://thalesgroup.com/RTTI/2012-01-13/ldb/GetServiceDetails")

TOKEN_KEYS = ("DARWIN_TOKEN", "DARWIN_API_TOKEN")

DEBUG = False

BOARD_TTL = 60
DETAILS_TTL = 180

# Repeat Darwin error lines at most this often (enrich runs every fetch).
ERR_REPEAT_SECS = 600

# How far a Darwin std may sit from the RTT scheduled time and still
# count as the same service.
STD_TOLERANCE_MIN = 3

_D = {
    "token": "",
    "source": "",
    "board_cache": {},    # crs -> (at, services or None)
    "details_cache": {},  # service_id -> (at, formation or None)
    "last_err": "",
    "last_err_at": 0.0,
}


def _debug(message):
    if DEBUG:
        print(f"[DARWIN DEBUG {time.strftime('%Y-%m-%d %H:%M:%S')}] "
              f"{message}", file=sys.stderr, flush=True)


def _read_dotenv(path):
    """Tiny .env parser: KEY=value, optional 'export ', quotes, #
    comments (full-line and trailing ' #...')."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k:
                    continue
                if v and v[0] in "\"'":
                    # quoted: take what's inside, ignore the rest
                    # (e.g. TOKEN="abc" # my token)
                    end = v.find(v[0], 1)
                    v = v[1:end] if end != -1 else v[1:]
                else:
                    # trailing comment, e.g. TOKEN=abc # my token
                    if " #" in v:
                        v = v.split(" #", 1)[0].rstrip()
                    v = v.strip()
                out[k] = v
    except OSError:
        pass
    return out


def _first(d, keys):
    for k in keys:
        if d.get(k, "").strip():
            return d[k].strip()
    return ""


def load_token():
    """Token from environment, .env, then darwin_token.txt.

    Remembers where it came from (see token_info). Accepts
    DARWIN_TOKEN or DARWIN_API_TOKEN.
    """
    _D["source"] = ""
    tok = _first(os.environ, TOKEN_KEYS)
    if tok:
        _D["source"] = "environment"
        return tok
    tok = _first(_read_dotenv(os.path.join(THIS_DIR, ".env")), TOKEN_KEYS)
    if tok:
        _D["source"] = ".env"
        return tok
    try:
        with open(os.path.join(THIS_DIR, "darwin_token.txt")) as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    if tok:
        _D["source"] = "darwin_token.txt"
    return tok


def token_info():
    """(source, length) of the loaded token for startup diagnostics.

    The value itself is never logged.
    """
    return _D["source"] or "none", len(_D["token"])


def set_token(token):
    _D["token"] = (token or "").strip()
    if _D["token"] and not _D["source"]:
        _D["source"] = "set directly"
    if not _D["token"]:
        _D["source"] = ""
    _D["board_cache"] = {}
    _D["details_cache"] = {}


def configured():
    return bool(_D["token"])


# ---------------------------------------------------------------------------
# SOAP
# ---------------------------------------------------------------------------

def _local(tag):
    """Strip any {namespace} prefix for version-robust parsing."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _kids(elem):
    return list(elem)


def _find(elem, *names):
    """First direct child with any of the local names, else None."""
    for kid in _kids(elem):
        if _local(kid.tag) in names:
            return kid
    return None


def _findall(elem, *names):
    return [k for k in _kids(elem) if _local(k.tag) in names]


def _text(elem, *names, default=""):
    kid = _find(elem, *names)
    if kid is None or kid.text is None:
        return default
    return kid.text.strip()


def _soap(action, operation, params_xml, timeout=12):
    """POST one SOAP call. Returns the response body element. Raises."""
    token = _D["token"]
    if not token:
        raise RuntimeError("no Darwin token: set $DARWIN_TOKEN or create "
                           "darwin_token.txt next to darwin.py")
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ldb="' + LDB_NS + '" '
        'xmlns:tok="' + TOKEN_NS + '">'
        "<soap:Header>"
        '<tok:AccessToken><tok:TokenValue>' + _xml_escape(token) +
        "</tok:TokenValue></tok:AccessToken>"
        "</soap:Header>"
        "<soap:Body>"
        "<ldb:" + operation + ">" + params_xml +
        "</ldb:" + operation + ">"
        "</soap:Body></soap:Envelope>"
    )
    data = envelope.encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=data,
        headers={"Content-Type": "text/xml; charset=utf-8",
                 "SOAPAction": action,
                 "User-Agent": "departure-display/1.0"})
    _debug(f"POST {operation} bytes={len(data)}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    _debug(f"{operation} -> {len(raw)} bytes")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError(f"Darwin returned non-XML ({e})")
    fault = None
    for elem in root.iter():
        if _local(elem.tag) == "Fault":
            fault = elem
            break
    if fault is not None:
        msg = _text(fault, "faultstring", default="SOAP fault")
        _debug(f"FAULT {operation}: {msg}")
        raise RuntimeError(f"Darwin fault: {msg}")
    return root


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Boards + details
# ---------------------------------------------------------------------------

def _parse_formation(elem):
    """FormationData element -> {'coaches': [{number, class, loading}]}.

    loading is 0..100 int or None when Darwin doesn't know it.
    Returns None when no usable coach list is present.
    """
    if elem is None:
        return None
    coaches_elem = _find(elem, "coaches")
    if coaches_elem is None:
        return None
    coaches = []
    for coach in _findall(coaches_elem, "coach"):
        number = coach.get("number") or ""
        loading = None
        raw = _text(coach, "loading", default="")
        if raw != "":
            try:
                loading = max(0, min(100, int(float(raw))))
            except (TypeError, ValueError):
                loading = None
        coaches.append({
            "number": number.strip(),
            "class": _text(coach, "coachClass", default=""),
            "loading": loading,
        })
    if not coaches:
        return None
    return {"coaches": coaches}


def _parse_service(svc):
    """One board service element -> dict (formation may be None)."""
    dests = _find(svc, "destination")
    dest_name, dest_crs = "", ""
    if dests is not None:
        loc = _find(dests, "location")
        if loc is not None:
            dest_name = _text(loc, "locationName")
            dest_crs = _text(loc, "crs")
    return {
        "service_id": _text(svc, "serviceID"),
        "std": _text(svc, "std"),
        "etd": _text(svc, "etd"),
        "operator": _text(svc, "operator"),
        "operator_code": _text(svc, "operatorCode"),
        "platform": _text(svc, "platform"),
        "is_cancelled": _text(svc, "isCancelled").lower() == "true",
        "dest_name": dest_name,
        "dest_crs": dest_crs.upper(),
        "formation": _parse_formation(_find(svc, "formation")),
    }


def get_board(crs, rows=10, window=120):
    """Darwin departures for a CRS code. Cached BOARD_TTL seconds."""
    crs = (crs or "").strip().upper()
    if not crs:
        return []
    now = time.time()
    hit = _D["board_cache"].get(crs)
    if hit and now - hit[0] < BOARD_TTL and hit[1] is not None:
        _debug(f"BOARD CACHE HIT {crs}")
        return hit[1]
    params = (f"<ldb:numRows>{int(rows)}</ldb:numRows>"
              f"<ldb:crs>{_xml_escape(crs)}</ldb:crs>"
              f"<ldb:timeWindow>{int(window)}</ldb:timeWindow>")
    root = _soap(ACT_BOARD, "GetDepartureBoardRequest", params)
    services = []
    for elem in root.iter():
        if _local(elem.tag) != "service":
            continue
        # service elements also appear nested in departure lists; only
        # trainServices entries carry std/operator -- skip the rest
        if _find(elem, "std") is None and _find(elem, "operator") is None:
            continue
        services.append(_parse_service(elem))
    _debug(f"BOARD {crs}: {len(services)} services")
    _D["board_cache"][crs] = (now, services)
    if len(_D["board_cache"]) > 20:
        oldest = min(_D["board_cache"],
                     key=lambda k: _D["board_cache"][k][0])
        del _D["board_cache"][oldest]
    return services


def get_details_formation(service_id):
    """FormationData for one board serviceID. Cached DETAILS_TTL."""
    if not service_id:
        return None
    now = time.time()
    hit = _D["details_cache"].get(service_id)
    if hit and now - hit[0] < DETAILS_TTL:
        _debug("DETAILS CACHE HIT")
        return hit[1]
    params = (f"<ldb:serviceID>{_xml_escape(service_id)}</ldb:serviceID>")
    try:
        root = _soap(ACT_DETAILS, "GetServiceDetailsRequest", params)
    except Exception as e:
        _debug(f"details failed: {e}")
        _D["details_cache"][service_id] = (now, None)
        return None
    formation = None
    for elem in root.iter():
        if _local(elem.tag) == "GetServiceDetailsResult":
            formation = _parse_formation(_find(elem, "formation"))
            break
    _D["details_cache"][service_id] = (now, formation)
    return formation


# ---------------------------------------------------------------------------
# Matching RTT -> Darwin
# ---------------------------------------------------------------------------

def _hhmm_to_minutes(hhmm):
    try:
        return int(hhmm[0:2]) * 60 + int(hhmm[3:5])
    except (ValueError, IndexError, TypeError):
        return None


def _operator_match(rtt_op, dw_op, dw_code):
    """Operator names/codes agree? e.g. 'Avanti West Coast' vs AV."""
    r = (rtt_op or "").strip().lower()
    o = (dw_op or "").strip().lower()
    c = (dw_code or "").strip().lower()
    if not r or (not o and not c):
        return False
    if r == o or r == c:
        return True
    table = {
        "avanti west coast": ("av", "vt"),
        "crosscountry": ("xc",),
        "east midlands railway": ("em",),
        "west midlands railway": ("wm", "lm"),
        "london northwestern railway": ("ln", "lm"),
        "northern": ("nt",),
        "transpennine express": ("tp",),
        "great western railway": ("gw",),
        "southern": ("sn",),
        "thameslink": ("tl",),
        "great northern": ("gn",),
        "southeastern": ("se",),
        "south western railway": ("sw",),
        "lner": ("gr",),
        "lumo": ("ld",),
        "transport for wales": ("tf", "tw"),
        "scotrail": ("sr", "sc"),
        "merseyrail": ("mr",),
        "c2c": ("cc",),
        "greater anglia": ("ga",),
        "chiltern railways": ("ch",),
    }
    for name, codes in table.items():
        if r == name or r in codes:
            if o == name or o in codes or c == name or c in codes:
                return True
    # last resort: one name contains the other
    if o and (r in o or o in r):
        return True
    return False


def match_service(board, sched_hhmm, operator="", dest_name="",
                  platform=""):
    """Best Darwin board entry for an RTT departure, or None.

    std must sit within STD_TOLERANCE_MIN of scheduled; operator must
    agree; destination/platform break ties.
    """
    want = _hhmm_to_minutes(sched_hhmm)
    if want is None or not board:
        return None
    dest = (dest_name or "").strip().lower()
    plat = (platform or "").strip()
    best, best_score = None, None
    for svc in board:
        if svc.get("is_cancelled"):
            continue
        got = _hhmm_to_minutes(svc.get("std"))
        if got is None:
            continue
        delta = abs(got - want)
        if delta > 12 * 60:
            delta = 24 * 60 - delta  # midnight wrap
        if delta > STD_TOLERANCE_MIN:
            continue
        if not _operator_match(operator, svc.get("operator"),
                               svc.get("operator_code")):
            continue
        score = (delta, 0, 0)
        d = (svc.get("dest_name") or "").strip().lower()
        if dest and d and (dest in d or d in dest):
            score = (delta, -1, score[2])
        if plat and svc.get("platform") == plat:
            score = (delta, score[1], -1)
        if best is None or score < best_score:
            best, best_score = svc, score
    if best is not None:
        _debug(f"MATCH {sched_hhmm} {operator} -> Darwin std="
               f"{best.get('std')} {best.get('operator')} "
               f"{best.get('dest_name')} formation="
               f"{bool(best.get('formation'))}")
    else:
        _debug(f"NO MATCH {sched_hhmm} {operator}")
    return best


# ---------------------------------------------------------------------------
# Loadings -> formation cars
# ---------------------------------------------------------------------------

def _capacity_of(loading):
    if loading is None:
        return None
    try:
        return max(0.0, min(1.0, float(loading) / 100.0))
    except (TypeError, ValueError):
        return None


def apply_formation(dep, formation):
    """Merge Darwin coach loadings into the departure's formation cars.

    Same coach count: keep the existing cars (RTT class/wheelchair
    flags) and just fill in capacities. Different count: rebuild from
    Darwin (class from coachClass, capacity from loading). Returns the
    number of cars given a real loading, 0 when nothing usable.
    """
    coaches = (formation or {}).get("coaches") or []
    if not coaches:
        return 0
    cars = ((dep.get("formation") or {}).get("cars")) or []
    loadings = [_capacity_of(c.get("loading")) for c in coaches]
    known = sum(1 for v in loadings if v is not None)
    if not known:
        return 0
    if cars and len(cars) == len(coaches):
        for car, cap in zip(cars, loadings):
            if cap is not None:
                car["capacity"] = cap
    else:
        cars = []
        for c, cap in zip(coaches, loadings):
            cls = (c.get("class") or "").strip().lower()
            cars.append({
                "first": cls in ("first", "mixed"),
                "accessible": False,
                "capacity": cap if cap is not None else 0.10,
            })
        dep["formation"] = {"cars": cars, "label": _label(len(cars))}
    dep["loading_source"] = "darwin"
    return known


def _label(n):
    return f"{n} coach" + ("" if n == 1 else "es")


def enrich_loading(dep, station_crs):
    """Attach Darwin coach loadings to one RTT departure. Never raises.

    Returns True when real loadings were applied.
    """
    try:
        if not configured():
            return False
        if not isinstance(dep, dict):
            return False
        if dep.get("is_passing") or dep.get("is_cancelled"):
            return False
        station = (station_crs or "").strip().upper()
        if not station:
            return False
        board = get_board(station)
        match = match_service(
            board, dep.get("scheduled_time") or dep.get("planned_time"),
            operator=dep.get("operator") or "",
            dest_name=dep.get("destination_name") or "",
            platform=dep.get("platform") or "")
        if match is None:
            return False
        formation = match.get("formation")
        if formation is None and match.get("service_id"):
            formation = get_details_formation(match["service_id"])
        if apply_formation(dep, formation):
            _debug(f"LOADINGS {dep.get('headcode')}: "
                   f"{len((formation or {}).get('coaches') or [])} coaches")
            return True
        return False
    except Exception as e:
        _debug(f"enrich_loading failed: {e}")
        # Throttled: enrichment runs every fetch, so only repeat a
        # recurring error occasionally (or when it changes).
        now = time.time()
        msg = str(e)
        if msg != _D["last_err"] or now - _D["last_err_at"] > ERR_REPEAT_SECS:
            _D["last_err"], _D["last_err_at"] = msg, now
            print(f"Darwin loading lookup failed: {e}", file=sys.stderr)
        return False
