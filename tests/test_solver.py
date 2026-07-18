"""Set-solver rules: a bonus is wearable with >=3 pieces across >=3 slots."""

from app.catalog import Catalog, Item, SetInfo
from app.solver import solve


def _catalog() -> Catalog:
    items = [
        Item(name="Flower Crown", slot="Head", sets=["Hippie"]),
        Item(name="Peace Symbol", slot="Neck", sets=["Hippie", "Twine"]),
        Item(name="Tie Dye Shirt", slot="Trinket", sets=["Hippie"]),
        Item(name="Round Glasses", slot="Face", sets=["Hippie"]),
        Item(name="Twine Hat", slot="Head", sets=["Twine"]),
        Item(name="Hemp Rope", slot="Trinket", sets=["Twine"]),
        Item(name="Bandana A", slot="Head", sets=["Cowboy"]),
        Item(name="Bandana B", slot="Head", sets=["Cowboy"]),
        Item(name="Bandana C", slot="Head", sets=["Cowboy"]),
        Item(name="Lasso", slot="Weapon", sets=["Cowboy"]),
    ]
    sets: dict[str, SetInfo] = {}
    for it in items:
        for s in it.sets:
            sets.setdefault(s, SetInfo(name=s, bonus=f"{s} bonus")).members.append(it.name)
    return Catalog(items=items, sets=sets)


def test_wearable_with_three_distinct_slots():
    cat = _catalog()
    res = solve(cat, ["Flower Crown", "Peace Symbol", "Tie Dye Shirt"])
    hippie = next(r for r in res if r.name == "Hippie")
    assert hippie.wearable
    assert len(hippie.combo) == 3
    assert {i.normalized_slot() for i in hippie.combo} == {"Head", "Neck", "Trinket"}


def test_not_wearable_with_two_pieces():
    cat = _catalog()
    res = solve(cat, ["Flower Crown", "Peace Symbol"])
    hippie = next(r for r in res if r.name == "Hippie")
    assert not hippie.wearable
    assert hippie.status == "close"
    assert set(hippie.missing) == {"Tie Dye Shirt", "Round Glasses"}


def test_slot_collision_blocks_set():
    cat = _catalog()
    res = solve(cat, ["Bandana A", "Bandana B", "Bandana C"])
    cowboy = next(r for r in res if r.name == "Cowboy")
    assert not cowboy.wearable
    assert cowboy.status == "slot_blocked"


def test_duplicates_do_not_double_count():
    cat = _catalog()
    res = solve(cat, ["Flower Crown", "Flower Crown", "Peace Symbol"])
    hippie = next(r for r in res if r.name == "Hippie")
    assert not hippie.wearable
    assert len(hippie.owned) == 2


def test_shared_item_counts_for_both_sets():
    cat = _catalog()
    res = solve(cat, ["Peace Symbol", "Twine Hat", "Hemp Rope"])
    twine = next(r for r in res if r.name == "Twine")
    assert twine.wearable
    assert "Hippie" in {r.name for r in res}


def test_unknown_items_ignored():
    cat = _catalog()
    assert solve(cat, ["Nonexistent Thing"]) == []
