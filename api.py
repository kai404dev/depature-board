#!/usr/bin/env python3
"""Data layer for the Peak Rail departures board.

Fetching + formatting of the departures/timetable APIs. Pure standard
library, no hardware needed (importable on a Mac for --mock runs).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

API_BASE = "https://peakraildepartures.com/api/departures/"
TIMETABLE_BASE = "https://peakraildepartures.com/api/timetable/"

THIS_DIR = os.path.abspath(os.path.dirname(__file__))


def find_font(name):
    p = os.path.join(THIS_DIR, "fonts", name)
    if os.path.exists(p):
        return p
    p2 = os.path.join(THIS_DIR, "..", "RGB-Matrix-Px-xx", "example",
                      "Raspberry-Pi", "fonts", name)
    if os.path.exists(p2):
        return p2
    return p  # best guess, error will show if missing


def api_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "departure-display/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_departures(railway="PR", station="RWS", limit=3, date="2026-10-04", timeout=10):
    """Call the Peak Rail departures API. Returns list of departure dicts."""
    params = {"railway": railway, "station": station, "limit": limit}
    if date:  # allow --date "" for live (API defaults to next from now)
        params["date"] = date
    url = API_BASE + "?" + urllib.parse.urlencode(params)
    data = api_get(url, timeout)
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def fetch_timetable(headcode, date, railway, timeout=10):
    """Call the timetable API for one service. Returns the timetable dict."""
    params = {}
    if date:
        params["date"] = date
    if railway:
        params["railway"] = railway
    url = (TIMETABLE_BASE + urllib.parse.quote(headcode) + "/?"
           + urllib.parse.urlencode(params))
    data = api_get(url, timeout)
    return data if isinstance(data, dict) else {}


def calling_at_text(timetable, origin=None):
    """Build 'Calling at A, B, C' from timetable movements.

    Only real stops count (STOP/DEST); pass-through (PASS) and the
    origin movement are skipped.
    """
    stops = []
    for m in timetable.get("movements") or []:
        if m.get("removed"):
            continue
        if m.get("movement_type") not in ("STOP", "DEST"):
            continue
        code = m.get("station") or ""
        if origin and code == origin and m.get("movement_type") != "DEST":
            continue
        name = m.get("station_name") or code
        if name and name not in stops:
            stops.append(name)
    if not stops:
        return ""
    return "Calling at " + ", ".join(stops)


def departure_status(d):
    """Status text: delay / on-time info (no platform)."""
    if d.get("is_cancelled"):
        return "Cancelled"
    if d.get("is_delayed"):
        return "Delayed"
    if d.get("is_tbc"):
        return "TBC"
    return "On time"


def expected_time(d):
    """Expected departure HH:MM for a delayed service.

    Prefers planned_time when it differs from the schedule (amended
    working), otherwise adds the delay onto the scheduled time.
    """
    sched = d.get("scheduled_time") or "??:??"
    planned = d.get("planned_time") or ""
    if planned and planned != sched:
        return planned
    mins = d.get("delay_minutes", 0) or 0
    try:
        h, m = int(sched[0:2]), int(sched[3:5])
        m += mins
        h = (h + m // 60) % 24
        m %= 60
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return sched


def format_departure(d):
    """
    Turn one API record into display strings.
    Returns (time_text, destination, status_text, raw_dict).
    """
    t = d.get("planned_time") or d.get("scheduled_time") or "??:??"
    dest = d.get("destination_name") or d.get("destination") or "?"
    return t, dest, departure_status(d), d


def enrich_with_timetables(departures, railway, fallback_date, timeout=10):
    """Attach 'calling_at' + 'note_lines' to each departure.

    note_lines holds free-text info from the APIs: cancellation /
    delay / amendment reasons plus the timetable's operational notes.
    """
    for d in departures:
        d["calling_at"] = ""
        d["note_lines"] = []
        headcode = d.get("headcode")
        if not headcode:
            continue
        try:
            tt = fetch_timetable(headcode,
                                 d.get("operating_date") or fallback_date,
                                 railway or d.get("railway"),
                                 timeout)
            d["calling_at"] = calling_at_text(
                tt, origin=d.get("origin") or d.get("station"))
            notes = []
            if d.get("is_cancelled") and d.get("cancellation_reason"):
                notes.append(d["cancellation_reason"])
            if d.get("is_delayed") and d.get("delay_reason"):
                notes.append(d["delay_reason"])
            if d.get("is_amended") and d.get("amendment_note"):
                notes.append(d["amendment_note"])
            for n in tt.get("operational_notes") or []:
                if n and str(n) not in notes:
                    notes.append(str(n))
            d["note_lines"] = notes
        except Exception as e:
            print(f"Timetable fetch failed for {headcode}: {e}", file=sys.stderr)
    return departures


def live_status(raw, flip_seconds):
    """Status text, flipping Delayed <-> expected time like real boards."""
    base = departure_status(raw)
    if raw.get("is_delayed") and not raw.get("is_cancelled"):
        if int(time.time() // flip_seconds) % 2 == 1:
            return f"Exp {expected_time(raw)}"
    return base


def format_console(departures):
    out = []
    for d in departures:
        t, dest, status, raw = format_departure(d)
        plat = raw.get("platform") or ""
        right = ((plat + " ") if plat else "") + status
        if raw.get("is_delayed") and not raw.get("is_cancelled"):
            right += f" (Exp {expected_time(raw)})"
        out.append(f"{t} {dest} [{right}]")
        if d.get("calling_at"):
            out.append(f"  {d['calling_at']}")
    return "\n".join(out)


def board_signature(departures):
    """Fingerprint of everything shown, to skip redraws when unchanged."""
    rows = []
    for d in departures:
        t, dest, status, raw = format_departure(d)
        rows.append((t, dest, status, raw.get("platform"),
                     d.get("calling_at", ""),
                     tuple(d.get("note_lines") or []),
                     json.dumps(d.get("formation", {}), sort_keys=True),
                     d.get("coaches"), json.dumps(d.get("units", []))))
    return json.dumps(rows, sort_keys=True)


def car_capacity(c):
    """Load factor 0..1 for one car. Defaults to 10% when the API
    sends nothing. Accepts fraction or percent under various keys."""
    for k in ("capacity", "load", "occupancy", "occupancy_percent",
              "percent", "loading"):
        if not isinstance(c, dict) or c.get(k) is None:
            continue
        try:
            v = float(c[k])
        except (TypeError, ValueError):
            continue
        if v > 1:
            v /= 100.0
        return max(0.0, min(1.0, v))
    return 0.10


def formation_of(dep, default_cars=4):
    """Coach formation for the diagram page.

    Custom API bits (optional) per departure:
      "formation": {"cars": [{"first": true, "accessible": false,
                              "capacity": 0.35}, ...],
                    "label": "4 coaches"}   # label optional
    or simply:
      "coaches": 4

    Car capacity defaults to 10% when absent (fraction or percent).
    Default: 4 cards, first class at the front (first card),
    accessible at the last car. Returns (cars, label).
    """
    f = dep.get("formation") or {}
    if isinstance(f, dict) and isinstance(f.get("cars"), list) and f["cars"]:
        cars = [{"first": bool(c.get("first") or c.get("first_class")),
                 "accessible": bool(c.get("accessible")),
                 "capacity": car_capacity(c)}
                for c in f["cars"] if isinstance(c, dict)]
        if cars:
            n = len(cars)
            label = f.get("label") or f"{n} coach" + ("" if n == 1 else "es")
            return cars, label
    n = dep.get("coaches") if isinstance(dep.get("coaches"), int) else 0
    if not n or n < 1:
        n = default_cars
    cars = [{"first": i == 0, "accessible": i == n - 1,
             "capacity": car_capacity({})} for i in range(n)]
    return cars, f"{n} coach" + ("" if n == 1 else "es")
