"""Match each storage cell against the wiki icon catalog.

Why the previous attempt failed, and what this one does differently:

* **Alignment**: icons float inside their tile with unknown padding. We never
  compare "whole cell vs whole icon" at one fixed framing — we slide the
  reference over the cell (coarse offset grid, then refined) and search over
  several icon scales.
* **Background**: the in-game tile background (grey card, blessed-yellow glow,
  cursed-red tint, corner slot glyphs, NEW ribbons) is noise. All comparisons
  are **masked by the icon's alpha channel**, so only pixels where the icon
  has art are ever scored. Corner overlays land outside the mask.
* **Lighting/tint**: scores are zero-normalized (ZNCC), which is invariant to
  brightness/contrast shifts, and blended with an edge-magnitude ZNCC that is
  even more tint-robust. A small chroma term separates same-shape items with
  different colours (red pill vs blue pill).
* **Speed**: a batched coarse pass (48px, all references × 5 scales × 9
  offsets as BLAS matmuls) screens candidates; only the top-K get the precise
  96px refinement.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from .catalog import Catalog, Item

BASE = 96          # refinement canvas
COARSE = 48        # screening canvas
PAD_C = 4          # patch padding around the coarse canvas (offset search room)
PAD_B = 8          # padding at refinement scale
SCALES = (0.55, 0.62, 0.69, 0.76, 0.83, 0.90, 0.98)  # icon max-dim / canvas
REFINE_DSIGMA = 0.045
TOP_COARSE = 20    # candidates surviving the coarse pass
BLUR_SIGMA = 0.8   # mutual smoothing: widens correlation peaks of thin line art
COVERAGE_W = 0.30  # penalty weight for cell ink left outside the ref's mask
INTERIOR_INSET = 0.0    # match on the full tile: icons can nearly fill it
EMPTY_STD = 5.0    # empty-cell thresholds (32x32 grayscale units)
EMPTY_EDGE = 14.0

_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Reference preparation
# --------------------------------------------------------------------------- #
def _load_icon_rgba(path: str) -> np.ndarray | None:
    data = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if data is None:
        return None
    if data.ndim == 2:
        data = cv2.cvtColor(data, cv2.COLOR_GRAY2BGRA)
    elif data.shape[2] == 3:
        # No alpha: treat near-white as background (wiki thumbs are RGBA,
        # this is just a safety net).
        alpha = 255 - (np.all(data > 240, axis=2).astype(np.uint8) * 255)
        data = np.dstack([data, alpha])
    return data  # BGRA uint8


def _trim_to_alpha(bgra: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(bgra[:, :, 3] > 16)
    if len(xs) < 12:
        return None
    pad = 1
    y0, y1 = max(0, ys.min() - pad), min(bgra.shape[0], ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(bgra.shape[1], xs.max() + 1 + pad)
    return bgra[y0:y1, x0:x1]


def _render(art: np.ndarray, canvas: int, sigma: float) -> dict:
    """Render trimmed BGRA art centered on a canvas at the given scale.

    Returns float32 planes: gray (bg-composited), edge magnitude, mask (alpha),
    plus chroma stats over the mask.
    """
    ah, aw = art.shape[:2]
    target = max(4, int(round(canvas * sigma)))
    if ah >= aw:
        nh, nw = target, max(2, int(round(aw * target / ah)))
    else:
        nw, nh = target, max(2, int(round(ah * target / aw)))
    interp = cv2.INTER_AREA if nh < ah else cv2.INTER_LINEAR
    small = cv2.resize(art, (nw, nh), interpolation=interp)

    frame = np.zeros((canvas, canvas, 4), np.uint8)
    oy, ox = (canvas - nh) // 2, (canvas - nw) // 2
    frame[oy:oy + nh, ox:ox + nw] = small

    alpha = frame[:, :, 3].astype(np.float32) / 255.0
    bgr = frame[:, :, :3].astype(np.float32)
    lum = 0.114 * bgr[:, :, 0] + 0.587 * bgr[:, :, 1] + 0.299 * bgr[:, :, 2]
    gray = alpha * lum + (1.0 - alpha) * 140.0
    gray = cv2.GaussianBlur(gray, (5, 5), BLUR_SIGMA)
    # High-pass: in-game cells can have low-frequency background gradients
    # (blessed-yellow glow); remove that band from both sides of the match.
    gray = gray - cv2.GaussianBlur(gray, (0, 0), canvas / 10.0)

    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    edge = cv2.magnitude(gx, gy)

    comp = (alpha[..., None] * bgr + (1 - alpha[..., None]) * 140.0).astype(np.uint8)
    lab = cv2.cvtColor(comp, cv2.COLOR_BGR2Lab).astype(np.float32)
    msum = float(alpha.sum()) + _EPS
    mean_a = float((alpha * lab[:, :, 1]).sum() / msum)
    mean_b = float((alpha * lab[:, :, 2]).sum() / msum)

    return {"gray": gray, "edge": edge, "mask": alpha,
            "mean_a": mean_a, "mean_b": mean_b}


def _masked_stats(gray: np.ndarray, edge: np.ndarray, mask: np.ndarray) -> dict:
    m = mask.ravel().astype(np.float32)
    g = gray.ravel().astype(np.float32)
    e = edge.ravel().astype(np.float32)
    msum = float(m.sum()) + _EPS
    gmean = float((m * g).sum() / msum)
    emean = float((m * e).sum() / msum)
    mg = m * (g - gmean)          # note: (m*g' ).sum() == 0 by construction
    me = m * (e - emean)
    gnorm = float(np.sqrt((mg * (g - gmean)).sum())) + _EPS
    enorm = float(np.sqrt((me * (e - emean)).sum())) + _EPS
    return {"m": m, "mg": mg, "me": me, "msum": msum,
            "gnorm": gnorm, "enorm": enorm}


@dataclass
class _ScaleBank:
    """Stacked reference matrices for one (canvas=COARSE, sigma)."""
    M: np.ndarray       # [N, D] alpha masks
    MG: np.ndarray      # [N, D] mask * (gray - masked mean)
    ME: np.ndarray      # [N, D] mask * (edge - masked mean)
    msum: np.ndarray    # [N]
    gnorm: np.ndarray   # [N]
    enorm: np.ndarray   # [N]


@dataclass
class Candidate:
    name: str
    score: float
    confidence: float


@dataclass
class CellResult:
    row: int
    col: int
    box: tuple[int, int, int, int]
    empty: bool
    candidates: list[Candidate] = field(default_factory=list)
    crop_b64: str = ""


class Matcher:
    def __init__(self, catalog: Catalog, data_dir: str):
        self.items: list[Item] = []
        self.arts: list[np.ndarray] = []
        self.chroma: list[tuple[float, float]] = []
        self._render_cache: dict[tuple[int, int, float], dict] = {}

        for it in catalog.items:
            path = catalog.icon_path(it, data_dir)
            if not path or not os.path.exists(path):
                continue
            raw = _load_icon_rgba(path)
            if raw is None:
                continue
            art = _trim_to_alpha(raw)
            if art is None:
                continue
            self.items.append(it)
            self.arts.append(art)

        self.banks: dict[float, _ScaleBank] = {}
        self.ref_ab = np.zeros((len(self.items), 2), np.float32)
        if self.items:
            self._build_banks()

    @property
    def n_refs(self) -> int:
        return len(self.items)

    def _build_banks(self) -> None:
        for sigma in SCALES:
            Ms, MGs, MEs, msums, gnorms, enorms = [], [], [], [], [], []
            for idx, art in enumerate(self.arts):
                r = _render(art, COARSE, sigma)
                st = _masked_stats(r["gray"], r["edge"], r["mask"])
                Ms.append(st["m"]); MGs.append(st["mg"]); MEs.append(st["me"])
                msums.append(st["msum"]); gnorms.append(st["gnorm"]); enorms.append(st["enorm"])
                if sigma == SCALES[0]:
                    self.ref_ab[idx] = (r["mean_a"], r["mean_b"])
            self.banks[sigma] = _ScaleBank(
                M=np.stack(Ms), MG=np.stack(MGs), ME=np.stack(MEs),
                msum=np.array(msums, np.float32),
                gnorm=np.array(gnorms, np.float32),
                enorm=np.array(enorms, np.float32),
            )

    def _ref_render(self, idx: int, canvas: int, sigma: float) -> dict:
        key = (idx, canvas, round(sigma, 3))
        r = self._render_cache.get(key)
        if r is None:
            r = _render(self.arts[idx], canvas, sigma)
            r.update(_masked_stats(r["gray"], r["edge"], r["mask"]))
            if len(self._render_cache) > 4000:
                self._render_cache.clear()
            self._render_cache[key] = r
        return r

    # ------------------------------------------------------------------ #
    # Patch preparation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _interior(bgr: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray | None:
        x0, y0, x1, y1 = box
        H, W = bgr.shape[:2]
        s = x1 - x0
        d = int(round(s * INTERIOR_INSET))
        x0, y0, x1, y1 = x0 + d, y0 + d, x1 - d, y1 - d
        x0c, y0c = max(0, x0), max(0, y0)
        x1c, y1c = min(W, x1), min(H, y1)
        if x1c - x0c < 8 or y1c - y0c < 8:
            return None
        return bgr[y0c:y1c, x0c:x1c]

    @staticmethod
    def _planes(patch_bgr: np.ndarray, size: int, window: int) -> dict:
        p = cv2.resize(patch_bgr, (size, size), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray = cv2.GaussianBlur(gray, (5, 5), BLUR_SIGMA)
        gray = gray - cv2.GaussianBlur(gray, (0, 0), window / 10.0)
        gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
        edge = cv2.magnitude(gx, gy)
        # Ink-energy plane: |high-passed gray|, slightly spread. Used for the
        # coverage term that punishes matches leaving cell ink unexplained.
        energy = cv2.GaussianBlur(np.abs(gray), (3, 3), 0.7)
        lab = cv2.cvtColor(p, cv2.COLOR_BGR2Lab).astype(np.float32)
        a, b = lab[:, :, 1], lab[:, :, 2]
        # The cell's own background chroma (border ring median): item colour is
        # measured relative to this, so cursed-red tiles don't read as a red item.
        ring = max(2, size // 12)
        border = np.concatenate([
            a[:ring].ravel(), a[-ring:].ravel(), a[:, :ring].ravel(), a[:, -ring:].ravel()])
        border_b = np.concatenate([
            b[:ring].ravel(), b[-ring:].ravel(), b[:, :ring].ravel(), b[:, -ring:].ravel()])
        return {"gray": gray, "edge": edge, "energy": energy, "a": a, "b": b,
                "bg_a": float(np.median(border)), "bg_b": float(np.median(border_b))}

    @staticmethod
    def _windows(plane: np.ndarray, win: int, offsets: list[tuple[int, int]]) -> np.ndarray:
        return np.stack([plane[dy:dy + win, dx:dx + win].ravel()
                         for dx, dy in offsets])

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    def _chroma_term(self, ref_ab: np.ndarray, pa: np.ndarray, pb: np.ndarray,
                     bg_a: float, bg_b: float) -> np.ndarray:
        """Small bonus/penalty from colour agreement. Shapes broadcast.

        Reference colour is relative to its neutral compositing background;
        patch colour is relative to the cell's own background chroma.
        """
        ra = ref_ab[..., 0] - 128.0
        rb = ref_ab[..., 1] - 128.0
        qa = pa - bg_a
        qb = pb - bg_b
        sat_r = np.hypot(ra, rb)
        sat_p = np.hypot(qa, qb)
        dist = np.hypot(ra - qa, rb - qb)
        bonus = 0.06 * np.clip(1.0 - dist / 50.0, 0.0, 1.0)
        term = np.where((sat_r > 10) & (sat_p > 10), bonus, 0.0)
        mismatch = ((sat_r > 16) & (sat_p < 5)) | ((sat_r < 5) & (sat_p > 16))
        return np.where(mismatch, -0.05, term).astype(np.float32)

    def _coarse(self, planes: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Score all refs.

        Returns (best_score[N], per_sigma_score[N, S], per_sigma_off[N, S]) so
        the refinement stage can revisit each candidate's top scales rather
        than committing to a single, possibly spurious, coarse lock-in.
        """
        offs = [(dx, dy) for dy in (0, PAD_C, 2 * PAD_C) for dx in (0, PAD_C, 2 * PAD_C)]
        Wg = self._windows(planes["gray"], COARSE, offs)          # [9, D]
        We = self._windows(planes["edge"], COARSE, offs)
        Wa = self._windows(planes["a"], COARSE, offs)
        Wb = self._windows(planes["b"], COARSE, offs)
        Wn = self._windows(planes["energy"], COARSE, offs)
        Wg2, We2 = Wg * Wg, We * We
        tot_ink = Wn.sum(axis=1) + _EPS                            # [9]

        n_refs = self.n_refs
        per_sigma = np.full((n_refs, len(SCALES)), -1e9, np.float32)
        per_sigma_off = np.zeros((n_refs, len(SCALES)), np.int32)

        for si, sigma in enumerate(SCALES):
            bank = self.banks[sigma]
            # [N, 9] each — batched over offsets via BLAS
            m_g = bank.M @ Wg.T
            m_g2 = bank.M @ Wg2.T
            num_g = bank.MG @ Wg.T
            var_g = np.maximum(m_g2 - (m_g * m_g) / bank.msum[:, None], 0.0)
            zg = num_g / (bank.gnorm[:, None] * np.sqrt(var_g) + _EPS)

            m_e = bank.M @ We.T
            m_e2 = bank.M @ We2.T
            num_e = bank.ME @ We.T
            var_e = np.maximum(m_e2 - (m_e * m_e) / bank.msum[:, None], 0.0)
            ze = num_e / (bank.enorm[:, None] * np.sqrt(var_e) + _EPS)

            pa = (bank.M @ Wa.T) / bank.msum[:, None]
            pb = (bank.M @ Wb.T) / bank.msum[:, None]
            chroma = self._chroma_term(self.ref_ab[:, None, :], pa, pb,
                                       planes["bg_a"], planes["bg_b"])

            cov = np.clip((bank.M @ Wn.T) / tot_ink[None, :], 0.0, 1.0)
            score = 0.5 * zg + 0.5 * ze + chroma - COVERAGE_W * (1.0 - cov)
            per_sigma[:, si] = score.max(axis=1)
            per_sigma_off[:, si] = score.argmax(axis=1)
        return per_sigma.max(axis=1), per_sigma, per_sigma_off

    def _refine_one(self, idx: int, planes: dict, sigma0: float,
                    off0: tuple[int, int]) -> float:
        deltas = (-3, 0, 3)
        offs = []
        for dy in deltas:
            for dx in deltas:
                ox = min(2 * PAD_B, max(0, off0[0] + dx))
                oy = min(2 * PAD_B, max(0, off0[1] + dy))
                offs.append((ox, oy))
        offs = list(dict.fromkeys(offs))
        Wg = self._windows(planes["gray"], BASE, offs)
        We = self._windows(planes["edge"], BASE, offs)
        Wa = self._windows(planes["a"], BASE, offs)
        Wb = self._windows(planes["b"], BASE, offs)
        Wn = self._windows(planes["energy"], BASE, offs)
        Wg2, We2 = Wg * Wg, We * We
        tot_ink = Wn.sum(axis=1) + _EPS

        best = -1e9
        sigmas = {round(min(1.0, max(0.5, sigma0 + k * REFINE_DSIGMA)), 3)
                  for k in (-2, -1, 0, 1, 2)}
        for sigma in sorted(sigmas):
            r = self._ref_render(idx, BASE, sigma)
            m, mg, me = r["m"], r["mg"], r["me"]
            m_g = Wg @ m
            var_g = np.maximum(Wg2 @ m - (m_g * m_g) / r["msum"], 0.0)
            zg = (Wg @ mg) / (r["gnorm"] * np.sqrt(var_g) + _EPS)
            m_e = We @ m
            var_e = np.maximum(We2 @ m - (m_e * m_e) / r["msum"], 0.0)
            ze = (We @ me) / (r["enorm"] * np.sqrt(var_e) + _EPS)
            pa = (Wa @ m) / r["msum"]
            pb = (Wb @ m) / r["msum"]
            chroma = self._chroma_term(self.ref_ab[idx][None, :], pa, pb,
                                       planes["bg_a"], planes["bg_b"])
            cov = np.clip((Wn @ m) / tot_ink, 0.0, 1.0)
            score = float((0.5 * zg + 0.5 * ze + chroma
                           - COVERAGE_W * (1.0 - cov)).max())
            best = max(best, score)
        return best

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @staticmethod
    def looks_empty(patch_bgr: np.ndarray) -> bool:
        # Judge emptiness on the inner region only — the tile's own border and
        # rounded corners at the crop edge would otherwise register as content.
        h, w = patch_bgr.shape[:2]
        dy, dx = int(h * 0.15), int(w * 0.15)
        inner = patch_bgr[dy:h - dy, dx:w - dx]
        if inner.size == 0:
            inner = patch_bgr
        small = cv2.resize(inner, (32, 32), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if float(gray.std()) > EMPTY_STD:
            return False
        gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
        return float(cv2.magnitude(gx, gy).mean()) < EMPTY_EDGE

    @staticmethod
    def _crop_b64(patch_bgr: np.ndarray) -> str:
        thumb = cv2.resize(patch_bgr, (64, 64), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", thumb)
        return base64.b64encode(buf).decode("ascii") if ok else ""

    def match_cell(self, bgr: np.ndarray, box: tuple[int, int, int, int],
                   top_k: int = 6) -> tuple[bool, list[Candidate], str]:
        patch = self._interior(bgr, box)
        if patch is None:
            return True, [], ""
        crop_b64 = self._crop_b64(patch)
        if self.looks_empty(patch):
            return True, [], crop_b64
        if not self.n_refs:
            return False, [], crop_b64

        coarse_planes = self._planes(patch, COARSE + 2 * PAD_C, COARSE)
        best, per_sigma, per_sigma_off = self._coarse(coarse_planes)
        order = np.argsort(-best)[:TOP_COARSE]

        fine_planes = self._planes(patch, BASE + 2 * PAD_B, BASE)
        offs9 = [(dx, dy) for dy in (0, PAD_C, 2 * PAD_C) for dx in (0, PAD_C, 2 * PAD_C)]
        rescored: list[tuple[int, float]] = []
        for idx in order:
            # Refine at this candidate's two most promising coarse scales.
            sig_order = np.argsort(-per_sigma[idx])[:2]
            score = -1e9
            for si in sig_order:
                o48 = offs9[per_sigma_off[idx, si]]
                score = max(score, self._refine_one(int(idx), fine_planes,
                                                    SCALES[si],
                                                    (o48[0] * 2, o48[1] * 2)))
            rescored.append((int(idx), score))
        rescored.sort(key=lambda t: -t[1])

        cands = [Candidate(name=self.items[i].name,
                           score=round(float(s), 4),
                           confidence=round(float(np.clip((s - 0.15) / 0.75, 0, 1)), 3))
                 for i, s in rescored[:top_k]]
        return False, cands, crop_b64

    def analyze(self, bgr: np.ndarray, grid, top_k: int = 6) -> list[CellResult]:
        out: list[CellResult] = []
        for r, c, box in grid.cells():
            empty, cands, crop = self.match_cell(bgr, box, top_k=top_k)
            out.append(CellResult(row=r, col=c, box=box, empty=empty,
                                  candidates=cands, crop_b64=crop))
        return out
