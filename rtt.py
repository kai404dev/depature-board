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
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:
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

SERVICE_TTL = 90
FAIL_TTL = 120

# How far after the scheduled/expected departure a service can remain
# on the board if RTT has not supplied an actual departure.
#
# This is deliberately short. It prevents an old timetable service with
# no realtime report from appearing as the "next" train for hours.
STALE_SERVICE_GRACE = 120

_S = {
    "token": "",
    "access": None,
    "valid_until": 0.0,
    "direct": False,
    "blocked_until": 0.0,
    "window_ok": True,
    "svc_cache": {},
}


TOKEN_KEYS = ("RTT_TOKEN", "RTT_API_KEY")


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

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

                if (
                    len(v) >= 2
                    and v[0] == v[-1]
                    and v[0] in "\"'"
                ):
                    v = v[1:-1]

                out[k.strip()] = v

    except OSError:
        pass

    return out


def load_token():
    """Token from environment, .env, then rtt_token.txt."""
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
    _S["valid_until"] = 0.0
    _S["direct"] = False


# ---------------------------------------------------------------------------
# HTTP + auth
# ---------------------------------------------------------------------------

def _request(path, params=None, bearer="", timeout=10):
    now = time.time()

    if now < _S["blocked_until"]:
        raise RuntimeError(
            f"RTT rate limited, retry in "
            f"{int(_S['blocked_until'] - now)}s"
        )

    url = RTT_BASE + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + bearer,
            "User-Agent": "departure-display/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()

            if not body.strip():
                return None

            return json.loads(body)

    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                wait = int(e.headers.get("Retry-After", "60"))
            except (TypeError, ValueError):
                wait = 60

            _S["blocked_until"] = time.time() + wait

        raise


def _parse_dt(value):
    """Parse an RTT ISO-8601 timestamp into an aware datetime."""
    if not value or not isinstance(value, str):
        return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        # RTT timestamps should be timezone-aware. If one somehow arrives
        # without a timezone, treat it as UTC rather than mixing naive and
        # aware datetimes later.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except ValueError:
        return None


def _timestamp(value):
    """Return an RTT timestamp as Unix seconds, or None."""
    dt = _parse_dt(value)

    if dt is None:
        return None

    return dt.timestamp()


def _hhmm(value):
    """ISO datetime -> HH:MM in UK local time."""
    dt = _parse_dt(value)

    if dt is None:
        return None

    if LONDON is not None:
        dt = dt.astimezone(LONDON)

    return dt.strftime("%H:%M")


def _bearer():
    tok = _S["token"]

    if not tok:
        raise RuntimeError(
            "no RTT token: set $RTT_TOKEN or create "
            "rtt_token.txt next to departures.py"
        )

    if _S["direct"]:
        return tok

    if (
        _S["access"]
        and time.time() < _S["valid_until"] - 60
    ):
        return _S["access"]

    try:
        data = _request(
            "/api/get_access_token",
            bearer=tok,
        ) or {}

    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            _S["direct"] = True
            return tok

        raise

    access = data.get("token")

    if not access:
        _S["direct"] = True
        return tok

    vu = _parse_dt(data.get("validUntil"))

    _S["access"] = access
    _S["valid_until"] = (
        vu.timestamp()
        if vu
        else time.time() + 300
    )

    return access


def _get(path, params=None):
    for attempt in (0, 1):
        try:
            return _request(
                path,
                params,
                _bearer(),
            )

        except urllib.error.HTTPError as e:
            if (
                e.code == 401
                and attempt == 0
                and not _S["direct"]
            ):
                _S["access"] = None
                _S["valid_until"] = 0.0
                continue

            if e.code == 401:
                raise RuntimeError(
                    "RTT 401: token rejected"
                ) from e

            raise


# ---------------------------------------------------------------------------
# Mapping RTT -> board departure dicts
# ---------------------------------------------------------------------------

def _plain_formation(n):
    """n coaches, no class/accessibility markers."""
    return {
        "cars": [
            {
                "first": False,
                "accessible": False,
            }
            for _ in range(n)
        ]
    }


def _departure_times(dep):
    """Return scheduled, expected and actual Unix timestamps.

    RTT can provide several different times:

      scheduleAdvertised
      scheduleInternal
      realtimeForecast
      realtimeEstimate
      realtimeActual

    For deciding whether a train is still relevant, realtimeActual takes
    precedence, then forecast, then estimate, then schedule.
    """

    scheduled = (
        _timestamp(dep.get("scheduleAdvertised"))
        or _timestamp(dep.get("scheduleInternal"))
    )

    forecast = (
        _timestamp(dep.get("realtimeForecast"))
        or _timestamp(dep.get("realtimeEstimate"))
    )

    actual = _timestamp(dep.get("realtimeActual"))

    expected = actual or forecast or scheduled

    return scheduled, expected, actual


def _to_departure(svc, now):
    """Convert one RTT location service into a board departure.

    The important part here is that a service is explicitly checked against
    the current time.

    A timetable service whose scheduled time has passed is allowed through
    only if RTT says it is still expected to depart in the future.

    Once RTT reports an actual departure, the service is removed shortly
    afterwards.
    """

    tdat = svc.get("temporalData") or {}
    dep = tdat.get("departure") or {}
    meta = svc.get("scheduleMetadata") or {}
    lmeta = svc.get("locationMetadata") or {}

    if not dep:
        return None

    if meta.get("inPassengerService") is False:
        return None

    if (
        meta.get("trainReportingIdentity") in HIDE_HEADCODES
        or meta.get("modeType") in HIDE_MODES
    ):
        return None

    display = tdat.get("displayAs")

    if display not in (
        "CALL",
        "STARTS",
        "CANCELLED",
        "DIVERTED",
    ):
        return None

    if tdat.get("realtimeCallType") == "OPERATIONAL_ONLY":
        return None

    sched_s = (
        dep.get("scheduleAdvertised")
        or dep.get("scheduleInternal")
    )

    sched = _parse_dt(sched_s)
    hhmm = _hhmm(sched_s)

    if not sched or not hhmm:
        return None

    scheduled_ts, expected_ts, actual_ts = _departure_times(dep)

    if scheduled_ts is None:
        return None

    cancelled = bool(dep.get("isCancelled")) or display in (
        "CANCELLED",
        "DIVERTED",
    )

    # ---------------------------------------------------------------
    # IMPORTANT: stale/future filtering
    # ---------------------------------------------------------------
    #
    # If RTT has given us an actual departure, the train has gone.
    #
    # If it has not departed but has a realtime forecast/estimate, use
    # that realtime time to decide whether it is still relevant.
    #
    # If there is no realtime prediction, use the scheduled time with
    # a small grace period.
    #
    # This prevents old services from becoming the first result simply
    # because RTT returned them in the location response.
    # ---------------------------------------------------------------

    if actual_ts is not None:
        if actual_ts < now - 60:
            return None

        # If the actual is in the future, that is unusual but should
        # still be treated as the current expected departure.
        expected_ts = actual_ts

    elif expected_ts is not None:
        if expected_ts < now - STALE_SERVICE_GRACE:
            return None

    else:
        if scheduled_ts < now - STALE_SERVICE_GRACE:
            return None

        expected_ts = scheduled_ts

    # Cancelled services should not hang around after their scheduled
    # departure unless RTT has a genuine future realtime movement.
    if cancelled:
        if expected_ts is None or expected_ts < now:
            return None

    # ---------------------------------------------------------------
    # Delay calculation
    # ---------------------------------------------------------------

    fc = (
        _parse_dt(dep.get("realtimeActual"))
        or _parse_dt(dep.get("realtimeForecast"))
        or _parse_dt(dep.get("realtimeEstimate"))
    )

    mins = 0

    try:
        if sched and fc:
            mins = int(
                round(
                    (
                        fc.timestamp()
                        - sched.timestamp()
                    ) / 60
                )
            )

        elif isinstance(
            dep.get("realtimeAdvertisedLateness"),
            (int, float),
        ):
            mins = int(
                round(
                    dep["realtimeAdvertisedLateness"]
                )
            )

    except (TypeError, ValueError):
        mins = 0

    mins = max(0, mins)

    delayed = mins >= 1 and not cancelled

    # ---------------------------------------------------------------
    # Reasons
    # ---------------------------------------------------------------

    cancel_reason = None
    delay_reason = None

    for r in svc.get("reasons") or []:
        txt = r.get("shortText")

        if r.get("type") == "CANCEL" and not cancel_reason:
            cancel_reason = txt

        elif r.get("type") == "DELAY" and not delay_reason:
            delay_reason = txt

    # ---------------------------------------------------------------
    # Platform
    # ---------------------------------------------------------------

    pm = lmeta.get("platform") or {}

    platform = str(
        pm.get("actual")
        or pm.get("forecast")
        or pm.get("planned")
        or ""
    )

    # ---------------------------------------------------------------
    # Destination
    # ---------------------------------------------------------------

    names = [
        (p.get("location") or {}).get("description")
        for p in (svc.get("destination") or [])
    ]

    dest = (
        " & ".join(
            dict.fromkeys(
                n for n in names if n
            )
        )
        or "?"
    )

    # ---------------------------------------------------------------
    # Departure object
    # ---------------------------------------------------------------

    d = {
        "scheduled_time": hhmm,
        "planned_time": hhmm,
        "destination_name": dest,
        "platform": platform,
        "is_cancelled": cancelled,
        "is_delayed": delayed,
        "is_tbc": False,
        "delay_minutes": mins,

        "headcode": (
            meta.get("trainReportingIdentity")
            or meta.get("identity")
            or ""
        ),

        "service_type_name": (
            meta.get("operator") or {}
        ).get("name", ""),

        "operating_date": meta.get("departureDate"),
        "rtt_identity": meta.get("identity"),

        "cancellation_reason": cancel_reason,
        "delay_reason": delay_reason,

        "calling_at": "",

        "operator": (
            meta.get("operator") or {}
        ).get("name", ""),

        "note_lines": [],

        "_loc_status": tdat.get("status"),
        "_plat_planned": pm.get("planned"),
        "_alloc_index": lmeta.get("allocationIndex"),

        # Internal values used for sorting/debugging.
        "_scheduled_ts": scheduled_ts,
        "_expected_ts": expected_ts,
        "_actual_ts": actual_ts,
    }

    nv = lmeta.get("numberOfVehicles")

    if isinstance(nv, int) and nv > 0:
        d["formation"] = _plain_formation(nv)

    d["note_lines"] = _notes(d)

    # Sort by the time the train is actually expected to depart.
    #
    # This is important for delayed trains. For example:
    #
    #   05:20 scheduled, 05:45 expected
    #   05:30 scheduled, 05:35 expected
    #
    # The second train should appear first.
    sort_key = (
        expected_ts
        if expected_ts is not None
        else scheduled_ts
    )

    return sort_key, d


def _cars_from_alloc(a):
    """Coach list from a NetworkRailAllocation."""
    cars = []

    kyt = a.get("knowYourTrainData") or {}

    for g in kyt.get("data") or []:
        for v in g.get("vehicles") or []:
            if v.get("isPassengerVehicle") is False:
                continue

            fac = {
                str(x).lower()
                for x in (
                    v.get("individualFacilities")
                    or []
                )
            }

            cars.append({
                "first": "first" in fac,
                "accessible": "wheelchair" in fac,
            })

    if not cars:
        n = a.get("passengerVehicles")

        if isinstance(n, int) and n > 0:
            cars = _plain_formation(n)["cars"]

    if REVERSE_FORMATION:
        cars.reverse()

    return cars


def _notes(d):
    """Page-2 information as one sentence."""

    if d.get("is_cancelled"):
        r = d.get("cancellation_reason")

        return [
            "This service has been cancelled"
            + (f": {r}." if r else ".")
        ]

    preds = []

    n = len(
        (d.get("formation") or {}).get("cars") or []
    )

    if n:
        preds.append(
            f"is formed of {n} coach"
            + ("" if n == 1 else "es")
        )

    st = STATUS_TEXT.get(
        d.get("_loc_status")
    )

    if st:
        preds.append(st)

    out = []

    if preds:
        out.append(
            "This train "
            + " and ".join(preds)
            + "."
        )

    if d.get("is_delayed"):
        line = (
            f"It is running "
            f"{d['delay_minutes']} min late"
        )

        if d.get("delay_reason"):
            line += (
                f": {d['delay_reason']}."
            )
        else:
            line += "."

        out.append(line)

    pp = d.get("_plat_planned")
    pl = d.get("platform")

    if (
        pp
        and pl
        and str(pp) != str(pl)
    ):
        out.append(
            f"Platform changed from {pp} to {pl}."
        )

    return [
        " ".join(out)
    ] if out else []


# ---------------------------------------------------------------------------
# Calling points / service enrichment
# ---------------------------------------------------------------------------

def _stop_label(l):
    """'Name (10:42)' or 'Name (10:42 exp 10:45)'."""
    name = (
        l.get("location") or {}
    ).get("description")

    if not name:
        return None

    td = l.get("temporalData") or {}

    for key in ("arrival", "departure"):
        t = td.get(key) or {}

        sched = _hhmm(
            t.get("scheduleAdvertised")
            or t.get("scheduleInternal")
        )

        if sched:
            exp = _hhmm(
                t.get("realtimeActual")
                or t.get("realtimeForecast")
                or t.get("realtimeEstimate")
            )

            if exp and exp != sched:
                return (
                    f"{name} "
                    f"({sched} exp {exp})"
                )

            return f"{name} ({sched})"

    return name


def _apply_service(d, svc, station):
    locs = svc.get("locations") or []

    idx = None

    for i, l in enumerate(locs):
        loc = l.get("location") or {}

        if (
            station in (loc.get("shortCodes") or [])
            or station in (loc.get("longCodes") or [])
        ):
            idx = i
            break

    stops = []

    if idx is not None:
        for l in locs[idx + 1:]:
            td = l.get("temporalData") or {}

            if td.get("displayAs") not in (
                "CALL",
                "TERMINATES",
            ):
                continue

            label = _stop_label(l)

            if label and label not in stops:
                stops.append(label)

    op = (
        (
            svc.get("scheduleMetadata") or {}
        ).get("operator") or {}
    ).get("name") or d.get("operator")

    if op:
        d["operator"] = op

    text = (
        "Calling at: "
        + ", ".join(stops)
        + "."
    ) if stops else ""

    if op:
        text = (
            (text + " ") if text else ""
        ) + f"This service is operated by {op}."

    d["calling_at"] = text

    alloc = svc.get("allocationData") or []

    if alloc:
        want = d.get("_alloc_index")

        if idx is not None:
            want = (
                locs[idx].get("locationMetadata") or {}
            ).get(
                "allocationIndex",
                want,
            )

        a = next(
            (
                x for x in alloc
                if x.get("allocationIndex") == want
            ),
            alloc[0],
        )

        cars = _cars_from_alloc(a)

        if cars:
            d["formation"] = {
                "cars": cars
            }

    d["note_lines"] = _notes(d)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_departures(station, limit=3, window=360):
    """Return the next `limit` passenger departures.

    Services are filtered against the current UTC timestamp rather than
    trusting the order returned by RTT.
    """

    params = {
        "code": station,
        "timeWindow": window,
        "stpFilter": "WVSC",
        "detailed": "true",
    }

    if not _S["window_ok"]:
        params.pop("timeWindow")

    try:
        data = _get(
            "/gb-nr/location",
            params,
        )

    except urllib.error.HTTPError as e:
        if (
            e.code in (400, 403)
            and "timeWindow" in params
        ):
            _S["window_ok"] = False
            params.pop("timeWindow")

            data = _get(
                "/gb-nr/location",
                params,
            )
        else:
            raise

    # Use one fixed "now" for the entire response so every service is
    # evaluated against exactly the same instant.
    now = time.time()

    rows = []

    for svc in (
        (data or {}).get("services") or []
    ):
        result = _to_departure(
            svc,
            now,
        )

        if result:
            rows.append(result)

    # Sort by expected realtime departure, falling back to timetable
    # departure where RTT has no prediction.
    rows.sort(
        key=lambda r: r[0]
    )

    departures = [
        d for _, d in rows[:limit]
    ]

    return departures


def enrich(deps, station):
    """Attach calling_at + formation from /gb-nr/service.

    Only the lead departure is enriched. This keeps the RTT API quota low.
    """

    cache = _S["svc_cache"]
    now = time.time()

    for d in deps[:1]:
        ident = d.get("rtt_identity")
        date = d.get("operating_date")

        if not ident or not date:
            continue

        key = f"{ident}:{date}"

        hit = cache.get(key)

        ttl = (
            SERVICE_TTL
            if hit and hit[1] is not None
            else FAIL_TTL
        )

        if (
            hit
            and now - hit[0] < ttl
        ):
            svc = hit[1]

        else:
            try:
                data = _get(
                    "/gb-nr/service",
                    {
                        "identity": ident,
                        "departureDate": date,
                    },
                )

                svc = (
                    (data or {}).get("service")
                    or None
                )

            except Exception as e:
                print(
                    f"RTT service fetch failed "
                    f"for {ident}: {e}",
                    file=sys.stderr,
                )

                svc = None

            cache[key] = (
                now,
                svc,
            )

            if len(cache) > 40:
                cache.pop(
                    min(
                        cache,
                        key=lambda k: cache[k][0],
                    )
                )

        if svc:
            _apply_service(
                d,
                svc,
                station,
            )

    return deps


def get_departures(station, limit=3):
    """Return enriched next departures."""
    return enrich(
        fetch_departures(
            station,
            limit,
        ),
        station,
    )
