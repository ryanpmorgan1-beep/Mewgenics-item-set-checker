"""Scrape the Mewgenics wiki for the item + set catalog and cache it.

Data sources (https://mewgenics.wiki.gg):
  * ``Items``      table -> item name, equipment slot, rarity, effect, set tags, icon
  * ``Item_sets``  page  -> per-set bonus description (+ piece count when stated)

We go through the MediaWiki API (``api.php?action=parse``) which returns clean,
already-rendered HTML as JSON and is far less fragile than scraping the skinned
page. The rendered HTML is then parsed with BeautifulSoup.

Everything is cached to ``data/catalog.json`` and icons to ``data/icons/`` so
the wiki is only hit once. Commit ``data/`` to reuse the catalog offline.

NOTE: this must run somewhere the wiki is reachable. In some sandboxes outbound
network is restricted to package registries; run it on your own machine.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from typing import Optional
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .models import Catalog, Item, SetInfo, canonical_slot

WIKI = "https://mewgenics.wiki.gg"
API = f"{WIKI}/api.php"
HEADERS = {
    # A descriptive UA is polite and dodges naive bot blocks.
    "User-Agent": "MewgenicsSetFinder/0.1 (personal tool; contact: local user)",
    "Accept": "application/json",
}

DEFAULT_DATA_DIR = "data"
CATALOG_JSON = "catalog.json"
ICON_DIR = "icons"


# --------------------------------------------------------------------------- #
# Low-level fetch
# --------------------------------------------------------------------------- #
def _parse_page_html(page: str, session: requests.Session) -> BeautifulSoup:
    """Return rendered HTML of a wiki page via the MediaWiki parse API."""
    resp = session.get(
        API,
        params={
            "action": "parse",
            "page": page,
            "prop": "text",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"wiki API error for {page!r}: {data['error']}")
    html = data["parse"]["text"]
    return BeautifulSoup(html, "lxml")


def _abs_url(src: str) -> str:
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    return urljoin(WIKI, src)


def _best_icon_url(img_tag) -> str:
    """Pick the highest-resolution source from an <img> (handles srcset/thumb)."""
    if img_tag is None:
        return ""
    srcset = img_tag.get("srcset")
    if srcset:
        # "url 1x, url 2x" -> take the largest scale.
        best, best_scale = "", 0.0
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            scale = 1.0
            if len(bits) > 1 and bits[1].endswith("x"):
                try:
                    scale = float(bits[1][:-1])
                except ValueError:
                    scale = 1.0
            if scale >= best_scale:
                best, best_scale = bits[0], scale
        if best:
            return _abs_url(best)
    return _abs_url(img_tag.get("src", ""))


# --------------------------------------------------------------------------- #
# Items table
# --------------------------------------------------------------------------- #
def _header_map(table) -> dict[str, int]:
    """Map normalized header label -> column index for a wikitable."""
    head_row = table.find("tr")
    headers = head_row.find_all(["th", "td"]) if head_row else []
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(headers):
        label = cell.get_text(" ", strip=True).lower()
        mapping[label] = idx
    return mapping


def _find_col(headers: dict[str, int], *needles: str) -> Optional[int]:
    for label, idx in headers.items():
        if any(n in label for n in needles):
            return idx
    return None


def parse_items(soup: BeautifulSoup) -> list[Item]:
    """Parse the Items page into Item records.

    The real table has class 'shuffle__items' and headers:
        ['Name', 'Slot', 'Rarity', 'Cursed?', 'Description', 'Uses', 'Item Set']
    Each data row has ONE extra leading cell (the icon image) that is NOT
    represented in the header row, so every header index must be shifted by +1
    when reading a data row.
    """
    # Target the specific items table; fall back to the largest wikitable.
    table = soup.find("table", class_="shuffle__items")
    if not table:
        tables = soup.select("table.wikitable")
        table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
    if not table:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = rows[0].find_all(["th", "td"])
    n_headers = len(header_cells)
    hmap = {c.get_text(" ", strip=True).lower(): i for i, c in enumerate(header_cells)}

    name_col  = _find_col(hmap, "name")
    slot_col  = _find_col(hmap, "slot")
    rar_col   = _find_col(hmap, "rarit")
    desc_col  = _find_col(hmap, "description", "effect")
    sets_col  = _find_col(hmap, "set")

    if name_col is None:
        return []

    items: list[Item] = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # Data rows have an extra icon cell at index 0 not reflected in headers.
        off = 1 if len(cells) > n_headers else 0

        def get(col: Optional[int]) -> str:
            if col is None:
                return ""
            idx = col + off
            return cells[idx].get_text(" ", strip=True) if idx < len(cells) else ""

        def cell(col: Optional[int]):
            if col is None:
                return None
            idx = col + off
            return cells[idx] if idx < len(cells) else None

        name = get(name_col)
        if not name:
            continue

        link = (cell(name_col) or row).find("a")
        page_url = _abs_url(link.get("href", "")) if link else ""

        # Icon lives in the leading image cell (index 0 when off==1).
        icon_url = ""
        if off == 1:
            img = cells[0].find("img")
            icon_url = _best_icon_url(img)

        sets_cell = cell(sets_col)
        sets_raw = get(sets_col)
        sets = _split_sets(sets_raw, sets_cell) if sets_cell is not None else []

        items.append(Item(
            name=name,
            slot=canonical_slot(get(slot_col)),
            rarity=get(rar_col),
            effect=get(desc_col),
            sets=sets,
            page_url=page_url,
            icon_url=icon_url,
        ))

    return items


def _split_sets(raw: str, cell) -> list[str]:
    """Split a set-tags cell. Tags may be comma-separated text or links."""
    # Prefer the linked set names if present (cleaner).
    links = [a.get_text(strip=True) for a in cell.find_all("a")]
    if links:
        names = links
    else:
        names = re.split(r"[,/]| and ", raw)
    out: list[str] = []
    for n in names:
        n = n.strip()
        if n and n not in {"-", "—", "None"}:
            out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Set bonuses
# --------------------------------------------------------------------------- #
def parse_sets(soup: BeautifulSoup) -> dict[str, SetInfo]:
    """Parse the Item_sets page into {set_name: SetInfo}.

    The real page has ~97 separate tables, one per set, each with class
    'mew-sets-table' and columns ['Slot', 'Item'].  The set name and bonus
    description live in the heading / paragraphs that precede each table in
    the document flow.
    """
    sets: dict[str, SetInfo] = {}

    for table in soup.find_all("table", class_="mew-sets-table"):
        # ---- set name: nearest preceding heading ----
        heading = table.find_previous(["h2", "h3", "h4"])
        if not heading:
            continue
        # Prefer the mw-headline span (cleaner, no [edit] link text).
        span = heading.find("span", class_="mw-headline")
        if span:
            set_name = span.get_text(" ", strip=True)
        else:
            set_name = re.sub(r"\[edit[^\]]*\]", "", heading.get_text(" ", strip=True)).strip()
        if not set_name:
            continue

        # ---- bonus: text between the heading and this table ----
        bonus_parts: list[str] = []
        for sib in heading.find_next_siblings():
            if sib is table:
                break
            if sib.name in ("h2", "h3", "h4"):
                break  # hit the next set's heading before reaching our table
            if hasattr(sib, "get_text"):
                txt = sib.get_text(" ", strip=True)
                if txt and sib.name not in ("table",):
                    bonus_parts.append(txt)
        bonus = " ".join(bonus_parts)

        # ---- member items from table rows ----
        member_items: list[str] = []
        for row in table.find_all("tr")[1:]:   # skip the Slot/Item header row
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            # The Item cell contains "Name Name (Slot) description..." with a
            # link; use the first <a> tag to get a clean item name.
            link = cells[1].find("a")
            item_name = link.get_text(strip=True) if link else cells[1].get_text(strip=True)
            if item_name:
                member_items.append(item_name)

        # Only insert once per set name (first table encountered wins).
        if set_name not in sets:
            sets[set_name] = SetInfo(
                name=set_name,
                bonus=bonus,
                member_items=member_items,
                pieces=len(member_items),
            )

    return sets


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #
def _batch_png_thumbnail_urls(
    svg_filenames: list[str],
    session: requests.Session,
    width: int = 64,
) -> dict[str, str]:
    """Query MediaWiki in batches of 50 to get PNG thumbnail URLs for SVG files.

    Returns {svg_filename: png_thumb_url}, e.g. {"Peace_Symbol.svg": "https://..."}.
    """
    result: dict[str, str] = {}
    for i in range(0, len(svg_filenames), 50):
        batch = svg_filenames[i : i + 50]
        titles = "|".join(f"File:{f}" for f in batch)
        try:
            r = session.get(
                API,
                params={
                    "action": "query",
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": str(width),
                    "titles": titles,
                    "format": "json",
                },
                headers=HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages", {})
            for pg in pages.values():
                title = pg.get("title", "")          # "File:Peace_Symbol.svg"
                fname = title.replace("File:", "", 1)
                info = (pg.get("imageinfo") or [{}])[0]
                url = info.get("thumburl", "")
                if url:
                    result[fname] = _abs_url(url)
        except requests.RequestException as exc:
            print(f"  ! batch thumbnail query failed: {exc}")
    return result


def _source_file_from_image_url(url: str) -> str:
    """Recover the underlying wiki File name from an <img> URL.

    MediaWiki renders SVG icons to PNG thumbnails, so the URL we scrape often
    looks like
        /images/thumb/a/ab/Peace_Symbol.svg/40px-Peace_Symbol.svg.png
    whose *source* file is ``Peace_Symbol.svg`` (the segment before the final
    size-prefixed thumbnail name). A non-thumbnail URL like
        /images/a/ab/Peace_Symbol.svg
    has the source file as its last path segment.
    """
    path = url.split("?")[0].rstrip("/")
    segments = path.split("/")
    if "/thumb/" in path or "thumb" in segments:
        # original filename is the segment just before the final thumb name
        if len(segments) >= 2:
            return unquote(segments[-2])
    return unquote(segments[-1]) if segments else ""


def download_icons(
    catalog: Catalog,
    data_dir: str,
    session: requests.Session,
    png_mode: bool = False,
    sleep: float = 0.05,
) -> None:
    """Download item icons.

    png_mode=False (default): download whatever URL is in icon_url (often the
                   wiki's own PNG thumbnail of an SVG, but occasionally raw SVG).
    png_mode=True: for every icon whose *source* file is an SVG, ask the
                   MediaWiki thumbnail API for a guaranteed-raster PNG render —
                   no local SVG-rasterization library needed.
    """
    icon_dir = os.path.join(data_dir, ICON_DIR)
    os.makedirs(icon_dir, exist_ok=True)

    if png_mode:
        # Detect the SOURCE file behind each icon URL (the img src is usually a
        # ".png" thumbnail even when the underlying file is ".svg").
        svg_items = []
        svg_fnames = []
        for it in catalog.items:
            if not it.icon_url:
                continue
            src = _source_file_from_image_url(it.icon_url)
            if src.lower().endswith(".svg"):
                svg_items.append(it)
                svg_fnames.append(src)
        unique_fnames = list(dict.fromkeys(svg_fnames))   # deduplicate, preserve order
        print(f"  fetching PNG thumbnail URLs for {len(unique_fnames)} SVG icons ...")
        thumb_map = _batch_png_thumbnail_urls(unique_fnames, session)
        # Attach resolved PNG urls back to items.
        still_svg = 0
        for it, fname in zip(svg_items, svg_fnames):
            if fname in thumb_map:
                url = thumb_map[fname]
                it.icon_url = url
                if url.lower().endswith(".svg"):
                    still_svg += 1
        print(f"  resolved {len(thumb_map)}/{len(unique_fnames)} thumbnail URLs")
        if still_svg:
            print(f"  WARNING: {still_svg} resolved URLs are still SVGs — "
                  "wiki thumbnail API may not be rasterizing them")

    ok = 0
    for it in catalog.items:
        if not it.icon_url:
            continue
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", it.name).strip("_") or "item"
        ext = ".png" if png_mode else (os.path.splitext(it.icon_url.split("?")[0])[1] or ".png")
        path = os.path.join(icon_dir, f"{safe}{ext}")
        # In png_mode we force re-download: a previous run may have saved SVG bytes
        # under this name (because the old logic didn't detect SVG-backed icons).
        if png_mode or not os.path.exists(path):
            try:
                r = session.get(it.icon_url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(r.content)
                ok += 1
                time.sleep(sleep)
            except requests.RequestException as exc:
                print(f"  ! failed icon for {it.name}: {exc}")
                continue
        it.icon_path = path
    if ok:
        print(f"  downloaded {ok} new icon files")


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def scrape(
    data_dir: str = DEFAULT_DATA_DIR,
    with_icons: bool = True,
    png_icons: bool = False,
) -> Catalog:
    os.makedirs(data_dir, exist_ok=True)
    session = requests.Session()

    print("Fetching item catalog ...")
    items_soup = _parse_page_html("Items", session)
    items = parse_items(items_soup)
    print(f"  parsed {len(items)} items")

    print("Fetching set bonuses ...")
    sets_soup = _parse_page_html("Item_sets", session)
    sets = parse_sets(sets_soup)
    print(f"  parsed {len(sets)} sets")

    catalog = Catalog(items=items, sets=sets)

    # Backfill member_items from item->set tags.
    for it in items:
        for s in it.sets:
            info = catalog.sets.setdefault(s, SetInfo(name=s))
            info.member_items.append(it.name)

    if with_icons:
        print("Downloading icons ...")
        download_icons(catalog, data_dir, session, png_mode=png_icons)

    save_catalog(catalog, data_dir)
    return catalog


def save_catalog(catalog: Catalog, data_dir: str = DEFAULT_DATA_DIR) -> str:
    path = os.path.join(data_dir, CATALOG_JSON)
    payload = {
        "items": [asdict(it) for it in catalog.items],
        "sets": {name: asdict(info) for name, info in catalog.sets.items()},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"Saved catalog -> {path}")
    return path


def load_catalog(data_dir: str = DEFAULT_DATA_DIR) -> Catalog:
    path = os.path.join(data_dir, CATALOG_JSON)
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    items = [Item(**it) for it in payload["items"]]
    sets = {name: SetInfo(**info) for name, info in payload["sets"].items()}
    return Catalog(items=items, sets=sets)
