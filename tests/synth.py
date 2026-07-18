"""Synthetic Mewgenics-style screenshot fixture.

Generates procedural line-art "item icons", composes them into a screenshot
that mimics the game's storage screen (parchment background, grey tile
lattice, corner slot glyphs, blessed-yellow glows, cursed-red tints, NEW
ribbons, sensor noise + JPEG compression, decoy UI on the left), and records
the ground truth. Lets us regression-test grid detection and icon matching
end-to-end without the real game or network.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

import cv2
import numpy as np

ICON_CANVAS = 128


# --------------------------------------------------------------------------- #
# Procedural icons
# --------------------------------------------------------------------------- #
def _draw_icon(rng: random.Random) -> np.ndarray:
    """One distinct line-art icon: BGRA uint8, opaque silhouette + outlines."""
    img = np.zeros((ICON_CANVAS, ICON_CANVAS, 4), np.uint8)
    ink = (30, 28, 26, 255)
    n_shapes = rng.randint(2, 4)
    colored = rng.random() < 0.22
    fill_color = rng.choice([
        (196, 196, 240, 255),   # reddish (BGR)
        (208, 170, 244, 255),   # pink
        (150, 222, 246, 255),   # yellow
        (240, 205, 160, 255),   # blue
    ]) if colored else (235, 233, 230, 255)

    for _ in range(n_shapes):
        kind = rng.choice(["poly", "circle", "rect", "stroke", "arc"])
        cx, cy = rng.randint(30, 98), rng.randint(30, 98)
        size = rng.randint(18, 46)
        if kind == "poly":
            pts = []
            for k in range(rng.randint(3, 6)):
                ang = 2 * np.pi * k / 5 + rng.uniform(-0.5, 0.5)
                rr = size * rng.uniform(0.55, 1.0)
                pts.append([int(cx + rr * np.cos(ang)), int(cy + rr * np.sin(ang))])
            pts = np.array([pts], np.int32)
            cv2.fillPoly(img, pts, fill_color)
            cv2.polylines(img, pts, True, ink, rng.randint(3, 5), cv2.LINE_AA)
        elif kind == "circle":
            cv2.circle(img, (cx, cy), size // 2 + 6, fill_color, -1, cv2.LINE_AA)
            cv2.circle(img, (cx, cy), size // 2 + 6, ink, rng.randint(3, 5), cv2.LINE_AA)
        elif kind == "rect":
            w, h = size, int(size * rng.uniform(0.5, 1.4))
            cv2.rectangle(img, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2),
                          fill_color, -1)
            cv2.rectangle(img, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2),
                          ink, rng.randint(3, 5))
        elif kind == "stroke":
            x2, y2 = rng.randint(20, 108), rng.randint(20, 108)
            cv2.line(img, (cx, cy), (x2, y2), ink, rng.randint(4, 7), cv2.LINE_AA)
        else:
            cv2.ellipse(img, (cx, cy), (size, int(size * 0.7)),
                        rng.uniform(0, 180), 0, rng.uniform(120, 300), ink,
                        rng.randint(3, 5), cv2.LINE_AA)
    # A couple of detail dots so icons with similar outlines still differ.
    for _ in range(rng.randint(1, 4)):
        cv2.circle(img, (rng.randint(24, 104), rng.randint(24, 104)),
                   rng.randint(2, 5), ink, -1, cv2.LINE_AA)
    return img


def make_icons(n: int, seed: int = 7) -> list[np.ndarray]:
    rng = random.Random(seed)
    return [_draw_icon(rng) for _ in range(n)]


# --------------------------------------------------------------------------- #
# Scene composition
# --------------------------------------------------------------------------- #
@dataclass
class SynthTruth:
    grid_x: float
    grid_y: float
    pitch: float
    side: float
    rows: int
    cols: int
    cell_items: dict[tuple[int, int], int]   # (row, col) -> icon index


def _paste_rgba(dst: np.ndarray, art: np.ndarray, x: int, y: int) -> None:
    h, w = art.shape[:2]
    H, W = dst.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = art[y0 - y:y1 - y, x0 - x:x1 - x]
    alpha = sub[:, :, 3:4].astype(np.float32) / 255.0
    dst[y0:y1, x0:x1] = (alpha * sub[:, :, :3] +
                         (1 - alpha) * dst[y0:y1, x0:x1]).astype(np.uint8)


def _rounded_rect(img, x0, y0, x1, y1, color, r=6, border=None):
    cv2.rectangle(img, (x0 + r, y0), (x1 - r, y1), color, -1)
    cv2.rectangle(img, (x0, y0 + r), (x1, y1 - r), color, -1)
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
        cv2.circle(img, (cx, cy), r, color, -1, cv2.LINE_AA)
    if border:
        cv2.rectangle(img, (x0, y0), (x1, y1), border, 1)


def compose_screenshot(
    icons: list[np.ndarray],
    seed: int = 11,
    rows: int = 10,
    cols: int = 11,
    filled: int = 102,
    out_width: int | None = None,
) -> tuple[np.ndarray, SynthTruth]:
    rng = random.Random(seed)
    W, H = 2000, 1040
    img = np.full((H, W, 3), (192, 198, 200), np.uint8)   # parchment (BGR-ish)

    # --- left panel decoys ------------------------------------------------- #
    cv2.rectangle(img, (120, 60), (980, 980), (183, 189, 191), -1)
    for i in range(5):   # equipped slot boxes (bigger than storage tiles)
        x0 = 170 + i * 165
        _rounded_rect(img, x0, 130, x0 + 110, 240, (170, 176, 178), 8, (120, 120, 120))
    cv2.circle(img, (560, 430), 130, (245, 245, 243), -1, cv2.LINE_AA)   # cat blob
    cv2.circle(img, (560, 430), 130, (40, 40, 40), 3, cv2.LINE_AA)
    cv2.rectangle(img, (380, 520), (760, 560), (238, 238, 236), -1)      # name plate
    cv2.putText(img, "Grayson", (420, 550), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (30, 30, 30), 2, cv2.LINE_AA)
    for i in range(8):   # stat rows
        y = 620 + i * 42
        cv2.rectangle(img, (150, y), (330, y + 30), (176, 182, 184), -1)
        cv2.putText(img, f"{rng.randint(3, 12)}", (200, y + 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2, cv2.LINE_AA)
    for i in range(2):   # item cards near the cat (square-ish decoys)
        x0 = 390 + i * 150
        _rounded_rect(img, x0, 640, x0 + 120, 770, (225, 227, 228), 6, (60, 60, 60))
        cv2.putText(img, "12", (x0 + 20, 760), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (20, 20, 20), 2, cv2.LINE_AA)

    # --- storage panel ----------------------------------------------------- #
    cv2.rectangle(img, (1020, 40), (1930, 990), (198, 204, 206), -1)
    cv2.putText(img, "Storage", (1300, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.6,
                (30, 30, 30), 3, cv2.LINE_AA)
    for i in range(4):   # filter tab buttons above the grid (decoys)
        x0 = 1115 + i * 92
        _rounded_rect(img, x0, 135, x0 + 50, 183, (205, 210, 212), 4, (120, 120, 120))

    gx, gy = 1110.0, 205.0
    pitch, side = 67.0, 62.0
    tile = (178, 183, 185)
    tile_border = (140, 144, 146)

    order = [(r, c) for r in range(rows) for c in range(cols)]
    cell_items: dict[tuple[int, int], int] = {}
    for idx, (r, c) in enumerate(order):
        if idx < filled:
            cell_items[(r, c)] = rng.randrange(len(icons))

    for r in range(rows):
        for c in range(cols):
            x0 = int(round(gx + c * pitch))
            y0 = int(round(gy + r * pitch))
            x1, y1 = x0 + int(side), y0 + int(side)
            filled_cell = (r, c) in cell_items
            base = tile
            if filled_cell and rng.random() < 0.08:          # cursed red tint
                base = (168, 168, 205)
            _rounded_rect(img, x0, y0, x1, y1, base, 5, tile_border)

            if not filled_cell:
                continue

            if rng.random() < 0.10:                          # blessed glow
                glow = img[y0:y1, x0:x1].astype(np.float32)
                yy, xx = np.mgrid[0:y1 - y0, 0:x1 - x0]
                cyy, cxx = (y1 - y0) / 2, (x1 - x0) / 2
                d = np.hypot(yy - cyy, xx - cxx) / (side / 2)
                boost = np.clip(1 - d, 0, 1)[..., None] * np.array([0, 40, 55])
                img[y0:y1, x0:x1] = np.clip(glow + boost, 0, 255).astype(np.uint8)

            icon = icons[cell_items[(r, c)]]
            ys, xs = np.where(icon[:, :, 3] > 16)   # tight sprite, like the game
            icon = icon[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            ratio = rng.uniform(0.70, 0.84)
            target = int(side * ratio)
            ih, iw = icon.shape[:2]
            s = target / max(ih, iw)
            small = cv2.resize(icon, (max(2, int(iw * s)), max(2, int(ih * s))),
                               interpolation=cv2.INTER_AREA)
            jx, jy = rng.randint(-2, 2), rng.randint(-2, 2)
            px = x0 + (int(side) - small.shape[1]) // 2 + jx
            py = y0 + (int(side) - small.shape[0]) // 2 + jy
            _paste_rgba(img, small, px, py)

            # slot glyph, top-left corner
            g = rng.randrange(4)
            if g == 0:
                cv2.line(img, (x0 + 4, y0 + 10), (x0 + 12, y0 + 4), (40, 40, 40), 2)
            elif g == 1:
                cv2.circle(img, (x0 + 8, y0 + 8), 4, (40, 40, 40), 1, cv2.LINE_AA)
            elif g == 2:
                cv2.rectangle(img, (x0 + 4, y0 + 4), (x0 + 12, y0 + 12), (40, 40, 40), 1)
            else:
                cv2.putText(img, "e", (x0 + 3, y0 + 13), cv2.FONT_HERSHEY_PLAIN,
                            0.8, (40, 40, 40), 1, cv2.LINE_AA)
            if rng.random() < 0.06:                          # NEW ribbon
                cv2.rectangle(img, (x1 - 24, y0 + 2), (x1 - 2, y0 + 12), (60, 60, 200), -1)
                cv2.putText(img, "NEW", (x1 - 23, y0 + 11), cv2.FONT_HERSHEY_PLAIN,
                            0.6, (255, 255, 255), 1, cv2.LINE_AA)
            if rng.random() < 0.10:                          # stack count
                cv2.putText(img, str(rng.randint(2, 9)), (x1 - 12, y1 - 4),
                            cv2.FONT_HERSHEY_PLAIN, 0.9, (30, 30, 30), 1, cv2.LINE_AA)

    # --- global degradation: noise, slight blur, JPEG ---------------------- #
    noise = np.random.default_rng(seed).normal(0, 2.2, img.shape).astype(np.float32)
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (3, 3), 0.55)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)

    truth = SynthTruth(grid_x=gx, grid_y=gy, pitch=pitch, side=side,
                       rows=rows, cols=cols, cell_items=cell_items)

    if out_width and out_width != W:
        f = out_width / W
        img = cv2.resize(img, (out_width, int(H * f)), interpolation=cv2.INTER_AREA)
        truth = SynthTruth(grid_x=gx * f, grid_y=gy * f, pitch=pitch * f,
                           side=side * f, rows=rows, cols=cols,
                           cell_items=cell_items)
    return img, truth


# --------------------------------------------------------------------------- #
# Catalog on disk
# --------------------------------------------------------------------------- #
def write_catalog(icons: list[np.ndarray], data_dir: str):
    """Write icons + a catalog.json mapping them to fake items/sets."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.catalog import Catalog, Item, SetInfo, save_catalog, EQUIPMENT_SLOTS

    icon_dir = os.path.join(data_dir, "icons")
    os.makedirs(icon_dir, exist_ok=True)
    items = []
    for i, icon in enumerate(icons):
        fname = f"item{i:03d}.png"
        cv2.imwrite(os.path.join(icon_dir, fname), icon)
        items.append(Item(
            name=f"Item {i:03d}",
            slot=EQUIPMENT_SLOTS[i % len(EQUIPMENT_SLOTS)],
            sets=[f"Set {i // 5:02d}"],
            icon_file=fname,
        ))
    sets = {}
    for it in items:
        for s in it.sets:
            sets.setdefault(s, SetInfo(name=s, bonus=f"Bonus of {s}")).members.append(it.name)
    cat = Catalog(items=items, sets=sets, generated_at="synthetic", source="synthetic")
    save_catalog(cat, data_dir)
    return cat
