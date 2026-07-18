"""API-level tests: the FastAPI app over a synthetic catalog + screenshot."""

import os

import cv2
import pytest
from fastapi.testclient import TestClient

from synth import compose_screenshot, make_icons, write_catalog


@pytest.fixture(scope="module")
def client():
    data_dir = os.environ["DATA_DIR"]          # set by conftest before imports
    icons = make_icons(30, seed=7)
    write_catalog(icons, data_dir)
    from app.main import app, _load_all
    _load_all("test")                          # pick up the freshly written data
    with TestClient(app) as c:
        yield c, icons


def test_health_and_catalog(client):
    c, _ = client
    h = c.get("/api/health").json()
    assert h["ok"] and h["catalog"]["loaded"]
    assert h["matcher"]["ready"] and h["matcher"]["refs"] == 30
    assert h["rules"]["set_size"] == 3

    cat = c.get("/api/catalog").json()
    assert len(cat["items"]) == 30
    first = cat["items"][0]
    assert first["icon"].startswith("/icons/")
    icon_resp = c.get(first["icon"])
    assert icon_resp.status_code == 200
    assert icon_resp.headers["content-type"] == "image/png"


def test_icon_path_traversal_blocked(client):
    c, _ = client
    assert c.get("/icons/..%2Fcatalog.json").status_code in (400, 404)


def test_analyze_auto_grid(client):
    c, icons = client
    img, truth = compose_screenshot(icons, seed=11, out_width=1280)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    r = c.post("/api/analyze", files={"file": ("shot.png", buf.tobytes(), "image/png")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["grid"]["rows"] == truth.rows
    assert data["grid"]["cols"] == truth.cols
    assert len(data["cells"]) == truth.rows * truth.cols
    filled = [c_ for c_ in data["cells"] if not c_["empty"]]
    assert filled and all(c_["candidates"] for c_ in filled)
    hits = sum(1 for c_ in data["cells"]
               if (c_["row"], c_["col"]) in truth.cell_items
               and not c_["empty"]
               and c_["candidates"][0]["name"] == f"Item {truth.cell_items[(c_['row'], c_['col'])]:03d}")
    assert hits / len(truth.cell_items) >= 0.9


def test_analyze_manual_grid(client):
    c, icons = client
    img, truth = compose_screenshot(icons, seed=11, out_width=1280)
    ok, buf = cv2.imencode(".png", img)
    r = c.post(
        "/api/analyze",
        files={"file": ("shot.png", buf.tobytes(), "image/png")},
        data={
            "grid_x0": truth.grid_x, "grid_y0": truth.grid_y,
            "grid_x1": truth.grid_x + truth.cols * truth.pitch,
            "grid_y1": truth.grid_y + truth.rows * truth.pitch,
            "rows": truth.rows, "cols": truth.cols,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["grid"]["rows"] == truth.rows


def test_analyze_rejects_garbage(client):
    c, _ = client
    r = c.post("/api/analyze", files={"file": ("x.png", b"nonsense", "image/png")})
    assert r.status_code == 400
