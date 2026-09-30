#!/usr/bin/env python3
"""Realtime Trains (next-generation API) data layer for the departures board.

Produces departure dicts in the same shape as the HTRS layer in api.py,
so departures.py can render either source unchanged.

  Base URL   https://data.rtt.io          (namespace gb-nr = Network Rail)
  Auth       Bearer token. A refresh token is exchanged for a short-life
             access token via /api/get_access_token; a long-life access
             token is used as-is (auto-detected).
  Quota      30/min, 750/hr, 9000/day, 30000/week. Calling-at + formation
             come from /gb-nr/service, which is cached per service so the
             board costs roughly one location call per refresh.

Token lookup (read once at startup, before the LED library drops root):
  1. $RTT_TOKEN
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

SERVICE_TTL = 600   # seconds a /gb-nr/service result is reused
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


def load_token():
    """Token from $RTT_TOKEN or rtt_token.txt ('' if neither)."""
    t = os.environ.get("RTT_TOKEN", "").strip()
    if not t and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
    return t


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
    notes = []
    if cancelled and cancel_reason:
        notes.append(cancel_reason)
    if delayed and delay_reason:
        notes.append(delay_reason)

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
        "note_lines": notes,
        "_alloc_index": lmeta.get("allocationIndex"),
    }
    nv = lmeta.get("numberOfVehicles")
    if isinstance(nv, int) and nv > 0:
        d["formation"] = _plain_formation(nv)
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


def _apply_service(d, svc, station):
    locs = svc.get("locations") or []
    idx = None
    for i, l in enumerate(locs):
        loc = l.get("location") or {}
        if (station in (loc.get("shortCodes") or [])
                or station in (loc.get("longCodes") or [])):
            idx = i
            break

    if idx is not None:
        stops = []
        for l in locs[idx + 1:]:
            td = l.get("temporalData") or {}
            if td.get("displayAs") not in ("CALL", "TERMINATES"):
                continue
            name = (l.get("location") or {}).get("description")
            if name and name not in stops:
                stops.append(name)
        if stops:
            d["calling_at"] = "Calling at " + ", ".join(stops)

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
    """Attach calling_at + formation from /gb-nr/service (cached)."""
    cache = _S["svc_cache"]
    now = time.time()
    for d in deps:
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
