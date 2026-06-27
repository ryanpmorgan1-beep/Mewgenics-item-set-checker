"""Render a self-contained, interactive HTML report.

The page embeds the catalog and the per-cell match candidates (icons + crops as
inline base64) plus a small JS re-implementation of the set solver. You confirm
or correct each cell's item via a dropdown; the "Achievable set bonuses" panel
recomputes live in the browser. No server, no rebuild needed.
"""

from __future__ import annotations

import base64
import html
import json
import os
from typing import Optional

from .models import Catalog, SET_SIZE, EQUIPMENT_SLOTS
from .vision import CellMatch


def _b64(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    mime = "jpeg" if ext in {"jpg", "jpeg"} else ext
    return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")


def _catalog_payload(catalog: Catalog, needed_items: set[str]) -> dict:
    items = {}
    for it in catalog.items:
        items[it.name] = {
            "slot": it.normalized_slot(),
            "sets": it.sets,
            "icon": _b64(it.icon_path) if (it.name in needed_items and it.icon_path) else "",
            "rarity": it.rarity,
        }
    sets = {
        name: {"bonus": info.bonus, "pieces": info.pieces}
        for name, info in catalog.sets.items()
    }
    return {"items": items, "sets": sets, "slots": EQUIPMENT_SLOTS, "setSize": SET_SIZE}


def render(
    catalog: Catalog,
    cells: list[CellMatch],
    out_path: str,
    title: str = "Mewgenics — Achievable Set Bonuses",
) -> str:
    filled = [c for c in cells if not c.empty and c.candidates]

    needed: set[str] = set()
    for c in filled:
        for cand in c.candidates:
            needed.add(cand.item.name)

    payload = _catalog_payload(catalog, needed)

    cell_data = []
    for c in filled:
        cell_data.append(
            {
                "id": f"{c.row}_{c.col}",
                "crop": _b64(c.crop_path),
                "candidates": [
                    {"name": cand.item.name, "score": round(cand.score, 3)}
                    for cand in c.candidates
                ],
            }
        )

    page = _TEMPLATE
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__CATALOG__", json.dumps(payload))
    page = page.replace("__CELLS__", json.dumps(cell_data))
    page = page.replace("__SETSIZE__", str(SET_SIZE))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#1d1f23; --panel:#262a30; --line:#3a3f47; --good:#52c77a;
          --bad:#c7625b; --text:#e6e8ea; --muted:#9aa1ab; --accent:#7aa2f7; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif; }
  header { padding:18px 22px; border-bottom:1px solid var(--line);
           position:sticky; top:0; background:var(--bg); z-index:5; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:var(--muted); font-size:13px; }
  .wrap { display:grid; grid-template-columns: 400px 1fr; gap:20px; padding:20px; }
  @media (max-width: 900px){ .wrap{ grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:10px; padding:14px; }
  h2 { font-size:15px; margin:0 0 10px; color:var(--muted);
       text-transform:uppercase; letter-spacing:.05em; }

  /* ---- cell row ---- */
  .cell { display:flex; gap:8px; align-items:center; padding:6px 4px;
          border-bottom:1px solid var(--line); }
  .cell img.crop { width:46px; height:46px; flex-shrink:0; border-radius:6px;
                   background:#111; object-fit:contain; }
  .cell img.ref  { width:30px; height:30px; flex-shrink:0; border-radius:4px;
                   background:#111; object-fit:contain; opacity:.75; }

  /* ---- searchable combobox ---- */
  .combo-wrap { position:relative; flex:1; }
  .combo-input { width:100%; background:#1b1e22; color:var(--text);
                 border:1px solid var(--line); border-radius:6px;
                 padding:5px 8px; font-size:14px; outline:none; }
  .combo-input:focus { border-color:var(--accent); }
  .combo-drop { position:absolute; z-index:99; left:0; right:0; top:100%;
                background:#1b1e22; border:1px solid var(--accent);
                border-top:none; border-radius:0 0 6px 6px;
                max-height:260px; overflow-y:auto; display:none; }
  .combo-opt { padding:5px 8px; cursor:pointer; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .combo-opt:hover, .combo-opt.focused { background:var(--accent); color:#000; }
  .combo-opt.hi { color:#d8b063; }
  .combo-opt.none { color:var(--muted); font-style:italic; cursor:default; }

  /* ---- set bonuses panel ---- */
  .setlist { display:flex; flex-direction:column; gap:12px; }
  .set { border:1px solid var(--line); border-radius:10px; padding:12px;
         background:#21252b; }
  .set.ok { border-color:var(--good); }
  .set.near { border-color:#7a6b3a; }
  .set h3 { margin:0 0 4px; font-size:16px; display:flex; gap:8px; align-items:center; }
  .badge { font-size:11px; padding:2px 7px; border-radius:20px; font-weight:600; }
  .badge.ok { background:rgba(82,199,122,.18); color:var(--good); }
  .badge.near { background:rgba(199,160,91,.18); color:#d8b063; }
  .bonus { color:var(--text); margin:4px 0 8px; }
  .pieces { display:flex; flex-wrap:wrap; gap:8px; }
  .piece { display:flex; align-items:center; gap:6px; font-size:13px;
           background:#1b1e22; border:1px solid var(--line);
           border-radius:20px; padding:3px 9px; }
  .piece img { width:22px; height:22px; object-fit:contain; }
  .piece .slot { color:var(--muted); font-size:11px; }
  .note { color:var(--muted); font-size:12px; margin-top:6px; }
  .controls { margin-bottom:10px; display:flex; gap:8px; align-items:center; }
  .controls input[type=checkbox] { width:18px; height:18px; }
  .empty-msg { color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">Type to search for an item name on the left; achievable set
    bonuses update on the right. A set needs __SETSIZE__ items across distinct
    equipment slots.</div>
</header>
<div class="wrap">
  <div class="panel">
    <h2>Detected items <span class="sub" id="cellcount"></span></h2>
    <div id="cells"></div>
  </div>
  <div class="panel">
    <div class="controls">
      <label><input type="checkbox" id="showNear" checked>
        Show near-misses (owned but not yet wearable)</label>
    </div>
    <h2>Achievable set bonuses</h2>
    <div id="sets" class="setlist"></div>
  </div>
</div>

<script>
const CATALOG = __CATALOG__;
const CELLS = __CELLS__;
const SET_SIZE = CATALOG.setSize;
const ITEMS = CATALOG.items;
const SETS = CATALOG.sets;

const selection = {};   // cellId -> item name ("" = none)

function allItemNames() { return Object.keys(ITEMS).sort(); }

// ------------------------------------------------------------------ //
// Searchable combobox
// ------------------------------------------------------------------ //
function buildCombobox(cell, allNames) {
  const wrap = document.createElement('div');
  wrap.className = 'combo-wrap';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'combo-input';
  input.autocomplete = 'off';
  input.spellcheck = false;

  const drop = document.createElement('div');
  drop.className = 'combo-drop';

  // candidates = [{name, label, hi}]
  const candidates = cell.candidates.map(c => ({
    name: c.name,
    label: c.name + '  (' + Math.round(c.score * 100) + '%)',
    hi: true,
  }));
  const candidateNames = new Set(candidates.map(c => c.name));
  const rest = allNames
    .filter(n => !candidateNames.has(n))
    .map(n => ({ name: n, label: n, hi: false }));
  const allOpts = [
    { name: '', label: '— none / empty —', hi: false },
    ...candidates,
    ...rest,
  ];

  const best = cell.candidates[0] ? cell.candidates[0].name : '';
  input.value = best;
  selection[cell.id] = best;

  let focusIdx = -1;

  function renderDrop(query) {
    const q = query.toLowerCase();
    const filtered = q
      ? allOpts.filter(o => o.label.toLowerCase().includes(q))
      : allOpts;
    drop.innerHTML = '';
    focusIdx = -1;
    if (filtered.length === 0) {
      const d = document.createElement('div');
      d.className = 'combo-opt none'; d.textContent = 'No matches';
      drop.appendChild(d);
      return;
    }
    filtered.slice(0, 80).forEach((opt, i) => {
      const d = document.createElement('div');
      d.className = 'combo-opt' + (opt.hi ? ' hi' : '');
      d.textContent = opt.label;
      d.addEventListener('mousedown', e => {
        e.preventDefault();
        commit(opt.name);
      });
      drop.appendChild(d);
    });
  }

  function openDrop() {
    renderDrop(input.value);
    drop.style.display = 'block';
  }
  function closeDrop() { drop.style.display = 'none'; }

  function commit(name) {
    input.value = name;
    selection[cell.id] = name;
    closeDrop();
    recompute();
    // update reference icon
    const refImg = wrap.parentElement.querySelector('img.ref');
    if (refImg) {
      const ic = ITEMS[name] ? ITEMS[name].icon : '';
      refImg.src = ic; refImg.style.display = ic ? '' : 'none';
    }
  }

  input.addEventListener('focus', openDrop);
  input.addEventListener('click', openDrop);
  input.addEventListener('input', () => renderDrop(input.value));
  input.addEventListener('blur', () => setTimeout(closeDrop, 160));

  input.addEventListener('keydown', e => {
    const opts = drop.querySelectorAll('.combo-opt:not(.none)');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      focusIdx = Math.min(focusIdx + 1, opts.length - 1);
      opts.forEach((o, i) => o.classList.toggle('focused', i === focusIdx));
      if (opts[focusIdx]) opts[focusIdx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      focusIdx = Math.max(focusIdx - 1, 0);
      opts.forEach((o, i) => o.classList.toggle('focused', i === focusIdx));
      if (opts[focusIdx]) opts[focusIdx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (focusIdx >= 0 && opts[focusIdx]) {
        // Find opt data by label text
        const label = opts[focusIdx].textContent;
        const found = allOpts.find(o => o.label === label);
        if (found) commit(found.name);
      }
    } else if (e.key === 'Escape') {
      closeDrop();
    }
  });

  wrap.appendChild(input);
  wrap.appendChild(drop);
  return wrap;
}

function buildCells() {
  const root = document.getElementById('cells');
  document.getElementById('cellcount').textContent = '(' + CELLS.length + ')';
  const names = allItemNames();
  CELLS.forEach(cell => {
    const row = document.createElement('div');
    row.className = 'cell';

    // Game screenshot crop
    const cropImg = document.createElement('img');
    cropImg.className = 'crop'; cropImg.src = cell.crop; cropImg.alt = '';
    row.appendChild(cropImg);

    // Wiki reference icon for current selection (updates on change)
    const refImg = document.createElement('img');
    refImg.className = 'ref'; refImg.alt = 'ref';
    const bestName = cell.candidates[0] ? cell.candidates[0].name : '';
    const bestIcon = bestName && ITEMS[bestName] ? ITEMS[bestName].icon : '';
    refImg.src = bestIcon;
    refImg.style.display = bestIcon ? '' : 'none';
    refImg.title = 'Wiki icon for selected item';
    row.appendChild(refImg);

    row.appendChild(buildCombobox(cell, names));
    root.appendChild(row);
  });
}

function ownedItems() {
  const set = new Set();
  Object.values(selection).forEach(n => { if (n) set.add(n); });
  return [...set];
}

function iconFor(name) {
  const it = ITEMS[name];
  return it && it.icon ? it.icon : '';
}

function solve() {
  const owned = ownedItems();
  const bySet = {};   // set -> [itemName]
  owned.forEach(name => {
    const it = ITEMS[name];
    if (!it) return;
    (it.sets || []).forEach(s => {
      (bySet[s] = bySet[s] || []).push(name);
    });
  });

  const results = [];
  for (const [setName, members] of Object.entries(bySet)) {
    const uniq = [...new Set(members)];
    const slots = [...new Set(uniq.map(n => ITEMS[n].slot).filter(Boolean))];
    const achievable = slots.length >= SET_SIZE;
    // one wearable example: first item per distinct slot
    const combo = []; const used = new Set();
    for (const n of uniq) {
      const s = ITEMS[n].slot;
      if (s && !used.has(s)) { used.add(s); combo.push(n); }
      if (combo.length === SET_SIZE) break;
    }
    const info = SETS[setName] || {};
    results.push({
      name: setName, bonus: info.bonus || '', pieces: info.pieces || 0,
      owned: uniq, slots, achievable,
      combo: achievable ? combo : []
    });
  }
  results.sort((a,b) =>
     (a.achievable===b.achievable ? b.owned.length-a.owned.length
        : (a.achievable? -1:1)) || a.name.localeCompare(b.name));
  return results;
}

function pieceChip(name) {
  const slot = ITEMS[name] ? ITEMS[name].slot : '';
  const ic = iconFor(name);
  return '<span class="piece">' + (ic? '<img src="'+ic+'">':'') +
         name + ' <span class="slot">'+slot+'</span></span>';
}

function recompute() {
  const showNear = document.getElementById('showNear').checked;
  const results = solve();
  const root = document.getElementById('sets');
  root.innerHTML = '';
  let shown = 0;
  results.forEach(r => {
    if (!r.achievable && !showNear) return;
    if (!r.achievable && r.owned.length < 2) return;
    shown++;
    const div = document.createElement('div');
    div.className = 'set ' + (r.achievable ? 'ok' : 'near');
    const badge = r.achievable
      ? '<span class="badge ok">COMPLETE</span>'
      : '<span class="badge near">'+r.owned.length+'/'+SET_SIZE+'</span>';
    const piecesSrc = r.achievable ? r.combo : r.owned;
    let note = '';
    if (!r.achievable) {
      if (r.owned.length < SET_SIZE)
        note = 'Need '+(SET_SIZE-r.owned.length)+' more '+r.name+' item(s).';
      else
        note = r.owned.length+' owned but only '+r.slots.length+
               ' distinct slot(s) ('+r.slots.join(', ')+'); '+
               'need '+SET_SIZE+' different slots.';
    }
    div.innerHTML =
      '<h3>'+badge+' '+r.name+(r.pieces? ' <span class="sub">('+r.pieces+'-piece set)</span>':'')+'</h3>' +
      (r.bonus? '<div class="bonus">'+r.bonus+'</div>':'') +
      '<div class="pieces">'+piecesSrc.map(pieceChip).join('')+'</div>' +
      (note? '<div class="note">'+note+'</div>':'');
    root.appendChild(div);
  });
  if (shown === 0)
    root.innerHTML = '<div class="empty-msg">No achievable sets yet. '+
      'Confirm more items on the left.</div>';
}

document.getElementById('showNear').addEventListener('change', recompute);
buildCells();
recompute();
</script>
</body>
</html>
"""
