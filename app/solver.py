"""Which set bonuses can you actually wear, given the items you own?

Game rules (per the wiki):
  * A cat has 5 equipment slots: Head, Face, Neck, Trinket, Weapon.
  * A set bonus activates while wearing >= SET_SIZE (3) items of that set.
  * One item per slot, so the 3 pieces must occupy 3 distinct slots.

Since every item occupies exactly one slot, "3 wearable pieces" is possible
iff the owned pieces of the set cover >= 3 distinct slots.

The web UI reimplements this in JS for live recompute; this module is the
canonical version used by the CLI and tests. Keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .catalog import SET_SIZE, Catalog, Item


@dataclass
class SetResult:
    name: str
    bonus: str
    members: list[str]                      # full member list per the wiki
    owned: list[Item] = field(default_factory=list)
    distinct_slots: list[str] = field(default_factory=list)
    wearable: bool = False
    combo: list[Item] = field(default_factory=list)   # one valid 3-piece pick
    missing: list[str] = field(default_factory=list)  # members that would help

    @property
    def status(self) -> str:
        if self.wearable:
            return "wearable"
        if len(self.owned) >= SET_SIZE:
            return "slot_blocked"
        if len(self.distinct_slots) == SET_SIZE - 1:
            return "close"
        return "started"


def _distinct_slot_combo(items: list[Item], size: int) -> list[Item]:
    for combo in combinations(items, size):
        slots = [it.normalized_slot() for it in combo]
        if len(set(slots)) == size and all(slots):
            return list(combo)
    return []


def solve(catalog: Catalog, owned_names: list[str]) -> list[SetResult]:
    """One SetResult per set with at least one owned piece, best first."""
    owned_items: dict[str, Item] = {}
    for name in owned_names:
        it = catalog.item_by_name(name)
        if it is not None:
            owned_items.setdefault(it.name, it)

    per_set: dict[str, list[Item]] = {}
    for it in owned_items.values():
        for set_name in it.sets:
            per_set.setdefault(set_name, []).append(it)

    results: list[SetResult] = []
    for set_name, pieces in per_set.items():
        info = catalog.sets.get(set_name)
        members = info.members if info else []
        slots_covered = sorted({p.normalized_slot() for p in pieces if p.normalized_slot()})
        combo = _distinct_slot_combo(pieces, SET_SIZE)

        # Which not-yet-owned members would add a new slot?
        owned_lower = {p.name.lower() for p in pieces}
        missing = []
        for m in members:
            if m.lower() in owned_lower:
                continue
            mi = catalog.item_by_name(m)
            if mi is None or not mi.normalized_slot() or mi.normalized_slot() not in slots_covered:
                missing.append(m)

        results.append(SetResult(
            name=set_name,
            bonus=info.bonus if info else "",
            members=members,
            owned=sorted(pieces, key=lambda p: p.name),
            distinct_slots=slots_covered,
            wearable=len(combo) == SET_SIZE,
            combo=combo,
            missing=missing,
        ))

    results.sort(key=lambda r: (not r.wearable, -len(r.distinct_slots), -len(r.owned), r.name))
    return results
