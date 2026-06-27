"""Run this to show the actual table structure from the wiki so we can fix parsing."""

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


def show_tables(page):
    print(f"\n{'='*60}")
    print(f"PAGE: {page}")
    print('='*60)
    soup = fetch(page)
    tables = soup.find_all("table")
    print(f"Total tables found: {len(tables)}")
    for i, tbl in enumerate(tables):
        classes = tbl.get("class", [])
        rows = tbl.find_all("tr")
        print(f"\n  Table {i}: classes={classes}, rows={len(rows)}")
        # First 2 rows (headers + maybe first data row)
        for j, row in enumerate(rows[:2]):
            cells = row.find_all(["th", "td"])
            texts = [c.get_text(" ", strip=True)[:40] for c in cells]
            print(f"    row {j}: {texts}")


show_tables("Items")
show_tables("Item_sets")
