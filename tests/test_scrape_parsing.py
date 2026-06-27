"""Tests for the wiki HTML parsing (no network; uses inline fixtures)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from mewgenics_sets import scrape

ITEMS_HTML = """
<table class="wikitable sortable">
<tr><th>Icon</th><th>Name</th><th>Slot</th><th>Rarity</th><th>Stackable</th>
    <th>Effect</th><th>Multiplier</th><th>Sets</th></tr>
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
  <td>Head</td><td>Common</td><td>No</td><td>Bloom.</td><td>&mdash;</td><td>Hippie</td>
</tr>
</table>
"""

SETS_HTML = """
<table class="wikitable">
<tr><th>Set</th><th>Pieces</th><th>Bonus</th></tr>
<tr><td>Hippie</td><td>4</td><td>Spawn flowers each turn.</td></tr>
<tr><td>Twine</td><td>3</td><td>Tangle nearby enemies.</td></tr>
</table>
"""


def test_parse_items():
    items = scrape.parse_items(BeautifulSoup(ITEMS_HTML, "lxml"))
    assert len(items) == 2
    peace = items[0]
    assert peace.name == "Peace Symbol"
    assert peace.slot == "Neck"
    assert peace.rarity == "Uncommon"
    assert peace.sets == ["Hippie", "Twine"]
    # srcset 2x is the highest-res source
    assert peace.icon_url.endswith("80px-Peace.png")
    assert peace.page_url.endswith("/wiki/Peace_Symbol")
    # protocol-relative src is made absolute
    assert items[1].icon_url == "https://mewgenics.wiki.gg/images/Crown.png"


def test_parse_sets():
    sets = scrape.parse_sets(BeautifulSoup(SETS_HTML, "lxml"))
    assert set(sets) == {"Hippie", "Twine"}
    assert sets["Hippie"].pieces == 4
    assert "flowers" in sets["Hippie"].bonus
    assert sets["Twine"].pieces == 3


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
