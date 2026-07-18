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
  * icon images         -> the Items table links icons as **raw SVG files**
                           and this wiki serves no PNG renditions of them at
                           all: the on-demand thumb path 404s, and the
                           ``imageinfo`` API's 50-titles-long query strings
                           trip Cloudflare's WAF (403 even from residential
                           IPs, while plain page and image GETs pass). So we
                           download the SVGs and rasterize them locally with
                           resvg (prebuilt wheels, no system deps).

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

# The wiki's item icons are raw SVGs; resvg rasterizes them to PNG locally.
try:
    import resvg_py
except ImportError:                                     # pragma: no cover
    resvg_py = None

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


class _NotFound(RuntimeError):
    """Deterministic 404 — retrying is pointless."""


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

    def get(self, url: str, *, params=None, tries: int = 3, timeout: int = 40,
            progress: Optional[ProgressCb] = None):
        last: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self._s.get(url, params=params, timeout=timeout)
                if resp.status_code == 404:
                    raise _NotFound(f"HTTP 404: {url}")
                if resp.status_code in (403, 429, 503) and attempt < tries - 1:
                    if progress:
                        progress(f"HTTP {resp.status_code} from wiki — retrying "
                                 f"with a different browser fingerprint ...")
                    time.sleep(2 * (attempt + 1))
                    self._make_session(attempt + 1)
                    continue
                resp.raise_for_status()
                return resp
            except _NotFound:
                raise
            except Exception as exc:   # network/TLS layer errors from either backend
                last = exc
                if attempt < tries - 1:
                    time.sleep(1 + attempt)
                    self._make_session(attempt + 1)
        raise RuntimeError(f"{type(last).__name__}: {last}")


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


def _looks_like_svg(data: bytes) -> bool:
    head = data.lstrip()[:512].lower()
    return head.startswith(b"<") and (b"<svg" in head or head.startswith(b"<?xml"))


def _rasterize_svg(data: bytes, px: int) -> bytes | None:
    """SVG bytes -> PNG bytes (with alpha) at the given width, or None."""
    if resvg_py is not None:
        try:
            out = resvg_py.svg_to_bytes(svg_string=data.decode("utf-8", "replace"),
                                        width=px)
            png = bytes(out)
            if _looks_like_raster(png):
                return png
        except Exception:
            pass
    try:                                               # optional fallback
        import cairosvg
        png = cairosvg.svg2png(bytestring=data, output_width=px)
        if png and _looks_like_raster(png):
            return png
    except Exception:
        pass
    return None


ICON_TIMEOUT = 12       # seconds per image request (2 tries)
ICON_WORKERS = 8        # parallel downloads
FAIL_FAST_AFTER = 25    # abort if this many icons fail before any succeeds


def _icon_candidates(icon_url: str) -> list[str]:
    """Candidate URLs for one icon, most-preferred first.

    The scraped URL itself comes first for direct files (it's a raw SVG we
    rasterize locally); size-rewritten thumb URLs help when the scraped URL
    is already a thumb; the constructed thumb is a last resort (404s when
    the wiki has no PNG renditions, which costs one fast request).
    """
    src = source_file_from_image_url(icon_url)
    candidates = []
    up = upsize_thumb_url(icon_url, ICON_PX)
    if up:
        candidates.append(up)
    candidates.append(icon_url)
    orig = original_url_from_thumb(icon_url)
    if orig and not src.lower().endswith(".svg"):
        candidates.append(orig)
    built = thumb_url_from_original(icon_url, ICON_PX)
    if built:
        candidates.append(built)
    return list(dict.fromkeys(candidates))


def download_icons(catalog: Catalog, data_dir: str, session=None,
                   progress: Optional[ProgressCb] = None) -> dict:
    """Fetch every item's icon as a raster PNG using plain image GETs.

    Runs ICON_WORKERS downloads in parallel (each worker owns its own
    session), logs the first few failures verbosely, and aborts early if
    nothing succeeds — so a blocked network fails loudly in seconds instead
    of grinding silently for an hour.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    icon_dir = os.path.join(data_dir, ICON_DIR)
    os.makedirs(icon_dir, exist_ok=True)

    stats = {"ok": 0, "failed": 0, "no_url": 0}
    lock = threading.Lock()
    fail_notes: list[str] = []
    done_count = [0]
    abort = threading.Event()
    fetchers = threading.local()

    total = len(catalog.items)

    def work(it: Item) -> None:
        if abort.is_set():
            return
        if not it.icon_url:
            with lock:
                stats["no_url"] += 1
            return
        f = getattr(fetchers, "f", None)
        if f is None:
            f = _Fetcher()
            fetchers.f = f
        data, last_err = None, ""
        for u in _icon_candidates(it.icon_url):
            try:
                r = f.get(u, tries=2, timeout=ICON_TIMEOUT)
            except RuntimeError as exc:
                last_err = f"{exc}"
                continue
            if _looks_like_raster(r.content):
                data = r.content
                break
            if _looks_like_svg(r.content):
                png = _rasterize_svg(r.content, ICON_PX)
                if png:
                    data = png
                    break
                last_err = ("SVG icon but no rasterizer worked — "
                            "pip install resvg-py")
            else:
                last_err = f"unrecognized content ({len(r.content)}B) from {u}"
        with lock:
            if data is None:
                stats["failed"] += 1
                if len(fail_notes) < 6:
                    fail_notes.append(last_err)
                    if progress:
                        progress(f"icon failed: {it.name}: {last_err}")
                if stats["ok"] == 0 and stats["failed"] >= FAIL_FAST_AFTER:
                    abort.set()
            else:
                fname = _slug(it.name) + ".png"
                with open(os.path.join(icon_dir, fname), "wb") as fh:
                    fh.write(data)
                it.icon_file = fname
                stats["ok"] += 1
            done_count[0] += 1
            if progress and done_count[0] % 50 == 0:
                progress(f"icons {done_count[0]}/{total} "
                         f"(ok {stats['ok']}, failed {stats['failed']}) ...")

    with ThreadPoolExecutor(max_workers=ICON_WORKERS) as pool:
        list(pool.map(work, catalog.items))

    if abort.is_set():
        raise RuntimeError(
            f"aborted: first {stats['failed']} icon downloads all failed "
            f"(e.g. {fail_notes[0] if fail_notes else 'unknown'}). The wiki "
            "is refusing image requests from this network right now — wait "
            "10-20 minutes and re-run, or run the bootstrap elsewhere.")
    return stats


def probe(data_dir: str) -> None:
    """Verbosely test icon fetching for the first few catalog items."""
    try:
        catalog = load_catalog(data_dir)
    except FileNotFoundError:
        print("no catalog.json yet — fetching the Items page first ...")
        session = _session()
        items = parse_items(_parse_page_html("Items", session, progress=print))
        catalog = Catalog(items=items)
        print(f"parsed {len(items)} items")

    with_icons = [it for it in catalog.items if it.icon_url][:3]
    if not with_icons:
        print("no items with icon URLs — the Items page parse found no images")
        return
    f = _Fetcher()
    print(f"fetch backend: {f._mode}, svg rasterizer: "
          f"{'resvg' if resvg_py is not None else 'MISSING (pip install resvg-py)'}")
    for it in with_icons:
        print(f"\n=== {it.name}")
        print(f"    scraped icon_url: {it.icon_url}")
        for u in _icon_candidates(it.icon_url):
            t0 = time.time()
            try:
                r = f.get(u, tries=1, timeout=10)
            except RuntimeError as exc:
                print(f"    [ERR] {time.time()-t0:5.1f}s  {u}\n          {exc}")
                continue
            if _looks_like_raster(r.content):
                kind = "PNG"
            elif _looks_like_svg(r.content):
                png = _rasterize_svg(r.content, ICON_PX)
                kind = f"SVG->PNG({len(png)}B)" if png else "SVG (raster FAILED)"
            else:
                kind = "other"
            print(f"    [{r.status_code}] {kind:18s} {len(r.content):7d}B "
                  f"{time.time()-t0:5.1f}s  {u}")


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
    ap.add_argument("--probe", action="store_true",
                    help="verbosely test icon fetching for 3 items, then exit")
    args = ap.parse_args()

    if args.probe:
        probe(args.data_dir)
        return

    catalog = run_bootstrap(args.data_dir, progress=print)
    print(f"catalog: {len(catalog.items)} items, {len(catalog.sets)} sets")
    if args.bundle:
        export_bundle(args.data_dir, args.bundle)
        print(f"bundle written -> {args.bundle}")


if __name__ == "__main__":
    main()
