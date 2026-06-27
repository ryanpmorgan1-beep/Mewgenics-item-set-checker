"""Inspect the live wiki structure so we can verify/fix the parser."""

import requests
from bs4 import BeautifulSoup

API = "https://mewgenics.wiki.gg/api.php"
HEADERS = {"User-Agent": "MewgenicsSetFinder/0.1 (debug)"}


def fetch(page):
    resp = requests.get(
        API,
        params={"action": "parse", "page": page, "prop": "text",
                "format": "json", "formatversion": "2"},
        headers=HEADERS, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return BeautifulSoup(data["parse"]["text"], "lxml")


# ---------- Items page ----------
print("\n" + "="*60)
print("ITEMS PAGE")
print("="*60)
soup = fetch("Items")
tables = soup.find_all("table")
print(f"Total tables: {len(tables)}")
for i, t in enumerate(tables):
    cls = t.get("class", [])
    rows = t.find_all("tr")
    print(f"\n  Table {i}: classes={cls}, rows={len(rows)}")
    for j, row in enumerate(rows[:2]):
        cells = row.find_all(["th", "td"])
        print(f"    row {j} ({len(cells)} cells): {[c.get_text(' ',strip=True)[:35] for c in cells]}")

# Show a few full data rows from the shuffle__items table to confirm offset
main = soup.find("table", class_="shuffle__items")
if main:
    rows = main.find_all("tr")
    print(f"\n  shuffle__items table: {len(rows)} rows")
    # Find a row that has a non-dash set value
    found = 0
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if cells and cells[-1].get_text(strip=True) not in ("—", "-", ""):
            print(f"  sample set row ({len(cells)} cells): {[c.get_text(' ',strip=True)[:30] for c in cells]}")
            found += 1
            if found >= 3:
                break

# ---------- Item_sets page ----------
print("\n" + "="*60)
print("ITEM_SETS PAGE — first 3 mew-sets-tables with surrounding context")
print("="*60)
soup2 = fetch("Item_sets")
tables2 = soup2.find_all("table", class_="mew-sets-table")
print(f"Total mew-sets-tables: {len(tables2)}")

for i, table in enumerate(tables2[:3]):
    print(f"\n--- mew-sets-table #{i} ---")

    # Nearest preceding heading
    heading = table.find_previous(["h2", "h3", "h4"])
    if heading:
        span = heading.find("span", class_="mw-headline")
        set_name = span.get_text(strip=True) if span else heading.get_text(" ", strip=True)
        print(f"  Preceding heading: {set_name!r}")
    else:
        print("  No preceding heading found!")

    # Elements between heading and table
    print("  Elements between heading and table:")
    if heading:
        for sib in heading.find_next_siblings():
            if sib is table:
                break
            if hasattr(sib, "name") and sib.name:
                txt = sib.get_text(" ", strip=True)[:80]
                print(f"    <{sib.name} class={sib.get('class','[]')}> {txt!r}")

    # Table rows
    rows = table.find_all("tr")
    print(f"  Table rows ({len(rows)} total):")
    for row in rows[:4]:
        cells = row.find_all(["td", "th"])
        print(f"    {[c.get_text(' ',strip=True)[:40] for c in cells]}")
