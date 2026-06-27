"""Work out which set bonuses are achievable from a set of owned items.

A set is achievable when you own >= SET_SIZE items belonging to it AND you can
pick SET_SIZE of them that occupy distinct equipment slots (since you can only
wear one item per slot at a time).

Because every item occupies exactly one slot, "pick SET_SIZE items with
pairwise-distinct slots" is possible iff the owned items of that set cover at
least SET_SIZE distinct slots. (Pick one item from each of SET_SIZE slots.)
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .models import Catalog, Item, SetInfo, SET_SIZE


@dataclass
class SetResult:
    name: str
    bonus: str
    owned_items: list[Item]            # all owned items belonging to this set
    distinct_slots: list[str]          # distinct slots covered by owned items
    achievable: bool                   # >= SET_SIZE distinct slots
    example_combo: list[Item]          # one valid wearable combo (if achievable)
    pieces: int = 0                    # total pieces in the set per the wiki

    @property
    def owned_count(self) -> int:
        return len(self.owned_items)

    @property
    def missing_slot_note(self) -> str:
        """Explain why an owned>=SET_SIZE set is still not achievable."""
        if self.achievable:
            return ""
        if self.owned_count < SET_SIZE:
            return f"only {self.owned_count} of {SET_SIZE} pieces owned"
        return (
            f"{self.owned_count} pieces owned but they only cover "
            f"{len(self.distinct_slots)} slot(s) "
            f"({', '.join(self.distinct_slots)}); need {SET_SIZE} distinct slots"
        )


def _pick_distinct_slot_combo(items: list[Item], size: int) -> list[Item]:
    """Return `size` items with pairwise-distinct slots, or [] if impossible."""
    # Greedy by slot is sufficient and we also want a nice deterministic combo;
    # try combinations (small lists) and return the first slot-distinct one.
    for combo in combinations(items, size):
        slots = [it.normalized_slot() for it in combo]
        if len(set(slots)) == size and all(slots):
            return list(combo)
    return []


def build_set_index(catalog: Catalog, owned: list[Item]) -> dict[str, list[Item]]:
    """Map set name -> owned items belonging to it."""
    index: dict[str, list[Item]] = {}
    for it in owned:
        for set_name in it.sets:
            index.setdefault(set_name, []).append(it)
    return index


def solve(catalog: Catalog, owned: list[Item]) -> list[SetResult]:
    """Return one SetResult per set that has at least one owned item.

    Sorted so achievable sets come first, then by how close you are.
    """
    index = build_set_index(catalog, owned)
    results: list[SetResult] = []

    for set_name, members in sorted(index.items()):
        # De-duplicate by item name (same item may appear twice in a screenshot
        # crop pass, but for set purposes we count distinct item identities).
        unique: dict[str, Item] = {}
        for it in members:
            unique.setdefault(it.name.lower(), it)
        owned_items = list(unique.values())

        distinct_slots = sorted({it.normalized_slot() for it in owned_items if it.normalized_slot()})
        combo = _pick_distinct_slot_combo(owned_items, SET_SIZE)
        achievable = len(combo) == SET_SIZE

        info: SetInfo | None = catalog.sets.get(set_name)
        results.append(
            SetResult(
                name=set_name,
                bonus=info.bonus if info else "",
                owned_items=owned_items,
                distinct_slots=distinct_slots,
                achievable=achievable,
                example_combo=combo,
                pieces=info.pieces if info else 0,
            )
        )

    # Achievable first; then by owned_count desc; then name.
    results.sort(key=lambda r: (not r.achievable, -r.owned_count, r.name))
    return results
