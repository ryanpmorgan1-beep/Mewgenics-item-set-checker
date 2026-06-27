# Mewgenics Set-Bonus Finder

Point it at a screenshot of your in-game **Storage / inventory** screen and it
tells you every **set bonus** you can actually build with the items you own.

A set bonus needs **3 items that share a set name** — but with a catch: the 3
items must occupy **distinct equipment slots** (Head / Face / Neck / Trinket /
Weapon). You can only wear one Head item at a time, so three "Hippie" items that
are all Head pieces do *not* make a wearable set. This tool enforces that rule.

## How it works

```
scrape   ─ pull the item + set catalog and icon images from the Mewgenics wiki
           (cached to data/, so it only happens once)
analyze  ─ crop the storage grid out of your screenshot, match each cell to a
           wiki icon, then build an interactive HTML report
```

Because matching tiny, desaturated in-game icons against the wiki art is never
100% reliable, the report is **assisted**: each detected cell has a dropdown
pre-filled with the best guess (and the runner-up candidates). You confirm or
correct anything that looks wrong, and the **Achievable set bonuses** panel
recomputes live in the browser — no re-run needed.

## Install

```bash
pip install -r requirements.txt
```

`opencv-python-headless` is optional (only used for automatic grid detection).
Everything else degrades gracefully if it's missing — just pass `--grid`.

## Usage

### 1. Fetch the catalog (once)

```bash
python -m mewgenics_sets scrape
```

This writes `data/catalog.json` and downloads icons to `data/icons/`. Commit
`data/catalog.json` if you want the tool to work offline afterwards.

> **Network note:** scraping must run somewhere the wiki
> (`mewgenics.wiki.gg`) is reachable. Some CI / sandbox environments restrict
> outbound traffic to package registries only — run `scrape` on your own
> machine in that case.

### 2. Analyze a screenshot

```bash
python -m mewgenics_sets analyze \
    --screenshot my_storage.png \
    --grid 888,255,690,690 --rows 11 --cols 11 \
    --open
```

* `--grid x,y,w,h` is the pixel bounding box of the **storage grid only** (the
  block of square slots on the right — not the whole screenshot). `--rows` /
  `--cols` are how many slots fit in that box.
* Omit `--grid` to try automatic detection (needs opencv); if it can't find a
  convincing grid it'll tell you to pass an explicit box.
* `--open` opens the generated `report.html` in your browser.

### 3. Confirm and read off your sets

In the report, scan the left column and fix any mis-identified items. Sets
marked **COMPLETE** (green) are wearable right now; near-misses show how many
more pieces — or which extra slot — you'd need.

## Finding the grid box

Open the screenshot in any image editor and read the pixel coordinates of the
top-left corner of the first storage slot and the bottom-right of the last one.
`x,y` is the top-left corner; `w,h` is the width/height of the whole grid block.
The default `11x11` matches the standard storage layout; adjust if yours differs.

## Project layout

```
mewgenics_sets/
  models.py    # Item / SetInfo / Catalog dataclasses, slot definitions
  scrape.py    # wiki catalog + icon fetching (MediaWiki API + BeautifulSoup)
  vision.py    # grid detection, cell cropping, icon matching (pHash + template)
  solver.py    # slot-aware "is this set achievable?" logic
  report.py    # interactive self-contained HTML report
  cli.py       # `scrape` / `analyze` commands
tests/         # solver + parsing tests (no network needed)
```

## Tests

```bash
python tests/test_solver.py
python tests/test_scrape_parsing.py
```

## Notes & limitations

* Item **slots** are read straight from the Items table on the wiki, which is
  what makes the slot-collision check possible.
* Icon matching is a heuristic (perceptual hash + normalized template
  correlation). It's good enough to seed the dropdowns, but always eyeball the
  confirmations — that's exactly why the report is interactive.
* If the wiki changes its table layout, `scrape.py` parses by **column header
  name** (not position), so it should keep working; adjust the header keywords
  in `_find_col(...)` if a column gets renamed.
