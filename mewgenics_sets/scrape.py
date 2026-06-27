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
from urllib.parse import urljoin

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
    """Parse the Items page table into Item records (column-order agnostic)."""
    items: list[Item] = []
    tables = soup.select("table.wikitable") or soup.select("table")
    for table in tables:
        headers = _header_map(table)
        name_col = _find_col(headers, "name")
        slot_col = _find_col(headers, "slot", "type")
        rarity_col = _find_col(headers, "rarit")
        effect_col = _find_col(headers, "effect", "description")
        sets_col = _find_col(headers, "set")
        if name_col is None or sets_col is None:
            continue  # not the items table

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(c for c in [name_col, sets_col] if c is not None):
                continue

            def cell_text(col: Optional[int]) -> str:
                if col is None or col >= len(cells):
                    return ""
                return cells[col].get_text(" ", strip=True)

            name = cell_text(name_col)
            if not name:
                continue

            name_cell = cells[name_col]
            link = name_cell.find("a")
            page_url = _abs_url(link.get("href")) if link and link.get("href") else ""

            # Icon: first <img> in the row.
            img = row.find("img")
            icon_url = _best_icon_url(img)

            sets_raw = cell_text(sets_col)
            sets = _split_sets(sets_raw, cells[sets_col])

            items.append(
                Item(
                    name=name,
                    slot=canonical_slot(cell_text(slot_col)),
                    rarity=cell_text(rarity_col),
                    effect=cell_text(effect_col),
                    sets=sets,
                    page_url=page_url,
                    icon_url=icon_url,
                )
            )
        if items:
            break  # found and parsed the items table
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

    The page commonly renders each set as a table or a section. We handle both:
      * tables with a 'Set'/'Name' column and a 'Bonus'/'Effect' column
      * <h2>/<h3> section headings followed by descriptive text
    """
    sets: dict[str, SetInfo] = {}

    # Strategy 1: tables.
    for table in soup.select("table.wikitable") or soup.select("table"):
        headers = _header_map(table)
        name_col = _find_col(headers, "set", "name")
        bonus_col = _find_col(headers, "bonus", "effect", "description")
        pieces_col = _find_col(headers, "piece", "items", "count")
        if name_col is None or bonus_col is None:
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(name_col, bonus_col):
                continue
            name = cells[name_col].get_text(" ", strip=True)
            bonus = cells[bonus_col].get_text(" ", strip=True)
            if not name:
                continue
            pieces = 0
            if pieces_col is not None and pieces_col < len(cells):
                m = re.search(r"\d+", cells[pieces_col].get_text())
                if m:
                    pieces = int(m.group())
            sets[name] = SetInfo(name=name, bonus=bonus, pieces=pieces)

    if sets:
        return sets

    # Strategy 2: headings + following paragraph.
    for heading in soup.select("h2, h3"):
        name = heading.get_text(" ", strip=True)
        name = re.sub(r"\[edit\]", "", name).strip()
        if not name or name.lower() in {"contents", "navigation", "references"}:
            continue
        sib = heading.find_next_sibling()
        bonus = ""
        while sib is not None and sib.name not in {"h2", "h3"}:
            if sib.name in {"p", "ul", "div"}:
                txt = sib.get_text(" ", strip=True)
                if txt:
                    bonus = txt
                    break
            sib = sib.find_next_sibling()
        if bonus:
            sets[name] = SetInfo(name=name, bonus=bonus)
    return sets


# --------------------------------------------------------------------------- #
# Icons
# --------------------------------------------------------------------------- #
def download_icons(catalog: Catalog, data_dir: str, session: requests.Session,
                   sleep: float = 0.1) -> None:
    icon_dir = os.path.join(data_dir, ICON_DIR)
    os.makedirs(icon_dir, exist_ok=True)
    for it in catalog.items:
        if not it.icon_url:
            continue
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", it.name).strip("_") or "item"
        ext = os.path.splitext(it.icon_url.split("?")[0])[1] or ".png"
        path = os.path.join(icon_dir, f"{safe}{ext}")
        if not os.path.exists(path):
            try:
                r = session.get(it.icon_url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(r.content)
                time.sleep(sleep)
            except requests.RequestException as exc:  # pragma: no cover - network
                print(f"  ! failed to download icon for {it.name}: {exc}")
                continue
        it.icon_path = path


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def scrape(data_dir: str = DEFAULT_DATA_DIR, with_icons: bool = True) -> Catalog:
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
        download_icons(catalog, data_dir, session)

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
