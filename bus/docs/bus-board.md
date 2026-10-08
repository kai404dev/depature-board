# Bus destination board (`bus/programs/bus.json`)

All bus board code lives in `bus/`: the runner (`program.py`), the
ICU 602 portal (`portal.py`), the laptop uploader (`push_bus.py`),
the browser editor (`program_editor.py`), the JSON
(`programs/bus.json`), the PNGs (`bitmap/destinations/bus/...`) and
the live pick (`program_control.json`). Run everything from the repo
root — image paths in the JSON are repo-root-relative
(`bus/bitmap/...`).

Image-format destination board for the bus: full-panel 240x40 PNGs
designed in Sign Studio, played by `bus/program.py`, controlled from
the ICU 602 web portal, uploaded from the laptop over SSH.

## Run it

On the Pi (matrix + portal together):

```bash
sudo python3 bus/program.py bus/programs/bus.json --program 43 --portal --port 4040
# http://<pi-ip>:4040
```

Portal only (no matrix, e.g. to control from another machine):

```bash
python3 bus/program.py bus/programs/bus.json --serve --port 4040
```

Check what is in the file without hardware:

```bash
python3 bus/program.py bus/programs/bus.json --list
python3 bus/program.py bus/programs/bus.json --program 43 --mock --once
```

`bus/programs/bus.json` ships with route `43` (Sheffield, 2 screens)
and `000` (Not in Service). The PNGs live under
`bus/bitmap/destinations/bus/...` in Sign Studio image format.

## Design new blinds in Sign Studio

1. `python3 sign-studio.py` — design pages (route + destination +
   via, bitmap or system fonts, per-page seconds, pixel touch-up).
2. `File → Send to program…` — pick file `bus/programs/bus.json`,
   the program (route) and destination. One PNG per page lands in
   `bitmap/destinations/<program>/<route>/` (repo-root-relative:
   `bus/bitmap/…`) and is appended to the destination (new
   programs/destinations are created as needed).
3. Push to the Pi (below) or upload from the portal (below).
   `Show on screen` in Sign Studio writes both
   `program_control.json` and `bus/program_control.json`, so the bus
   matrix follows too.

## Push from laptop to Pi (`bus/push_bus.py`)

`.env` at the repo root (never committed — see `.env.example`):

```ini
BUS_SSH_HOST=<pi-ip>
BUS_SSH_PORT=22
BUS_SSH_USER=kai
BUS_SSH_PASSWORD=<pi-password>   # needs `sshpass`; empty = SSH key
BUS_SSH_DIR=/home/kai/depature-board
BUS_SSH_PROGRAM_FILE=bus/programs/bus.json
```

```bash
python3 bus/push_bus.py --dry-run            # list what would be sent
python3 bus/push_bus.py                      # push JSON + all its PNGs
python3 bus/push_bus.py --program 43         # JSON + only route 43's PNGs
python3 bus/push_bus.py --show 43 --dest Sheffield   # push, then show it
```

Password auth needs `sshpass` on the laptop
(macOS: `brew install hudochenkov/sshpass/sshpass`,
Linux: `sudo apt install sshpass`); without a password it uses your
SSH key/agent. Files land at the same repo-relative paths under
`BUS_SSH_DIR`. `--show` writes `bus/program_control.json` on the Pi,
so the matrix + portal follow within ~1s.

## Control the screen from the portal

Open `http://<pi-ip>:4040` (ICU 602 replica):

- **F1** route, **F2** destination (arrows or numeric id), **✓**
  shows it on the matrix; **F5** browses every destination;
  the keypad takes route+dest codes (e.g. `4300` = route 43 dest 0).
- **Chips** under the unit pick a destination with one click.
- **Screens strip** previews every screen of the live pick (the
  matrix rotates through them).
- **Upload a Sign Studio PNG**: choose file + program +
  destination (+ optional seconds) → `Upload + show` saves it into
  `bus/bitmap/destinations/…`, appends it to
  `bus/programs/bus.json`, and puts it on screen. New
  programs/destinations are created.

Picks are written to `bus/program_control.json`, so a portal on one
process steers a matrix run in another within ~0.5s.

## Pi auto-start

`/etc/systemd/system/bus.service` (root, GPIO):

```ini
[Unit]
Description=Bus destination board
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/kai/depature-board
ExecStart=/home/kai/depature-board/.venv/bin/python bus/program.py bus/programs/bus.json --program 43 --portal --port 4040
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bus.service
journalctl -u bus.service -f
```

## Troubleshooting

- `no Pi set` from `push_bus.py`: fill `BUS_SSH_HOST` in `.env`.
- `sshpass not installed` + password in `.env`: install it (above)
  or clear the password to use keys.
- `referenced PNG(s) missing`: that route's PNGs were never
  rendered — `Send to program…` in Sign Studio first.
- Portal upload rejected `not a PNG file`: only 240x40 PNGs
  (Sign Studio output); max 2MB.
- Uploaded screen 404s / never previews: fixed — the image
  allowlist now covers every screen, not just each destination's
  first one.
- Picks do nothing on the board: the matrix run must be live
  (`--portal` or a plain run watching `bus/program_control.json`);
  `--mock`/`--preview` are one-shot.
