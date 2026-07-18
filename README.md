# Mewgenics Set Checker

Upload a screenshot of your in-game **Storage** screen and get back every
**set bonus** you can actually wear, computed from the items you own.

- Finds the storage grid in the screenshot automatically (any resolution).
- Identifies each item by matching it against the icon catalog from the
  [Mewgenics wiki](https://mewgenics.wiki.gg/wiki/Items).
- Applies the real set rules from the
  [Item sets page](https://mewgenics.wiki.gg/wiki/Item_sets): a bonus needs
  **3+ pieces of the set worn at once**, across the 5 equipment slots
  (Head / Face / Neck / Trinket / Weapon) — so three Head pieces of one set
  don't count. The checker enforces the slot math for you.
- Every guess is correctable: click any cell, pick the right item from a
  searchable list, and the set results recompute instantly. Low-confidence
  guesses are highlighted so you know what to double-check.
- Multiple storage pages? Upload several screenshots — items merge into one
  inventory. Items equipped on cats can be added manually.

## Deploy on Railway

1. Push this repo to GitHub (already done if you're reading it there).
2. In [Railway](https://railway.com): **New Project → Deploy from GitHub repo**
   → pick this repo. Railway reads `railway.json` and builds the `Dockerfile`
   automatically — no other settings needed.
3. Generate a domain (service → **Settings → Networking → Generate Domain**).
4. First boot: the app spends ~1–2 minutes scraping the wiki catalog and item
   icons (progress shows in the page banner and `/api/health`). After that
   it's instant.

Optional:

- **Persist the catalog across deploys**: add a Volume mounted at `/app/data`.
  Without it the app just re-scrapes on each deploy, which is fine too.
- Set `AUTO_BOOTSTRAP=0` to disable the startup scrape (you'd then import a
  bundle instead, see below).

### If the wiki blocks the server (rare)

The catalog banner would show a bootstrap failure. Build the data bundle on
your own machine and import it — no redeploy needed:

```bash
pip install -r requirements.txt
python -m app.bootstrap --bundle data_bundle.zip
```

Then click **⇪ Import** in the app header and upload `data_bundle.zip`.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

Same first-boot behavior: it scrapes the wiki into `data/` once.

## Using it

1. **Screenshot the Storage screen** (the grid on the right side). Fullscreen
   PNG screenshots work best; JPEG is fine.
2. Drop it on the page. Analysis takes ~10–40 s depending on the machine.
3. Review the grid overlay: **green** = confident, **amber** = check me,
   **blue** = corrected by you, dashed = empty. Click any cell to fix it —
   the picker shows the cell crop, the top match candidates, and a search box.
4. Read the results: **Wearable now** (with an example 3-piece combo),
   **One slot away** (with the exact items that would complete the set),
   and progress on everything else. **⧉ Copy results** exports text.
5. If the grid wasn't found or looks misaligned: **⌗ Adjust grid**, click the
   top-left corner of the first tile and the bottom-right corner of the last
   tile, set rows/columns, re-analyze.

## How the matching works (and why it now works)

The original version of this tool compared whole cell crops against whole
wiki icons at one fixed framing and failed: in-game icons float inside their
tile at an unknown scale/offset, tiles carry corner slot-glyphs, "NEW"
ribbons, blessed-yellow glows and cursed-red tints, and screenshots add noise
and compression.

The rewrite treats it as a proper template-matching problem:

- **Grid**: square-tile contours are clustered by size, snapped onto a
  pitch lattice, and the largest consistent component becomes the grid —
  resolution-independent, tolerant of missed tiles, with sparse boundary
  rows (stray UI buttons) trimmed off. Verified to sub-2px accuracy on
  fixtures at multiple resolutions.
- **Matching**: every wiki icon is compared under its **alpha mask only**
  (tile background and overlays never pollute the score) using
  zero-normalized cross-correlation of high-passed grayscale + edge
  magnitude, swept over 7 icon scales and a grid of offsets (coarse batched
  pass over all ~270 icons, then precise refinement of the top 20). A
  coverage term rejects matches that leave cell ink unexplained, and a
  chroma term separates same-shape different-color items (red pill vs blue
  pill) while ignoring tile tints.
- The synthetic end-to-end test (`tests/test_grid_and_match.py`) renders a
  fake storage screen — glows, tints, glyphs, ribbons, JPEG artifacts, two
  resolutions — and requires ≥95% top-1 accuracy with zero
  *confidently*-wrong matches. It currently measures ~99%, and real
  screenshots are the easier case (identical art). Anything the matcher is
  unsure about is flagged amber in the UI for a one-click fix.

## Project layout

```
app/
  main.py       FastAPI app: /api/analyze, /api/health, /api/catalog, ...
  grid.py       storage-grid detection (lattice fit)
  matcher.py    masked multi-scale icon matching
  solver.py     set-bonus rules (3 pieces, distinct slots)
  bootstrap.py  wiki scraper: catalog + PNG icons (+ bundle export/import)
  catalog.py    data model + persistence
  static/       the web UI (vanilla JS)
tests/          pytest suite incl. synthetic end-to-end vision test
Dockerfile      Railway/anywhere deployment
railway.json    Railway build + healthcheck config
```

## API (if you want to script it)

- `POST /api/analyze` — multipart `file` (+ optional `grid_x0,grid_y0,
  grid_x1,grid_y1,rows,cols` for a manual grid; `debug=1` adds an overlay
  image) → grid + per-cell candidates with confidences.
- `GET /api/catalog` — items (name/slot/sets/icon) and sets (bonus/members).
- `GET /api/health` — catalog & bootstrap status.
- `POST /api/bootstrap` — re-scrape the wiki.
- `POST /api/import-data` — upload a data bundle zip.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Notes & limits

- Item identification is ~99% on fixtures but not guaranteed perfect on
  every real screenshot — that's why every cell is click-to-fix and unsure
  cells are highlighted. The set math is exact once items are confirmed.
- The catalog mirrors the wiki; if the wiki is missing an item or set, so is
  the checker. Press **↻ Catalog** anytime to re-scrape.
- Set-requirement modifiers (Rune of Perthro, Item Proxy) aren't modeled;
  the checker assumes the standard 3-piece rule.
