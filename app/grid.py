"""Find the storage grid in a Mewgenics screenshot.

The storage panel is a lattice of identical square tiles. Strategy:

1. Detect square-ish contours with two complementary detectors (Canny edges
   and adaptive threshold) — tiles, and the icons inside them, both yield
   boxes.
2. Find the dominant box size (the ~121 storage tiles massively outnumber
   everything else square on screen).
3. Estimate the horizontal/vertical pitch from nearest-neighbour center
   distances, snap all boxes onto that lattice, and keep the largest
   (gap-tolerant) connected component.
4. Fit the grid origin from the surviving boxes' residuals.

This is resolution-independent (all size filters are relative to image width)
and tolerant of missing tiles (empty cells that produced no contour). If it
still fails, the UI lets the user click the two outer corners of the grid.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Tiles are between 2% and 9% of screenshot width in every layout we've seen
# (1047p fullscreen: ~62px of 2001px ≈ 3.1%).
MIN_SIDE_FRAC = 0.018
MAX_SIDE_FRAC = 0.095
WORK_WIDTH = 1500          # detection runs on a copy downscaled to this width
MIN_GRID_ROWS = 3
MIN_GRID_COLS = 3


@dataclass
class Grid:
    x: float          # top-left corner of the first tile (not the pitch cell)
    y: float
    pitch_x: float    # center-to-center spacing
    pitch_y: float
    side: float       # tile side length
    rows: int
    cols: int
    confidence: float = 0.0
    detected_boxes: int = 0

    def cell_box(self, r: int, c: int) -> tuple[int, int, int, int]:
        x0 = int(round(self.x + c * self.pitch_x))
        y0 = int(round(self.y + r * self.pitch_y))
        s = int(round(self.side))
        return x0, y0, x0 + s, y0 + s

    def cells(self):
        for r in range(self.rows):
            for c in range(self.cols):
                yield r, c, self.cell_box(r, c)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 1), "y": round(self.y, 1),
            "pitch_x": round(self.pitch_x, 2), "pitch_y": round(self.pitch_y, 2),
            "side": round(self.side, 1), "rows": self.rows, "cols": self.cols,
            "confidence": round(self.confidence, 3),
            "detected_boxes": self.detected_boxes,
        }

    @staticmethod
    def from_corners(x0: float, y0: float, x1: float, y1: float,
                     rows: int, cols: int) -> "Grid":
        """Build a grid from the outer corners of the whole tile block."""
        rows = max(1, rows)
        cols = max(1, cols)
        pitch_x = (x1 - x0) / cols
        pitch_y = (y1 - y0) / rows
        side = min(pitch_x, pitch_y) * 0.94
        return Grid(x=x0, y=y0, pitch_x=pitch_x, pitch_y=pitch_y,
                    side=side, rows=rows, cols=cols, confidence=1.0)


# --------------------------------------------------------------------------- #
# Box candidates
# --------------------------------------------------------------------------- #
def _candidate_boxes(gray: np.ndarray) -> list[tuple[float, float, float, float]]:
    """Square-ish bounding boxes from two binarizations, merged."""
    h, w = gray.shape
    lo_side = MIN_SIDE_FRAC * w
    hi_side = MAX_SIDE_FRAC * w
    boxes: list[tuple[float, float, float, float]] = []

    blur = cv2.GaussianBlur(gray, (3, 3), 0)

    # Fixed gradient thresholds: UI tiles sit on low-contrast parchment, so the
    # usual median-intensity auto-Canny heuristic lands far too high there.
    canny = cv2.Canny(blur, 25, 70)
    canny = cv2.dilate(canny, np.ones((3, 3), np.uint8), iterations=1)

    adap = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 6)

    for binary in (canny, adap):
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            x, y, bw, bh = cv2.boundingRect(cnt)
            if bw < lo_side or bh < lo_side or bw > hi_side or bh > hi_side:
                continue
            ar = bw / bh
            if not (0.75 <= ar <= 1.33):
                continue
            boxes.append((float(x), float(y), float(bw), float(bh)))
    return boxes


def _dominant_side(boxes: list[tuple[float, float, float, float]]) -> float | None:
    """Robust mode of box sides: the window of ±12% holding the most boxes."""
    sides = sorted((b[2] + b[3]) / 2 for b in boxes)
    if len(sides) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return None
    best_count, best_val = 0, None
    j = 0
    for i, s in enumerate(sides):
        while sides[j] < s * 0.88:
            j += 1
        # window covers sides in [0.88*s, s]; center value = median of window
        count = i - j + 1
        if count > best_count:
            best_count = count
            best_val = sides[(i + j) // 2]
    return best_val


def _pitch(vals: list[float], side: float) -> float | None:
    """Median nearest-neighbour spacing in one axis, restricted to ~one tile."""
    good = [v for v in vals if 0.9 * side <= v <= 1.7 * side]
    if len(good) < 4:
        return None
    return float(np.median(good))


# --------------------------------------------------------------------------- #
# Lattice fit
# --------------------------------------------------------------------------- #
def _components(cells: set[tuple[int, int]], reach: int = 2) -> list[set[tuple[int, int]]]:
    """Connected components allowing gaps up to Chebyshev distance `reach`."""
    remaining = set(cells)
    comps = []
    while remaining:
        seed = remaining.pop()
        comp = {seed}
        frontier = [seed]
        while frontier:
            ci, cj = frontier.pop()
            for di in range(-reach, reach + 1):
                for dj in range(-reach, reach + 1):
                    nb = (ci + di, cj + dj)
                    if nb in remaining:
                        remaining.remove(nb)
                        comp.add(nb)
                        frontier.append(nb)
        comps.append(comp)
    return comps


def detect_grid(bgr: np.ndarray) -> Grid | None:
    H, W = bgr.shape[:2]
    scale = 1.0
    work = bgr
    if W > WORK_WIDTH:
        scale = WORK_WIDTH / W
        work = cv2.resize(bgr, (WORK_WIDTH, int(H * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    boxes = _candidate_boxes(gray)
    side = _dominant_side(boxes)
    if side is None:
        return None

    tiles = [b for b in boxes if abs((b[2] + b[3]) / 2 - side) / side < 0.14]
    if len(tiles) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return None

    centers = np.array([[b[0] + b[2] / 2, b[1] + b[3] / 2] for b in tiles])

    # Deduplicate near-identical centers (canny + adaptive often both fire).
    order = np.lexsort((centers[:, 1], centers[:, 0]))
    kept_idx: list[int] = []
    for i in order:
        if all(np.hypot(*(centers[i] - centers[k])) > side * 0.3 for k in kept_idx[-8:]):
            kept_idx.append(i)
    centers = centers[kept_idx]

    # Pitch per axis from nearest same-row/column neighbours.
    dxs, dys = [], []
    for i in range(len(centers)):
        dx_cands = [centers[j, 0] - centers[i, 0] for j in range(len(centers))
                    if j != i and abs(centers[j, 1] - centers[i, 1]) < 0.35 * side
                    and centers[j, 0] > centers[i, 0]]
        if dx_cands:
            dxs.append(min(dx_cands))
        dy_cands = [centers[j, 1] - centers[i, 1] for j in range(len(centers))
                    if j != i and abs(centers[j, 0] - centers[i, 0]) < 0.35 * side
                    and centers[j, 1] > centers[i, 1]]
        if dy_cands:
            dys.append(min(dy_cands))
    pitch_x = _pitch(dxs, side) or side * 1.08
    pitch_y = _pitch(dys, side) or pitch_x

    # Snap to lattice around a central reference tile.
    ref = centers[np.argmin(np.abs(centers - np.median(centers, axis=0)).sum(axis=1))]
    lattice: dict[tuple[int, int], list[np.ndarray]] = {}
    for c in centers:
        fi = (c[0] - ref[0]) / pitch_x
        fj = (c[1] - ref[1]) / pitch_y
        i, j = round(fi), round(fj)
        if abs(fi - i) < 0.28 and abs(fj - j) < 0.28:
            lattice.setdefault((i, j), []).append(c)

    if len(lattice) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return None

    comps = _components(set(lattice.keys()))
    comp = max(comps, key=len)
    if len(comp) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return None

    is_ = [ij[0] for ij in comp]
    js_ = [ij[1] for ij in comp]
    i0, i1 = min(is_), max(is_)
    j0, j1 = min(js_), max(js_)

    # Trim sparse boundary rows/cols: a real grid row is almost fully detected
    # (empty tiles still render as tiles), while stray aligned UI elements
    # (filter buttons, panel trim) contribute only one or two boxes.
    def row_count(j):
        return sum(1 for ij in comp if ij[1] == j and i0 <= ij[0] <= i1)

    def col_count(i):
        return sum(1 for ij in comp if ij[0] == i and j0 <= ij[1] <= j1)

    changed = True
    while changed:
        changed = False
        cols_now = i1 - i0 + 1
        rows_now = j1 - j0 + 1
        need_r = max(2, (cols_now + 1) // 2)
        need_c = max(2, (rows_now + 1) // 2)
        if rows_now > MIN_GRID_ROWS and row_count(j0) < need_r:
            j0 += 1; changed = True; continue
        if rows_now > MIN_GRID_ROWS and row_count(j1) < need_r:
            j1 -= 1; changed = True; continue
        if cols_now > MIN_GRID_COLS and col_count(i0) < need_c:
            i0 += 1; changed = True; continue
        if cols_now > MIN_GRID_COLS and col_count(i1) < need_c:
            i1 -= 1; changed = True; continue
    comp = {ij for ij in comp if i0 <= ij[0] <= i1 and j0 <= ij[1] <= j1}

    cols = i1 - i0 + 1
    rows = j1 - j0 + 1
    if rows < MIN_GRID_ROWS or cols < MIN_GRID_COLS or len(comp) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return None

    # Refit origin: average observed center minus its lattice offset.
    ox = float(np.mean([c[0] - ij[0] * pitch_x
                        for ij in comp for c in lattice[ij]]))
    oy = float(np.mean([c[1] - ij[1] * pitch_y
                        for ij in comp for c in lattice[ij]]))
    first_cx = ox + i0 * pitch_x
    first_cy = oy + j0 * pitch_y

    occupancy = len(comp) / float(rows * cols)
    g = Grid(
        x=(first_cx - side / 2) / scale,
        y=(first_cy - side / 2) / scale,
        pitch_x=pitch_x / scale,
        pitch_y=pitch_y / scale,
        side=side / scale,
        rows=rows,
        cols=cols,
        confidence=min(1.0, occupancy + 0.2),
        detected_boxes=len(comp),
    )

    # Clamp to image bounds (a bad outer row that hangs off-screen is dropped).
    while g.rows > 1 and g.y + (g.rows - 1) * g.pitch_y + g.side > H + 2:
        g.rows -= 1
    while g.cols > 1 and g.x + (g.cols - 1) * g.pitch_x + g.side > W + 2:
        g.cols -= 1
    while g.rows > 1 and g.y < -2:
        g.y += g.pitch_y
        g.rows -= 1
    while g.cols > 1 and g.x < -2:
        g.x += g.pitch_x
        g.cols -= 1
    return g


def draw_debug(bgr: np.ndarray, grid: Grid) -> np.ndarray:
    """Overlay the detected lattice for eyeballing alignment."""
    out = bgr.copy()
    for r, c, (x0, y0, x1, y1) in grid.cells():
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 200, 0), 2)
        cv2.putText(out, f"{r},{c}", (x0 + 3, y0 + 14),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 0, 255), 1, cv2.LINE_AA)
    return out
