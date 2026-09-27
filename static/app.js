"use strict";
/* printer-dash frontend. Live state arrives over SSE from /api/events/stream. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const store = {
  get(k, d) { try { const v = localStorage.getItem("pd." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pd." + k, JSON.stringify(v)); } catch {} },
};

let S = { online: false, detail: {}, product: {} };
let settings = { presets: [], notify: { targets: [] }, palette: {}, notifyEvents: [] };
let lastStatus = null;
const renderErrors = new Set();

// ------------------------------------------------------------ helpers
function fmtDur(sec) {
  if (sec == null || isNaN(sec) || sec < 0) return "–";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}
const fmtTime = ts => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const fmtClock = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const t1 = v => v == null ? "–" : `${Math.round(v * 10) / 10}°`;

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 8000 : 4000);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) {
    if (opts.body instanceof FormData) init.body = opts.body;
    else { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(path, init);
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok || data.ok === false) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function act(label, path, body) {
  try { await api(path, { body: body ?? {} }); toast(label, "ok"); return true; }
  catch (e) { toast(`${label} failed: ${e.message}`, "err"); return false; }
}

function confirmDlg(title, text) {
  const dlg = $("#confirm-dlg");
  $("#cf-title").textContent = title; $("#cf-text").textContent = text;
  dlg.showModal();
  return new Promise(res => dlg.addEventListener("close", () => res(dlg.returnValue === "ok"), { once: true }));
}

// ------------------------------------------------------------ cards (collapsible)
function initCards() {
  const closed = store.get("closed", null);
  $$(".card").forEach(c => {
    const key = c.dataset.card;
    const isClosed = closed ? closed.includes(key) : c.dataset.default === "closed";
    c.classList.toggle("closed", isClosed);
    $(".card-h", c).addEventListener("click", e => {
      if (e.target.closest("button,input,select,a")) return;
      c.classList.toggle("closed");
      saveCards();
      if (!c.classList.contains("closed")) onCardOpen(key);
    });
  });
  $("#btn-layout").onclick = () => {
    const anyOpen = $$(".card").some(c => !c.classList.contains("closed"));
    $$(".card").forEach(c => c.classList.toggle("closed", anyOpen));
    saveCards();
    if (!anyOpen) $$(".card").forEach(c => onCardOpen(c.dataset.card));
  };
}
function saveCards() { store.set("closed", $$(".card.closed").map(c => c.dataset.card)); }
const isOpen = key => !$(`.card[data-card="${key}"]`).classList.contains("closed");
function onCardOpen(key) {
  if (key === "chart") loadHistory();
  if (key === "files") loadFiles();
  if (key === "history") loadJobs();
  if (key === "console") loadEvents();
  if (key === "camera") startCam();
  if (key === "chart" || key === "history") setTimeout(resizeCharts, 50);
}

// ------------------------------------------------------------ theme
function initTheme() {
  const t = store.get("theme", "dark");
  document.documentElement.dataset.theme = t;
  $("#btn-theme").onclick = () => {
    const n = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = n; store.set("theme", n);
    buildTempChart(); buildUtilChart();
  };
}

// ------------------------------------------------------------ live state
function connect() {
  const es = new EventSource("/api/events/stream");
  es.onmessage = e => { S = JSON.parse(e.data); render(); };
  es.onerror = () => { $("#conn-dot").classList.remove("on"); };
}

function render() {
  const d = S.detail || {};
  const status = d.status || "unknown";
  $("#conn-dot").classList.toggle("on", S.online);
  $("#conn-dot").title = S.online ? "Printer online" : "Printer not responding";
  $("#offline-banner").hidden = S.online || !S.lastOk;
  $("#printer-name").textContent = d.name || "Creator 5 Pro";
  const chip = $("#status-chip");
  chip.textContent = status; chip.className = "chip " + status;
  document.title = status === "printing" ? `${Math.round((d.printProgress || 0) * 100)}% · ${d.name}` : d.name || "Creator 5 Pro";

  const printing = status === "printing", paused = ["pause", "paused", "pausing"].includes(status);
  $$("[data-job=pause]").forEach(b => b.disabled = !printing);
  $$("[data-job=resume]").forEach(b => b.disabled = !paused);
  $$("[data-job=cancel]").forEach(b => b.disabled = !(printing || paused));
  $("#btn-light").classList.toggle("active", d.lightStatus === "open");

  const parts = [["job", () => renderJob(d, printing || paused)], ["temps", () => renderTemps(d)], ["lanes", () => renderLanes(d)],
                 ["tune", () => renderTune()], ["sensors", () => renderSensors(d)], ["machine", () => renderMachine(d)],
                 ["notify", () => notifyBrowser(status, d)]];
  for (const [name, fn] of parts) {
    try { fn(); } catch (e) { if (!renderErrors.has(name)) { renderErrors.add(name); console.error(name, e); toast(`Display error in ${name}: ${e.message}`, "err"); } }
  }
}

function renderJob(d, active) {
  const pct = Math.round((d.printProgress || 0) * 1000) / 10;
  $("#job-file").textContent = d.printFileName || "No active job";
  $("#job-bar").style.width = (active || pct ? pct : 0) + "%";
  $("#job-pct").textContent = pct + "%";
  $("#job-layer").textContent = d.targetPrintLayer ? `${d.printLayer} / ${d.targetPrintLayer}` : "–";
  $("#job-elapsed").textContent = fmtDur(d.printDuration);
  $("#job-remaining").textContent = active ? fmtDur(d.estimatedTime) : "–";
  $("#job-eta").textContent = active && d.estimatedTime ? fmtClock(Date.now() / 1000 + d.estimatedTime) : "–";
  $("#job-speed").textContent = d.printSpeedAdjust != null ? d.printSpeedAdjust + "%" : "–";
  $("#job-z").textContent = d.zAxisCompensation != null ? Number(d.zAxisCompensation).toFixed(3) : "–";
  const want = d.printFileName ? "/api/thumb/current?f=" + encodeURIComponent(d.printFileName) : "";
  const img = $("#job-thumb");
  if (img.dataset.src !== want) { img.dataset.src = want; if (want) img.src = want; else img.removeAttribute("src"); }
}

// ------------------------------------------------------------ temperatures
const heaterRows = () => {
  const d = S.detail || {};
  const n = d.nozzleTemps || [], nt = d.nozzleTargetTemps || [];
  const slots = (d.matlStationInfo || {}).slotInfos || [];
  const rows = [];
  for (let i = 0; i < (d.nozzleCnt || 4); i++) {
    const slot = slots[i] || {};
    rows.push({ key: "T" + i, heater: "tool", index: i, actual: n[i], target: nt[i], color: slot.materialColor, sub: slot.materialName });
  }
  rows.push({ key: "Bed", heater: "bed", actual: d.platTemp, target: d.platTargetTemp });
  if (d.chamberTemp != null && d.chamberTemp > -100) rows.push({ key: "Chamber", heater: "chamber", actual: d.chamberTemp, target: d.chamberTargetTemp });
  return rows;
};

function renderTemps() {
  const tb = $("#temp-rows");
  const rows = heaterRows();
  if (tb.children.length !== rows.length) {
    tb.innerHTML = rows.map(r => `<tr data-key="${r.key}">
      <td><span class="swatch"></span>${r.key}<div class="sub"></div></td>
      <td class="a"></td><td class="t"></td>
      <td><input type="number" min="0" max="300" step="1" placeholder="°C"></td>
      <td><button class="btn ghost off" title="Turn off">Off</button></td></tr>`).join("");
    rows.forEach(r => {
      const tr = $(`tr[data-key="${r.key}"]`, tb), inp = $("input", tr);
      const send = v => act(`${r.key} → ${v ? v + "°C" : "off"}`, "/api/temp", { heater: r.heater, index: r.index, value: v });
      inp.addEventListener("keydown", e => { if (e.key === "Enter" && inp.value !== "") { send(Number(inp.value)); inp.value = ""; inp.blur(); } });
      $(".off", tr).onclick = () => send(0);
    });
  }
  rows.forEach(r => {
    const tr = $(`tr[data-key="${r.key}"]`, tb);
    $(".swatch", tr).style.background = r.color || "transparent";
    $(".swatch", tr).style.display = r.color ? "" : "none";
    $(".sub", tr).textContent = r.sub || "";
    const a = $(".a", tr);
    a.textContent = t1(r.actual);
    a.className = "a " + (r.target > 0 ? (Math.abs(r.actual - r.target) <= 3 ? "at-temp" : "heating") : "");
    $(".t", tr).textContent = r.target > 0 ? t1(r.target) : "off";
  });
}

function renderPresets() {
  $("#preset-bar").innerHTML = settings.presets.map((p, i) =>
    `<button class="btn" data-preset="${i}" title="Tool ${p.tool}° · Bed ${p.bed}° · Chamber ${p.chamber || 0}°">${esc(p.name)}</button>`).join("")
    + `<button class="btn ghost" data-preset="cooldown">Cooldown</button>`;
  $$("#preset-bar [data-preset]").forEach(b => b.onclick = async () => {
    const d = S.detail || {};
    const active = (d.nozzleTargetTemps || []).map((t, i) => t > 0 ? i : -1).filter(i => i >= 0);
    const tools = active.length ? active : [((d.matlStationInfo || {}).currentSlot || 1) - 1];
    if (b.dataset.preset === "cooldown") {
      if (!await confirmDlg("Cooldown", "Turn off all tool, bed and chamber heaters?")) return;
      for (let i = 0; i < 4; i++) await api("/api/temp", { body: { heater: "tool", index: i, value: 0 } }).catch(() => {});
      await act("Cooldown", "/api/temp", { heater: "bed", value: 0 });
      if (S.product.chamberTempCtrlState) await api("/api/temp", { body: { heater: "chamber", value: 0 } }).catch(() => {});
      return;
    }
    const p = settings.presets[+b.dataset.preset];
    for (const i of tools) await act(`T${i} → ${p.tool}°C`, "/api/temp", { heater: "tool", index: i, value: p.tool });
    await act(`Bed → ${p.bed}°C`, "/api/temp", { heater: "bed", value: p.bed });
    if (p.chamber && S.product.chamberTempCtrlState) await act(`Chamber → ${p.chamber}°C`, "/api/temp", { heater: "chamber", value: p.chamber });
  });
}

// ------------------------------------------------------------ charts
let tempChart = null, utilChart = null, histData = null, utilData = null;
const TOOL_COLORS = ["#38bdf8", "#f472b6", "#a3e635", "#fbbf24"];
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function chartWidth(el) { return Math.max(260, el.clientWidth || el.parentElement.clientWidth - 24); }
function axes() {
  const stroke = cssVar("--muted"), grid = { stroke: cssVar("--line"), width: 1 };
  return { stroke, grid, ticks: grid };
}

function buildTempChart() {
  const el = $("#temp-chart");
  if (tempChart) { tempChart.destroy(); tempChart = null; }
  if (!histData || !window.uPlot) return;
  const showT = $("#show-targets").checked, showIdle = $("#show-idle-tools").checked, showFans = $("#show-fans").checked;
  const h = histData, series = [{}], data = [h.ts];
  const used = i => showIdle || h["tt" + i].some(v => v > 0) || h["t" + i].some(v => v > 45);
  for (let i = 0; i < 4; i++) {
    if (!used(i)) continue;
    series.push({ label: `T${i}`, stroke: TOOL_COLORS[i], width: 2, value: (u, v) => v == null ? "–" : v.toFixed(1) + "°" }); data.push(h["t" + i]);
    if (showT) { series.push({ label: `T${i} target`, stroke: TOOL_COLORS[i], dash: [5, 4], width: 1, value: (u, v) => v == null ? "–" : v + "°" }); data.push(h["tt" + i]); }
  }
  series.push({ label: "Bed", stroke: "#ef4444", width: 2, value: (u, v) => v == null ? "–" : v.toFixed(1) + "°" }); data.push(h.bed);
  if (showT) { series.push({ label: "Bed target", stroke: "#ef4444", dash: [5, 4], width: 1 }); data.push(h.bed_t); }
  if (h.chamber.some(v => v != null)) { series.push({ label: "Chamber", stroke: "#a78bfa", width: 2, value: (u, v) => v == null ? "–" : v.toFixed(1) + "°" }); data.push(h.chamber); }
  if (showFans) {
    series.push({ label: "Cooling fan %", stroke: "#94a3b8", width: 1, scale: "pct" }); data.push(h.cooling_fan);
    series.push({ label: "Chamber fan %", stroke: "#64748b", width: 1, scale: "pct" }); data.push(h.chamber_fan);
    series.push({ label: "Speed %", stroke: "#22c55e", width: 1, scale: "pct" }); data.push(h.speed);
  }
  const ax = axes();
  tempChart = new uPlot({
    width: chartWidth(el), height: 280, series,
    scales: { x: { time: true }, y: { range: (u, min, max) => [0, Math.max(60, Math.ceil((max + 10) / 10) * 10)] }, pct: { range: [0, 300] } },
    axes: [{ ...ax }, { ...ax, label: "°C", size: 48 }, ...(showFans ? [{ ...ax, scale: "pct", side: 1, label: "%", grid: { show: false } }] : [])],
    cursor: { drag: { x: true, y: false } }, legend: { live: true },
  }, data, el);
}

async function loadHistory() {
  if (!isOpen("chart")) return;
  const range = store.get("range", 3600);
  try { histData = await api("/api/history?seconds=" + range); buildTempChart(); } catch (e) { toast("History: " + e.message, "err"); }
}

function buildUtilChart() {
  const el = $("#util-chart");
  if (utilChart) { utilChart.destroy(); utilChart = null; }
  if (!utilData || !window.uPlot || !utilData.days.length) { el.textContent = utilData ? "No data yet" : ""; return; }
  el.textContent = "";
  const days = utilData.days;
  const xs = days.map(d => new Date(d.date + "T12:00:00").getTime() / 1000);
  const bars = uPlot.paths.bars({ size: [0.7, 40] });
  const ax = axes();
  utilChart = new uPlot({
    width: chartWidth(el), height: 160,
    series: [{}, { label: "Printing h", fill: "#0ea5e9", stroke: "#0ea5e9", paths: bars, value: (u, v) => v == null ? "–" : v.toFixed(1) + " h" },
                 { label: "Paused h", fill: "#f59e0b", stroke: "#f59e0b", paths: bars, value: (u, v) => v == null ? "–" : v.toFixed(1) + " h" }],
    scales: { x: { time: true, range: () => { const now = Date.now() / 1000; return [now - 30.5 * 86400, now + 0.5 * 86400]; } }, y: { range: [0, 24] } },
    axes: [{ ...ax }, { ...ax, size: 36 }], legend: { live: true },
  }, [xs, days.map(d => d.printing / 3600), days.map(d => d.paused / 3600)], el);
}

function resizeCharts() {
  if (tempChart) tempChart.setSize({ width: chartWidth($("#temp-chart")), height: 280 });
  if (utilChart) utilChart.setSize({ width: chartWidth($("#util-chart")), height: 160 });
}

function initChartControls() {
  const range = store.get("range", 3600);
  $$("#range-seg button").forEach(b => {
    b.classList.toggle("on", +b.dataset.range === range);
    b.onclick = () => { $$("#range-seg button").forEach(x => x.classList.remove("on")); b.classList.add("on"); store.set("range", +b.dataset.range); loadHistory(); };
  });
  ["show-targets", "show-idle-tools", "show-fans"].forEach(id => {
    $("#" + id).checked = store.get(id, $("#" + id).checked);
    $("#" + id).onchange = () => { store.set(id, $("#" + id).checked); buildTempChart(); };
  });
  window.addEventListener("resize", () => { clearTimeout(window._rz); window._rz = setTimeout(resizeCharts, 150); });
  setInterval(loadHistory, 15000);
}

// ------------------------------------------------------------ lanes / material station
function renderLanes(d) {
  const ms = d.matlStationInfo || {}, slots = ms.slotInfos || [];
  const n = d.nozzleTemps || [], nt = d.nozzleTargetTemps || [];
  const models = (d.nozzleModel || "").split(";");
  const html = [0, 1, 2, 3].map(i => {
    const s = slots[i] || { slotId: i + 1 };
    const cur = ms.currentSlot === s.slotId;
    return `<div class="lane ${cur ? "current" : ""}" data-slot="${s.slotId}" title="${cur ? "Active tool" : ""}">
      <div class="t">T${i}${cur ? " ●" : ""}</div>
      <div class="spool ${s.hasFilament ? "" : "empty"}" style="background:${s.hasFilament ? esc(s.materialColor) : "transparent"}"></div>
      <div class="m">${s.hasFilament ? esc(s.materialName || "?") : "empty"}</div>
      <div class="temp ${nt[i] > 0 ? "heating" : ""}">${t1(n[i])}${nt[i] > 0 ? " / " + t1(nt[i]) : ""}</div>
      <div class="sub">${esc(models[i] || "")}</div></div>`;
  }).join("");
  const el = $("#lanes");
  if (el.dataset.html !== html) {
    el.innerHTML = html; el.dataset.html = html;
    $$(".lane", el).forEach(l => l.onclick = () => openSlotDlg(+l.dataset.slot));
  }
}

function openSlotDlg(slot) {
  const s = ((S.detail.matlStationInfo || {}).slotInfos || []).find(x => x.slotId === slot) || {};
  const dlg = $("#slot-dlg");
  $("#sd-slot").textContent = `T${slot - 1} (slot ${slot})`;
  $("#sd-material").value = s.materialName || "PLA";
  let sel = Object.values(settings.palette).includes(s.materialColor) ? s.materialColor : "#FFFFFF";
  $("#sd-palette").innerHTML = Object.entries(settings.palette).map(([name, hex]) =>
    `<button type="button" title="${name}" data-hex="${hex}" style="background:${hex}" class="${hex === sel ? "sel" : ""}"></button>`).join("");
  $$("#sd-palette button").forEach(b => b.onclick = () => { $$("#sd-palette button").forEach(x => x.classList.remove("sel")); b.classList.add("sel"); sel = b.dataset.hex; });
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue === "ok") act(`Slot ${slot} → ${$("#sd-material").value}`, "/api/slot", { slot, material: $("#sd-material").value, rgb: sel });
  }, { once: true });
}

// ------------------------------------------------------------ tune (staged: nothing is sent until Apply)
const TUNE_KEYS = { speed: "printSpeedAdjust", coolingFan: "coolingFanSpeed", chamberFan: "chamberFanSpeed", zOffset: "zAxisCompensation" };
const tunePending = {};  // key -> staged value (only keys the user has changed)
const liveTune = key => { const v = (S.detail || {})[TUNE_KEYS[key]]; return v == null ? null : Number(v); };
const fmtTune = (key, v) => v == null ? "–" : key === "zOffset" ? Number(v).toFixed(3) + " mm" : Math.round(v) + "%";

function tuneRow(key) { return $(`.tune-row[data-tune="${key}"]`); }
function setPending(key, value) {
  const row = tuneRow(key), min = +row.dataset.min, max = +row.dataset.max;
  value = Math.min(max, Math.max(min, value));
  value = key === "zOffset" ? Math.round(value * 1000) / 1000 : Math.round(value);
  if (value === liveTune(key)) delete tunePending[key]; else tunePending[key] = value;
  renderTuneRows();
}
function renderTuneRows() {
  const printing = (S.detail || {}).status === "printing";
  for (const key of Object.keys(TUNE_KEYS)) {
    const row = tuneRow(key), live = liveTune(key), dirty = key in tunePending;
    const shown = dirty ? tunePending[key] : live;
    row.classList.toggle("dirty", dirty);
    const range = $("input[type=range]", row);
    if (range && document.activeElement !== range && shown != null) range.value = shown;
    $("output", row).innerHTML = `<span class="val">${fmtTune(key, shown)}</span>` + (dirty ? `<span class="now">now ${fmtTune(key, live)}</span>` : "");
    $$("button, input", row).forEach(el => el.disabled = !printing);
  }
  const n = Object.keys(tunePending).length;
  $("#tune-apply").disabled = !printing || !n;
  $("#tune-revert").disabled = !n;
  $("#tune-pending").textContent = n ? `${n} change${n > 1 ? "s" : ""} pending` : "";
  $("#tune-note").textContent = printing ? "" : "(only while printing)";
}
function renderTune() { renderTuneRows(); }
function initTune() {
  $$(".tune-row").forEach(row => {
    const key = row.dataset.tune, range = $("input[type=range]", row);
    if (range) {
      Object.assign(range, { min: row.dataset.min, max: row.dataset.max, step: row.dataset.step });
      range.oninput = () => setPending(key, +range.value);
    }
    $$("[data-d]", row).forEach(b => b.onclick = () => {
      const cur = key in tunePending ? tunePending[key] : (liveTune(key) ?? 0);
      setPending(key, cur + Number(b.dataset.d) * Number(row.dataset.step));
    });
    $$("[data-z]", row).forEach(b => b.onclick = () => {
      const cur = key in tunePending ? tunePending[key] : (liveTune(key) ?? 0);
      setPending(key, b.dataset.z === "reset" ? 0 : cur + Number(b.dataset.z));
    });
  });
  $("#tune-revert").onclick = () => { for (const k in tunePending) delete tunePending[k]; renderTuneRows(); };
  $("#tune-apply").onclick = async () => {
    const changes = { ...tunePending };
    const label = Object.entries(changes).map(([k, v]) => `${k === "zOffset" ? "Z offset" : k === "speed" ? "Speed" : k === "coolingFan" ? "Cooling fan" : "Chamber fan"} ${fmtTune(k, v)}`).join(", ");
    $("#tune-apply").disabled = true;
    if (await act(`Applied: ${label}`, "/api/printer-ctl", changes)) for (const k in changes) delete tunePending[k];
    renderTuneRows();
  };
}

// ------------------------------------------------------------ sensors / machine
function tile(label, value, cls = "") { return `<div class="tile ${cls}"><label>${label}</label><span>${value}</span></div>`; }
function renderSensors(d) {
  const tvoc = d.tvoc;
  $("#sensor-tiles").innerHTML = [
    tile("Chamber", d.chamberTemp != null && d.chamberTemp > -100 ? t1(d.chamberTemp) : "n/a"),
    tile("Bed", t1(d.platTemp)),
    tile("Air quality (TVOC)", tvoc ?? "–", tvoc > 2 ? "bad" : tvoc > 0.5 ? "warn" : "good"),
    tile("Door", d.doorStatus || "–", d.doorStatus === "open" ? "warn" : ""),
    tile("Cooling fan", (d.coolingFanSpeed ?? "–") + "%"),
    tile("Chamber fan", (d.chamberFanSpeed ?? "–") + "%"),
    tile("Internal fan", d.internalFanStatus || "–"),
    tile("External fan", d.externalFanStatus || "–"),
    tile("Light", d.lightStatus === "open" ? "on" : "off"),
    tile("Camera", d.camera ? "present" : "–"),
    tile("Lidar", d.lidar ? "present" : "–"),
    tile("Error", d.errorCode || "none", d.errorCode ? "bad" : "good"),
  ].join("");
}
function renderMachine(d) {
  $("#sw-light").checked = d.lightStatus === "open";
  $("#sw-internal").checked = d.internalFanStatus === "open";
  $("#sw-external").checked = d.externalFanStatus === "open";
  $("#sw-internal").disabled = !S.product.internalFanCtrlState;
  $("#sw-external").disabled = !S.product.externalFanCtrlState;
  const rows = [["Model", d.model], ["Firmware", d.firmwareVersion], ["IP / MAC", `${d.ipAddr || "–"} / ${d.macAddr || "–"}`],
    ["Build volume", d.measure], ["Nozzles", `${d.nozzleCnt} × ${(d.nozzleModel || "").split(";")[0]}`], ["Location", d.location],
    ["Disk free", d.remainingDiskSpace != null ? d.remainingDiskSpace.toFixed(2) + " GB" : "–"],
    ["Auto shutdown", d.autoShutdown === "open" ? `on (${d.autoShutdownTime} min)` : "off"],
    ["Lifetime print time", fmtDur(d.cumulativePrintTime * 60)], ["Lifetime filament", d.cumulativeFilament != null ? d.cumulativeFilament.toFixed(1) + " m" : "–"]];
  const html = rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v ?? "–")}</dd></div>`).join("");
  if ($("#machine-info").innerHTML !== html) $("#machine-info").innerHTML = html;
}
function initMachine() {
  const light = on => act(`Light ${on ? "on" : "off"}`, "/api/light", { on });
  $("#btn-light").onclick = () => light(S.detail.lightStatus !== "open");
  $("#sw-light").onchange = e => light(e.target.checked);
  const filt = () => act("Filtration", "/api/filtration", { internal: $("#sw-internal").checked, external: $("#sw-external").checked });
  $("#sw-internal").onchange = filt; $("#sw-external").onchange = filt;
  $("#btn-clear-platform").onclick = () => act("Bed marked as cleared", "/api/clear-platform");
  $$("[data-job]").forEach(b => b.onclick = async () => {
    const a = b.dataset.job;
    if (a === "cancel" && !await confirmDlg("Cancel print?", `Cancel "${S.detail.printFileName}"? This can't be undone.`)) return;
    act(`${a[0].toUpperCase() + a.slice(1)} sent`, "/api/job/" + a);
  });
}

// ------------------------------------------------------------ camera
let camOn = true;
function startCam() {
  if (!camOn || !isOpen("camera")) return;
  const img = $("#cam");
  if (!img.src.includes("/camera/stream")) img.src = "/camera/stream?t=" + Date.now();
  $("#cam-off").hidden = true;
}
function stopCam() { $("#cam").removeAttribute("src"); $("#cam-off").hidden = false; }
function initCam() {
  camOn = store.get("camOn", true);
  const tf = store.get("camTf", { rot: 0, flip: false });
  const apply = () => { $("#cam").style.transform = `rotate(${tf.rot}deg) scaleX(${tf.flip ? -1 : 1})`; store.set("camTf", tf); };
  apply();
  $("#cam-toggle").onclick = () => { camOn = !camOn; store.set("camOn", camOn); camOn ? startCam() : stopCam(); $("#cam-toggle").textContent = camOn ? "Pause view" : "Resume view"; };
  $("#cam-toggle").textContent = camOn ? "Pause view" : "Resume view";
  $("#cam-start").onclick = () => { camOn = true; store.set("camOn", true); startCam(); $("#cam-toggle").textContent = "Pause view"; };
  $("#cam-rot").onclick = () => { tf.rot = (tf.rot + 90) % 360; apply(); };
  $("#cam-flip").onclick = () => { tf.flip = !tf.flip; apply(); };
  $("#cam-full").onclick = () => $("#cam-wrap").requestFullscreen?.();
  $("#cam-power-on").onclick = () => act("Camera on", "/api/camera", { on: true });
  $("#cam-power-off").onclick = () => act("Camera off", "/api/camera", { on: false });
  $("#cam").onerror = () => { if (camOn) setTimeout(() => { $("#cam").removeAttribute("src"); startCam(); }, 5000); };
  // card width (Normal / Wide / Full) and drag-to-resize height, both remembered per browser
  const card = $("#cam-card"), wrap = $("#cam-wrap");
  const setSize = size => {
    card.classList.toggle("span-2", size === "2"); card.classList.toggle("span-full", size === "full");
    $$("#cam-size button").forEach(b => b.classList.toggle("on", b.dataset.size === size));
    store.set("camSize", size); setTimeout(resizeCharts, 50);
  };
  $$("#cam-size button").forEach(b => b.onclick = e => { e.stopPropagation(); setSize(b.dataset.size); });
  setSize(store.get("camSize", "2"));
  const h = store.get("camHeight", null);
  if (h) { wrap.style.height = h + "px"; wrap.classList.add("sized"); }
  let lastH = wrap.offsetHeight;
  new ResizeObserver(() => {
    const nh = wrap.offsetHeight;
    if (Math.abs(nh - lastH) > 2 && wrap.style.height) { wrap.classList.add("sized"); store.set("camHeight", nh); }
    lastH = nh;
  }).observe(wrap);
  wrap.ondblclick = () => { wrap.style.height = ""; wrap.classList.remove("sized"); store.set("camHeight", null); };
  document.addEventListener("visibilitychange", () => document.hidden ? stopCam() : (camOn && startCam()));
  camOn ? startCam() : stopCam();
}

// ------------------------------------------------------------ files
let files = [];
async function loadFiles(refresh) {
  if (!isOpen("files")) return;
  try { files = (await api("/api/files" + (refresh ? "?refresh=1" : ""))).files; renderFiles(); }
  catch (e) { $("#files").textContent = "Couldn't load files: " + e.message; }
}
function renderFiles() {
  const q = $("#files-filter").value.toLowerCase();
  $("#files").innerHTML = files.filter(f => f.name.toLowerCase().includes(q)).map((f, i) => `
    <div class="file">
      <img class="thumb" loading="lazy" src="/api/files/thumb?name=${encodeURIComponent(f.name)}" onerror="this.removeAttribute('src')" alt="">
      <div class="meta"><div class="name">${esc(f.name)}</div>
        <div class="sub">${f.printingTime ? "⏱ " + fmtDur(f.printingTime) : ""}${f.filamentG ? " · " + f.filamentG.toFixed(1) + " g" : ""}
          ${(f.tools || []).map(t => `<span title="${esc(t.materialName)} → slot ${t.slotId}"><span class="swatch" style="background:${esc(t.materialColor)}"></span>${esc(t.materialName)}</span>`).join("")}</div></div>
      <button class="btn primary" data-print="${files.indexOf(f)}">Print</button></div>`).join("") || "<p class='sub'>No files</p>";
  $$("[data-print]").forEach(b => b.onclick = () => openPrintDlg(files[+b.dataset.print]));
}

function openPrintDlg(f) {
  const d = S.detail || {};
  if (["printing", "pause", "paused", "pausing"].includes(d.status)) { toast("The printer is busy with another job", "err"); return; }
  const slots = (d.matlStationInfo || {}).slotInfos || [];
  $("#pd-name").textContent = f.name;
  $("#pd-thumb").src = "/api/files/thumb?name=" + encodeURIComponent(f.name);
  $("#pd-meta").textContent = [f.printingTime && fmtDur(f.printingTime), f.filamentG && f.filamentG.toFixed(1) + " g"].filter(Boolean).join(" · ");
  const tools = f.tools || [];
  $("#pd-mapping").innerHTML = tools.length ? "<h3>Tool → slot mapping</h3>" + tools.map((t, i) => `
    <div class="map-row"><span><span class="swatch" style="background:${esc(t.materialColor)}"></span>${esc(t.materialName)}</span>
      <select data-map="${i}">${slots.map(s => `<option value="${s.slotId}" ${s.slotId === t.slotId ? "selected" : ""}>Slot ${s.slotId} · T${s.slotId - 1} · ${esc(s.materialName || "empty")}</option>`).join("")}</select></div>`).join("") : "";
  const dlg = $("#print-dlg");
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "ok") return;
    const mappings = tools.map((t, i) => {
      const slotId = +$(`[data-map="${i}"]`).value, s = slots.find(x => x.slotId === slotId) || {};
      // gcodeListDetail's toolId is already the 0-based G-code tool (verified: toolId 1 = T1 on slot 2).
      const toolId = Math.min(3, Math.max(0, t.toolId | 0));
      return { toolId, slotId, materialName: t.materialName || s.materialName || "PLA", toolMaterialColor: t.materialColor, slotMaterialColor: s.materialColor || t.materialColor };
    });
    act(`Starting ${f.name}`, "/api/print", { fileName: f.name, levelingBeforePrint: $("#pd-level").checked,
      flowCalibration: $("#pd-flow").checked, timeLapseVideo: $("#pd-timelapse").checked, materialMappings: mappings });
  }, { once: true });
}

function initFiles() {
  $("#files-refresh").onclick = () => loadFiles(true);
  $("#files-filter").oninput = renderFiles;
  $("#upload-input").onchange = e => { if (e.target.files[0]) uploadDlg(e.target.files[0]); e.target.value = ""; };
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", e => { const f = e.dataTransfer.files[0]; if (f) uploadDlg(f); });
}
function uploadDlg(file) {
  if (!/\.(gcode|gx|3mf)$/i.test(file.name)) { toast("Only .gcode, .gx or .3mf files", "err"); return; }
  const dlg = $("#upload-dlg");
  $("#ud-name").textContent = file.name;
  dlg.showModal();
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "ok") return;
    const fd = new FormData();
    fd.append("printNow", $("#ud-now").checked); fd.append("levelingBeforePrint", $("#ud-level").checked);
    fd.append("flowCalibration", $("#ud-flow").checked); fd.append("timeLapseVideo", $("#ud-timelapse").checked);
    fd.append("useMatlStation", $("#ud-matl").checked); fd.append("gcodeToolCnt", $("#ud-tools").value);
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = e => { if (e.lengthComputable) $("#upload-status").textContent = `Uploading ${file.name}: ${Math.round(e.loaded / e.total * 100)}%`; };
    xhr.onload = () => {
      let r = {}; try { r = JSON.parse(xhr.responseText); } catch {}
      $("#upload-status").textContent = "";
      if (xhr.status === 200 && r.ok) { toast(`Uploaded ${file.name}`, "ok"); loadFiles(true); } else toast(`Upload failed: ${r.error || xhr.status}`, "err");
    };
    xhr.onerror = () => { $("#upload-status").textContent = ""; toast("Upload failed: network error", "err"); };
    xhr.send(fd);
  }, { once: true });
}

// ------------------------------------------------------------ job history & stats
async function loadJobs() {
  if (!isOpen("history")) return;
  try {
    const [{ jobs }, util] = await Promise.all([api("/api/jobs"), api("/api/utilization?days=30")]);
    utilData = util; buildUtilChart();
    const done = jobs.filter(j => j.ended), ok = done.filter(j => j.result === "completed");
    const printSec = done.reduce((a, j) => a + (j.ended - j.started), 0);
    const d = S.detail || {};
    const u7 = util.days.slice(-7).reduce((a, x) => a + x.printing, 0);
    $("#stat-tiles").innerHTML = [
      tile("Jobs recorded", jobs.length), tile("Success rate", done.length ? Math.round(ok.length / done.length * 100) + "%" : "–"),
      tile("Recorded print time", fmtDur(printSec)), tile("Utilization (7d)", Math.round(u7 / (7 * 864)) + "%"),
      tile("Lifetime print time", fmtDur((d.cumulativePrintTime || 0) * 60)), tile("Lifetime filament", d.cumulativeFilament ? d.cumulativeFilament.toFixed(0) + " m" : "–"),
    ].join("");
    $("#job-rows").innerHTML = jobs.map(j => {
      const res = j.ended ? j.result : "running", dur = (j.ended || Date.now() / 1000) - j.started;
      return `<tr><td>${j.hasThumb ? `<img class="thumb" src="/api/jobs/${j.id}/thumb" alt="">` : ""}</td><td>${esc(j.file)}</td>
        <td>${fmtTime(j.started)}</td><td>${fmtDur(dur)}</td><td class="res-${res}">${res}${j.progress != null && res !== "completed" ? ` (${Math.round(j.progress * 100)}%)` : ""}</td>
        <td>${j.ended ? `<button class="btn ghost" data-deljob="${j.id}" title="Remove from history">✕</button>` : ""}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="sub">No jobs recorded yet. History starts from when this dashboard was installed.</td></tr>`;
    $$("[data-deljob]").forEach(b => b.onclick = async () => { await api("/api/jobs/" + b.dataset.deljob, { method: "DELETE" }); loadJobs(); });
  } catch (e) { toast("Jobs: " + e.message, "err"); }
}

// ------------------------------------------------------------ console / events
async function loadEvents() {
  if (!isOpen("console")) return;
  try {
    const { events } = await api("/api/events?limit=300");
    const f = $("#event-filter").value;
    const match = e => !f || (f === "command" && e.kind.startsWith("command")) || (f === "status" && e.kind === "status")
      || (f === "print" && e.kind.startsWith("print_")) || (f === "problem" && /failed|error|offline/.test(e.kind));
    $("#console").innerHTML = events.filter(match).map(e =>
      `<div class="ev k-${esc(e.kind)}"><span class="ts">${fmtTime(e.ts)}</span> [${esc(e.kind)}] ${esc(e.message)}${e.source && e.source !== "system" ? ` <span class="ts">(${esc(e.source)})</span>` : ""}</div>`).join("") || "<div class='ts'>No events</div>";
  } catch (e) { $("#console").textContent = e.message; }
}

// ------------------------------------------------------------ settings
function renderPresetEditor() {
  $("#preset-rows").innerHTML = settings.presets.map((p, i) => `<tr>
    <td><input data-p="${i}" data-f="name" value="${esc(p.name)}"></td>
    <td><input type="number" data-p="${i}" data-f="tool" value="${p.tool}"></td>
    <td><input type="number" data-p="${i}" data-f="bed" value="${p.bed}"></td>
    <td><input type="number" data-p="${i}" data-f="chamber" value="${p.chamber || 0}"></td>
    <td><button class="btn ghost" data-pdel="${i}">✕</button></td></tr>`).join("");
  $$("[data-p]").forEach(inp => inp.oninput = () => { const p = settings.presets[+inp.dataset.p]; p[inp.dataset.f] = inp.type === "number" ? +inp.value : inp.value; });
  $$("[data-pdel]").forEach(b => b.onclick = () => { settings.presets.splice(+b.dataset.pdel, 1); renderPresetEditor(); });
}
function renderNotifyEditor() {
  const t = settings.notify.targets;
  $("#notify-targets").innerHTML = t.map((x, i) => `<div class="target">
    <select data-n="${i}" data-f="type"><option value="ntfy" ${x.type === "ntfy" ? "selected" : ""}>ntfy</option><option value="webhook" ${x.type === "webhook" ? "selected" : ""}>webhook</option></select>
    <input data-n="${i}" data-f="url" value="${esc(x.url || "")}" placeholder="https://ntfy.sh/your-topic">
    <button class="btn ghost" data-ndel="${i}">✕</button>
    <div class="evs">${settings.notifyEvents.map(ev => `<label class="check"><input type="checkbox" data-n="${i}" data-ev="${ev}" ${(x.events || settings.notifyEvents).includes(ev) ? "checked" : ""}>${ev.replace(/_/g, " ")}</label>`).join("")}</div></div>`).join("")
    || "<p class='sub'>No notification targets configured.</p>";
  $$("[data-n][data-f]").forEach(el => el.oninput = () => { t[+el.dataset.n][el.dataset.f] = el.value; });
  $$("[data-n][data-ev]").forEach(el => el.onchange = () => {
    const x = t[+el.dataset.n]; x.events = $$(`[data-n="${el.dataset.n}"][data-ev]`).filter(c => c.checked).map(c => c.dataset.ev);
  });
  $$("[data-ndel]").forEach(b => b.onclick = () => { t.splice(+b.dataset.ndel, 1); renderNotifyEditor(); });
}
async function initSettings() {
  settings = await api("/api/settings");
  renderPresets(); renderPresetEditor(); renderNotifyEditor();
  $("#preset-add").onclick = () => { settings.presets.push({ name: "New", tool: 210, bed: 60, chamber: 0 }); renderPresetEditor(); };
  $("#preset-save").onclick = async () => { try { await api("/api/settings/presets", { method: "PUT", body: settings.presets }); renderPresets(); toast("Presets saved", "ok"); } catch (e) { toast(e.message, "err"); } };
  $("#notify-add").onclick = () => { settings.notify.targets.push({ type: "ntfy", url: "", events: [...settings.notifyEvents] }); renderNotifyEditor(); };
  $("#notify-save").onclick = async () => { try { await api("/api/settings/notify", { method: "PUT", body: settings.notify }); toast("Notifications saved", "ok"); } catch (e) { toast(e.message, "err"); } };
  $("#notify-test").onclick = () => act("Test notification sent", "/api/notify/test");
  $("#browser-notify").checked = store.get("browserNotify", false) && window.Notification?.permission === "granted";
  $("#browser-notify").onchange = async e => {
    if (e.target.checked && window.Notification) { e.target.checked = (await Notification.requestPermission()) === "granted"; }
    store.set("browserNotify", e.target.checked);
  };
}
function notifyBrowser(status, d) {
  if (lastStatus && status !== lastStatus && store.get("browserNotify", false) && window.Notification?.permission === "granted") {
    const was = lastStatus;
    let msg = null;
    if (status === "printing" && !["pause", "paused", "pausing"].includes(was)) msg = `Started: ${d.printFileName}`;
    else if (["completed", "ready"].includes(status) && ["printing"].includes(was)) msg = `Finished: ${d.printFileName}`;
    else if (["pause", "paused"].includes(status)) msg = `Paused: ${d.printFileName}`;
    else if (status === "error") msg = `Error ${d.errorCode || ""}`;
    if (msg) new Notification(d.name || "Printer", { body: msg });
  }
  lastStatus = status;
}

// ------------------------------------------------------------ boot
// Live connection first, and each card initialised independently, so one failing card
// can never leave the whole page disconnected.
connect();
for (const [name, fn] of [["theme", initTheme], ["cards", initCards], ["chart", initChartControls], ["tune", initTune],
                          ["machine", initMachine], ["camera", initCam], ["files", initFiles]]) {
  try { fn(); } catch (e) { console.error(name, e); toast(`Page error in ${name}: ${e.message}`, "err"); }
}
$("#events-refresh").onclick = loadEvents; $("#event-filter").onchange = loadEvents;
initSettings().catch(e => toast("Settings: " + e.message, "err"));
["chart", "files", "history", "console"].forEach(k => { try { isOpen(k) && onCardOpen(k); } catch (e) { console.error(k, e); } });
setInterval(loadEvents, 10000);
setInterval(loadJobs, 60000);
