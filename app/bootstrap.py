"""Fetch the item + set catalog and icon images from the Mewgenics wiki.

Runs wherever outbound network is available: automatically at app startup on
Railway (first boot takes a minute or two), or manually on your own machine:

    python -m app.bootstrap                 # writes data/catalog.json + icons
    python -m app.bootstrap --bundle data_bundle.zip   # also zips it up

The zip can be imported into a deployed instance via the UI ("Import data
bundle") — the offline fallback for the unlikely case the wiki blocks the
server's requests.

Data sources (https://mewgenics.wiki.gg, MediaWiki):
  * ``Items`` page      -> name, slot, rarity, cursed, description, uses,
                           set tags, icon image
  * ``Item_sets`` page  -> per-set bonus text + member items
  * icon images         -> fetched from the thumbnail URLs already present in
                           the Items table, rewritten to a larger size.
                           MediaWiki thumb URLs are predictable
                           (``.../thumb/a/ab/X.svg/40px-X.svg.png`` ->
                           ``160px-X.svg.png``), and the server rasterizes
                           SVG sources to PNG on demand. We deliberately do
                           NOT use the ``imageinfo`` API: its 50-titles-long
                           query strings trip Cloudflare's WAF (observed 403s
                           even from residential IPs while plain page and
                           image GETs pass).

Parsers select columns by header *name*, not position, so modest wiki layout
changes keep working. Structure verified against the live wiki (table class
``shuffle__items`` with a leading icon cell not present in the header row;
per-set tables with class ``mew-sets-table``).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .catalog import (
    Catalog, Item, SetInfo, ICON_DIR, canonical_slot, catalog_path,
    load_catalog, save_catalog,
)

# The wiki sits behind Cloudflare, which 403s generic Python HTTP clients from
# datacenter IPs (e.g. Railway) based on TLS fingerprinting. curl_cffi
# impersonates a real browser's TLS/HTTP2 fingerprint and usually passes.
# Plain requests remains as fallback (fine from residential IPs).
try:
    from curl_cffi import requests as curl_requests
except ImportError:                                     # pragma: no cover
    curl_requests = None

WIKI = "https://mewgenics.wiki.gg"
API = f"{WIKI}/api.php"
ICON_PX = 160          # requested thumbnail width — plenty for 96px matching

_UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "MewgenicsSetChecker/1.0 (personal tool)",
]

# Browser profiles curl_cffi can impersonate, tried in order on retries.
_IMPERSONATE = ["chrome", "safari", "edge101"]

ProgressCb = Callable[[str], None]


class _Fetcher:
    """One retrying GET interface over curl_cffi (preferred) or requests."""

    def __init__(self) -> None:
        self._mode = "curl" if curl_requests is not None else "requests"
        self._make_session(0)

    def _make_session(self, attempt: int) -> None:
        if self._mode == "curl":
            profile = _IMPERSONATE[attempt % len(_IMPERSONATE)]
            try:
                self._s = curl_requests.Session(impersonate=profile)
                self._s.headers.update({"Accept-Language": "en-US,en;q=0.9",
                                        "Referer": f"{WIKI}/wiki/Items"})
                return
            except Exception:                            # pragma: no cover
                self._mode = "requests"                  # bad profile name etc.
        s = requests.Session()
        s.headers.update({
            "User-Agent": _UAS[attempt % len(_UAS)],
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{WIKI}/wiki/Items",
        })
        self._s = s

    def get(self, url: str, *, params=None, tries: int = 3,
            progress: Optional[ProgressCb] = None):
        last: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self._s.get(url, params=params, timeout=40)
                if resp.status_code in (403, 429, 503) and attempt < tries - 1:
                    if progress:
                        progress(f"HTTP {resp.status_code} from wiki — retrying "
                                 f"with a different browser fingerprint ...")
                    time.sleep(2 * (attempt + 1))
                    self._make_session(attempt + 1)
                    continue
                resp.raise_for_status()
                return resp
            except Exception as exc:   # network/TLS layer errors from either backend
                last = exc
                time.sleep(2 * (attempt + 1))
                self._make_session(attempt + 1)
        raise RuntimeError(f"wiki request failed after {tries} tries: {last}")


def _session(ua_index: int = 0) -> _Fetcher:
    return _Fetcher()


def _get(session: _Fetcher, url: str, *, params=None, tries: int = 3,
         progress: Optional[ProgressCb] = None):
    return session.get(url, params=params, tries=tries, progress=progress)


def _parse_page_html(page: str, session: requests.Session,
                     progress: Optional[ProgressCb] = None) -> BeautifulSoup:
    resp = _get(session, API, params={
        "action": "parse", "page": page, "prop": "text",
        "format": "json", "formatversion": "2", "redirects": "1",
    }, progress=progress)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"wiki API error for {page!r}: {data['error']}")
    return BeautifulSoup(data["parse"]["text"], "lxml")


def _abs_url(src: str) -> str:
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    return urljoin(WIKI, src)


def _best_icon_url(img_tag) -> str:
    """Highest-resolution source from an <img> (srcset-aware)."""
    if img_tag is None:
        return ""
    srcset = img_tag.get("srcset")
    if srcset:
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
def _find_col(hmap: dict[str, int], *needles: str) -> Optional[int]:
    for label, idx in hmap.items():
        if any(n in label for n in needles):
            return idx
    return None


def parse_items(soup: BeautifulSoup) -> list[Item]:
    """Parse the Items page.

    The header row has no Icon column, but every data row has a leading icon
    cell — so data-cell indices are shifted by +1 relative to the header.
    """
    table = soup.find("table", class_="shuffle__items")
    if not table:
        tables = soup.select("table.wikitable, table.sortable")
        table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
    if not table:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []
    header_cells = rows[0].find_all(["th", "td"])
    n_headers = len(header_cells)
    hmap = {c.get_text(" ", strip=True).lower(): i for i, c in enumerate(header_cells)}

    name_col = _find_col(hmap, "name")
    slot_col = _find_col(hmap, "slot")
    rar_col = _find_col(hmap, "rarit")
    cursed_col = _find_col(hmap, "cursed")
    desc_col = _find_col(hmap, "description", "effect")
    uses_col = _find_col(hmap, "uses")
    sets_col = _find_col(hmap, "set")
    if name_col is None:
        return []

    items: list[Item] = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
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

        icon_url = ""
        if off == 1:
            icon_url = _best_icon_url(cells[0].find("img"))
        if not icon_url:   # some layouts put the icon inside the name cell
            name_cell = cell(name_col)
            if name_cell is not None:
                icon_url = _best_icon_url(name_cell.find("img"))

        sets_cell = cell(sets_col)
        sets = _split_sets(get(sets_col), sets_cell) if sets_cell is not None else []

        items.append(Item(
            name=name,
            slot=canonical_slot(get(slot_col)),
            rarity=get(rar_col),
            cursed=get(cursed_col),
            effect=get(desc_col),
            uses=get(uses_col),
            sets=sets,
            page_url=page_url,
            icon_url=icon_url,
        ))
    return items


def _split_sets(raw: str, cell) -> list[str]:
    links = [a.get_text(strip=True) for a in cell.find_all("a")]
    names = links if links else re.split(r"[,/]| and ", raw)
    out: list[str] = []
    for n in names:
        n = n.strip()
        if n and n not in {"-", "—", "None"}:
            out.append(n)
    return out


# --------------------------------------------------------------------------- #
# Item sets page
# --------------------------------------------------------------------------- #
def parse_sets(soup: BeautifulSoup) -> dict[str, SetInfo]:
    """Each set: an h2/h3 heading, bonus paragraph(s), then a members table."""
    sets: dict[str, SetInfo] = {}
    tables = soup.find_all("table", class_="mew-sets-table")
    if not tables:   # fallback: any small Slot/Item table
        tables = [t for t in soup.find_all("table")
                  if t.find("tr") and "slot" in t.find("tr").get_text(" ", strip=True).lower()]

    for table in tables:
        heading = table.find_previous(["h2", "h3", "h4"])
        if not heading:
            continue
        span = heading.find("span", class_="mw-headline")
        if span:
            set_name = span.get_text(" ", strip=True)
        else:
            set_name = re.sub(r"\[edit[^\]]*\]", "", heading.get_text(" ", strip=True)).strip()
        if not set_name:
            continue

        bonus_parts: list[str] = []
        for sib in heading.find_next_siblings():
            if sib is table or sib.name in ("h2", "h3", "h4"):
                break
            if hasattr(sib, "get_text") and sib.name not in ("table",):
                txt = sib.get_text(" ", strip=True)
                if txt:
                    bonus_parts.append(txt)

        members: list[str] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            link = cells[1].find("a")
            item_name = link.get_text(strip=True) if link else cells[1].get_text(strip=True)
            if item_name:
                members.append(item_name)

        if set_name not in sets:
            sets[set_name] = SetInfo(name=set_name, bonus=" ".join(bonus_parts),
                                     members=members)
    return sets


# --------------------------------------------------------------------------- #
# Icons via direct (rewritten) thumbnail URLs
# --------------------------------------------------------------------------- #
def source_file_from_image_url(url: str) -> str:
    """Recover the wiki File name behind an <img> URL.

    Thumb URLs look like ``/images/thumb/a/ab/Name.svg/40px-Name.svg.png``
    (source = second-to-last segment); direct URLs end with the file name.
    """
    path = url.split("?")[0].rstrip("/")
    segments = path.split("/")
    if "thumb" in segments and len(segments) >= 2:
        return unquote(segments[-2])
    return unquote(segments[-1]) if segments else ""


def upsize_thumb_url(url: str, px: int) -> str | None:
    """Rewrite a MediaWiki thumb URL to a different pixel size.

    ``.../thumb/a/ab/X.svg/40px-X.svg.png`` -> ``.../thumb/a/ab/X.svg/160px-...``
    Returns None for non-thumb URLs.
    """
    path = url.split("?")[0]
    m = re.match(r"^(.*/thumb/.*/)\d+px-([^/]+)$", path)
    if not m:
        return None
    return f"{m.group(1)}{px}px-{m.group(2)}"


def original_url_from_thumb(url: str) -> str | None:
    """``.../images/thumb/a/ab/X.png/40px-X.png`` -> ``.../images/a/ab/X.png``."""
    path = url.split("?")[0]
    m = re.match(r"^(.*)/thumb/(.+)/[^/]+$", path)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def thumb_url_from_original(url: str, px: int) -> str | None:
    """Build the on-demand thumbnail URL for a direct file URL.

    The Items table links icons as raw ``.svg`` files (browsers render those
    natively). MediaWiki's thumb handler rasterizes them to PNG at any size
    via the predictable path
    ``/images/a/ab/X.svg`` -> ``/images/thumb/a/ab/X.svg/160px-X.svg.png``.
    Works for raster originals too (same pattern, no extra ``.png`` suffix).
    """
    path = url.split("?")[0]
    if "/thumb/" in path:
        return None
    m = re.match(r"^(.*)/images/((?:[^/]+/)*)([^/]+)$", path)
    if not m:
        return None
    base, mid, fname = m.group(1), m.group(2) or "", m.group(3)
    suffix = f"{px}px-{fname}"
    if fname.lower().endswith(".svg"):
        suffix += ".png"
    return f"{base}/images/thumb/{mid}{fname}/{suffix}"


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "item"


def _looks_like_raster(data: bytes) -> bool:
    return (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def download_icons(catalog: Catalog, data_dir: str, session,
                   progress: Optional[ProgressCb] = None) -> dict:
    """Fetch every item's icon as a raster PNG using plain image GETs.

    Candidate URLs per item, first raster wins:
      1. thumb URL rewritten to ICON_PX (when the scraped URL is a thumb)
      2. constructed on-demand thumb (when the scraped URL is a direct file,
         which is the norm here — the Items table links raw SVGs)
      3. the URL exactly as scraped
      4. the original full-size file (only when the source isn't an SVG)
    """
    icon_dir = os.path.join(data_dir, ICON_DIR)
    os.makedirs(icon_dir, exist_ok=True)

    stats = {"ok": 0, "failed": 0, "no_url": 0}
    bytes_cache: dict[str, bytes | None] = {}   # many items share one icon URL

    def fetch_raster(url: str) -> bytes | None:
        if url in bytes_cache:
            return bytes_cache[url]
        data = None
        try:
            r = _get(session, url, tries=2)
            if _looks_like_raster(r.content):
                data = r.content
        except RuntimeError:
            pass
        bytes_cache[url] = data
        time.sleep(0.03)
        return data

    total = len(catalog.items)
    for n, it in enumerate(catalog.items):
        if not it.icon_url:
            stats["no_url"] += 1
            continue
        src = source_file_from_image_url(it.icon_url)
        candidates = []
        up = upsize_thumb_url(it.icon_url, ICON_PX)
        if up:
            candidates.append(up)
        built = thumb_url_from_original(it.icon_url, ICON_PX)
        if built:
            candidates.append(built)
        candidates.append(it.icon_url)
        orig = original_url_from_thumb(it.icon_url)
        if orig and not src.lower().endswith(".svg"):
            candidates.append(orig)
        candidates = list(dict.fromkeys(candidates))

        data = None
        for u in candidates:
            data = fetch_raster(u)
            if data:
                break
        if data is None:
            stats["failed"] += 1
            continue
        fname = _slug(it.name) + ".png"
        with open(os.path.join(icon_dir, fname), "wb") as fh:
            fh.write(data)
        it.icon_file = fname
        stats["ok"] += 1
        if progress and (n + 1) % 50 == 0:
            progress(f"icons {n + 1}/{total} ...")
    return stats


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_bootstrap(data_dir: str, progress: Optional[ProgressCb] = None) -> Catalog:
    """Scrape everything and write catalog + icons + a report. Raises on failure."""
    os.makedirs(data_dir, exist_ok=True)
    log: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        if progress:
            progress(msg)

    session = _session()
    note("fetching Items page ...")
    items = parse_items(_parse_page_html("Items", session, progress=note))
    note(f"parsed {len(items)} items")
    if len(items) < 30:
        raise RuntimeError(
            f"only {len(items)} items parsed — the wiki layout may have "
            "changed, or the page was served incompletely")

    note("fetching Item_sets page ...")
    sets = parse_sets(_parse_page_html("Item_sets", session, progress=note))
    note(f"parsed {len(sets)} sets")

    catalog = Catalog(
        items=items, sets=sets,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=WIKI,
    )

    # Cross-fill: set tags on items <-> member lists on sets.
    for it in items:
        for s in it.sets:
            info = catalog.sets.setdefault(s, SetInfo(name=s))
            if it.name not in info.members:
                info.members.append(it.name)
    by_name = {it.name.lower(): it for it in items}
    for info in catalog.sets.values():
        for m in info.members:
            it = by_name.get(m.lower())
            if it and info.name not in it.sets:
                it.sets.append(info.name)

    note("downloading icons ...")
    stats = download_icons(catalog, data_dir, session, progress=note)
    note(f"icons: {stats}")

    save_catalog(catalog, data_dir)
    report = {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": len(items),
        "sets": len(catalog.sets),
        "icons": stats,
        "log": log[-50:],
    }
    with open(os.path.join(data_dir, "bootstrap_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    note("bootstrap complete")
    return catalog


# --------------------------------------------------------------------------- #
# Bundle export / import (offline fallback)
# --------------------------------------------------------------------------- #
def export_bundle(data_dir: str, out_path: str) -> str:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(catalog_path(data_dir), "catalog.json")
        icon_dir = os.path.join(data_dir, ICON_DIR)
        for fname in sorted(os.listdir(icon_dir)):
            z.write(os.path.join(icon_dir, fname), f"icons/{fname}")
    return out_path


def import_bundle(payload: bytes, data_dir: str) -> dict:
    """Validate and install a bundle zip. Returns summary stats."""
    if len(payload) > 80 * 1024 * 1024:
        raise ValueError("bundle too large")
    try:
        z = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip file: {exc}") from exc
    names = z.namelist()
    if "catalog.json" not in names:
        raise ValueError("bundle is missing catalog.json")
    try:
        cat_payload = json.loads(z.read("catalog.json").decode("utf-8"))
        n_items = len(cat_payload.get("items", []))
    except (ValueError, KeyError) as exc:
        raise ValueError(f"catalog.json is invalid: {exc}") from exc
    if n_items < 10:
        raise ValueError(f"catalog.json has only {n_items} items")

    os.makedirs(os.path.join(data_dir, ICON_DIR), exist_ok=True)
    with open(catalog_path(data_dir), "wb") as fh:
        fh.write(z.read("catalog.json"))
    n_icons = 0
    for name in names:
        if not name.startswith("icons/") or name.endswith("/"):
            continue
        base = os.path.basename(name)
        if not base or not base.lower().endswith(".png"):
            continue
        data = z.read(name)
        if len(data) > 4 * 1024 * 1024 or not _looks_like_raster(data):
            continue
        with open(os.path.join(data_dir, ICON_DIR, base), "wb") as fh:
            fh.write(data)
        n_icons += 1
    return {"items": n_items, "icons": n_icons}


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape the Mewgenics wiki catalog")
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--bundle", metavar="ZIP",
                    help="also export a data bundle zip for offline import")
    args = ap.parse_args()

    catalog = run_bootstrap(args.data_dir, progress=print)
    print(f"catalog: {len(catalog.items)} items, {len(catalog.sets)} sets")
    if args.bundle:
        export_bundle(args.data_dir, args.bundle)
        print(f"bundle written -> {args.bundle}")


if __name__ == "__main__":
    main()
