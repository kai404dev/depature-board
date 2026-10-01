#!/usr/bin/env python3
"""Realtime Trains (next-generation API) data layer for the departures board.

Produces departure dicts in the same shape as the HTRS layer in api.py,
so departures.py can render either source unchanged.

  Base URL   https://data.rtt.io          (namespace gb-nr = Network Rail)
  Auth       Bearer token. A refresh token is exchanged for a short-life
             access token via /api/get_access_token; a long-life access
             token is used as-is (auto-detected).
  Quota      30/min, 750/hr, 9000/day, 30000/week. Calling-at + formation
             come from /gb-nr/service for the lead departure only, cached
             for SERVICE_TTL seconds, so the board costs about one
             location call per refresh plus one service call per 90 s.

Token lookup (read once at startup, before the LED library drops root):
  1. $RTT_TOKEN (or RTT_API_KEY) in the environment, then .env next to this script
  2. rtt_token.txt next to this script (chmod 600, keep out of git)

Pure standard library. No hardware needed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # no tzdata: fall back to the offset the API sent
    LONDON = None

RTT_BASE = "https://data.rtt.io"
THIS_DIR = os.path.abspath(os.path.dirname(__file__))
TOKEN_FILE = os.path.join(THIS_DIR, "rtt_token.txt")

# Flip if the coach diagram on page 3 comes out back to front.
REVERSE_FORMATION = False

# Rail replacement buses: headcode 0B00 (and any bus-mode service).
HIDE_HEADCODES = {"0B00"}
HIDE_MODES = {"BUS", "SCHEDULED_BUS", "REPLACEMENT_BUS"}

STATUS_TEXT = {
    "APPROACHING": "is approaching the station",
    "ARRIVING": "is arriving at the platform",
    "AT_PLATFORM": "is currently on the platform",
    "DEPART_PREPARING": "is preparing to depart",
    "DEPART_READY": "is ready to depart",
    "DEPARTING": "is departing now",
}

SERVICE_TTL = 90    # seconds a /gb-nr/service result is reused
FAIL_TTL = 120      # seconds before retrying a failed service lookup

_S = {
    "token": "",
    "access": None,
    "valid_until": 0.0,
    "direct": False,       # token is a long-life access token
    "blocked_until": 0.0,  # set from Retry-After on HTTP 429
    "window_ok": True,     # False once timeWindow is rejected by the plan
    "svc_cache": {},       # "IDENT:date" -> (fetched_at, service|None)
}


TOKEN_KEYS = ("RTT_TOKEN", "RTT_API_KEY")


def _read_dotenv(path):
    """Tiny .env parser: KEY=value, optional 'export ', quotes, # comments."""
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
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def load_token():
    """Token from the environment, then .env next to this script, then
    rtt_token.txt ('' if none). .env keys: RTT_TOKEN or RTT_API_KEY."""
    for k in TOKEN_KEYS:
        if os.environ.get(k, "").strip():
            return os.environ[k].strip()
    env = _read_dotenv(os.path.join(THIS_DIR, ".env"))
    for k in TOKEN_KEYS:
        if env.get(k, "").strip():
            return env[k].strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    return ""


def set_token(token):
    _S["token"] = (token or "").strip()
    _S["access"] = None
    _S["direct"] = False


# ---------------------------------------------------------------------------
# HTTP + auth
# ---------------------------------------------------------------------------

def _request(path, params=None, bearer="", timeout=10):
    now = time.time()
    if now < _S["blocked_until"]:
        raise RuntimeError(
            f"RTT rate limited, retry in {int(_S['blocked_until'] - now)}s")
    url = RTT_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + bearer,
        "User-Agent": "departure-display/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body.strip() else None  # 204 = none
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = int(e.headers.get("Retry-After", "60"))
            except (TypeError, ValueError):
                wait = 60
            _S["blocked_until"] = time.time() + wait
        raise


def _parse_dt(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hhmm(s):
    """ISO datetime -> 'HH:MM' in UK local time."""
    dt = _parse_dt(s)
    if dt is None:
        return None
    if dt.tzinfo is not None and LONDON is not None:
        dt = dt.astimezone(LONDON)
    return dt.strftime("%H:%M")


def _bearer():
    tok = _S["token"]
    if not tok:
        raise RuntimeError("no RTT token: set $RTT_TOKEN or create "
                           "rtt_token.txt next to departures.py")
    if _S["direct"]:
        return tok
    if _S["access"] and time.time() < _S["valid_until"] - 60:
        return _S["access"]
    try:
        data = _request("/api/get_access_token", bearer=tok) or {}
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            _S["direct"] = True   # not a refresh token: use it as-is
            return tok
        raise
    access = data.get("token")
    if not access:
        _S["direct"] = True
        return tok
    vu = _parse_dt(data.get("validUntil"))
    _S["access"] = access
    _S["valid_until"] = vu.timestamp() if vu else time.time() + 300
    return access


def _get(path, params=None):
    for attempt in (0, 1):
        try:
            return _request(path, params, _bearer())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0 and not _S["direct"]:
                _S["access"] = None   # expired early: fetch a new one
                continue
            if e.code == 401:
                raise RuntimeError("RTT 401: token rejected") from e
            raise


# ---------------------------------------------------------------------------
# Mapping RTT -> board departure dicts
# ---------------------------------------------------------------------------

def _plain_formation(n):
    """n coaches, no class/accessibility markers (RTT gave us none)."""
    return {"cars": [{"first": False, "accessible": False}
                     for _ in range(n)]}


def _to_departure(svc, now):
    tdat = svc.get("temporalData") or {}
    dep = tdat.get("departure") or {}
    meta = svc.get("scheduleMetadata") or {}
    lmeta = svc.get("locationMetadata") or {}
    if not dep or meta.get("inPassengerService") is False:
        return None
    if (meta.get("trainReportingIdentity") in HIDE_HEADCODES
            or meta.get("modeType") in HIDE_MODES):
        return None
    display = tdat.get("displayAs")
    if display not in ("CALL", "STARTS", "CANCELLED", "DIVERTED"):
        return None  # passes, terminates here, or null (= pass)
    if tdat.get("realtimeCallType") == "OPERATIONAL_ONLY":
        return None

    sched_s = dep.get("scheduleAdvertised") or dep.get("scheduleInternal")
    sched = _parse_dt(sched_s)
    hhmm = _hhmm(sched_s)
    if not hhmm:
        return None

    actual = _parse_dt(dep.get("realtimeActual"))
    if actual and now - actual.timestamp() > 60:
        return None  # already gone

    fc = _parse_dt(dep.get("realtimeActual") or dep.get("realtimeForecast")
                   or dep.get("realtimeEstimate"))
    mins = 0
    try:
        if sched and fc:
            mins = int(round((fc - sched).total_seconds() / 60))
        elif isinstance(dep.get("realtimeAdvertisedLateness"), int):
            mins = dep["realtimeAdvertisedLateness"]
    except TypeError:  # naive vs aware mix
        mins = 0
    mins = max(0, mins)

    cancelled = bool(dep.get("isCancelled")) or display in (
        "CANCELLED", "DIVERTED")
    delayed = mins >= 1 and not cancelled

    cancel_reason = delay_reason = None
    for r in svc.get("reasons") or []:
        txt = r.get("shortText")
        if r.get("type") == "CANCEL" and not cancel_reason:
            cancel_reason = txt
        elif r.get("type") == "DELAY" and not delay_reason:
            delay_reason = txt

    pm = lmeta.get("platform") or {}
    platform = str(pm.get("actual") or pm.get("forecast")
                   or pm.get("planned") or "")

    names = [((p.get("location") or {}).get("description"))
             for p in (svc.get("destination") or [])]
    dest = " & ".join(dict.fromkeys(n for n in names if n)) or "?"

    d = {
        "scheduled_time": hhmm,
        "planned_time": hhmm,          # expected_time() adds delay_minutes
        "destination_name": dest,
        "platform": platform,
        "is_cancelled": cancelled,
        "is_delayed": delayed,
        "is_tbc": False,
        "delay_minutes": mins,
        "headcode": meta.get("trainReportingIdentity")
        or meta.get("identity") or "",
        "service_type_name": (meta.get("operator") or {}).get("name", ""),
        "operating_date": meta.get("departureDate"),
        "rtt_identity": meta.get("identity"),
        "cancellation_reason": cancel_reason,
        "delay_reason": delay_reason,
        "calling_at": "",
        "operator": (meta.get("operator") or {}).get("name", ""),
        "note_lines": [],
        "_loc_status": tdat.get("status"),
        "_plat_planned": pm.get("planned"),
        "_alloc_index": lmeta.get("allocationIndex"),
    }
    nv = lmeta.get("numberOfVehicles")
    if isinstance(nv, int) and nv > 0:
        d["formation"] = _plain_formation(nv)
    d["note_lines"] = _notes(d)
    sort_key = (fc or sched).timestamp() if (fc or sched) else 0.0
    return sort_key, d


def _cars_from_alloc(a):
    """Coach list from a NetworkRailAllocation (Know Your Train first,
    falling back to a bare passengerVehicles count)."""
    cars = []
    kyt = a.get("knowYourTrainData") or {}
    for g in kyt.get("data") or []:
        for v in g.get("vehicles") or []:
            if v.get("isPassengerVehicle") is False:
                continue
            fac = {str(x).lower()
                   for x in (v.get("individualFacilities") or [])}
            cars.append({"first": "first" in fac,
                         "accessible": "wheelchair" in fac})
    if not cars:
        n = a.get("passengerVehicles")
        if isinstance(n, int) and n > 0:
            cars = _plain_formation(n)["cars"]
    if REVERSE_FORMATION:
        cars.reverse()
    return cars


def _notes(d):
    """Page-2 info as ONE sentence (the display scrolls it if long):
    'This train is formed of 8 coaches and is currently on the platform.'
    Delay and platform-change sentences are added only when they apply."""
    if d.get("is_cancelled"):
        r = d.get("cancellation_reason")
        return ["This service has been cancelled" + (f": {r}." if r else ".")]
    preds = []
    n = len((d.get("formation") or {}).get("cars") or [])
    if n:
        preds.append(f"is formed of {n} coach" + ("" if n == 1 else "es"))
    st = STATUS_TEXT.get(d.get("_loc_status"))
    if st:
        preds.append(st)
    out = []
    if preds:
        out.append("This train " + " and ".join(preds) + ".")
    if d.get("is_delayed"):
        line = f"It is running {d['delay_minutes']} min late"
        out.append(line + (f": {d['delay_reason']}." if d.get("delay_reason")
                           else "."))
    pp, pl = d.get("_plat_planned"), d.get("platform")
    if pp and pl and str(pp) != str(pl):
        out.append(f"Platform changed from {pp} to {pl}.")
    return [" ".join(out)] if out else []


def _stop_label(l):
    """'Name (10:42)' or 'Name (10:42 exp 10:45)' for one calling point."""
    name = (l.get("location") or {}).get("description")
    if not name:
        return None
    td = l.get("temporalData") or {}
    for key in ("arrival", "departure"):
        t = td.get(key) or {}
        sched = _hhmm(t.get("scheduleAdvertised") or t.get("scheduleInternal"))
        if sched:
            exp = _hhmm(t.get("realtimeActual") or t.get("realtimeForecast")
                        or t.get("realtimeEstimate"))
            if exp and exp != sched:
                return f"{name} ({sched} exp {exp})"
            return f"{name} ({sched})"
    return name


def _apply_service(d, svc, station):
    locs = svc.get("locations") or []
    idx = None
    for i, l in enumerate(locs):
        loc = l.get("location") or {}
        if (station in (loc.get("shortCodes") or [])
                or station in (loc.get("longCodes") or [])):
            idx = i
            break

    stops = []
    if idx is not None:
        for l in locs[idx + 1:]:
            td = l.get("temporalData") or {}
            if td.get("displayAs") not in ("CALL", "TERMINATES"):
                continue
            label = _stop_label(l)
            if label and label not in stops:
                stops.append(label)
    op = ((svc.get("scheduleMetadata") or {}).get("operator") or {}).get(
        "name") or d.get("operator")
    if op:
        d["operator"] = op
    text = ("Calling at: " + ", ".join(stops) + ".") if stops else ""
    if op:
        text = (text + " " if text else "") + (
            f"This service is operated by {op}.")
    d["calling_at"] = text

    alloc = svc.get("allocationData") or []
    if alloc:
        want = d.get("_alloc_index")
        if idx is not None:
            want = (locs[idx].get("locationMetadata") or {}).get(
                "allocationIndex", want)
        a = next((x for x in alloc if x.get("allocationIndex") == want),
                 alloc[0])
        cars = _cars_from_alloc(a)
        if cars:
            d["formation"] = {"cars": cars}
    d["note_lines"] = _notes(d)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_departures(station, limit=3, window=120):
    """Next `limit` passenger departures from `station` (short/long code)."""
    params = {"code": station, "timeWindow": window}
    if not _S["window_ok"]:
        params.pop("timeWindow")
    try:
        data = _get("/gb-nr/location", params)
    except urllib.error.HTTPError as e:
        if e.code in (400, 403) and "timeWindow" in params:
            _S["window_ok"] = False  # plan doesn't allow it: default window
            params.pop("timeWindow")
            data = _get("/gb-nr/location", params)
        else:
            raise
    now = time.time()
    rows = []
    for svc in (data or {}).get("services") or []:
        r = _to_departure(svc, now)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r[0])
    return [d for _, d in rows[:limit]]


def enrich(deps, station):
    """Attach calling_at + formation from /gb-nr/service (cached).
    Only the lead departure is enriched: pages 1-3 only use its calling
    points and formation, and it keeps the quota low."""
    cache = _S["svc_cache"]
    now = time.time()
    for d in deps[:1]:
        ident, date = d.get("rtt_identity"), d.get("operating_date")
        if not ident or not date:
            continue
        key = f"{ident}:{date}"
        hit = cache.get(key)
        ttl = SERVICE_TTL if hit and hit[1] is not None else FAIL_TTL
        if hit and now - hit[0] < ttl:
            svc = hit[1]
        else:
            try:
                data = _get("/gb-nr/service",
                            {"identity": ident, "departureDate": date})
                svc = (data or {}).get("service") or None
            except Exception as e:
                print(f"RTT service fetch failed for {ident}: {e}",
                      file=sys.stderr)
                svc = None
            cache[key] = (now, svc)
            if len(cache) > 40:
                cache.pop(min(cache, key=lambda k: cache[k][0]))
        if svc:
            _apply_service(d, svc, station)
    return deps


def get_departures(station, limit=3):
    return enrich(fetch_departures(station, limit), station)
