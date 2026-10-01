#!/usr/bin/env python3
"""National Rail Darwin (LDBWS) data layer for carriage loadings.

RTT's Know Your Train gives formation + facilities but no seating
availability. Darwin is the only public source of per-coach loadings:
departure boards return formation with per-coach loading 0-100
wherever the train operator feeds it in (e.g. Avanti, CrossCountry).

This module fetches the Darwin departure board for a station, matches
one RTT departure (scheduled time + operator + destination) and merges
the loadings into the departure's formation cars as capacity 0..1,
which is exactly what the page-3 diagram renders.

Access is via the Rail Data Marketplace: subscribe (free) to the
"Live Departure Board" product and use its Consumer key:
  DARWIN_TOKEN=... in the environment, in .env, or in darwin_token.txt
next to this file. Without a key every function here degrades to a
no-op and the board keeps its default loadings.

If your product's API path differs from the default below (see its
"Try it" URL on RDM), set DARWIN_BASE_URL to match.

Pure standard library. No hardware needed.

REST (RDM, api1.raildata.org.uk, x-apikey header):
  GetDepartureBoard/{crs}  basic board; service.formation holds the
                           loadings at this location when known
  GetServiceDetails/{id}   fallback per serviceID
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

THIS_DIR = os.path.abspath(os.path.dirname(__file__))

DEFAULT_BASE_URL = ("https://api1.raildata.org.uk"
                    "/1010-live-departure-board-dep1_2"
                    "/LDBWS/api/20220120")
BASE_KEYS = ("DARWIN_BASE_URL",)

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
    "base_url": "",
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
    """Consumer key from environment, .env, then darwin_token.txt.

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


def set_token(token):
    _D["token"] = (token or "").strip()
    if _D["token"] and not _D["source"]:
        _D["source"] = "set directly"
    if not _D["token"]:
        _D["source"] = ""
    _D["board_cache"] = {}
    _D["details_cache"] = {}


def base_url():
    """RDM API base: DARWIN_BASE_URL or the default product path."""
    if not _D["base_url"]:
        env = _read_dotenv(os.path.join(THIS_DIR, ".env"))
        _D["base_url"] = (_first(os.environ, BASE_KEYS)
                          or _first(env, BASE_KEYS)
                          or DEFAULT_BASE_URL).rstrip("/")
    return _D["base_url"]


def set_base_url(url):
    _D["base_url"] = (url or "").strip().rstrip("/")


def configured():
    return bool(_D["token"])


def token_info():
    """(source, length) of the loaded key for startup diagnostics.

    The value itself is never logged.
    """
    return _D["source"] or "none", len(_D["token"])


def token_warning():
    """Human-readable hint when the loaded key looks wrong, else ''."""
    if not _D["token"]:
        return ("no key: subscribe to Live Departure Board on "
                "raildata.org.uk, then set $DARWIN_TOKEN, .env, "
                "or darwin_token.txt")
    if len(_D["token"]) < 20:
        return ("key looks short for an RDM consumer key -- check the "
                "Specification tab of your Live Departure Board product")
    return ""


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

def _alt_base(url):
    """The other known product path, for 404 fallback."""
    if "live-departure-board-dep1_2" in url:
        return url.replace("live-departure-board-dep1_2",
                           "live-departure-board-dep")
    if "live-departure-board-dep" in url:
        return url.replace("live-departure-board-dep",
                           "live-departure-board-dep1_2")
    return ""


def _get(path, params=None, timeout=12):
    """GET one REST resource, trying the alternate product path on 404.

    Returns parsed JSON. Raises RuntimeError with the status and a
    snippet of the body on failure.
    """
    bases = [base_url()]
    alt = _alt_base(bases[0])
    if alt and alt != bases[0]:
        bases.append(alt)
    last = None
    for base in bases:
        url = base + "/" + path.lstrip("/")
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
            if qs:
                url += "?" + qs
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "x-apikey": _D["token"],
                          "User-Agent": "departure-display/1.0"})
        _debug(f"GET {path} base={base}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            hint = ""
            if e.code == 401:
                hint = (" (check the RDM consumer key, and that "
                        "DARWIN_BASE_URL matches your product's Try-it URL)")
            last = RuntimeError(
                f"Darwin REST {e.code} {e.reason}: {detail}{hint}")
            _debug(f"{path} -> HTTP {e.code} on {base}")
            if e.code == 404 and base != bases[-1]:
                continue  # wrong product path? try the other one
            raise last
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError(f"Darwin returned non-JSON ({e})")
    raise last


# ---------------------------------------------------------------------------
# Boards + details
# ---------------------------------------------------------------------------

def _parse_formation(form):
    """formation dict -> {'coaches': [{number, class, loading}]}.

    loading is 0..100 int or None when Darwin doesn't know it.
    Returns None when no usable coach list is present.
    """
    if not isinstance(form, dict):
        return None
    raw = form.get("coaches")
    if isinstance(raw, dict):
        # some wrappers nest the list, e.g. {"coach": [...]}
        raw = raw.get("coach", raw.get("coaches", []))
    if not isinstance(raw, list):
        return None
    coaches = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        number = c.get("number", c.get("coachNumber", c.get("@number", "")))
        cls = c.get("coachClass", c.get("class", "")) or ""
        loading = c.get("loading")
        if loading is not None:
            try:
                loading = max(0, min(100, int(float(loading))))
            except (TypeError, ValueError):
                loading = None
        coaches.append({
            "number": str(number or "").strip(),
            "class": str(cls or "").strip(),
            "loading": loading,
        })
    if not coaches:
        return None
    return {"coaches": coaches}


def _loc_name(loc):
    if isinstance(loc, dict):
        return loc.get("locationName") or "", (loc.get("crs") or "").upper()
    return "", ""


def _parse_service(svc):
    """One board service dict -> our shape (formation may be None)."""
    origins = svc.get("origin") or []
    dests = svc.get("destination") or []
    dest_name, dest_crs = _loc_name(dests[0]) if dests else ("", "")
    return {
        "service_id": (svc.get("serviceID") or svc.get("serviceId") or ""),
        "std": svc.get("std") or "",
        "etd": svc.get("etd") or "",
        "operator": svc.get("operator") or "",
        "operator_code": svc.get("operatorCode") or "",
        "platform": svc.get("platform") or "",
        "is_cancelled": bool(svc.get("isCancelled")),
        "dest_name": dest_name,
        "dest_crs": dest_crs,
        "formation": _parse_formation(svc.get("formation")),
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
    data = _get(f"GetDepartureBoard/{urllib.parse.quote(crs, safe='')}",
                {"numRows": int(rows), "timeWindow": int(window)})
    if not isinstance(data, dict):
        raise RuntimeError("Darwin returned an unexpected board shape")
    services = [_parse_service(s) for s in
                data.get("trainServices") or []]
    _debug(f"BOARD {crs}: {len(services)} services")
    _D["board_cache"][crs] = (now, services)
    if len(_D["board_cache"]) > 20:
        oldest = min(_D["board_cache"],
                     key=lambda k: _D["board_cache"][k][0])
        del _D["board_cache"][oldest]
    return services


def get_details_formation(service_id):
    """Formation for one board serviceID. Cached DETAILS_TTL."""
    if not service_id:
        return None
    now = time.time()
    hit = _D["details_cache"].get(service_id)
    if hit and now - hit[0] < DETAILS_TTL:
        _debug("DETAILS CACHE HIT")
        return hit[1]
    try:
        data = _get("GetServiceDetails/"
                    f"{urllib.parse.quote(service_id, safe='')}")
    except Exception as e:
        _debug(f"details failed: {e}")
        _D["details_cache"][service_id] = (now, None)
        return None
    formation = _parse_formation((data or {}).get("formation"))
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


def main():
    """Standalone key/board check: python3 darwin.py [CRS].

    Prints where the key came from (never its value), fetches the
    Darwin departure board, and lists per-coach loadings. Exit non-zero
    with the exact error when something fails.
    """
    import argparse
    global DEBUG
    ap = argparse.ArgumentParser(
        description="Test the Darwin (RDM) key and show coach loadings")
    ap.add_argument("crs", nargs="?", default="SOT",
                    help="Station CRS code (default SOT)")
    ap.add_argument("--debug", action="store_true",
                    help="Log requests to stderr")
    args = ap.parse_args()
    DEBUG = args.debug
    set_token(load_token())
    src, nchars = token_info()
    print(f"key: from {src}, {nchars} chars")
    print(f"base: {base_url()}")
    warn = token_warning()
    if warn:
        print(f"WARNING: {warn}")
    if not configured():
        sys.exit("no key to test with")
    try:
        services = get_board(args.crs.strip().upper(), rows=10, window=120)
    except Exception as e:
        sys.exit(f"board fetch failed: {e}")
    print(f"{len(services)} services at {args.crs.strip().upper()}")
    for s in services:
        form = s.get("formation")
        if form:
            detail = ", ".join(
                f"{c['number'] or '?'}:"
                f"{c['loading'] if c['loading'] is not None else '-'}"
                for c in form["coaches"])
        else:
            detail = "no formation"
        print(f"{s['std']} {s['operator']} -> {s['dest_name']} "
              f"plat {s['platform']} [{detail}]")


if __name__ == "__main__":
    main()
