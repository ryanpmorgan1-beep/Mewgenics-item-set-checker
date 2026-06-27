"""Shared data structures for the set-bonus finder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# The game has exactly five equipment slots. Two items in the same slot cannot
# be worn together, which is what makes some 3-item set combos impossible.
EQUIPMENT_SLOTS = ["Head", "Face", "Neck", "Trinket", "Weapon"]

# Number of matching-set items required to complete a set bonus.
SET_SIZE = 3


@dataclass
class Item:
    """A single equippable item from the wiki catalog."""

    name: str
    slot: str                      # one of EQUIPMENT_SLOTS (or "" if unknown)
    rarity: str = ""
    effect: str = ""               # the item's own effect text
    sets: list[str] = field(default_factory=list)   # set names this item belongs to
    page_url: str = ""             # wiki page for the item
    icon_url: str = ""             # absolute URL of the wiki icon image
    icon_path: str = ""            # local cached path of the downloaded icon

    def normalized_slot(self) -> str:
        return canonical_slot(self.slot)


@dataclass
class SetInfo:
    """A set and the bonus you get for completing it."""

    name: str
    bonus: str = ""
    pieces: int = 0                # total pieces in the set, if the wiki states it
    member_items: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    """The full scraped wiki catalog."""

    items: list[Item] = field(default_factory=list)
    sets: dict[str, SetInfo] = field(default_factory=dict)

    def item_by_name(self, name: str) -> Optional[Item]:
        key = name.strip().lower()
        for it in self.items:
            if it.name.strip().lower() == key:
                return it
        return None

    def set_names(self) -> list[str]:
        names: set[str] = set(self.sets.keys())
        for it in self.items:
            names.update(it.sets)
        return sorted(names)


def canonical_slot(slot: str) -> str:
    """Map a wiki slot label to one of EQUIPMENT_SLOTS (best effort)."""
    s = (slot or "").strip().lower()
    aliases = {
        "head": "Head",
        "hat": "Head",
        "face": "Face",
        "eyes": "Face",
        "glasses": "Face",
        "neck": "Neck",
        "necklace": "Neck",
        "amulet": "Neck",
        "trinket": "Trinket",
        "ring": "Trinket",
        "weapon": "Weapon",
        "held": "Weapon",
    }
    return aliases.get(s, slot.strip().title() if slot else "")
