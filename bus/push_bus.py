#!/usr/bin/env python3
"""Push bus image programmes from this laptop to the Pi over SSH.

Lives in bus/; the repo root is the working directory and .env stays
at the root (never committed). Run from the root:

  python3 bus/push_bus.py ...

Reads SSH credentials from .env at the repo root:

  BUS_SSH_HOST=       Pi hostname/IP (required)
  BUS_SSH_PORT=22
  BUS_SSH_USER=kai
  BUS_SSH_PASSWORD=   password auth (needs `sshpass` installed);
                      leave empty to use your SSH key/agent instead
  BUS_SSH_DIR=/home/kai/depature-board
  BUS_SSH_PROGRAM_FILE=bus/programs/bus.json

What it sends: the program JSON (Sign Studio writes these via
"Send to program…") plus every bitmap PNG it references -- so a new
blind designed in Sign Studio reaches the Pi in one command:

  python3 bus/push_bus.py                             # push bus.json + PNGs
  python3 bus/push_bus.py --program 43                # JSON + only route 43's PNGs
  python3 bus/push_bus.py --dry-run                   # list, send nothing
  python3 bus/push_bus.py --show 43 --dest Sheffield  # push, then show it

Stdlib only + the OpenSSH client (`scp`/`ssh`). Password mode needs:
  macOS: brew install hudochenkov/sshpass/sshpass
  Linux: sudo apt install sshpass
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
REPO_ROOT = os.path.abspath(os.path.join(THIS_DIR, os.pardir))
sys.path.insert(0, THIS_DIR)


def read_dotenv(path):
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


def cfg(name, default=""):
    if os.environ.get(name, "").strip():
        return os.environ[name].strip()
    for path in (os.path.join(REPO_ROOT, ".env"),
                 os.path.join(THIS_DIR, ".env")):
        env = read_dotenv(path)
        if env.get(name, "").strip():
            return env[name].strip()
    return default


def screen_image(s):
    if isinstance(s, dict):
        return str(s.get("image", "") or "")
    return str(s or "")


def collect_images(data, only_program=None):
    """All bitmap paths referenced by the program file (in order)."""
    progs = data.get("programs", {})
    out = []
    for pname, raw in progs.items():
        if only_program and pname != only_program:
            continue
        if not isinstance(raw, dict):
            continue
        dests = raw.get("destinations", {})
        if isinstance(dests, dict):
            for _d, entry in dests.items():
                lst = entry.get("images", entry.get("screens", entry)) \
                    if isinstance(entry, dict) else entry
                if isinstance(lst, list):
                    out.extend(p for p in
                               (screen_image(s) for s in lst) if p)
        screens = raw.get("screens", [])
        if isinstance(screens, list):
            out.extend(p for p in
                       (screen_image(s) for s in screens) if p)
    seen, ordered = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def run(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return proc


def main():
    ap = argparse.ArgumentParser(description="Push bus programmes to the Pi")
    ap.add_argument("--program-file", default=None,
                    help="Program JSON to push "
                         "(default: $BUS_SSH_PROGRAM_FILE or "
                         "bus/programs/bus.json, repo-root-relative)")
    ap.add_argument("--program", default=None,
                    help="Only send this program's PNGs (JSON still goes whole)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be sent, send nothing")
    ap.add_argument("--show", default=None, metavar="PROGRAM",
                    help="After pushing, show this program on the Pi screen")
    ap.add_argument("--dest", default=None,
                    help="Destination to show with --show (default: all)")
    args = ap.parse_args()

    host = args.host or cfg("BUS_SSH_HOST")
    port = args.port or cfg("BUS_SSH_PORT", "22")
    user = args.user or cfg("BUS_SSH_USER", "kai")
    password = cfg("BUS_SSH_PASSWORD")
    remote_dir = args.dir or cfg("BUS_SSH_DIR", "/home/kai/depature-board")
    prog_rel = (args.program_file or cfg("BUS_SSH_PROGRAM_FILE")
                or "bus/programs/bus.json")
    if not host:
        sys.exit("no Pi set: put BUS_SSH_HOST=... in .env (see .env.example)")
    if os.path.isabs(prog_rel):
        prog_full = prog_rel
    elif os.path.isfile(prog_rel):
        prog_full = os.path.abspath(prog_rel)  # as given from CWD
    else:
        prog_full = os.path.join(REPO_ROOT, prog_rel)
    if not os.path.isfile(prog_full):
        sys.exit(f"program file not found: {prog_full}")

    try:
        with open(prog_full) as f:
            data = json.load(f)
    except ValueError as e:
        sys.exit(f"{prog_rel} is not valid JSON: {e}")
    if not isinstance(data.get("programs"), dict):
        sys.exit(f"{prog_rel}: need a 'programs' object")

    if args.program and args.program not in data["programs"]:
        names = ", ".join(sorted(data["programs"]))
        sys.exit(f"program '{args.program}' not in {prog_rel} "
                 f"(have: {names})")

    images = collect_images(data, args.program)
    missing = [p for p in images
               if not os.path.isfile(os.path.join(REPO_ROOT, p))]
    files = [os.path.relpath(prog_full, REPO_ROOT)] + images

    print(f"program file: {prog_rel}")
    print(f"target: {user}@{host}:{remote_dir} (port {port})")
    print(f"files: 1 JSON + {len(images)} PNGs"
          + (f" (route {args.program} only)" if args.program else ""))
    for p in files:
        tag = "  MISSING" if p in missing else ""
        print(f"  {p}{tag}")
    if missing:
        sys.exit(f"{len(missing)} referenced PNG(s) missing locally -- "
                 f"render them from Sign Studio first (Send to program…)")
    if args.dry_run:
        print("(dry run: nothing sent)")
        return

    for tool in ("ssh", "scp"):
        if shutil.which(tool) is None:
            sys.exit(f"'{tool}' not found -- install the OpenSSH client")
    use_pass = bool(password)
    if use_pass and shutil.which("sshpass") is None:
        sys.exit("BUS_SSH_PASSWORD is set but 'sshpass' is not installed:\n"
                 "  macOS: brew install hudochenkov/sshpass/sshpass\n"
                 "  Linux: sudo apt install sshpass\n"
                 "or unset the password to use your SSH key instead.")

    def wrap(base):
        return (["sshpass", "-p", password] if use_pass else []) + base

    ssh_base = wrap(["ssh", "-p", str(port), "-o", "BatchMode=yes"
                     if not use_pass else "NumberOfPasswordPrompts=1",
                     f"{user}@{host}"])
    # mkdir every destination dir on the Pi first
    remote_dirs = sorted({remote_dir + "/" + os.path.dirname(p).replace(
        os.sep, "/") for p in files if os.path.dirname(p)})
    mk = "mkdir -p " + " ".join(shlex.quote(d) for d in
                                [remote_dir] + remote_dirs)
    proc = run(ssh_base + [mk])
    if proc.returncode != 0:
        sys.exit(f"ssh mkdir failed:\n{proc.stderr.strip()}"
                 f"\nhint: check BUS_SSH_HOST/USER/PORT/PASSWORD in .env")

    fails = 0
    for p in files:
        local = os.path.join(REPO_ROOT, p)
        remote = remote_dir + "/" + p.replace(os.sep, "/")
        cmd = wrap(["scp", "-P", str(port), local,
                    f"{user}@{host}:{remote}"])
        proc = run(cmd)
        if proc.returncode != 0:
            print(f"FAIL {p}:\n{proc.stderr.strip()}")
            fails += 1
        else:
            print(f"sent {p}")
    if fails:
        sys.exit(f"{fails} file(s) failed to upload")

    if args.show is not None:
        ctl = {"program": args.show, "destination": args.dest}
        payload = json.dumps(ctl)
        cmd = (ssh_base + [f"cat > {shlex.quote(remote_dir + '/bus/program_control.json')}"])
        proc = subprocess.run(cmd, input=payload.encode(),
                              capture_output=True)
        if proc.returncode != 0:
            sys.exit(f"uploaded, but could not set screen:\n"
                     f"{proc.stderr.decode().strip()}")
        print(f"screen: showing {args.show} / {args.dest or 'all'} "
              f"(portal + matrix follow within ~1s)")

    print(f"done: {len(files)} file(s) pushed. On the Pi (repo root):")
    print(f"  sudo python3 bus/program.py {prog_rel} --program "
          f"{args.show or args.program or next(iter(data['programs']))} --portal")


if __name__ == "__main__":
    main()
