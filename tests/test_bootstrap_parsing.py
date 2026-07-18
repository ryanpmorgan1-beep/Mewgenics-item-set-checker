"""Wiki parsing against fixtures that mirror the real page structure
(verified live: 'shuffle__items' table with a leading icon cell absent from
the header row; per-set 'mew-sets-table' tables under h2 headings)."""

import io
import os
import zipfile

import pytest
from bs4 import BeautifulSoup

from app.bootstrap import (import_bundle, original_url_from_thumb, parse_items,
                           parse_sets, source_file_from_image_url,
                           thumb_url_from_original, upsize_thumb_url)

ITEMS_HTML = """
<table class="shuffle__items wikitable sortable mew-sticky-header">
<tr>
  <th>Name</th><th>Slot</th><th>Rarity</th><th>Cursed?</th>
  <th>Description</th><th>Uses</th><th>Item Set</th>
</tr>
<tr>
  <td><img src="/images/thumb/Peace.svg/40px-Peace.svg.png"
       srcset="/images/thumb/Peace.svg/60px-Peace.svg.png 1.5x,
               /images/thumb/Peace.svg/80px-Peace.svg.png 2x"></td>
  <td><a href="/wiki/Peace_Symbol">Peace Symbol</a></td>
  <td>Neck</td><td>Uncommon</td><td>No</td>
  <td>If you end your turn without dealing damage, gain +1 energy.</td>
  <td>&mdash;</td>
  <td><a href="/wiki/Hippie">Hippie</a>,<a href="/wiki/Twine">Twine</a></td>
</tr>
<tr>
  <td><img src="//mewgenics.wiki.gg/images/Crown.png"></td>
  <td><a href="/wiki/Flower_Crown">Flower Crown</a></td>
  <td>Head</td><td>Common</td><td>No</td><td>Bloom effect.</td><td>&mdash;</td>
  <td>Hippie</td>
</tr>
<tr>
  <td><img src="//mewgenics.wiki.gg/images/Rifle.png"></td>
  <td><a href="/wiki/22_Rifle">.22 Rifle</a></td>
  <td>Weapon</td><td>Rare</td><td>No</td>
  <td>Use: Fire a shot.</td><td>2-4</td><td>&mdash;</td>
</tr>
</table>
"""

SETS_HTML = """
<div>
  <h2><span class="mw-headline">Hippie</span></h2>
  <p>Spawn flowers at the start of each battle.</p>
  <table class="wikitable mew-sets-table">
    <tr><th>Slot</th><th>Item</th></tr>
    <tr><td>Head</td><td><a href="/wiki/Flower_Crown">Flower Crown</a> (Head)</td></tr>
    <tr><td>Neck</td><td><a href="/wiki/Peace_Symbol">Peace Symbol</a> (Neck)</td></tr>
    <tr><td>Trinket</td><td><a href="/wiki/Tie_Dye_Shirt">Tie Dye Shirt</a> (Trinket)</td></tr>
  </table>
  <h2><span class="mw-headline">Twine</span></h2>
  <p>Tangle nearby enemies at the start of battle.</p>
  <table class="wikitable mew-sets-table">
    <tr><th>Slot</th><th>Item</th></tr>
    <tr><td>Neck</td><td><a href="/wiki/Peace_Symbol">Peace Symbol</a></td></tr>
    <tr><td>Head</td><td><a href="/wiki/Twine_Hat">Twine Hat</a></td></tr>
    <tr><td>Trinket</td><td><a href="/wiki/Hemp_Rope">Hemp Rope</a></td></tr>
  </table>
</div>
"""


def test_parse_items_fields():
    items = parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    assert len(items) == 3
    peace = items[0]
    assert peace.name == "Peace Symbol"
    assert peace.slot == "Neck"
    assert peace.rarity == "Uncommon"
    assert peace.cursed == "No"
    assert peace.uses == "—"
    assert peace.sets == ["Hippie", "Twine"]
    assert peace.icon_url.endswith("80px-Peace.svg.png")   # srcset 2x wins
    assert peace.page_url == "https://mewgenics.wiki.gg/wiki/Peace_Symbol"


def test_parse_items_no_set_and_protocol_relative():
    items = parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    assert items[1].icon_url == "https://mewgenics.wiki.gg/images/Crown.png"
    assert items[2].name == ".22 Rifle"
    assert items[2].sets == []


def test_parse_sets():
    sets = parse_sets(BeautifulSoup(SETS_HTML, "lxml"))
    assert set(sets) == {"Hippie", "Twine"}
    assert "flowers" in sets["Hippie"].bonus
    assert sets["Hippie"].members == ["Flower Crown", "Peace Symbol", "Tie Dye Shirt"]
    assert "Peace Symbol" in sets["Twine"].members


def test_source_file_from_image_url():
    assert source_file_from_image_url(
        "https://mewgenics.wiki.gg/images/thumb/a/ab/Peace_Symbol.svg/40px-Peace_Symbol.svg.png"
    ) == "Peace_Symbol.svg"
    assert source_file_from_image_url(
        "https://mewgenics.wiki.gg/images/a/ab/Crown.png?v=3") == "Crown.png"


def test_upsize_thumb_url():
    assert upsize_thumb_url(
        "https://mewgenics.wiki.gg/images/thumb/a/ab/P.svg/40px-P.svg.png?v=1", 160
    ) == "https://mewgenics.wiki.gg/images/thumb/a/ab/P.svg/160px-P.svg.png"
    # non-thumb URLs can't be resized
    assert upsize_thumb_url("https://mewgenics.wiki.gg/images/a/ab/C.png", 160) is None


def test_original_url_from_thumb():
    assert original_url_from_thumb(
        "https://mewgenics.wiki.gg/images/thumb/a/ab/C.png/40px-C.png"
    ) == "https://mewgenics.wiki.gg/images/a/ab/C.png"
    assert original_url_from_thumb("https://mewgenics.wiki.gg/images/a/ab/C.png") is None


def test_thumb_url_from_original():
    # raw SVG file (the norm on the Items page) -> rasterized PNG thumb
    assert thumb_url_from_original(
        "https://mewgenics.wiki.gg/images/a/ab/Peace_Symbol.svg?v=2", 160
    ) == ("https://mewgenics.wiki.gg/images/thumb/a/ab/Peace_Symbol.svg/"
          "160px-Peace_Symbol.svg.png")
    # raster original: same pattern, no extra .png suffix
    assert thumb_url_from_original(
        "https://mewgenics.wiki.gg/images/a/ab/Crown.png", 160
    ) == "https://mewgenics.wiki.gg/images/thumb/a/ab/Crown.png/160px-Crown.png"
    # already a thumb -> handled by upsize, not this builder
    assert thumb_url_from_original(
        "https://mewgenics.wiki.gg/images/thumb/a/ab/X.svg/40px-X.svg.png", 160) is None


# --------------------------------------------------------------------------- #
# Bundle import
# --------------------------------------------------------------------------- #
_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def _bundle_bytes(n_items=12, with_icon=True) -> bytes:
    import json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        items = [{"name": f"I{i}", "slot": "Head", "sets": [], "icon_file": "i.png"}
                 for i in range(n_items)]
        z.writestr("catalog.json", json.dumps({"items": items, "sets": {}}))
        if with_icon:
            z.writestr("icons/i.png", _PNG)
    return buf.getvalue()


def test_import_bundle_ok(tmp_path):
    stats = import_bundle(_bundle_bytes(), str(tmp_path))
    assert stats["items"] == 12
    assert stats["icons"] == 1
    assert os.path.exists(tmp_path / "catalog.json")
    assert os.path.exists(tmp_path / "icons" / "i.png")


def test_import_bundle_rejects_garbage(tmp_path):
    with pytest.raises(ValueError):
        import_bundle(b"not a zip", str(tmp_path))
    with pytest.raises(ValueError):
        import_bundle(_bundle_bytes(n_items=2), str(tmp_path))   # too few items


def test_import_bundle_skips_non_png(tmp_path):
    import json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        items = [{"name": f"I{i}"} for i in range(12)]
        z.writestr("catalog.json", json.dumps({"items": items, "sets": {}}))
        z.writestr("icons/evil.png", b"<svg>not a raster</svg>")
        z.writestr("icons/../escape.png", _PNG)
        z.writestr("icons/fine.png", _PNG)
    stats = import_bundle(buf.getvalue(), str(tmp_path))
    assert stats["icons"] == 2   # escape.png flattened to basename, evil skipped
    assert not os.path.exists(tmp_path.parent / "escape.png")
    assert os.path.exists(tmp_path / "icons" / "fine.png")
