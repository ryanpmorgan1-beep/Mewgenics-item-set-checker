"""Command-line entry point.

Typical usage:

    # 1. one-time: fetch the wiki catalog + icons into ./data (run where the
    #    wiki is reachable; the result is cached and can be committed)
    python -m mewgenics_sets scrape

    # 2. analyze a screenshot -> interactive HTML report
    python -m mewgenics_sets analyze --screenshot shot.png \
        --grid 888,255,690,690 --rows 11 --cols 11 --open
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from . import scrape as scrape_mod
from . import vision
from . import report as report_mod
from .solver import solve


def cmd_scrape(args: argparse.Namespace) -> int:
    scrape_mod.scrape(data_dir=args.data_dir, with_icons=not args.no_icons,
                      png_icons=args.png_icons)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    if not os.path.exists(os.path.join(args.data_dir, scrape_mod.CATALOG_JSON)):
        print("No cached catalog found. Run `scrape` first "
              "(somewhere the wiki is reachable).", file=sys.stderr)
        return 2
    catalog = scrape_mod.load_catalog(args.data_dir)
    print(f"Loaded {len(catalog.items)} items, {len(catalog.sets)} sets.")

    from PIL import Image
    img = Image.open(args.screenshot).convert("RGB")

    if args.grid:
        grid = vision.parse_grid_arg(args.grid, args.rows, args.cols)
    else:
        print("No --grid given; attempting auto-detection ...")
        grid = vision.auto_detect_grid(img, cols_hint=args.cols)
        if grid is None:
            print("Auto-detection failed. Re-run with "
                  "--grid x,y,w,h --rows R --cols C "
                  "(bounding box of the storage grid).", file=sys.stderr)
            return 3
        print(f"  detected grid: x={grid.x} y={grid.y} w={grid.w} h={grid.h} "
              f"rows={grid.rows} cols={grid.cols}")

    refs = vision.build_references(catalog)
    if not refs:
        print("No reference icons available. Re-run `scrape` (icons missing).",
              file=sys.stderr)
        return 4
    print(f"Built {len(refs)} icon references.")

    crop_dir = os.path.join(args.data_dir, "crops")
    cells = vision.analyze_screenshot(
        args.screenshot, grid, refs, crop_dir, top_k=args.top_k
    )
    filled = [c for c in cells if not c.empty and c.candidates]
    print(f"Detected {len(filled)} filled cells out of {len(cells)}.")

    # Text preview of best-guess achievable sets (the HTML report is interactive).
    owned = []
    seen = set()
    for c in filled:
        if c.candidates:
            it = c.candidates[0].item
            if it.name not in seen:
                seen.add(it.name)
                owned.append(it)
    results = solve(catalog, owned)
    achievable = [r for r in results if r.achievable]
    print(f"\nBest-guess achievable sets ({len(achievable)}):")
    for r in achievable:
        combo = ", ".join(f"{it.name} [{it.normalized_slot()}]" for it in r.example_combo)
        print(f"  • {r.name}: {combo}")
        if r.bonus:
            print(f"      bonus: {r.bonus}")

    out = report_mod.render(catalog, cells, args.out)
    print(f"\nInteractive report -> {out}")
    print("Open it and confirm/correct the detected items; achievable sets "
          "update live.")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mewgenics_sets",
                                description="Find achievable Mewgenics set bonuses "
                                            "from an inventory screenshot.")
    p.add_argument("--data-dir", default="data", help="cache directory (default: data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="fetch + cache the wiki catalog and icons")
    s.add_argument("--no-icons", action="store_true", help="skip downloading icons")
    s.add_argument("--png-icons", action="store_true",
                   help="download icons as PNG (via MediaWiki thumbnail API) instead of SVG")
    s.set_defaults(func=cmd_scrape)

    a = sub.add_parser("analyze", help="analyze a screenshot and build the report")
    a.add_argument("--screenshot", required=True, help="path to the inventory screenshot")
    a.add_argument("--grid", help="storage grid bounding box 'x,y,w,h' (pixels)")
    a.add_argument("--rows", type=int, default=11, help="grid rows (default: 11)")
    a.add_argument("--cols", type=int, default=11, help="grid columns (default: 11)")
    a.add_argument("--top-k", type=int, default=5, help="candidates per cell (default: 5)")
    a.add_argument("--out", default="report.html", help="output HTML path")
    a.add_argument("--open", action="store_true", help="open the report in a browser")
    a.set_defaults(func=cmd_analyze)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
