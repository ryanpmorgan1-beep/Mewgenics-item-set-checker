"""Mewgenics set-bonus finder.

Given a screenshot of the in-game inventory/storage screen, identify the items
present, then surface every set bonus that is *achievable* with those items.

A set bonus needs 3 items sharing a set name, AND those 3 items must occupy
distinct equipment slots (you can only wear one Head item at a time, etc.).

Pipeline:
    scrape  -> fetch the wiki item + set catalog and icon images (cached JSON)
    vision  -> crop the storage grid from the screenshot and match each cell
    solver  -> compute which sets are achievable given the matched items
    report  -> render a self-contained HTML report
"""

__version__ = "0.1.0"
