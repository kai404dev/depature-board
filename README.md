# Depature Board — Peak Rail LED departures display

Live departure board for three chained RGB LED panels on a Raspberry Pi,
driven by [peakraildepartures.com](https://peakraildepartures.com).
Two pages cycle on the panels: the departures board and a train
formation diagram (the next-departure-big page 2 is disabled by
default — re-enable with `--pages 1,2,3`). The page indicator counts
position in the cycle, so page 3 of `[1, 3]` shows `2/2`. Only
stopping services appear on the pages; a non-stopping service shows
nothing (its stand-back warning still takes over as below).
Within the warning window (`--passing-warning-time`, seconds, default
30) of passing through it takes over the whole screen with a bordered
stand-back warning (fast-train approaching, stand back from the edge,
hold pushchairs/wheelchairs, stand behind the yellow line). The warning
is latched so it stays up until the train has passed, even if the
service drops off the data feed first.
A web UI on port 4000 previews every page and live-edits the layout —
tweaks reach the real LEDs within about a second, no restart.

## Files

| File | What it is |
|---|---|
| `departures.py` | The board: fetches data, drives the matrix |
| `api.py` | API fetching + formatting (no hardware needed) |
| `preview.py` | Fake matrix + ASCII page previews (`--preview`) |
| `webui.py` | Browser tweaker + live previews on `:4000` |
| `font-test.py` | Step through every BDF font / preview a JSON layout |
| `layout/shared.json` | Font roles, colours, clock, page number, progress bar |
| `layout/page1.json` | Board: lead service, calling-at, compact rows |
| `layout/page2.json` | Next departure big + notes |
| `layout/page3.json` | Formation diagram (coaches, markers, letters) |
| `layout/local.json` | **Your** live overrides (untracked — see below) |
| `control.json` | `{"page": null}` cycle, or `1/2/3` to hold a page |
| `fonts/` | Bundled BDF fonts |
| `install.sh` | Pi one-shot installer for the `rgbmatrix` driver |

`--mock` prints board data to the console (no hardware).
`--preview` prints a to-scale ASCII map of every page with element
bounds and overlap warnings — the fastest way to check spacing.

## Carriage loadings (Darwin, RTT mode only)

RTT's Know Your Train gives coach count and facilities but no seating
availability. The page-3 fill levels come from National Rail Darwin,
which publishes per-coach loadings (0–100) wherever the train
operator feeds them in (e.g. Avanti, CrossCountry — but not every
operator, and heritage railways not at all). One request per refresh,
stdlib only:

1. Register at `raildata.org.uk` (consumer access is enough) and
   subscribe to a **Live Departure Board** product — departures-only
   or arrivals+departures (free, approved immediately).
2. Open the product → **Specification** tab → copy the **Consumer key**.
3. Put `DARWIN_TOKEN=<consumer key>` in `.env` next to
   `departures.py` (or set `$DARWIN_TOKEN` / create
   `darwin_token.txt`).
4. Run with `--source rtt` and check it with `python3 darwin.py SOT`
   (never prints the key; shows per-coach loadings like `A:80`).

The board matches each RTT departure to Darwin by scheduled time +
operator + destination and fills the formation cars. Without a key,
or where Darwin has no data, the diagram silently keeps its defaults.

Alternative: if your RDM subscription is the Darwin push-port (Kafka)
product instead, set `DARWIN_KAFKA_GROUP/USER/PASSWORD` in `.env`
(plus `pip install kafka-python`) and the board sips loadings from
the firehose as a fallback — see `darwin_kafka.py`. Never share these
credentials (treat a posted password as burned and rotate it in RDM).

## Pi setup

```bash
git clone https://github.com/kai404dev/depature-board ~/depature-board
cd ~/depature-board
chmod +x install.sh && ./install.sh   # builds the rgbmatrix driver (takes minutes)
python3 departures.py --mock --once   # check the API data first
```

Run the board (needs root for GPIO; use your working flags):

```bash
sudo .venv/bin/python departures.py --led-rows 40 --led-cols 80 --led-chain 3 --led-no-hardware-pulse
```

Web UI (no root needed):

```bash
.venv/bin/python webui.py   # http://<pi-ip>:4000
```

## Auto-start on boot (systemd)

`/etc/systemd/system/departures.service` (root, GPIO):

```ini
[Unit]
Description=Peak Rail departures LED board
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/kai/depature-board
ExecStart=/home/kai/depature-board/.venv/bin/python departures.py --led-no-hardware-pulse --led-rows 40 --led-cols 80 --led-chain 3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/depature-webui.service` (normal user is fine):

```ini
[Unit]
Description=Departures layout tweaker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kai
WorkingDirectory=/home/kai/depature-board
ExecStart=/home/kai/depature-board/.venv/bin/python webui.py --port 4000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now departures.service depature-webui.service
sudo systemctl status departures.service depature-webui.service
journalctl -u departures.service -f
```

## Live editing how-it-works

- `layout/*.json` are shipped defaults (tracked by git). The web UI
  writes only your *differences* to untracked `layout/local.json`,
  so `git pull` never conflicts with your tweaks. Setting a value
  back to its default removes the override. Delete `local.json` to
  reset everything.
- `departures.py` re-reads the layout twice a second and applies it
  live (`layout reloaded` in the journal). Bad values keep the old
  layout with a warning — a typo can never blank the board.
- Pause buttons write `control.json` (`null` = cycle, `1/2/3` =
  hold). The board follows within ~1s and logs
  `control: holding page N`.

## Troubleshooting (live editing dead?)

```bash
cd ~/depature-board
git log --oneline -1            # must include the hot-reload + pause commits
git status --short              # layout/*.json must be CLEAN (edits live in local.json now)
ps aux | grep "[d]epartures.py" # one process, from ~/depature-board, current code?
sudo systemctl restart departures.service depature-webui.service
journalctl -u departures.service --no-pager -n 15
# expect: board 240x40 layout=... control=... pages=[1, 2, 3]
```

Then: hold page 2 in the web UI → panel must freeze within ~1s and
the journal must print `control: holding page 2`. Change any position
→ journal must print `layout reloaded`. Whichever step fails tells you
exactly where it breaks: old code still running, wrong directory,
or unreachable files. See `docs/layout.md` for every knob.

Known Pi gotchas: `snd_bcm2835` sound module conflicts with the LED
driver (`--led-no-hardware-pulse` or blacklist it); building
`rgbmatrix` needs `python3-pil` for `Imaging.h` (`install.sh` covers
it); the matrix drops root→`daemon` after init, so fonts load before
that happens — don't move font loading after matrix creation.
