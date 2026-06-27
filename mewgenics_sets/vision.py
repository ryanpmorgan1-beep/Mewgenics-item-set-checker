"""Locate the storage grid in a screenshot, crop each cell, and match each
filled cell to the wiki icon catalog.

In-game storage icons are small, desaturated and tinted, so we normalize both
the cell crops and the reference icons (grayscale + autocontrast + resize) and
score with a blend of perceptual hashing and normalized template correlation.
Matching is imperfect by nature, so we return the top-K candidates per cell and
let the HTML report ask you to confirm (assisted mode).

Grid handling:
  * If you pass an explicit --grid x,y,w,h (+ --rows/--cols), we slice that.
  * Otherwise we attempt auto-detection of a regular grid of square cells
    (needs opencv). Auto-detection is best-effort; prefer the explicit box for
    reliable results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageOps

try:
    import imagehash
except ImportError:  # pragma: no cover
    imagehash = None


def _looks_like_svg(head: bytes) -> bool:
    """True if the first bytes of a file are SVG/XML rather than a raster image."""
    sniff = head.lstrip()[:512].lower()
    return sniff.startswith(b"<?xml") or sniff.startswith(b"<svg") or b"<svg" in sniff


def _rasterize_svg(path: str) -> Image.Image:
    """Rasterize an SVG file to an RGB image, trying available renderers.

    Raises ImportError (with install hint) if no working renderer is found.
    """
    # --- attempt 1: svglib + reportlab (pure Python, easiest on Windows) ---
    try:
        from svglib.svglib import svg2rlg          # type: ignore
        from reportlab.graphics import renderPM    # type: ignore
        drawing = svg2rlg(path)
        if drawing is not None and getattr(drawing, "width", 0) > 0:
            return renderPM.drawToPIL(drawing, dpi=96).convert("RGB")
    except ImportError:
        pass
    except Exception:
        pass  # malformed SVG / renderer incompatibility — fall through

    # --- attempt 2: cairosvg ---
    try:
        import cairosvg                            # type: ignore
        from io import BytesIO
        png = cairosvg.svg2png(url=path, output_width=64, output_height=64)
        return Image.open(BytesIO(png)).convert("RGB")
    except ImportError:
        pass
    except Exception:
        pass

    raise ImportError(
        f"Cannot rasterize SVG content at {path}.\n"
        "The icon files contain SVG (vector) data, which Pillow cannot read.\n"
        "Re-download icons as PNG:  python -m mewgenics_sets scrape --png-icons\n"
        "or install a renderer:     pip install cairosvg"
    )


def _open_icon(path: str) -> Image.Image:
    """Open an icon file as an RGB image, detecting SVG by *content* not name.

    The wiki sometimes hands back SVG bytes even for a ``.png`` URL, so we
    sniff the file header instead of trusting the extension.
    """
    with open(path, "rb") as fh:
        head = fh.read(512)
    if _looks_like_svg(head):
        return _rasterize_svg(path)
    return Image.open(path).convert("RGB")

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from .models import Catalog, Item

NORM_SIZE = 64          # everything is compared at 64x64 grayscale
EMPTY_STD_THRESHOLD = 12.0   # cells flatter than this are treated as empty slots


# --------------------------------------------------------------------------- #
# Grid geometry
# --------------------------------------------------------------------------- #
@dataclass
class Grid:
    x: int
    y: int
    w: int
    h: int
    rows: int
    cols: int

    def cells(self) -> list[tuple[int, int, int, int, int, int]]:
        """Yield (row, col, x0, y0, x1, y1) for each cell."""
        cw = self.w / self.cols
        ch = self.h / self.rows
        out = []
        for r in range(self.rows):
            for c in range(self.cols):
                x0 = int(self.x + c * cw)
                y0 = int(self.y + r * ch)
                x1 = int(self.x + (c + 1) * cw)
                y1 = int(self.y + (r + 1) * ch)
                out.append((r, c, x0, y0, x1, y1))
        return out


def parse_grid_arg(grid: str, rows: int, cols: int) -> Grid:
    parts = [int(p) for p in grid.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError("--grid must be 'x,y,w,h'")
    x, y, w, h = parts
    return Grid(x=x, y=y, w=w, h=h, rows=rows, cols=cols)


def auto_detect_grid(img: Image.Image, cols_hint: int = 11) -> Optional[Grid]:
    """Best-effort detection of the storage grid using contour analysis.

    Returns None if opencv is unavailable or no convincing grid is found; the
    caller should then fall back to an explicit --grid box.
    """
    if cv2 is None:
        return None
    arr = np.array(img.convert("L"))
    # Cell borders are darker outlines on a light sheet; threshold + find squares.
    thr = cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 8
    )
    contours, _ = cv2.findContours(thr, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w == 0 or h == 0:
            continue
        ar = w / h
        if 0.8 <= ar <= 1.25 and 28 <= w <= 120 and 28 <= h <= 120:
            boxes.append((x, y, w, h))
    if len(boxes) < cols_hint * 2:
        return None

    # Most common cell size -> the grid pitch.
    widths = sorted(b[2] for b in boxes)
    cell = widths[len(widths) // 2]
    xs = sorted(b[0] for b in boxes)
    ys = sorted(b[1] for b in boxes)
    x0, y0 = xs[0], ys[0]
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    cols = max(1, round((x1 - x0) / cell))
    rows = max(1, round((y1 - y0) / cell))
    return Grid(x=x0, y=y0, w=x1 - x0, h=y1 - y0, rows=rows, cols=cols)


# --------------------------------------------------------------------------- #
# Normalization + matching
# --------------------------------------------------------------------------- #
def _normalize(img: Image.Image, inset: float = 0.12) -> Image.Image:
    """Crop padding, grayscale, autocontrast, resize to NORM_SIZE."""
    w, h = img.size
    dx, dy = int(w * inset), int(h * inset)
    if w - 2 * dx > 4 and h - 2 * dy > 4:
        img = img.crop((dx, dy, w - dx, h - dy))
    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=2)
    return img.resize((NORM_SIZE, NORM_SIZE), Image.LANCZOS)


def _to_vec(img: Image.Image) -> np.ndarray:
    a = np.asarray(img, dtype=np.float32).ravel()
    a -= a.mean()
    n = np.linalg.norm(a)
    return a / n if n > 0 else a


@dataclass
class Reference:
    item: Item
    phash: object
    vec: np.ndarray


def build_references(catalog: Catalog) -> list[Reference]:
    import os

    refs: list[Reference] = []
    svg_warn_shown = False

    n_total = len(catalog.items)
    n_no_path = 0          # item has no icon_path recorded in the catalog
    n_missing_file = 0     # icon_path set but file not on disk
    n_svg_unrenderable = 0 # file is SVG and no renderer worked
    n_open_failed = 0      # some other open/decode error
    sample_failures: list[str] = []

    for it in catalog.items:
        if not it.icon_path:
            n_no_path += 1
            continue
        if not os.path.exists(it.icon_path):
            n_missing_file += 1
            if len(sample_failures) < 5:
                sample_failures.append(f"missing file: {it.icon_path}")
            continue
        try:
            icon = _open_icon(it.icon_path)
        except ImportError as exc:
            n_svg_unrenderable += 1
            if not svg_warn_shown:
                print(f"\nWARNING: {exc}\n")
                svg_warn_shown = True
            continue
        except (OSError, ValueError) as exc:
            n_open_failed += 1
            if len(sample_failures) < 5:
                sample_failures.append(f"{type(exc).__name__} on {it.icon_path}: {exc}")
            continue
        norm = _normalize(icon, inset=0.04)
        ph = imagehash.phash(norm) if imagehash else None
        refs.append(Reference(item=it, phash=ph, vec=_to_vec(norm)))

    if not refs:
        print("\nNo usable icon references were built. Breakdown:")
        print(f"  items in catalog ............ {n_total}")
        print(f"  with no icon_path ........... {n_no_path}")
        print(f"  icon file missing on disk ... {n_missing_file}")
        print(f"  SVG (no renderer) ........... {n_svg_unrenderable}")
        print(f"  other open/decode failures .. {n_open_failed}")
        for line in sample_failures:
            print(f"    - {line}")
        if n_svg_unrenderable:
            print("\n  Fix: re-download icons as PNG ->  "
                  "python -m mewgenics_sets scrape --png-icons")
    return refs


@dataclass
class Candidate:
    item: Item
    score: float        # 0..1, higher is better


@dataclass
class CellMatch:
    row: int
    col: int
    box: tuple[int, int, int, int]
    empty: bool
    crop_path: str = ""
    candidates: list[Candidate] = field(default_factory=list)


def _score(cell_norm: Image.Image, cell_vec: np.ndarray, ref: Reference) -> float:
    # Template correlation in [-1, 1] -> [0, 1].
    corr = float(np.dot(cell_vec, ref.vec))
    corr01 = (corr + 1.0) / 2.0
    if ref.phash is not None and imagehash is not None:
        ph = imagehash.phash(cell_norm)
        dist = ph - ref.phash            # 0..64 hamming
        ph01 = 1.0 - (dist / 64.0)
        return 0.5 * corr01 + 0.5 * ph01
    return corr01


def match_cell(crop: Image.Image, refs: list[Reference], top_k: int = 3) -> list[Candidate]:
    norm = _normalize(crop)
    vec = _to_vec(norm)
    scored = [Candidate(item=r.item, score=_score(norm, vec, r)) for r in refs]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_k]


def _is_empty(crop: Image.Image) -> bool:
    a = np.asarray(crop.convert("L"), dtype=np.float32)
    return float(a.std()) < EMPTY_STD_THRESHOLD


def analyze_screenshot(
    screenshot_path: str,
    grid: Grid,
    refs: list[Reference],
    crop_dir: str,
    top_k: int = 3,
) -> list[CellMatch]:
    import os

    os.makedirs(crop_dir, exist_ok=True)
    img = Image.open(screenshot_path).convert("RGB")
    results: list[CellMatch] = []
    for (r, c, x0, y0, x1, y1) in grid.cells():
        crop = img.crop((x0, y0, x1, y1))
        empty = _is_empty(crop)
        cm = CellMatch(row=r, col=c, box=(x0, y0, x1, y1), empty=empty)
        if not empty:
            crop_path = os.path.join(crop_dir, f"cell_{r:02d}_{c:02d}.png")
            crop.save(crop_path)
            cm.crop_path = crop_path
            cm.candidates = match_cell(crop, refs, top_k=top_k)
        results.append(cm)
    return results
