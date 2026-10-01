#!/usr/bin/env python3
"""Darwin push-port (Kafka) loadings for the departures board.

Consumes the National Rail Darwin firehose via the Rail Data
Marketplace Kafka product (JSON topic) in a background thread and
keeps per-train carriage loadings in memory:

  schedule            rid + headcode (trainId) + TOC + date + calling
                      pattern (TIPLOCs + times)
  scheduleFormations  rid -> static formation (coach numbers + classes)
  formationLoading    rid + station + per-coach loadings 0-100
  serviceLoading      rid + station + whole-train loading % (fallback)

An RTT departure is matched by headcode (+ operating date, TOC as a
sanity check) to a Darwin rid; the latest loadings for that rid feed
the page-3 diagram.

Config (environment first, then .env next to this file):
  DARWIN_KAFKA_BOOTSTRAP  default pkc-z3p1v0...confluent.cloud:9092
  DARWIN_KAFKA_TOPIC      default ...-Push-Port-IIII2_0-JSON
  DARWIN_KAFKA_GROUP      consumer group from the RDM product page
  DARWIN_KAFKA_USER       consumer username (secret)
  DARWIN_KAFKA_PASSWORD   consumer password (secret)

Needs the kafka-python package (pip install kafka-python). Without it,
or without credentials, enrich_loading() is a no-op and the board is
unaffected. Never import kafka at module level, so rtt.py can import
this freely.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

THIS_DIR = os.path.abspath(os.path.dirname(__file__))

DEFAULT_BOOTSTRAP = ("pkc-z3p1v0.europe-west2.gcp.confluent.cloud:9092")
DEFAULT_TOPIC = ("prod-1010-Darwin-Train-Information-Push-Port-IIII2_0-JSON")

DEBUG = False

STATE_FILE = os.path.join(THIS_DIR, "darwin_kafka_state.json")
STATE_SAVE_EVERY_SECS = 120

# Memory hygiene: forget schedules from older operating days and
# loadings not refreshed recently.
SCHEDULE_KEEP_SECS = 36 * 3600
LOADING_KEEP_SECS = 4 * 3600
PRUNE_EVERY_SECS = 300

TOC_CODES = {
    "avanti west coast": ("VT", "AV"),
    "crosscountry": ("XC",),
    "east midlands railway": ("EM",),
    "west midlands railway": ("WM",),
    "london northwestern railway": ("LN",),
    "northern": ("NT",),
    "transpennine express": ("TP",),
    "great western railway": ("GW",),
    "southern": ("SN",),
    "thameslink": ("TL",),
    "gatwick express": ("GX",),
    "great northern": ("GN",),
    "southeastern": ("SE",),
    "south western railway": ("SW",),
    "lner": ("GR",),
    "lumo": ("LD",),
    "transport for wales": ("TF",),
    "scotrail": ("SR",),
    "merseyrail": ("MR",),
    "c2c": ("CC",),
    "greater anglia": ("GA",),
    "chiltern railways": ("CH",),
    "london overground": ("LO",),
}

_K = {
    "thread": None,
    "stop": False,
    "lock": threading.Lock(),
    "schedules": {},    # rid -> {trainId, toc, ssd, ts}
    "formations": {},   # rid -> {ts, coaches: [{number, class}]}
    "loadings": {},     # rid -> {ts, tpl, coaches: [{number, loading}]}
    "svc_loading": {},  # rid -> {ts, tpl, pct}
    "started": False,
    "connected": False,
    "msgs": 0,
    "dirty": False,
    "no_kafka_warned": False,
    "last_err": "",
    "last_err_at": 0.0,
}


def _debug(message):
    if DEBUG:
        print(f"[KAFKA DEBUG {time.strftime('%Y-%m-%d %H:%M:%S')}] "
              f"{message}", file=sys.stderr, flush=True)


def _read_dotenv(path):
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
                    end = v.find(v[0], 1)
                    v = v[1:end] if end != -1 else v[1:]
                else:
                    if " #" in v:
                        v = v.split(" #", 1)[0].rstrip()
                    v = v.strip()
                out[k] = v
    except OSError:
        pass
    return out


def _conf(key, default=""):
    if os.environ.get(key, "").strip():
        return os.environ[key].strip(), "environment"
    env = _read_dotenv(os.path.join(THIS_DIR, ".env"))
    if env.get(key, "").strip():
        return env[key].strip(), ".env"
    return default, "default" if default else "missing"


def config():
    """(dict, sources) of Kafka connection settings, values unstripped."""
    boot, boot_src = _conf("DARWIN_KAFKA_BOOTSTRAP", DEFAULT_BOOTSTRAP)
    topic, topic_src = _conf("DARWIN_KAFKA_TOPIC", DEFAULT_TOPIC)
    group, group_src = _conf("DARWIN_KAFKA_GROUP")
    user, _ = _conf("DARWIN_KAFKA_USER")
    password, _ = _conf("DARWIN_KAFKA_PASSWORD")
    return ({"bootstrap": boot, "topic": topic, "group": group,
             "user": user, "password": password},
            {"bootstrap": boot_src, "topic": topic_src,
             "group": group_src})


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()


def _num(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Message handling (all called with _K["lock"] held)
# ---------------------------------------------------------------------------

def _handle_schedule(ts, sched):
    if not isinstance(sched, dict):
        return
    rid = sched.get("rid")
    if not rid:
        return
    _K["schedules"][str(rid)] = {
        "trainId": (sched.get("trainId") or "").strip().upper(),
        "toc": (sched.get("toc") or "").strip().upper(),
        "ssd": (sched.get("ssd") or "")[:10],
        "ts": ts,
    }
    _K["dirty"] = True


def _handle_schedule_formations(ts, sf):
    if not isinstance(sf, dict):
        return
    rid = sf.get("rid")
    form = sf.get("formation") or {}
    coaches = (form.get("coaches") or {}).get("coach") or []
    if isinstance(coaches, dict):
        coaches = [coaches]
    out = []
    for c in coaches:
        if not isinstance(c, dict):
            continue
        out.append({
            "number": str(c.get("coachNumber", "") or "").strip(),
            "class": str(c.get("coachClass", "") or "").strip(),
        })
    if rid and out:
        _K["formations"][str(rid)] = {"ts": ts, "coaches": out}
        _K["dirty"] = True


def _handle_formation_loading(ts, fl):
    if not isinstance(fl, dict):
        return
    rid = fl.get("rid")
    loadings = fl.get("loading") or []
    if isinstance(loadings, dict):
        loadings = [loadings]
    out = []
    for c in loadings:
        if not isinstance(c, dict):
            continue
        number = c.get("coachNumber", c.get("@coachNumber", ""))
        value = None
        for k, v in c.items():
            if k in ("coachNumber", "@coachNumber"):
                continue
            value = _num(v)
            break
        out.append({"number": str(number or "").strip(),
                    "loading": value})
    if rid and out:
        _K["loadings"][str(rid)] = {
            "ts": ts,
            "tpl": (fl.get("tpl") or "").strip().upper(),
            "coaches": out,
        }
        _K["dirty"] = True


def _handle_service_loading(ts, sl):
    items = sl if isinstance(sl, list) else [sl]
    for entry in items:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("rid")
        pct = _num(entry.get("loadingPercentage"))
        if not rid or pct is None:
            continue
        prev = _K["svc_loading"].get(str(rid))
        if prev and prev["ts"] >= ts:
            continue
        _K["svc_loading"][str(rid)] = {
            "ts": ts,
            "tpl": (entry.get("tpl") or "").strip().upper(),
            "pct": max(0, min(100, pct)),
        }
        _K["dirty"] = True


def _handle_message(obj):
    try:
        inner = obj
        if isinstance(obj, dict) and isinstance(obj.get("bytes"), str):
            inner = json.loads(obj["bytes"])  # RDM portal envelope
        ur = (inner.get("uR") if isinstance(inner, dict) else None) or {}
        ts = _parse_ts(inner.get("ts"))
        if "schedule" in ur:
            _handle_schedule(ts, ur["schedule"])
        if "scheduleFormations" in ur:
            _handle_schedule_formations(ts, ur["scheduleFormations"])
        if "formationLoading" in ur:
            _handle_formation_loading(ts, ur["formationLoading"])
        if "serviceLoading" in ur:
            _handle_service_loading(ts, ur["serviceLoading"])
    except Exception as e:
        _debug(f"parse failed: {e}")


def _prune(now):
    for store, keep in (("schedules", SCHEDULE_KEEP_SECS),
                        ("loadings", LOADING_KEEP_SECS),
                        ("svc_loading", LOADING_KEEP_SECS),
                        ("formations", SCHEDULE_KEEP_SECS)):
        d = _K[store]
        for k in [k for k, v in d.items() if now - v.get("ts", 0) > keep]:
            del d[k]


def _save_state():
    """Persist rid stores so a restart keeps today's schedules."""
    try:
        with _K["lock"]:
            _prune(time.time())
            snapshot = {k: _K[k] for k in ("schedules", "formations",
                                           "loadings", "svc_loading")}
            _K["dirty"] = False
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, STATE_FILE)
        _debug("state saved "
               f"({sum(len(v) for v in snapshot.values())} rids)")
    except Exception as e:
        _debug(f"state save failed: {e}")


def _load_state():
    """Restore rid stores saved by an earlier run (if fresh enough)."""
    try:
        with open(STATE_FILE) as f:
            snapshot = json.load(f)
        if not isinstance(snapshot, dict):
            return
        now = time.time()
        with _K["lock"]:
            for k in ("schedules", "formations",
                      "loadings", "svc_loading"):
                v = snapshot.get(k)
                if isinstance(v, dict):
                    _K[k].update(v)
            _prune(now)
        _debug("state restored "
               f"({sum(len(_K[k]) for k in ('schedules', 'formations', 'loadings', 'svc_loading'))} rids)")
    except (OSError, ValueError) as e:
        _debug(f"no state to restore: {e}")


# ---------------------------------------------------------------------------
# Background consumer
# ---------------------------------------------------------------------------

def _run(cfg):
    try:
        from kafka import KafkaConsumer
    except ImportError:
        return
    while not _K["stop"]:
        try:
            consumer = KafkaConsumer(
                cfg["topic"],
                bootstrap_servers=[cfg["bootstrap"]],
                security_protocol="SASL_SSL",
                sasl_mechanism="PLAIN",
                sasl_plain_username=cfg["user"],
                sasl_plain_password=cfg["password"],
                group_id=cfg["group"],
                auto_offset_reset="latest",
                enable_auto_commit=True,
                consumer_timeout_ms=5000,
                api_version=(3, 7, 0),
            )
            _K["connected"] = True
            print("Darwin Kafka loadings: connected", file=sys.stderr,
                  flush=True)
            last_prune = time.time()
            last_save = time.time()
            while not _K["stop"]:
                batch = consumer.poll(timeout_ms=5000)
                with _K["lock"]:
                    for _tp, msgs in batch.items():
                        for m in msgs:
                            _K["msgs"] += 1
                            try:
                                _handle_message(json.loads(
                                    m.value.decode("utf-8")))
                            except Exception as e:
                                _debug(f"bad message: {e}")
                    if time.time() - last_prune > PRUNE_EVERY_SECS:
                        _prune(time.time())
                        last_prune = time.time()
                if _K["dirty"] and time.time() - last_save > \
                        STATE_SAVE_EVERY_SECS:
                    _save_state()
                    last_save = time.time()
        except Exception as e:
            _K["connected"] = False
            now = time.time()
            msg = str(e)
            if msg != _K["last_err"] or now - _K["last_err_at"] > 600:
                _K["last_err"], _K["last_err_at"] = msg, now
                print(f"Darwin Kafka error: {e} (retrying)",
                      file=sys.stderr, flush=True)
            for _ in range(60):
                if _K["stop"]:
                    break
                time.sleep(1)
        finally:
            try:
                consumer.close()
            except Exception:
                pass


def ensure_started():
    """Start the background consumer once. Returns True when running."""
    if _K["started"]:
        return _K["connected"]
    _K["started"] = True
    try:
        import kafka  # noqa: F401
    except ImportError:
        if not _K["no_kafka_warned"]:
            _K["no_kafka_warned"] = True
            print("Darwin Kafka loadings off (pip install kafka-python)",
                  file=sys.stderr, flush=True)
        return False
    cfg, srcs = config()
    missing = [k for k in ("group", "user", "password") if not cfg[k]]
    if missing:
        print(f"Darwin Kafka loadings off (missing in .env: "
              f"{', '.join('DARWIN_KAFKA_' + k.upper() for k in missing)})",
              file=sys.stderr, flush=True)
        return False
    _debug(f"config from {srcs}")
    _load_state()
    t = threading.Thread(target=_run, args=(cfg,), daemon=True,
                         name="darwin-kafka")
    t.start()
    _K["thread"] = t
    return True


def status():
    """Snapshot of consumer state for diagnostics/tests."""
    with _K["lock"]:
        return {"started": _K["started"], "connected": _K["connected"],
                "msgs": _K["msgs"],
                "schedules": len(_K["schedules"]),
                "formations": len(_K["formations"]),
                "loadings": len(_K["loadings"])}


# ---------------------------------------------------------------------------
# Matching + enrichment
# ---------------------------------------------------------------------------

def _toc_ok(rtt_operator, dw_toc):
    r = (rtt_operator or "").strip().lower()
    t = (dw_toc or "").strip().upper()
    if not r or not t:
        return True  # nothing to check against
    for name, codes in TOC_CODES.items():
        if r == name or r in [c.lower() for c in codes]:
            return t in codes
    return True  # unknown operator: headcode+date decide


def find_rid(headcode, date="", operator=""):
    """Darwin rid for a headcode on an operating date (YYYY-MM-DD)."""
    hc = (headcode or "").strip().upper()
    if not hc:
        return None
    day = (date or "")[:10]
    best, best_ts = None, -1.0
    with _K["lock"]:
        for rid, s in _K["schedules"].items():
            if s.get("trainId") != hc:
                continue
            if day and s.get("ssd") and s.get("ssd") != day:
                continue
            if not _toc_ok(operator, s.get("toc")):
                continue
            if s.get("ts", 0) > best_ts:
                best, best_ts = rid, s["ts"]
    return best


def loadings_for(rid):
    """(formation-ish dict, source) for a rid, or (None, '').

    Prefers per-coach loadings; falls back to the whole-train
    serviceLoading percentage applied uniformly.
    """
    with _K["lock"]:
        lo = _K["loadings"].get(str(rid))
        form = _K["formations"].get(str(rid))
        svc = _K["svc_loading"].get(str(rid))
    if lo and lo.get("coaches"):
        by_num = {c["number"]: c for c in form.get("coaches", [])} \
            if form else {}
        coaches = []
        for i, c in enumerate(lo["coaches"]):
            cls = (by_num.get(c["number"]) or {}).get("class", "")
            if not cls and form and len(form.get("coaches", [])) == \
                    len(lo["coaches"]):
                cls = form["coaches"][i].get("class", "")
            coaches.append({"number": c["number"], "class": cls,
                            "loading": c["loading"]})
        return {"coaches": coaches}, "kafka"
    if svc and svc.get("pct") is not None:
        return {"uniform": svc["pct"] / 100.0}, "kafka-train"
    return None, ""


def _capacity_of(loading):
    if loading is None:
        return None
    try:
        return max(0.0, min(1.0, float(loading) / 100.0))
    except (TypeError, ValueError):
        return None


def _label(n):
    return f"{n} coach" + ("" if n == 1 else "es")


def apply_formation(dep, formation):
    """Merge coach loadings into the departure's formation cars.

    Same coach count: keep the existing cars (RTT class/wheelchair
    flags) and just fill in capacities. Different count: rebuild from
    the loadings (class from coachClass, capacity from loading).
    Returns the number of cars given a real loading, 0 when nothing
    usable.
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
    dep["loading_source"] = "darwin-kafka"
    return known


def enrich_loading(dep, station_crs=None):
    """Attach Kafka loadings to one RTT departure. Never raises.

    station_crs is currently unused (loadings track the train, latest
    report wins). Returns True when real loadings were applied.
    """
    try:
        if not isinstance(dep, dict):
            return False
        if dep.get("is_passing") or dep.get("is_cancelled"):
            return False
        if not ensure_started():
            return False
        rid = find_rid(dep.get("headcode"),
                       dep.get("operating_date") or "",
                       dep.get("operator") or "")
        if not rid:
            return False
        formation, source = loadings_for(rid)
        if formation is None:
            return False
        if "uniform" in formation:
            cars = ((dep.get("formation") or {}).get("cars")) or []
            if not cars:
                return False
            for car in cars:
                car["capacity"] = formation["uniform"]
            dep["loading_source"] = "darwin-kafka-train"
            return True
        if apply_formation(dep, formation):
            dep["loading_source"] = "darwin-kafka"
            _debug(f"KAFKA LOADINGS {dep.get('headcode')} rid={rid}")
            return True
        return False
    except Exception as e:
        _debug(f"kafka enrich failed: {e}")
        return False


def main():
    """Watch the feed briefly: python3 darwin_kafka.py [--secs N]."""
    import argparse
    global DEBUG
    ap = argparse.ArgumentParser(
        description="Watch Darwin Kafka loadings (diagnostic)")
    ap.add_argument("--secs", type=int, default=60)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    DEBUG = args.debug
    cfg, srcs = config()
    print(f"bootstrap: {cfg['bootstrap']} ({srcs['bootstrap']})")
    print(f"topic: {cfg['topic']} ({srcs['topic']})")
    print(f"group: {cfg['group'] or 'MISSING'} ({srcs['group']})")
    print(f"user: {'set' if cfg['user'] else 'MISSING'} "
          f"password: {'set' if cfg['password'] else 'MISSING'}")
    if not ensure_started():
        sys.exit("consumer did not start (see above)")
    time.sleep(args.secs)
    print(status())


if __name__ == "__main__":
    main()
