# Layout reference (`layout/` + `local.json`)

All look-and-position lives in JSON. `layout/shared.json` + `layout/pageN.json`
are shipped defaults (tracked). The web UI writes only your differences to
untracked `layout/local.json`, deep-merged over the defaults at load time —
so `git pull` never fights your tweaks, new keys from updates appear
automatically, and deleting `local.json` resets everything.

Position language (baselines, DrawText convention):
`x` = `left` (1px margin) | `center` | `right` (1px margin) | absolute pixels.
`y` = absolute pixels | `bottom` (= last row) | `bottom-N`.

Fonts are roles from `shared.fonts` (`top`, `row`, `small`, `big`, `tiny`
→ BDF files in `fonts/`). Colours are names from `shared.colors`
(`text`, `time`, `platform`, `ok`, `alert`, `mark` → `[R,G,B]`).
Status text picks `ok`/`alert` itself by service state. Anything invalid
fails fast with `layout <file>: <section>.<key> ...` — check the message,
it names the exact culprit.

## shared.json

- `fonts.*` — one BDF filename per role.
- `colors.*` — `[R,G,B]`.
- `clock` — live clock. `{font, color, format (strftime), x, y}`.
  Pinned bottom-middle by default (`center`/`bottom`).
- `page_num` — `n/3` indicator `{font, color, x, y}` (tiny, bottom-right).
- `progress` — full-width page-dwell loading bar across the top edge
  (`{color, height}`): fills left→right over the dwell, full when a page
  is held, hidden with `--pages 1`.

## page1.json (board)

Flow is top-anchored: lead row at `top.y`, calling-at `calling.dy` below
it, compact rows `rows.dy` below calling repeating every `rows.pitch`.

- `top` — `{font, y}` lead service baseline. `y: 11` etc.
- `segments` — `{time, destination, platform}` colour names for the
  `10:00 Matlock Town` left part and `1 On time` right part.
- `calling` — `{font, color, show, x, dy}`. `show` is `top-only`
  (only value supported). Long lists truncate to the screen width.
- `rows` — `{font, dy, pitch}` for services 2+. Rows past the screen
  edge are skipped with a `service row(s) off-screen` warning (max
  once a minute) — if you see it, the fonts/pitch don't fit the panel.
- `exp` — `{dx, gap}` for a flipped delayed status, rendered as two
  parts (`Exp` + `10:25`) in the row font. The time stays right-aligned;
  `dx` slides the `Exp` label (`0` = snug). Expected time = planned
  time when amended, else scheduled + `delay_minutes`.

## page2.json (next departure big)

- `headline` — `{font, y, time_color, dest_color, dest_dy}`. `dest_dy`
  lifts the destination vs the time (`0` = shared baseline).
- `status` — `{font, dy}` below the headline. Text flips
  `Delayed` ↔ `Exp HH:MM` (`--flip-seconds`). No platform here: it
  would run under the centred clock.
- `note` — `{font, color, dy}` below status: delay/cancel/amendment
  reason, else timetable `operational_notes`, else calling-at.
  Truncated before the clock/page-number zones when sharing the line.

## page3.json (formation)

- `header` — `{font, y, time_color, dest_color}` full-width headline.
- `coach` — `{width, height, gap, margin, slant, dy, outline, fill,
  mark, mark_off, default_coaches}`. Cards are fixed `width` LEDs,
  left-aligned from `margin`; first car gets a slanted front of
  `slant` px; cards start `dy` below the header. Interiors fill to the
  per-car load (`capacity`/`load`/`occupancy`, fraction or percent,
  default 10%) in `fill`; class markers (`1ST`, wheelchair icon) draw
  in `mark` on fill, `mark_off` off it. API override per departure:
  `"formation": {"cars": [{"first": true, "accessible": false,
  "capacity": 0.35}, ...]}` or `"coaches": N`.
- `letters` — `{font, color, dy}` below the card bottoms: carriage
  letters A,B,C… centred per car (capitals never descend, so the last
  row is safe).

## control.json (repo root, next to departures.py)

`{"page": null}` = cycle, `1/2/3` = hold. Written by the web UI pause
buttons; the board applies it within ~1s and logs
`control: holding page N`. Preview mode ignores it.

## Common tweaks

- Move a line: change its `y` (absolute) or `dy` (relative), rerun or
  wait ~1s on the live board, check `--preview` overlap report.
- Bigger/smaller text: swap a `font` role value (must exist in
  `fonts/`), then re-check pitches — bigger fonts need bigger gaps.
- Recolour: edit `shared.colors`, or point a single element at another
  colour name.
- New panels: nothing in layout is width/height-specific except
  absolute `y` values — re-tune `y`/pitches for the new height and
  verify with `--preview --led-rows H --led-cols W --led-chain C`.
