"""Tests for the wiki HTML parsing (no network; uses fixtures matching real wiki structure).

Real wiki structure (discovered from debug_scrape.py output):

Items page:
  - Table class: 'shuffle__items' (plus wikitable, sortable, mew-sticky-header)
  - Header row: ['Name', 'Slot', 'Rarity', 'Cursed?', 'Description', 'Uses', 'Item Set']
    (7 columns — NO 'Icon' header)
  - Data rows: 8 cells — leading icon cell (index 0) not in header, then name, slot, ...
    Offset = len(data_cells) - len(header_cells) = 1

Item_sets page:
  - ~97 tables, each with class 'mew-sets-table', columns ['Slot', 'Item']
  - Set name is in the nearest preceding h2/h3 heading
  - Set bonus is in the paragraph(s) between the heading and the table
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from mewgenics_sets import scrape

# --------------------------------------------------------------------------- #
# Items page fixture — mirrors the real shuffle__items structure
# --------------------------------------------------------------------------- #
ITEMS_HTML = """
<table class="shuffle__items wikitable sortable mew-sticky-header">
<tr>
  <th>Name</th><th>Slot</th><th>Rarity</th><th>Cursed?</th>
  <th>Description</th><th>Uses</th><th>Item Set</th>
</tr>
<tr>
  <td><img src="/images/thumb/Peace.png/40px-Peace.png"
       srcset="/images/thumb/Peace.png/60px-Peace.png 1.5x,
               /images/thumb/Peace.png/80px-Peace.png 2x"></td>
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

# --------------------------------------------------------------------------- #
# Item_sets page fixture — mirrors the real mew-sets-table structure
# --------------------------------------------------------------------------- #
SETS_HTML = """
<div>
  <h2><span class="mw-headline">Hippie</span></h2>
  <p>Spawn flowers at the start of each battle.</p>
  <table class="wikitable mew-sets-table">
    <tr><th>Slot</th><th>Item</th></tr>
    <tr>
      <td>Head</td>
      <td><a href="/wiki/Flower_Crown">Flower Crown</a> Flower Crown (Head) Bloom effect.</td>
    </tr>
    <tr>
      <td>Neck</td>
      <td><a href="/wiki/Peace_Symbol">Peace Symbol</a> Peace Symbol (Neck) If you end...</td>
    </tr>
    <tr>
      <td>Trinket</td>
      <td><a href="/wiki/Tie_Dye_Shirt">Tie Dye Shirt</a> Tie Dye Shirt (Trinket) +1 Luck.</td>
    </tr>
  </table>

  <h2><span class="mw-headline">Twine</span></h2>
  <p>Tangle nearby enemies at the start of battle.</p>
  <table class="wikitable mew-sets-table">
    <tr><th>Slot</th><th>Item</th></tr>
    <tr>
      <td>Neck</td>
      <td><a href="/wiki/Peace_Symbol">Peace Symbol</a> Peace Symbol (Neck) If you end...</td>
    </tr>
    <tr>
      <td>Head</td>
      <td><a href="/wiki/Twine_Hat">Twine Hat</a> Twine Hat (Head) +3 +50% tangle chance.</td>
    </tr>
    <tr>
      <td>Trinket</td>
      <td><a href="/wiki/Hemp_Rope">Hemp Rope</a> Hemp Rope (Trinket) Bind on hit.</td>
    </tr>
  </table>
</div>
"""


def test_parse_items_count_and_offset():
    items = scrape.parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    assert len(items) == 3, f"Expected 3 items, got {len(items)}"


def test_parse_items_peace_symbol():
    items = scrape.parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    peace = items[0]
    assert peace.name == "Peace Symbol"
    assert peace.slot == "Neck"
    assert peace.rarity == "Uncommon"
    assert peace.sets == ["Hippie", "Twine"], peace.sets
    # icon: srcset 2x entry should be picked as highest resolution
    assert peace.icon_url.endswith("80px-Peace.png"), peace.icon_url
    assert peace.page_url.endswith("/wiki/Peace_Symbol"), peace.page_url


def test_parse_items_protocol_relative_icon():
    items = scrape.parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    crown = items[1]
    assert crown.icon_url == "https://mewgenics.wiki.gg/images/Crown.png", crown.icon_url


def test_parse_items_no_set():
    items = scrape.parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    rifle = items[2]
    assert rifle.name == ".22 Rifle"
    assert rifle.sets == [], rifle.sets


def test_parse_sets_names_and_count():
    sets = scrape.parse_sets(BeautifulSoup(SETS_HTML, "lxml"))
    assert set(sets) == {"Hippie", "Twine"}, set(sets)


def test_parse_sets_bonus():
    sets = scrape.parse_sets(BeautifulSoup(SETS_HTML, "lxml"))
    assert "flowers" in sets["Hippie"].bonus, sets["Hippie"].bonus
    assert "Tangle" in sets["Twine"].bonus, sets["Twine"].bonus


def test_parse_sets_member_items():
    sets = scrape.parse_sets(BeautifulSoup(SETS_HTML, "lxml"))
    assert sets["Hippie"].member_items == ["Flower Crown", "Peace Symbol", "Tie Dye Shirt"]
    assert sets["Hippie"].pieces == 3
    assert "Peace Symbol" in sets["Twine"].member_items


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    raise SystemExit(1 if failed else 0)
