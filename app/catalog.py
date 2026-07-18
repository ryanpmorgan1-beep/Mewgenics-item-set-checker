"""Item / set catalog: data model, (de)serialization, and slot canonicalization.

The catalog is produced by ``app.bootstrap`` (wiki scrape) and consumed by the
matcher and the web UI. It lives in ``DATA_DIR/catalog.json`` with icons in
``DATA_DIR/icons/``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

# The game has exactly five equipment slots. A cat wears at most one item per
# slot, so a set bonus (>= 3 pieces worn) needs pieces covering >= 3 slots.
EQUIPMENT_SLOTS = ["Head", "Face", "Neck", "Trinket", "Weapon"]

# Pieces of one set that must be worn simultaneously to activate its bonus.
SET_SIZE = 3

DATA_DIR = os.environ.get("DATA_DIR", "data")
CATALOG_JSON = "catalog.json"
ICON_DIR = "icons"


def canonical_slot(slot: str) -> str:
    """Map a wiki slot label to one of EQUIPMENT_SLOTS (best effort)."""
    s = (slot or "").strip().lower()
    aliases = {
        "head": "Head", "hat": "Head",
        "face": "Face", "eyes": "Face", "eye": "Face", "glasses": "Face",
        "neck": "Neck", "necklace": "Neck", "amulet": "Neck", "collar": "Neck",
        "trinket": "Trinket", "ring": "Trinket",
        "weapon": "Weapon", "held": "Weapon",
    }
    return aliases.get(s, slot.strip().title() if slot else "")


@dataclass
class Item:
    name: str
    slot: str = ""                 # one of EQUIPMENT_SLOTS ("" if unknown)
    rarity: str = ""
    cursed: str = ""
    effect: str = ""
    uses: str = ""
    sets: list[str] = field(default_factory=list)
    page_url: str = ""
    icon_url: str = ""             # absolute wiki URL the icon came from
    icon_file: str = ""            # filename inside DATA_DIR/icons/

    def normalized_slot(self) -> str:
        return canonical_slot(self.slot)


@dataclass
class SetInfo:
    name: str
    bonus: str = ""
    members: list[str] = field(default_factory=list)   # item names


@dataclass
class Catalog:
    items: list[Item] = field(default_factory=list)
    sets: dict[str, SetInfo] = field(default_factory=dict)
    generated_at: str = ""
    source: str = ""

    def item_by_name(self, name: str) -> Item | None:
        key = name.strip().lower()
        for it in self.items:
            if it.name.strip().lower() == key:
                return it
        return None

    def icon_path(self, item: Item, data_dir: str = DATA_DIR) -> str:
        if not item.icon_file:
            return ""
        return os.path.join(data_dir, ICON_DIR, item.icon_file)


def catalog_path(data_dir: str = DATA_DIR) -> str:
    return os.path.join(data_dir, CATALOG_JSON)


def save_catalog(catalog: Catalog, data_dir: str = DATA_DIR) -> str:
    os.makedirs(data_dir, exist_ok=True)
    payload = {
        "generated_at": catalog.generated_at,
        "source": catalog.source,
        "items": [asdict(it) for it in catalog.items],
        "sets": {name: asdict(info) for name, info in catalog.sets.items()},
    }
    path = catalog_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_catalog(data_dir: str = DATA_DIR) -> Catalog:
    with open(catalog_path(data_dir), encoding="utf-8") as fh:
        payload = json.load(fh)
    items = [Item(**it) for it in payload.get("items", [])]
    sets = {name: SetInfo(**info) for name, info in payload.get("sets", {}).items()}
    return Catalog(
        items=items,
        sets=sets,
        generated_at=payload.get("generated_at", ""),
        source=payload.get("source", ""),
    )


def has_catalog(data_dir: str = DATA_DIR) -> bool:
    return os.path.exists(catalog_path(data_dir))
