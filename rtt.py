#!/usr/bin/env python3
"""Realtime Trains (next-generation API) data layer for the departures board.

Produces departure dicts in the same shape as the HTRS layer in api.py,
so departures.py can render either source unchanged.

  Base URL   https://data.rtt.io          (namespace gb-nr = Network Rail)
  Auth       Bearer token. A refresh token is exchanged for a short-life
             access token via /api/get_access_token; a long-life access
             token is used as-is (auto-detected).
  Quota      30/min, 750/hr, 9000/day, 30000/week.

Debugging:
  Set DEBUG = True below to log RTT requests and every service filtering
  decision to stderr.

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RTT_BASE = "https://data.rtt.io"
THIS_DIR = os.path.abspath(os.path.dirname(__file__))
TOKEN_FILE = os.path.join(THIS_DIR, "rtt_token.txt")

DEBUG = True

REVERSE_FORMATION = False

HIDE_HEADCODES = {"0B00"}

HIDE_MODES = {
    "BUS",
    "SCHEDULED_BUS",
    "REPLACEMENT_BUS",
}

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
# Debugging
# ---------------------------------------------------------------------------

def _debug(message):
    """Write a timestamped debugging message to stderr."""
    if not DEBUG:
        return

    now = datetime.now().astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    print(
        f"[RTT DEBUG {now}] {message}",
        file=sys.stderr,
        flush=True,
    )


def _debug_service(svc, prefix="SERVICE"):
    """Print a compact summary of an RTT service."""

    tdat = svc.get("temporalData") or {}

    display_as = tdat.get("displayAs")

    # PASS services use temporalData.pass rather than
    # temporalData.departure.
    if display_as == "PASS":
        dep = tdat.get("pass") or {}
    else:
        dep = tdat.get("departure") or {}

    meta = svc.get("scheduleMetadata") or {}
    lmeta = svc.get("locationMetadata") or {}

    identity = meta.get("identity") or "?"
    headcode = meta.get("trainReportingIdentity") or "?"

    origin = [
        (x.get("location") or {}).get("description")
        for x in (svc.get("origin") or [])
    ]

    destination = [
        (x.get("location") or {}).get("description")
        for x in (svc.get("destination") or [])
    ]

    _debug(
        f"{prefix} "
        f"identity={identity} "
        f"headcode={headcode} "
        f"origin={origin} "
        f"destination={destination} "
        f"scheduled="
        f"{dep.get('scheduleAdvertised') or dep.get('scheduleInternal')} "
        f"forecast={dep.get('realtimeForecast')} "
        f"estimate={dep.get('realtimeEstimate')} "
        f"actual={dep.get('realtimeActual')} "
        f"cancelled={dep.get('isCancelled')} "
        f"displayAs={display_as} "
        f"callType={tdat.get('realtimeCallType')} "
        f"status={tdat.get('status')} "
        f"platform={lmeta.get('platform')}"
    )


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

                if (
                    not line
                    or line.startswith("#")
                    or "=" not in line
                ):
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
            _debug(
                f"Using RTT token from environment variable {k}"
            )
            return os.environ[k].strip()

    env = _read_dotenv(
        os.path.join(THIS_DIR, ".env")
    )

    for k in TOKEN_KEYS:
        if env.get(k, "").strip():
            _debug(
                f"Using RTT token from .env key {k}"
            )
            return env[k].strip()

    if os.path.exists(TOKEN_FILE):
        _debug(
            f"Using RTT token from {TOKEN_FILE}"
        )

        with open(TOKEN_FILE) as f:
            return f.read().strip()

    _debug("No RTT token found")

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
        remaining = int(
            _S["blocked_until"] - now
        )

        _debug(
            f"RATE LIMITED: refusing request for "
            f"another {remaining}s"
        )

        raise RuntimeError(
            f"RTT rate limited, retry in {remaining}s"
        )

    url = RTT_BASE + path

    if params:
        url += "?" + urllib.parse.urlencode(params)

    _debug(
        f"GET {path} "
        f"params={params}"
    )

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + bearer,
            "User-Agent": "departure-display/1.0",
        },
    )

    started = time.monotonic()

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
        ) as resp:
            body = resp.read()

            elapsed = (
                time.monotonic()
                - started
            )

            _debug(
                f"HTTP {resp.status} "
                f"{path} "
                f"in {elapsed:.3f}s "
                f"bytes={len(body)}"
            )

            if not body.strip():
                return None

            return json.loads(body)

    except urllib.error.HTTPError as e:
        elapsed = (
            time.monotonic()
            - started
        )

        _debug(
            f"HTTP ERROR {e.code} "
            f"{path} "
            f"in {elapsed:.3f}s"
        )

        if e.code == 429:
            try:
                wait = int(
                    e.headers.get(
                        "Retry-After",
                        "60",
                    )
                )
            except (TypeError, ValueError):
                wait = 60

            _S["blocked_until"] = (
                time.time() + wait
            )

            _debug(
                f"RTT rate limit: "
                f"Retry-After={wait}s"
            )

        raise


def _parse_dt(value):
    """Parse an RTT ISO-8601 timestamp into an aware datetime."""

    if not value or not isinstance(value, str):
        return None

    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        # RTT's railway timetable timestamps without an offset are
        # UK local time, not UTC.
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=(
                    LONDON
                    or timezone.utc
                )
            )

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
        and time.time()
        < _S["valid_until"] - 60
    ):
        return _S["access"]

    _debug(
        "Requesting RTT access token"
    )

    try:
        data = _request(
            "/api/get_access_token",
            bearer=tok,
        ) or {}

    except urllib.error.HTTPError as e:
        if e.code in (
            400,
            401,
            403,
            404,
        ):
            _debug(
                "Access-token exchange rejected; "
                "using supplied token directly"
            )

            _S["direct"] = True

            return tok

        raise

    access = data.get("token")

    if not access:
        _debug(
            "RTT access-token response contained no token; "
            "using supplied token directly"
        )

        _S["direct"] = True

        return tok

    vu = _parse_dt(
        data.get("validUntil")
    )

    _S["access"] = access
    _S["valid_until"] = (
        vu.timestamp()
        if vu
        else time.time() + 300
    )

    _debug(
        "RTT access token obtained; "
        f"valid_until={data.get('validUntil')}"
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
                _debug(
                    "RTT access token returned 401; "
                    "refreshing token"
                )

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
    """Return scheduled, expected and actual Unix timestamps."""

    scheduled = (
        _timestamp(
            dep.get("scheduleAdvertised")
        )
        or _timestamp(
            dep.get("scheduleInternal")
        )
    )

    forecast = (
        _timestamp(
            dep.get("realtimeForecast")
        )
        or _timestamp(
            dep.get("realtimeEstimate")
        )
    )

    actual = _timestamp(
        dep.get("realtimeActual")
    )

    expected = (
        forecast
        or scheduled
    )

    return (
        scheduled,
        expected,
        actual,
    )


def _find_station_location(svc, station):
    """Find the RTT location entry for the requested station."""

    if not station:
        return None

    locations = svc.get("locations") or []

    for location in locations:
        loc = location.get("location") or {}

        short_codes = loc.get("shortCodes") or []
        long_codes = loc.get("longCodes") or []

        if (
            station in short_codes
            or station in long_codes
            or loc.get("crs") == station
        ):
            return location

    return None


def _to_departure(svc, now, station=None):
    """Convert one RTT service into a board departure."""

    tdat = svc.get("temporalData") or {}

    display = tdat.get("displayAs")

    is_passing = display == "PASS"

    # THIS IS THE IMPORTANT PART:
    #
    # Normal services:
    #     temporalData.departure
    #
    # Passing services:
    #     temporalData.pass
    #
    # Do not overwrite this later with temporalData.departure.
    if is_passing:
        dep = tdat.get("pass") or {}
    else:
        dep = tdat.get("departure") or {}

    meta = svc.get("scheduleMetadata") or {}
    lmeta = svc.get("locationMetadata") or {}

    identity = meta.get("identity") or "?"
    headcode = meta.get("trainReportingIdentity") or "?"

    if DEBUG:
        _debug_service(
            svc,
            "CHECK",
        )

    # ---------------------------------------------------------------
    # Basic service filtering
    # ---------------------------------------------------------------

    # PASS services are deliberately allowed even when RTT says
    # inPassengerService=False. A passing movement can legitimately
    # be non-passenger while still being something the board should
    # display.
    if (
        meta.get("inPassengerService") is False
        and not is_passing
    ):
        _debug(
            f"REJECT {identity}/{headcode}: "
            "not in passenger service"
        )
        return None

    if (
        meta.get("trainReportingIdentity")
        in HIDE_HEADCODES
        or meta.get("modeType")
        in HIDE_MODES
    ):
        _debug(
            f"REJECT {identity}/{headcode}: "
            "hidden mode/headcode "
            f"mode={meta.get('modeType')}"
        )
        return None

    if display not in (
        "CALL",
        "STARTS",
        "CANCELLED",
        "DIVERTED",
        "PASS",
    ):
        _debug(
            f"REJECT {identity}/{headcode}: "
            f"displayAs={display}"
        )
        return None

    if (
        tdat.get("realtimeCallType")
        == "OPERATIONAL_ONLY"
    ):
        _debug(
            f"REJECT {identity}/{headcode}: "
            "operational-only call"
        )
        return None

    # ---------------------------------------------------------------
    # Find actual station location
    # ---------------------------------------------------------------

    station_location = None

    if station:
        station_location = _find_station_location(
            svc,
            station,
        )

    # For PASS services, RTT may additionally provide station-specific
    # timing information in locations[].temporalData.
    #
    # Use that if present, but temporalData.pass remains the primary
    # fallback because the location response can omit locations.
    station_tdat = {}

    if station_location:
        station_tdat = (
            station_location.get(
                "temporalData"
            )
            or {}
        )

    if is_passing and station_location:
        station_dep = (
            station_tdat.get("departure")
            or {}
        )

        station_arr = (
            station_tdat.get("arrival")
            or {}
        )

        if station_dep:
            dep = station_dep

        elif station_arr:
            dep = station_arr

        _debug(
            f"PASS LOCATION {identity}/{headcode}: "
            f"departure={station_dep or None} "
            f"arrival={station_arr or None}"
        )

    # ---------------------------------------------------------------
    # Departure timing
    # ---------------------------------------------------------------

    sched_s = (
        dep.get("scheduleAdvertised")
        or dep.get("scheduleInternal")
    )

    sched = _parse_dt(sched_s)

    if sched is None:
        _debug(
            f"REJECT {identity}/{headcode}: "
            f"invalid scheduled departure={sched_s!r}"
        )
        return None

    scheduled_ts = sched.timestamp()

    forecast_ts = (
        _timestamp(
            dep.get("realtimeForecast")
        )
        or _timestamp(
            dep.get("realtimeEstimate")
        )
    )

    actual_ts = _timestamp(
        dep.get("realtimeActual")
    )

    expected_ts = (
        forecast_ts
        or scheduled_ts
    )

    cancelled = (
        bool(dep.get("isCancelled"))
        or display in (
            "CANCELLED",
            "DIVERTED",
        )
    )

    now_dt = datetime.fromtimestamp(
        now,
        tz=(
            LONDON
            or timezone.utc
        ),
    )

    scheduled_dt = datetime.fromtimestamp(
        scheduled_ts,
        tz=(
            LONDON
            or timezone.utc
        ),
    )

    expected_dt = datetime.fromtimestamp(
        expected_ts,
        tz=(
            LONDON
            or timezone.utc
        ),
    )

    actual_dt = (
        datetime.fromtimestamp(
            actual_ts,
            tz=(
                LONDON
                or timezone.utc
            ),
        )
        if actual_ts is not None
        else None
    )

    _debug(
        f"TIMES {identity}/{headcode}: "
        f"passing={is_passing} "
        f"now={now_dt.isoformat()} "
        f"scheduled={scheduled_dt.isoformat()} "
        f"expected={expected_dt.isoformat()} "
        f"actual="
        f"{actual_dt.isoformat() if actual_dt else None}"
    )

    # ---------------------------------------------------------------
    # Already departed
    # ---------------------------------------------------------------

    if actual_ts is not None:
        if actual_ts < now - 60:
            _debug(
                f"REJECT {identity}/{headcode}: "
                f"already departed "
                f"{int(now - actual_ts)}s ago"
            )
            return None

        expected_ts = actual_ts

    # ---------------------------------------------------------------
    # Future expected time
    # ---------------------------------------------------------------

    else:
        age = now - expected_ts

        if age > STALE_SERVICE_GRACE:
            _debug(
                f"REJECT {identity}/{headcode}: "
                f"expected departure was "
                f"{int(age)}s ago "
                f"(grace={STALE_SERVICE_GRACE}s)"
            )
            return None

        _debug(
            f"KEEP {identity}/{headcode}: "
            f"expected departure is "
            f"{int(-age)}s from now"
        )

    # ---------------------------------------------------------------
    # Cancelled services
    # ---------------------------------------------------------------

    if cancelled:
        if expected_ts < now:
            _debug(
                f"REJECT {identity}/{headcode}: "
                "cancelled and no longer in the future"
            )
            return None

        _debug(
            f"KEEP {identity}/{headcode}: "
            "cancelled but future"
        )

    # ---------------------------------------------------------------
    # Delay
    # ---------------------------------------------------------------

    fc = (
        _parse_dt(
            dep.get("realtimeActual")
        )
        or _parse_dt(
            dep.get("realtimeForecast")
        )
        or _parse_dt(
            dep.get("realtimeEstimate")
        )
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
                    dep[
                        "realtimeAdvertisedLateness"
                    ]
                )
            )

    except (
        TypeError,
        ValueError,
    ):
        mins = 0

    mins = max(
        0,
        mins,
    )

    delayed = (
        mins >= 1
        and not cancelled
    )

    # ---------------------------------------------------------------
    # Reasons
    # ---------------------------------------------------------------

    cancel_reason = None
    delay_reason = None

    for r in svc.get("reasons") or []:
        txt = r.get("shortText")

        if (
            r.get("type") == "CANCEL"
            and not cancel_reason
        ):
            cancel_reason = txt

        elif (
            r.get("type") == "DELAY"
            and not delay_reason
        ):
            delay_reason = txt

    # ---------------------------------------------------------------
    # Platform
    # ---------------------------------------------------------------

    pm = lmeta.get("platform") or {}

    if station_location:
        station_lmeta = (
            station_location.get(
                "locationMetadata"
            )
            or {}
        )

        station_pm = (
            station_lmeta.get("platform")
            or {}
        )

        if station_pm:
            pm = station_pm

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
        (p.get("location") or {}).get(
            "description"
        )
        for p in (
            svc.get("destination")
            or []
        )
    ]

    dest = (
        " & ".join(
            dict.fromkeys(
                n
                for n in names
                if n
            )
        )
        or "?"
    )

    # ---------------------------------------------------------------
    # Departure object
    # ---------------------------------------------------------------

    station_status = (
        station_tdat.get("status")
        if is_passing and station_location
        else None
    )

    loc_status = (
        station_status
        or tdat.get("status")
    )

    station_alloc_index = None

    if station_location:
        station_alloc_index = (
            station_location.get(
                "locationMetadata"
            )
            or {}
        ).get(
            "allocationIndex"
        )

    d = {
        "scheduled_time": _hhmm(sched_s),
        "planned_time": _hhmm(sched_s),

        "destination_name": dest,
        "platform": platform,

        "is_cancelled": cancelled,
        "is_delayed": delayed,
        "is_tbc": False,

        # This is what departures.py can use to identify
        # passing services.
        "is_passing": is_passing,

        "delay_minutes": mins,

        "headcode": (
            meta.get(
                "trainReportingIdentity"
            )
            or meta.get("identity")
            or ""
        ),

        "service_type_name": (
            meta.get("operator") or {}
        ).get("name", ""),

        "operating_date": meta.get(
            "departureDate"
        ),

        "rtt_identity": meta.get(
            "identity"
        ),

        "cancellation_reason": (
            cancel_reason
        ),

        "delay_reason": (
            delay_reason
        ),

        "calling_at": "",

        "operator": (
            meta.get("operator") or {}
        ).get("name", ""),

        "note_lines": [],

        "_loc_status": loc_status,

        "_plat_planned": pm.get(
            "planned"
        ),

        "_alloc_index": (
            station_alloc_index
            or lmeta.get(
                "allocationIndex"
            )
        ),

        "_scheduled_ts": scheduled_ts,
        "_expected_ts": expected_ts,
        "_actual_ts": actual_ts,
    }

    nv = lmeta.get(
        "numberOfVehicles"
    )

    if isinstance(nv, int) and nv > 0:
        d["formation"] = _plain_formation(nv)

    d["note_lines"] = _notes(d)

    sort_key = expected_ts

    expected_raw = (
        dep.get("realtimeActual")
        or dep.get("realtimeForecast")
        or dep.get("realtimeEstimate")
        or sched_s
    )

    expected_hhmm = _hhmm(
        expected_raw
    )

    _debug(
        f"ACCEPT {identity}/{headcode}: "
        f"passing={is_passing} "
        f"scheduled={_hhmm(sched_s)} "
        f"expected={expected_hhmm} "
        f"delay={mins} "
        f"cancelled={cancelled} "
        f"platform={platform} "
        f"destination={dest}"
    )

    return sort_key, d


# ---------------------------------------------------------------------------
# Formation
# ---------------------------------------------------------------------------

def _cars_from_alloc(a):
    """Coach list from a NetworkRailAllocation."""

    cars = []

    kyt = (
        a.get("knowYourTrainData")
        or {}
    )

    for g in (
        kyt.get("data")
        or []
    ):
        for v in (
            g.get("vehicles")
            or []
        ):
            if (
                v.get(
                    "isPassengerVehicle"
                )
                is False
            ):
                continue

            fac = {
                str(x).lower()
                for x in (
                    v.get(
                        "individualFacilities"
                    )
                    or []
                )
            }

            cars.append({
                "first": (
                    "first" in fac
                ),
                "accessible": (
                    "wheelchair" in fac
                ),
            })

    if not cars:
        n = a.get(
            "passengerVehicles"
        )

        if (
            isinstance(n, int)
            and n > 0
        ):
            cars = _plain_formation(
                n
            )["cars"]

    if REVERSE_FORMATION:
        cars.reverse()

    return cars


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def _notes(d):
    """Page-2 information as one sentence."""

    if d.get("is_cancelled"):
        r = d.get(
            "cancellation_reason"
        )

        return [
            "This service has been cancelled"
            + (
                f": {r}."
                if r
                else "."
            )
        ]

    preds = []

    n = len(
        (
            d.get("formation")
            or {}
        ).get("cars")
        or []
    )

    if n:
        preds.append(
            f"is formed of {n} coach"
            + (
                ""
                if n == 1
                else "es"
            )
        )

    st = STATUS_TEXT.get(
        d.get("_loc_status")
    )

    if st:
        preds.append(st)

    out = []

    if preds:
        if d.get("is_passing"):
            out.append(
                "This train passes the station "
                + " and ".join(preds)
                + "."
            )
        else:
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

    pp = d.get(
        "_plat_planned"
    )

    pl = d.get(
        "platform"
    )

    if (
        pp
        and pl
        and str(pp) != str(pl)
    ):
        out.append(
            f"Platform changed from "
            f"{pp} to {pl}."
        )

    return [
        " ".join(out)
    ] if out else []


# ---------------------------------------------------------------------------
# Service calling points
# ---------------------------------------------------------------------------

def _stop_label(l):
    """'Name (10:42)' or 'Name (10:42 exp 10:45)'."""

    name = (
        l.get("location") or {}
    ).get(
        "description"
    )

    if not name:
        return None

    td = (
        l.get("temporalData")
        or {}
    )

    for key in (
        "arrival",
        "departure",
    ):
        t = td.get(key) or {}

        sched = _hhmm(
            t.get(
                "scheduleAdvertised"
            )
            or t.get(
                "scheduleInternal"
            )
        )

        if sched:
            exp = _hhmm(
                t.get(
                    "realtimeActual"
                )
                or t.get(
                    "realtimeForecast"
                )
                or t.get(
                    "realtimeEstimate"
                )
            )

            if (
                exp
                and exp != sched
            ):
                return (
                    f"{name} "
                    f"({sched} exp {exp})"
                )

            return (
                f"{name} ({sched})"
            )

    return name


def _apply_service(d, svc, station):
    locs = (
        svc.get("locations")
        or []
    )

    idx = None

    for i, l in enumerate(locs):
        loc = (
            l.get("location")
            or {}
        )

        if (
            station
            in (
                loc.get(
                    "shortCodes"
                )
                or []
            )
            or station
            in (
                loc.get(
                    "longCodes"
                )
                or []
            )
            or loc.get("crs") == station
        ):
            idx = i
            break

    stops = []

    if idx is not None:
        for l in locs[idx + 1:]:
            td = (
                l.get("temporalData")
                or {}
            )

            if td.get(
                "displayAs"
            ) not in (
                "CALL",
                "TERMINATES",
            ):
                continue

            label = _stop_label(l)

            if (
                label
                and label not in stops
            ):
                stops.append(label)

    op = (
        (
            svc.get(
                "scheduleMetadata"
            )
            or {}
        ).get("operator") or {}
    ).get(
        "name"
    ) or d.get("operator")

    if op:
        d["operator"] = op

    text = (
        "Calling at: "
        + ", ".join(stops)
        + "."
    ) if stops else ""

    if op:
        text = (
            (text + " ")
            if text
            else ""
        ) + (
            f"This service is operated "
            f"by {op}."
        )

    d["calling_at"] = text

    alloc = (
        svc.get(
            "allocationData"
        )
        or []
    )

    if alloc:
        want = d.get(
            "_alloc_index"
        )

        if idx is not None:
            want = (
                locs[idx].get(
                    "locationMetadata"
                )
                or {}
            ).get(
                "allocationIndex",
                want,
            )

        a = next(
            (
                x
                for x in alloc
                if x.get(
                    "allocationIndex"
                ) == want
            ),
            alloc[0],
        )

        cars = _cars_from_alloc(a)

        if cars:
            d["formation"] = {
                "cars": cars
            }

    d["note_lines"] = _notes(d)

    formation_cars = (
        d.get("formation") or {}
    ).get("cars") or []

    _debug(
        f"ENRICH {d.get('rtt_identity')}/"
        f"{d.get('headcode')}: "
        f"calling_points={len(stops)} "
        f"formation={len(formation_cars)} "
        f"operator={op}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_departures(
    station,
    limit=3,
    window=360,
):
    """Return the next `limit` passenger departures."""

    _debug(
        f"FETCH station={station} "
        f"limit={limit} "
        f"window={window}s"
    )

    params = {
        "code": station,
        "timeWindow": window,
        "stpFilter": "WVSC",
        "detailed": "true",
    }

    if not _S["window_ok"]:
        params.pop(
            "timeWindow"
        )

    try:
        data = _get(
            "/gb-nr/location",
            params,
        )

    except urllib.error.HTTPError as e:
        if (
            e.code in (
                400,
                403,
            )
            and "timeWindow" in params
        ):
            _debug(
                "RTT rejected timeWindow; "
                "retrying without timeWindow"
            )

            _S["window_ok"] = False

            params.pop(
                "timeWindow"
            )

            data = _get(
                "/gb-nr/location",
                params,
            )

        else:
            raise

    services = (
        (data or {}).get(
            "services"
        )
        or []
    )

    _debug(
        f"RTT returned "
        f"{len(services)} services"
    )

    now = time.time()

    now_local = datetime.fromtimestamp(
        now,
        tz=(
            LONDON
            or timezone.utc
        ),
    )

    _debug(
        f"FILTER NOW = "
        f"{now_local.isoformat()}"
    )

    rows = []

    for index, svc in enumerate(
        services,
        start=1,
    ):
        _debug(
            f"--- SERVICE {index}/"
            f"{len(services)} ---"
        )

        result = _to_departure(
            svc,
            now,
            station,
        )

        if result:
            rows.append(result)

    rows.sort(
        key=lambda r: r[0]
    )

    departures = [
        d
        for _, d in rows[:limit]
    ]

    _debug(
        f"AFTER FILTER: "
        f"{len(rows)} valid services"
    )

    for index, d in enumerate(
        departures,
        start=1,
    ):
        expected_hhmm = _hhmm_from_timestamp(
            d.get("_expected_ts")
        )

        _debug(
            f"RESULT {index}: "
            f"{d.get('scheduled_time')} "
            f"{d.get('headcode')} "
            f"{d.get('destination_name')} "
            f"platform={d.get('platform')} "
            f"delay={d.get('delay_minutes')} "
            f"cancelled={d.get('is_cancelled')} "
            f"passing={d.get('is_passing')} "
            f"expected={expected_hhmm}"
        )

    return departures


def _hhmm_from_timestamp(ts):
    """Unix timestamp -> local HH:MM:SS for debug output."""

    if ts is None:
        return None

    try:
        dt = datetime.fromtimestamp(
            ts,
            tz=(
                LONDON
                or timezone.utc
            ),
        )

        return dt.strftime(
            "%H:%M:%S"
        )

    except (
        TypeError,
        ValueError,
        OSError,
    ):
        return None


def enrich(
    deps,
    station,
):
    """Attach calling_at + formation from /gb-nr/service."""

    cache = _S["svc_cache"]
    now = time.time()

    if not deps:
        _debug(
            "ENRICH: no departures to enrich"
        )

        return deps

    for d in deps[:1]:
        ident = d.get(
            "rtt_identity"
        )

        date = d.get(
            "operating_date"
        )

        if not ident or not date:
            _debug(
                "ENRICH: missing identity/date"
            )
            continue

        key = f"{ident}:{date}"

        hit = cache.get(key)

        ttl = (
            SERVICE_TTL
            if (
                hit
                and hit[1] is not None
            )
            else FAIL_TTL
        )

        if (
            hit
            and now - hit[0] < ttl
        ):
            svc = hit[1]

            _debug(
                f"SERVICE CACHE HIT "
                f"{key} "
                f"age={int(now - hit[0])}s "
                f"ttl={ttl}s "
                f"found={svc is not None}"
            )

        else:
            _debug(
                f"SERVICE CACHE MISS "
                f"{key}"
            )

            try:
                data = _get(
                    "/gb-nr/service",
                    {
                        "identity": ident,
                        "departureDate": date,
                    },
                )

                svc = (
                    (data or {}).get(
                        "service"
                    )
                    or None
                )

                _debug(
                    f"SERVICE LOOKUP "
                    f"{key}: "
                    f"found={svc is not None}"
                )

            except Exception as e:
                _debug(
                    f"SERVICE LOOKUP FAILED "
                    f"{key}: {e}"
                )

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
                oldest = min(
                    cache,
                    key=lambda k: cache[k][0],
                )

                del cache[oldest]

        if svc:
            _apply_service(
                d,
                svc,
                station,
            )

    return deps


def get_departures(
    station,
    limit=3,
):
    """Return enriched next departures."""

    _debug(
        f"GET_DEPARTURES "
        f"station={station} "
        f"limit={limit}"
    )

    result = enrich(
        fetch_departures(
            station,
            limit,
        ),
        station,
    )

    _debug(
        f"GET_DEPARTURES COMPLETE "
        f"returned={len(result)}"
    )

    return result
```
