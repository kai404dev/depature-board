# Bus portal — ICU 602 replica (`program.py` + `portal.py`)

`program.py` plays image programmes (PNG screens per route and
destination) on the LED matrix. With `--portal` it also hosts a
browser replica of the Mobitec ICU 602 controller on port **4040**:
pick the route and destination there and the board follows, live.

```bash
sudo python3 program.py programs.json --program 401 --portal
python3 program.py programs.json --serve --port 4040   # portal only
```

Open `http://<pi-ip>:4040`. The matrix run and the portal stay in
step even as separate processes (see Control file below).

## The UI

It mirrors the ICU 602 handset in the cab:

- **LCD**: big `route destination` readout, with `Line:` (route),
  `Dest:` (destination id) and `Extr:` on the right. While typing,
  the big readout echoes the keypad buffer.
- **Softkeys**: `Dest` jumps to destination entry (same as F2),
  `Clear all` resets to the startup selection.
- **F1** enters the route number. Type digits, ✓ to confirm.
- **F2** picks the destination: ↑/↓ (or ←/→) steps through the
  destinations in file order, or type the numeric id. ✓ confirms.
- **F5** browses every destination in the file on the arrows
  (the LCD previews each one, the chips show `route name`);
  ✓ jumps the board straight there. Digits do nothing here.
- **Keypad** takes full route+dest codes in one go (see below).
- **X** clears the buffer. **✓** confirms. **Home** resets.
- **Chips** under the unit show each destination's first image
  (green = showing, outlined = F2/F5 highlight); clicking one
  selects it straight away.
- A PC keyboard works too: `0-9`, arrows, `Enter`, `Backspace`.

## Route + destination codes

- Route only: `401` (exact route number, case-insensitive).
- Route + destination id: `40101` is route 401, dest id `01`.
- Letter routes take a leading `1`: the `1` is stripped and the
  rest matches the route's digits, so `11204` is X12 dest `04`.
- Any route can also define a custom numeric `"code"` (e.g. igo
  answers to `446`); codes match exactly, before the 1-rules.
- Unknown routes/codes/ids show a message on the LCD; nothing
  changes until you enter something valid.

## Destination ids

Ids default to file order: `0, 1, 2, …`. Override per destination:

```json
"Tutbury": {"id": 5, "images": [".../401-tutbury-only.png"]}
```

`--list` shows them (`Tutbury#5x1`). Duplicate or negative ids
are rejected with a plain error.

## `programs.json` schema

```jsonc
{
  "rotate_seconds": 10,          // default dwell per screen
  "image_fit": "fit",           // fit | fill | stretch
  "colour": "#ff8000",          // default tint (optional)
  "programs": {
    "401": {
      "route": "401",
      "code": "446",            // optional keypad alias (digits)
      "colour": "#ffbb00",      // optional route tint
      "rotate_seconds": 10,
      "image_fit": "fit",
      "destinations": {
        "Burton": [             // or {"images": [...], "id": 1,
                                //  "colour": "#ffbb00"}
          "bitmap/destinations/401/burton/401-burton-1.png",
          {"image": "bitmap/x.png", "seconds": 5,
           "fit": "fill", "colour": "#ffbb00"}
        ]
      }
    }
  }
}
```

Defaults cascade screen → destination → route → file → built-in
(10s, fit, no tint). A screen is a path or an object overriding
`seconds` / `fit` / `colour`. Colour flattens the whole image to
exactly that one shade (any lit pixel takes it at full
brightness); `"full"` keeps the image's own colours. `"color"`
also works everywhere `"colour"` does.

## Control file

Confirmed picks are written to `program_control.json` next to the
scripts (`{"program": "401", "destination": "Burton"}`). Every live
matrix run watches it, so a `--serve` portal on one process (or Pi)
steers a matrix run in another within about half a second. The CLI
selection always wins at startup — the file only takes over when it
changes afterwards — and a broken `programs.json` save keeps the old
screens with an LCD message instead of blanking the board. Delete
the file to forget the last portal pick.

## Pi auto-start

`/etc/systemd/system/program.service` (root, GPIO):

```ini
[Unit]
Description=Bus image programme LED board
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/kai/depature-board
ExecStart=/home/kai/depature-board/.venv/bin/python program.py programs.json --program 401 --portal --port 4040
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now program.service
journalctl -u program.service -f   # expect: program '401' ... screens=N
```

## Troubleshooting

- Flickering on bright / full-colour screens, brightest first:
  1. Dim the images: `--image-dim 70` (peak current falls with
     it — this is the most common fix).
  2. Feed the panels properly: three chained panels at full white
     pull several amps; a weak 5V supply sags and the whole board
     shimmers. Use a supply rated for the panels, short thick
     wires, and power the panels directly, not through the Pi.
  3. Drop `--led-no-hardware-pulse` (and blacklist `snd_bcm2835`)
     so the driver uses hardware pulse generation — software
     pulsing flickers more, most visibly on bright colours.
  4. Lower `--led-brightness` / `--led-pwm-bits` a notch.
  Text blinds draw far less current, which is why they stay
  steady while photos flicker.
- Portal picks do nothing on the board: the matrix run must be
  live (`--portal`, plain run, or any run watching the file —
  `--mock`/`--preview` are one-shot and never follow). Check
  `program_control.json` updates when you press ✓.
- `port in use`: something else sits on 4040 — rerun with
  `--port 4041` (departures' tweaker is on 4000, no clash).
- `code must be digits` / `duplicate destination id`: fix the
  programme entry named in the message and re-pick; the board
  keeps showing the old screens meanwhile.
- Keypad code rejected: ids are file order starting at 0 unless
  overridden — `40105` needs `{"id": 5}` on that destination.
