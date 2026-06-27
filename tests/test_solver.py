"""Tests for the slot-aware set solver (the logic that decides achievability)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mewgenics_sets.models import Catalog, Item, SetInfo
from mewgenics_sets.solver import solve


def _item(name, slot, *sets):
    return Item(name=name, slot=slot, sets=list(sets))


def _catalog():
    cat = Catalog()
    cat.sets = {
        "Hippie": SetInfo(name="Hippie", bonus="Peace bonus"),
        "Twine": SetInfo(name="Twine", bonus="Twine bonus"),
        "Bone": SetInfo(name="Bone", bonus="+4 Shield"),
    }
    return cat


def test_three_distinct_slots_is_achievable():
    cat = _catalog()
    owned = [
        _item("Peace Symbol", "Neck", "Hippie", "Twine"),
        _item("Flower Crown", "Head", "Hippie"),
        _item("Tie Dye Shirt", "Trinket", "Hippie"),
    ]
    results = {r.name: r for r in solve(cat, owned)}
    assert results["Hippie"].achievable is True
    assert len(results["Hippie"].example_combo) == 3
    slots = {it.normalized_slot() for it in results["Hippie"].example_combo}
    assert len(slots) == 3


def test_slot_collision_blocks_set():
    # Three Hippie items but two share the Head slot -> cannot wear all three.
    cat = _catalog()
    owned = [
        _item("Flower Crown", "Head", "Hippie"),
        _item("Bandana", "Head", "Hippie"),
        _item("Peace Symbol", "Neck", "Hippie"),
    ]
    r = {x.name: x for x in solve(cat, owned)}["Hippie"]
    assert r.owned_count == 3
    assert r.achievable is False
    assert sorted(r.distinct_slots) == ["Head", "Neck"]
    assert "distinct slots" in r.missing_slot_note


def test_fewer_than_three_owned_not_achievable():
    cat = _catalog()
    owned = [
        _item("Peace Symbol", "Neck", "Hippie", "Twine"),
        _item("Flower Crown", "Head", "Hippie"),
    ]
    r = {x.name: x for x in solve(cat, owned)}["Hippie"]
    assert r.achievable is False
    assert "2 of 3" in r.missing_slot_note


def test_item_counts_toward_multiple_sets():
    cat = _catalog()
    owned = [
        _item("Peace Symbol", "Neck", "Hippie", "Twine"),
        _item("Hemp Rope", "Trinket", "Twine"),
        _item("Macrame Bag", "Head", "Twine"),
    ]
    r = {x.name: x for x in solve(cat, owned)}
    assert r["Twine"].achievable is True
    # Peace Symbol also seeds Hippie, but only 1 Hippie item -> not achievable.
    assert r["Hippie"].achievable is False


def test_duplicate_item_names_collapse():
    cat = _catalog()
    owned = [
        _item("Peace Symbol", "Neck", "Hippie"),
        _item("Peace Symbol", "Neck", "Hippie"),  # same item detected twice
        _item("Flower Crown", "Head", "Hippie"),
    ]
    r = {x.name: x for x in solve(cat, owned)}["Hippie"]
    assert r.owned_count == 2          # collapsed
    assert r.achievable is False


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)
