"""Web service: upload a Mewgenics storage screenshot, get back identified
items + achievable set bonuses, with an assisted-correction UI.

Endpoints:
  GET  /                  the single-page UI
  GET  /api/health        catalog / matcher / bootstrap status
  GET  /api/catalog       items + sets for the UI's picker and solver
  POST /api/analyze       multipart screenshot -> grid + per-cell candidates
  POST /api/bootstrap     (re)scrape the wiki in the background
  POST /api/import-data   upload a data bundle zip (offline fallback)
  GET  /icons/{fname}     cached wiki icon images
"""

from __future__ import annotations

import base64
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .bootstrap import import_bundle, run_bootstrap
from .catalog import (Catalog, DATA_DIR, EQUIPMENT_SLOTS, ICON_DIR, SET_SIZE,
                      has_catalog, load_catalog)
from .grid import Grid, detect_grid, draw_debug
from .matcher import Matcher

MAX_UPLOAD = 20 * 1024 * 1024
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _load_all("startup")
    if _catalog is None and os.environ.get("AUTO_BOOTSTRAP", "1") != "0":
        _start_bootstrap()
    yield


app = FastAPI(title="Mewgenics Set Checker", version=__version__,
              lifespan=_lifespan)

_state_lock = threading.Lock()
_catalog: Catalog | None = None
_matcher: Matcher | None = None
_bootstrap = {"status": "idle", "detail": "", "log": [], "started_at": "",
              "finished_at": ""}


# --------------------------------------------------------------------------- #
# Catalog / matcher lifecycle
# --------------------------------------------------------------------------- #
def _load_all(reason: str) -> None:
    """(Re)load catalog and rebuild matcher. Safe to call from any thread."""
    global _catalog, _matcher
    if not has_catalog(DATA_DIR):
        return
    try:
        catalog = load_catalog(DATA_DIR)
        matcher = Matcher(catalog, DATA_DIR)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _bootstrap["detail"] = f"failed to load catalog ({reason}): {exc}"
        return
    with _state_lock:
        _catalog = catalog
        _matcher = matcher


def _bootstrap_worker() -> None:
    _bootstrap.update(status="running", detail="starting", log=[],
                      started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      finished_at="")

    def note(msg: str) -> None:
        _bootstrap["detail"] = msg
        _bootstrap["log"] = (_bootstrap["log"] + [msg])[-40:]

    try:
        run_bootstrap(DATA_DIR, progress=note)
        _load_all("bootstrap")
        _bootstrap.update(status="ok", detail="catalog ready")
    except Exception as exc:  # surfaced to the UI — keep the app alive
        _bootstrap.update(status="failed", detail=str(exc))
    finally:
        _bootstrap["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _start_bootstrap() -> bool:
    with _state_lock:
        if _bootstrap["status"] == "running":
            return False
        _bootstrap["status"] = "running"
    threading.Thread(target=_bootstrap_worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Health / catalog
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    with _state_lock:
        cat, m = _catalog, _matcher
    icons = 0
    if cat:
        icons = sum(1 for it in cat.items if it.icon_file)
    return {
        "ok": True,
        "version": __version__,
        "catalog": {
            "loaded": cat is not None,
            "items": len(cat.items) if cat else 0,
            "sets": len(cat.sets) if cat else 0,
            "icons": icons,
            "generated_at": cat.generated_at if cat else "",
        },
        "matcher": {"ready": m is not None and m.n_refs > 0,
                    "refs": m.n_refs if m else 0},
        "bootstrap": {k: _bootstrap[k] for k in
                      ("status", "detail", "started_at", "finished_at")},
        "rules": {"set_size": SET_SIZE, "slots": EQUIPMENT_SLOTS},
    }


@app.get("/api/catalog")
def catalog_json() -> dict:
    with _state_lock:
        cat = _catalog
    if cat is None:
        return {"items": [], "sets": {}}
    return {
        "items": [{
            "name": it.name,
            "slot": it.normalized_slot(),
            "rarity": it.rarity,
            "sets": it.sets,
            "effect": it.effect,
            "icon": f"/icons/{it.icon_file}" if it.icon_file else "",
            "page_url": it.page_url,
        } for it in cat.items],
        "sets": {name: {"bonus": info.bonus, "members": info.members}
                 for name, info in cat.sets.items()},
    }


@app.get("/icons/{fname}")
def icon(fname: str):
    if "/" in fname or "\\" in fname or ".." in fname:
        raise HTTPException(404)
    path = os.path.join(DATA_DIR, ICON_DIR, fname)
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------- #
# Bootstrap / bundle
# --------------------------------------------------------------------------- #
@app.post("/api/bootstrap")
def bootstrap_endpoint() -> dict:
    started = _start_bootstrap()
    return {"started": started, "status": _bootstrap["status"],
            "detail": _bootstrap["detail"]}


@app.post("/api/import-data")
async def import_data(file: UploadFile = File(...)) -> dict:
    payload = await file.read()
    if len(payload) > 100 * 1024 * 1024:
        raise HTTPException(413, "bundle too large")
    try:
        stats = import_bundle(payload, DATA_DIR)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _load_all("import")
    with _state_lock:
        ready = _matcher is not None and _matcher.n_refs > 0
    return {"imported": stats, "matcher_ready": ready}


# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #
def _decode_image(payload: bytes) -> np.ndarray:
    arr = np.frombuffer(payload, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "could not decode image — upload a PNG/JPEG screenshot")
    h, w = img.shape[:2]
    if w < 400 or h < 300:
        raise HTTPException(400, f"image too small ({w}x{h}) — upload the full screenshot")
    if max(w, h) > 4200:
        f = 4200 / max(w, h)
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    return img


@app.post("/api/analyze")
def analyze(
    file: UploadFile = File(...),
    grid_x0: float | None = Form(None),
    grid_y0: float | None = Form(None),
    grid_x1: float | None = Form(None),
    grid_y1: float | None = Form(None),
    rows: int | None = Form(None),
    cols: int | None = Form(None),
    debug: int = Form(0),
) -> JSONResponse:
    payload = file.file.read()
    if len(payload) > MAX_UPLOAD:
        raise HTTPException(413, "screenshot larger than 20 MB")
    img = _decode_image(payload)

    manual = all(v is not None for v in (grid_x0, grid_y0, grid_x1, grid_y1, rows, cols))
    if manual:
        grid = Grid.from_corners(float(grid_x0), float(grid_y0),
                                 float(grid_x1), float(grid_y1),
                                 int(rows), int(cols))
    else:
        grid = detect_grid(img)
        if grid is None:
            raise HTTPException(422, "could not find the storage grid — use "
                                     "'Adjust grid' to mark its corners manually")

    with _state_lock:
        m = _matcher
    t0 = time.time()
    if m is None:
        m = Matcher(Catalog(), DATA_DIR)   # no refs: crops + empties only
    cells = m.analyze(img, grid)
    elapsed = time.time() - t0

    out = {
        "image": {"width": img.shape[1], "height": img.shape[0]},
        "grid": grid.to_dict(),
        "matcher_refs": m.n_refs,
        "elapsed_sec": round(elapsed, 2),
        "cells": [{
            "row": c.row, "col": c.col, "box": list(c.box), "empty": c.empty,
            "crop": c.crop_b64,
            "candidates": [{"name": k.name, "score": k.score,
                            "confidence": k.confidence} for k in c.candidates],
        } for c in cells],
    }
    if debug:
        overlay = draw_debug(img, grid)
        small = overlay
        if overlay.shape[1] > 1600:
            f = 1600 / overlay.shape[1]
            small = cv2.resize(overlay, (1600, int(overlay.shape[0] * f)))
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            out["debug_overlay"] = base64.b64encode(buf).decode("ascii")
    return JSONResponse(out)


# --------------------------------------------------------------------------- #
# Static UI (mounted last so /api/* wins)
# --------------------------------------------------------------------------- #
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
