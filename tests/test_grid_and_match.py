"""End-to-end vision test on the synthetic Mewgenics-style fixture.

This guards the failure mode of the original project: the pipeline must find
the storage grid on its own and identify the items in it, across resolutions,
despite corner glyphs, glows, tints, ribbons, noise and JPEG artifacts.
"""

import pytest

from app.catalog import load_catalog
from app.grid import Grid, detect_grid
from app.matcher import Matcher

from synth import compose_screenshot, make_icons, write_catalog

N_ICONS = 60


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    data_dir = str(tmp_path_factory.mktemp("synthdata"))
    icons = make_icons(N_ICONS, seed=7)
    write_catalog(icons, data_dir)
    catalog = load_catalog(data_dir)
    matcher = Matcher(catalog, data_dir)
    assert matcher.n_refs == N_ICONS
    return icons, matcher


@pytest.mark.parametrize("width", [2000, 1280])
def test_grid_and_matching(bundle, width):
    icons, matcher = bundle
    img, truth = compose_screenshot(icons, seed=11, out_width=width)

    grid = detect_grid(img)
    assert grid is not None, "grid not found"
    assert (grid.rows, grid.cols) == (truth.rows, truth.cols)
    assert abs(grid.x - truth.grid_x) < truth.side * 0.12
    assert abs(grid.y - truth.grid_y) < truth.side * 0.12
    assert abs(grid.pitch_x - truth.pitch) < truth.pitch * 0.03

    results = matcher.analyze(img, grid)
    n_filled = top1 = filled_as_empty = empty_as_filled = 0
    confident_wrong = []
    for cell in results:
        gt = truth.cell_items.get((cell.row, cell.col))
        if gt is None:
            if not cell.empty:
                empty_as_filled += 1
            continue
        n_filled += 1
        if cell.empty:
            filled_as_empty += 1
            continue
        got = cell.candidates[0]
        if got.name == f"Item {gt:03d}":
            top1 += 1
        elif got.score >= 0.55:
            confident_wrong.append((cell.row, cell.col, got.name, got.score))

    assert filled_as_empty == 0, "an item was misread as an empty slot"
    assert empty_as_filled <= 1
    accuracy = top1 / n_filled
    assert accuracy >= 0.95, f"top-1 accuracy {accuracy:.3f} below bar"
    # Any residual misses must at least be flagged as uncertain in the UI.
    assert not confident_wrong, f"confidently wrong matches: {confident_wrong}"


def test_manual_grid_from_corners():
    g = Grid.from_corners(100, 200, 100 + 11 * 67, 200 + 10 * 67, rows=10, cols=11)
    assert (g.rows, g.cols) == (10, 11)
    x0, y0, x1, y1 = g.cell_box(0, 0)
    assert (x0, y0) == (100, 200)
    boxes = list(g.cells())
    assert len(boxes) == 110
    # last cell stays inside the marked region
    _, _, (lx0, ly0, lx1, ly1) = boxes[-1]
    assert lx1 <= 100 + 11 * 67 + 1 and ly1 <= 200 + 10 * 67 + 1
