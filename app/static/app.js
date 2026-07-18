/* Mewgenics Set Checker — front-end.
 * Talks to /api/*, renders the annotated grid, lets the user correct guesses,
 * and recomputes achievable set bonuses live (mirror of app/solver.py). */
"use strict";

const $ = (id) => document.getElementById(id);
const CONF_SURE = 0.45;

let RULES = { set_size: 3, slots: ["Head", "Face", "Neck", "Trinket", "Weapon"] };
let catalog = null;           // {items:[], sets:{}, byName:Map}
let shots = [];               // {name, file, data, sel:[{name,sure,corrected,empty}]}
let activeShot = 0;
let manualItems = [];         // item names added by hand
let adjust = { step: 0, p0: null };
let pickerCtx = null;         // {shot, cell} | {manual:true}
let pollTimer = null;

/* ------------------------------------------------------------------ health */
async function refreshHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    RULES = h.rules || RULES;
    const pill = $("healthPill");
    const b = h.bootstrap || {};
    const banner = $("bootBanner");
    if (h.matcher.ready) {
      pill.textContent = `catalog: ${h.catalog.items} items · ${h.catalog.sets} sets`;
      pill.className = "pill ok";
    } else {
      pill.textContent = "catalog not ready";
      pill.className = "pill";
    }
    if (b.status === "running") {
      banner.className = "banner";
      banner.textContent = `⏳ Building the item catalog from the wiki — ${b.detail || "working"} (first boot takes a minute or two)`;
      schedulePoll(3000);
    } else if (b.status === "failed") {
      banner.className = "banner bad";
      banner.textContent = `⚠ Catalog bootstrap failed: ${b.detail}. Retry with the ↻ Catalog button, or build a bundle on your PC (python -m app.bootstrap --bundle data.zip) and use ⇪ Import.`;
    } else if (!h.catalog.loaded) {
      banner.className = "banner";
      banner.textContent = "No catalog yet — press ↻ Catalog to fetch it from the wiki.";
    } else {
      banner.className = "banner hidden";
      if (!catalog) loadCatalog();
    }
    if (h.catalog.loaded && !catalog) loadCatalog();
  } catch {
    $("healthPill").textContent = "server unreachable";
    $("healthPill").className = "pill bad";
    schedulePoll(5000);
  }
}
function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refreshHealth, ms);
}

async function loadCatalog() {
  const c = await (await fetch("/api/catalog")).json();
  if (!c.items.length) return;
  c.byName = new Map(c.items.map((it) => [it.name.toLowerCase(), it]));
  catalog = c;
  renderAll();
}

/* ------------------------------------------------------------------ upload */
function bindUpload() {
  const dz = $("dropzone");
  dz.addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", (e) => {
    if (e.target.files[0]) addShot(e.target.files[0]);
    e.target.value = "";
  });
  ["dragover", "dragenter"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) addShot(f);
  });
  document.addEventListener("paste", (e) => {
    for (const item of e.clipboardData.items) {
      if (item.type.startsWith("image/")) { addShot(item.getAsFile()); break; }
    }
  });
  $("btnAddShot").addEventListener("click", () => $("fileInput").click());
}

async function addShot(file) {
  const shot = { name: `Shot ${shots.length + 1}`, file, data: null, sel: [], imgUrl: URL.createObjectURL(file) };
  shots.push(shot);
  activeShot = shots.length - 1;
  await analyzeShot(activeShot, null);
}

async function analyzeShot(idx, corners) {
  const shot = shots[idx];
  showSpinner(`Analyzing ${shot.name}… (10–40 s)`);
  const fd = new FormData();
  fd.append("file", shot.file);
  if (corners) {
    fd.append("grid_x0", corners.x0); fd.append("grid_y0", corners.y0);
    fd.append("grid_x1", corners.x1); fd.append("grid_y1", corners.y1);
    fd.append("rows", corners.rows); fd.append("cols", corners.cols);
  }
  try {
    const resp = await fetch("/api/analyze", { method: "POST", body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    shot.data = await resp.json();
    shot.sel = shot.data.cells.map((c) => {
      if (c.empty || !c.candidates.length) return { name: null, sure: true, corrected: false, empty: true };
      const top = c.candidates[0];
      return { name: top.name, sure: top.confidence >= CONF_SURE, corrected: false, empty: false };
    });
  } catch (err) {
    alert(`Analyze failed: ${err.message}`);
    if (!shot.data) { shots.splice(idx, 1); activeShot = Math.max(0, shots.length - 1); }
  }
  hideSpinner();
  renderAll();
}

/* ------------------------------------------------------------------ canvas */
const canvas = $("canvas");
const ctx = canvas.getContext("2d");
const imgCache = new Map();

function drawShot() {
  const shot = shots[activeShot];
  if (!shot || !shot.data) return;
  let img = imgCache.get(shot.imgUrl);
  if (!img) {
    img = new Image();
    img.onload = () => drawShot();
    img.src = shot.imgUrl;
    imgCache.set(shot.imgUrl, img);
    return;
  }
  if (!img.complete) return;
  canvas.width = shot.data.image.width;
  canvas.height = shot.data.image.height;
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const lw = Math.max(2, Math.round(canvas.width / 700));
  shot.data.cells.forEach((c, i) => {
    const s = shot.sel[i];
    const [x0, y0, x1, y1] = c.box;
    let color = "#b0413e";
    let dash = [];
    if (s.empty && !s.corrected) { color = "rgba(200,200,200,.55)"; dash = [4, 4]; }
    else if (s.corrected) color = "#4a6fa5";
    else if (s.sure) color = "#2e8b57";
    else { color = "#d99a06"; }
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    if (!s.empty && !s.sure && !s.corrected) {
      ctx.fillStyle = "rgba(217,154,6,.18)";
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    }
  });
  ctx.setLineDash([]);
}

function canvasPoint(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - r.left) * (canvas.width / r.width),
    y: (e.clientY - r.top) * (canvas.height / r.height),
  };
}

function cellAt(shot, x, y) {
  if (!shot.data) return -1;
  return shot.data.cells.findIndex((c) => {
    const [x0, y0, x1, y1] = c.box;
    return x >= x0 && x < x1 && y >= y0 && y < y1;
  });
}

canvas.addEventListener("click", (e) => {
  const shot = shots[activeShot];
  if (!shot) return;
  const p = canvasPoint(e);
  if (adjust.step === 1) {
    adjust.p0 = p; adjust.step = 2;
    setGridHint("Now click the BOTTOM-RIGHT corner of the LAST storage tile.");
    return;
  }
  if (adjust.step === 2) {
    finishAdjust(p);
    return;
  }
  const i = cellAt(shot, p.x, p.y);
  if (i >= 0) openPicker({ shot: activeShot, cell: i });
});

/* ------------------------------------------------------------- adjust grid */
$("btnAdjust").addEventListener("click", () => {
  if (!shots[activeShot]) return;
  adjust = { step: 1, p0: null };
  setGridHint("Click the TOP-LEFT corner of the FIRST storage tile.");
});

function setGridHint(text) {
  const el = $("gridHint");
  if (!text) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  el.textContent = text;
}

function finishAdjust(p1) {
  const g = shots[activeShot].data?.grid;
  const el = $("gridHint");
  el.classList.remove("hidden");
  el.style.pointerEvents = "auto";
  el.innerHTML = `rows <input id="adjRows" type="number" min="1" max="20" value="${g ? g.rows : 10}" style="width:56px">
    cols <input id="adjCols" type="number" min="1" max="20" value="${g ? g.cols : 11}" style="width:56px">
    <button id="adjGo">Re-analyze</button> <button id="adjCancel">Cancel</button>`;
  $("adjCancel").onclick = () => { adjust = { step: 0, p0: null }; el.style.pointerEvents = "none"; setGridHint(null); };
  $("adjGo").onclick = () => {
    const corners = {
      x0: Math.min(adjust.p0.x, p1.x), y0: Math.min(adjust.p0.y, p1.y),
      x1: Math.max(adjust.p0.x, p1.x), y1: Math.max(adjust.p0.y, p1.y),
      rows: parseInt($("adjRows").value, 10) || 10,
      cols: parseInt($("adjCols").value, 10) || 11,
    };
    adjust = { step: 0, p0: null };
    el.style.pointerEvents = "none";
    setGridHint(null);
    analyzeShot(activeShot, corners);
  };
}

/* ------------------------------------------------------------------ picker */
function openPicker(ctxObj) {
  if (!catalog) { alert("Catalog not loaded yet."); return; }
  pickerCtx = ctxObj;
  const crop = $("pickerCrop");
  if (ctxObj.manual) {
    crop.classList.add("hidden");
  } else {
    const cell = shots[ctxObj.shot].data.cells[ctxObj.cell];
    crop.classList.remove("hidden");
    crop.src = cell.crop ? `data:image/png;base64,${cell.crop}` : "";
  }
  $("picker").classList.remove("hidden");
  $("pickerSearch").value = "";
  renderPickerList("");
  setTimeout(() => $("pickerSearch").focus(), 30);
}

function closePicker() { $("picker").classList.add("hidden"); pickerCtx = null; }
$("pickerClose").addEventListener("click", closePicker);
$("picker").addEventListener("click", (e) => { if (e.target === $("picker")) closePicker(); });
$("pickerEmpty").addEventListener("click", () => {
  if (pickerCtx && !pickerCtx.manual) {
    const s = shots[pickerCtx.shot].sel[pickerCtx.cell];
    s.name = null; s.empty = true; s.corrected = true; s.sure = true;
  }
  closePicker();
  renderAll();
});
$("pickerSearch").addEventListener("input", (e) => renderPickerList(e.target.value));
$("pickerSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const first = document.querySelector("#pickerList .pick-row");
    if (first) first.click();
  } else if (e.key === "Escape") closePicker();
});

function renderPickerList(q) {
  const list = $("pickerList");
  list.innerHTML = "";
  if (!catalog) return;
  const ql = q.trim().toLowerCase();
  let candidates = [];
  if (pickerCtx && !pickerCtx.manual && !ql) {
    const cell = shots[pickerCtx.shot].data.cells[pickerCtx.cell];
    candidates = cell.candidates.map((c) => ({ ...catalog.byName.get(c.name.toLowerCase()), _score: c.score }));
  }
  const rest = catalog.items
    .filter((it) => !ql || it.name.toLowerCase().includes(ql))
    .filter((it) => !candidates.some((c) => c && c.name === it.name))
    .slice(0, ql ? 60 : 40);
  const rows = [...candidates.filter(Boolean), ...rest];
  for (const it of rows) {
    const div = document.createElement("div");
    div.className = "pick-row";
    div.innerHTML = `${it.icon ? `<img src="${it.icon}" alt="">` : "<span style='width:30px'></span>"}
      <div><div>${esc(it.name)}</div>
      <div class="meta">${esc(it.slot || "?")}${it.sets?.length ? " · " + it.sets.map(esc).join(", ") : ""}</div></div>
      ${it._score !== undefined ? `<span class="score">match ${(it._score * 100 | 0)}%</span>` : ""}`;
    div.addEventListener("click", () => pickItem(it.name));
    list.appendChild(div);
  }
  if (!rows.length) list.innerHTML = "<div class='dim' style='padding:10px'>No items match.</div>";
}

function pickItem(name) {
  if (pickerCtx.manual) {
    if (!manualItems.includes(name)) manualItems.push(name);
  } else {
    const s = shots[pickerCtx.shot].sel[pickerCtx.cell];
    s.name = name; s.empty = false; s.corrected = true; s.sure = true;
  }
  closePicker();
  renderAll();
}

/* ---------------------------------------------------------------- solver */
function inventory() {
  const useUnsure = $("chkUnsure").checked;
  const inv = new Map();   // lower name -> {name, sure}
  for (const shot of shots) {
    (shot.sel || []).forEach((s) => {
      if (!s.name) return;
      if (!useUnsure && !s.sure) return;
      const key = s.name.toLowerCase();
      const cur = inv.get(key);
      if (!cur) inv.set(key, { name: s.name, sure: s.sure });
      else cur.sure = cur.sure || s.sure;
    });
  }
  for (const n of manualItems) inv.set(n.toLowerCase(), { name: n, sure: true });
  return inv;
}

function solveSets(inv) {
  const out = [];
  for (const [setName, info] of Object.entries(catalog.sets)) {
    const owned = [];
    for (const m of info.members) {
      const e = inv.get(m.toLowerCase());
      if (!e) continue;
      const it = catalog.byName.get(m.toLowerCase());
      owned.push({ name: m, slot: it ? it.slot : "", sure: e.sure, icon: it ? it.icon : "" });
    }
    if (!owned.length) continue;
    const slots = [...new Set(owned.map((o) => o.slot).filter(Boolean))];
    const wearable = slots.length >= RULES.set_size;
    const combo = [];
    if (wearable) {
      const used = new Set();
      for (const o of owned) {
        if (o.slot && !used.has(o.slot)) { combo.push(o); used.add(o.slot); }
        if (combo.length === RULES.set_size) break;
      }
    }
    const missing = info.members
      .filter((m) => !inv.get(m.toLowerCase()))
      .map((m) => {
        const it = catalog.byName.get(m.toLowerCase());
        return { name: m, slot: it ? it.slot : "", icon: it ? it.icon : "", helps: !it || !it.slot || !slots.includes(it.slot) };
      });
    let status = "started";
    if (wearable) status = "wearable";
    else if (slots.length === RULES.set_size - 1) status = "close";
    else if (owned.length >= RULES.set_size) status = "blocked";
    out.push({ name: setName, bonus: info.bonus, owned, slots, wearable, combo, missing, status });
  }
  const rank = { wearable: 0, close: 1, started: 2, blocked: 2 };
  out.sort((a, b) => rank[a.status] - rank[b.status] || b.owned.length - a.owned.length || a.name.localeCompare(b.name));
  return out;
}

/* ---------------------------------------------------------------- render */
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function chip(o, cls) {
  return `<span class="chip ${cls}">${o.icon ? `<img src="${o.icon}" alt="">` : ""}${esc(o.name)}<span class="slot">${esc(o.slot || "?")}</span></span>`;
}

function renderSets() {
  const listEl = $("setsList");
  const sumEl = $("setsSummary");
  if (!catalog || !shots.some((s) => s.data)) {
    listEl.innerHTML = "";
    return;
  }
  const inv = inventory();
  const results = solveSets(inv);
  const wearable = results.filter((r) => r.status === "wearable");
  const close = results.filter((r) => r.status === "close");
  const rest = results.filter((r) => r.status !== "wearable" && r.status !== "close");

  sumEl.innerHTML = `<b>${wearable.length}</b> set bonus${wearable.length === 1 ? "" : "es"} wearable right now · ` +
    `<b>${close.length}</b> one slot away · ${inv.size} distinct items counted`;

  const card = (r) => {
    const statusTxt = {
      wearable: `✔ wearable (${r.slots.length} slots)`,
      close: `needs 1 more slot`,
      blocked: `${r.owned.length} pieces, only ${r.slots.length} slot${r.slots.length === 1 ? "" : "s"}`,
      started: `${r.slots.length}/${RULES.set_size} slots`,
    }[r.status];
    const ownedChips = r.owned.map((o) => chip(o, "owned" + (o.sure ? "" : " unsure"))).join("");
    const missingChips = r.missing.map((mi) => chip(mi, "missing")).join("");
    let extra = "";
    if (r.status === "wearable") {
      extra = `<div class="dim">wear: ${r.combo.map((o) => `${esc(o.name)} (${esc(o.slot)})`).join(" + ")}</div>`;
    } else if (r.status === "close") {
      const helpers = r.missing.filter((mi) => mi.helps).map((mi) => esc(mi.name));
      if (helpers.length) extra = `<div class="dim">completes with: ${helpers.join(", ")}</div>`;
    }
    return `<div class="set-card ${r.status}">
      <div class="set-head"><span class="set-name">${esc(r.name)}</span>
      <span class="set-status ${r.status}">${statusTxt}</span></div>
      ${r.bonus ? `<div class="set-bonus">${esc(r.bonus)}</div>` : ""}
      <div class="chips">${ownedChips}${missingChips}</div>
      ${extra}</div>`;
  };

  let html = "";
  if (wearable.length) html += `<div class="group-title">Wearable now</div>` + wearable.map(card).join("");
  if (close.length) html += `<div class="group-title">One slot away</div>` + close.map(card).join("");
  if (rest.length) html += `<div class="group-title">Progress</div>` + rest.map(card).join("");
  if (!html) html = "<div class='dim'>No set pieces recognized yet.</div>";
  listEl.innerHTML = html;
  $("btnCopy").classList.toggle("hidden", !results.length);
  $("btnCopy").onclick = () => copySummary(wearable, close);
}

function copySummary(wearable, close) {
  const lines = ["Mewgenics set bonuses I can make:"];
  for (const r of wearable) {
    lines.push(`✔ ${r.name} — ${r.combo.map((o) => `${o.name} (${o.slot})`).join(" + ")}${r.bonus ? ` — ${r.bonus}` : ""}`);
  }
  if (close.length) {
    lines.push("", "One slot away:");
    for (const r of close) {
      const helpers = r.missing.filter((m) => m.helps).map((m) => m.name).slice(0, 6);
      lines.push(`· ${r.name} (have ${r.owned.map((o) => o.name).join(", ")}; need ${helpers.join(" or ") || "another slot"})`);
    }
  }
  navigator.clipboard.writeText(lines.join("\n"));
  $("btnCopy").textContent = "✓ Copied";
  setTimeout(() => ($("btnCopy").textContent = "⧉ Copy results"), 1500);
}

function renderItems() {
  const el = $("itemsList");
  const shot = shots[activeShot];
  if (!shot || !shot.data) { el.innerHTML = "<div class='dim'>No screenshot analyzed yet.</div>"; return; }
  el.innerHTML = "";
  shot.data.cells.forEach((c, i) => {
    const s = shot.sel[i];
    if (s.empty && !s.name) return;
    const it = s.name && catalog ? catalog.byName.get(s.name.toLowerCase()) : null;
    const conf = c.candidates.length ? c.candidates[0].confidence : 0;
    const confCls = s.corrected ? "hi" : conf >= 0.6 ? "hi" : conf >= CONF_SURE ? "mid" : "lo";
    const confTxt = s.corrected ? "fixed" : `${Math.round(conf * 100)}%`;
    const div = document.createElement("div");
    div.className = "item-row";
    div.innerHTML = `<img class="crop" src="data:image/png;base64,${c.crop}" alt="">
      ${it && it.icon ? `<img src="${it.icon}" width="26" height="26" alt="">` : ""}
      <div><div>${esc(s.name || "— unknown —")}</div>
      <div class="meta">r${c.row + 1} c${c.col + 1}${it ? ` · ${esc(it.slot)}${it.sets.length ? " · " + it.sets.map(esc).join(", ") : ""}` : ""}</div></div>
      <span class="conf ${confCls}">${confTxt}</span>`;
    div.addEventListener("click", () => openPicker({ shot: activeShot, cell: i }));
    el.appendChild(div);
  });
}

function renderShotTabs() {
  const el = $("shotTabs");
  if (!shots.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.innerHTML = "";
  shots.forEach((s, i) => {
    const b = document.createElement("button");
    b.className = "shot-tab" + (i === activeShot ? " active" : "");
    b.textContent = s.name;
    b.addEventListener("click", () => { activeShot = i; renderAll(); });
    const x = document.createElement("span");
    x.textContent = " ✕";
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      shots.splice(i, 1);
      activeShot = Math.max(0, activeShot - (i <= activeShot ? 1 : 0));
      renderAll();
    });
    b.appendChild(x);
    el.appendChild(b);
  });
}

function renderManual() {
  const el = $("manualChips");
  el.innerHTML = "";
  manualItems.forEach((n, i) => {
    const it = catalog ? catalog.byName.get(n.toLowerCase()) : null;
    const span = document.createElement("span");
    span.className = "chip owned";
    span.innerHTML = `${it && it.icon ? `<img src="${it.icon}">` : ""}${esc(n)} <b style="cursor:pointer">✕</b>`;
    span.querySelector("b").addEventListener("click", () => { manualItems.splice(i, 1); renderAll(); });
    el.appendChild(span);
  });
}

function unsureCells() {
  const shot = shots[activeShot];
  if (!shot || !shot.sel) return [];
  return shot.sel.map((s, i) => ({ s, i })).filter(({ s }) => s.name && !s.sure && !s.corrected);
}

function renderAll() {
  const has = shots.some((s) => s.data);
  $("canvasWrap").classList.toggle("hidden", !has);
  $("canvasBar").classList.toggle("hidden", !has);
  $("dropzone").style.display = has ? "none" : "";
  renderShotTabs();
  drawShot();
  renderSets();
  renderItems();
  renderManual();
  const u = unsureCells();
  $("unsureCount").textContent = u.length ? `(${u.length})` : "";
  $("btnReviewUnsure").style.display = u.length ? "" : "none";
  const shot = shots[activeShot];
  if (shot && shot.data) {
    $("analyzeInfo").textContent =
      `grid ${shot.data.grid.rows}×${shot.data.grid.cols} · ${shot.data.matcher_refs} refs · ${shot.data.elapsed_sec}s`;
  }
}

/* ------------------------------------------------------------------ misc */
function showSpinner(text) { $("spinnerText").textContent = text; $("spinner").classList.remove("hidden"); }
function hideSpinner() { $("spinner").classList.add("hidden"); }

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("paneSets").classList.toggle("hidden", t.dataset.tab !== "sets");
    $("paneItems").classList.toggle("hidden", t.dataset.tab !== "items");
  }));

$("chkUnsure").addEventListener("change", renderAll);
$("btnAddManual").addEventListener("click", () => openPicker({ manual: true }));
$("btnReviewUnsure").addEventListener("click", () => {
  const u = unsureCells();
  if (u.length) openPicker({ shot: activeShot, cell: u[0].i });
});
$("btnRescan").addEventListener("click", async () => {
  await fetch("/api/bootstrap", { method: "POST" });
  refreshHealth();
});
$("bundleInput").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  showSpinner("Importing bundle…");
  try {
    const resp = await fetch("/api/import-data", { method: "POST", body: fd });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.detail || "import failed");
    alert(`Imported: ${j.imported.items} items, ${j.imported.icons} icons.`);
    catalog = null;
    refreshHealth();
  } catch (err) {
    alert(`Import failed: ${err.message}`);
  }
  hideSpinner();
  e.target.value = "";
});

bindUpload();
refreshHealth();
