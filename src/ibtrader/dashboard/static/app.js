"use strict";

// ---------- helpers ----------
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
const color = (i) => css(SERIES[i % SERIES.length]);

function fmt(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
const fmtPx = (x) => (x == null ? "–" : fmt(x, Math.abs(x) < 10 ? 4 : 2));
const money = (x, ccy, d = 0) => (x === null || x === undefined ? "–" : `${fmt(x, d)} ${ccy || ""}`.trim());
const pct = (x, d = 2) => (x === null || x === undefined ? "–" : `${x > 0 ? "+" : ""}${fmt(x, d)}%`);
const signCls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
};

const state = {
  tab: "portfolio", session: store.get("ibdash.session") || "live", base: "EUR",
  groups: [], group: store.get("ibdash.group") || "Gold", metalRange: "1Y", metalView: "__compare__", quotes: [],
  side: "BUY", instrument: null, book: null, previewTimer: null, orderStatus: null,
};

function withSession(path) {
  return `${path}${path.includes("?") ? "&" : "?"}session=${state.session}`;
}
async function api(path, opts = {}) {
  const r = await fetch(withSession(path), {
    ...opts, headers: { "X-Requested-With": "ibdash", "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(-1)[0]}: ${x.msg}`).join("; ") : d || `HTTP ${r.status}`);
  }
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

function table(el, cols, rows, totalRow) {
  const head = `<thead><tr>${cols.map((c) => `<th class="${c.l ? "l" : ""} ${c.hc || ""}">${esc(c.h)}</th>`).join("")}</tr></thead>`;
  const tr = (r, cls = "") => `<tr class="${cls}">${cols.map((c) => `<td class="${c.l ? "l" : ""} ${c.cls ? c.cls(r) : ""}">${c.f(r)}</td>`).join("")}</tr>`;
  const body = rows.length ? rows.map((r) => tr(r)).join("") : `<tr><td colspan="${cols.length}" class="muted l">No data</td></tr>`;
  el.innerHTML = `${head}<tbody>${body}${totalRow ? tr(totalRow, "total") : ""}</tbody>`;
}
function banner(msg) { const b = $("#banner"); b.hidden = !msg; b.textContent = msg || ""; }

// ---------- charts ----------
const charts = {};
function chartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--grid");
  Object.assign(Chart.defaults.plugins.tooltip, { backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--ink-2"), borderColor: css("--border"), borderWidth: 1 });
  Object.assign(Chart.defaults.plugins.legend.labels, { color: css("--ink-2"), boxWidth: 10, boxHeight: 10 });
}
function draw(id, config) {
  if (!window.Chart) return;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), config);
}
function hbar(id, labels, values, fmtFn) {
  draw(id, {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: color(0), borderRadius: 4, borderSkipped: "start", maxBarThickness: 18 }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmtFn(c.parsed.x) } } },
      scales: { x: { grid: { color: css("--grid") }, ticks: { callback: (v) => fmtFn(v) } }, y: { grid: { display: false } } },
    },
  });
}

// ---------- sessions & status ----------
async function loadSessions() {
  const list = await fetch("/api/sessions").then((r) => r.json());
  $$("#session-switch button").forEach((b) => {
    const s = list.find((x) => x.session === b.dataset.s);
    b.classList.toggle("on", !!s?.connected);
    b.classList.toggle("active", b.dataset.s === state.session);
    b.title = s?.connected ? `Connected (port ${s.port})` : `Offline: ${s?.error || "not connected"}`;
  });
  document.body.classList.toggle("session-live", state.session === "live");
  document.body.classList.toggle("session-paper", state.session === "paper");
  table($("#sessions"), [
    { h: "Session", l: 1, f: (r) => `<strong>${esc(r.session)}</strong>` }, { h: "Port", f: (r) => r.port },
    { h: "Status", l: 1, f: (r) => (r.connected ? "Connected" : `Offline: ${esc(r.error || "")}`) },
    { h: "Orders from dashboard", l: 1, f: (r) => (r.orders_enabled ? "Enabled" : "Disabled") },
  ], list);
  return list;
}

async function loadStatus() {
  try {
    await loadSessions();
    const s = await api("/api/status");
    state.base = s.base_currency || state.base;
    state.orderStatus = s;
    $$(".ccy").forEach((e) => (e.textContent = state.base));
    const dataType = { 1: "live data", 2: "frozen", 3: "delayed*", 4: "delayed frozen" }[s.market_data_type] || "";
    $("#status-text").innerHTML = s.connected
      ? `${esc(s.account)} · ${esc(dataType)} · ${s.orders_enabled ? "<span class='pill'>orders on</span>" : "<span class='pill'>read-only</span>"}`
      : "";
    $("#footer").textContent = s.orders_enabled
      ? `${s.session.toUpperCase()} session: orders can be sent from the Trade tab (with preview + guardrails).`
      : `${s.session.toUpperCase()} session is read-only in this dashboard. ${s.orders_blocked_reason || ""}`;
    banner(s.connected ? "" : `${s.session.toUpperCase()} session not connected (port ${s.port}). ${s.connect_error || "Retrying…"}`);
    $("#conn").innerHTML = [
      ["Selected session", s.session], ["Gateway", s.host], ["Account", s.account], ["Base currency", s.base_currency],
      ["Market data", `${dataType} (*delayed mode returns live data where you have a subscription)`],
      ["Orders", s.orders_enabled ? "Enabled (preview + guardrails)" : s.orders_blocked_reason],
      ["Flex history", s.flex_configured ? "Configured" : "Not configured (see README)"], ["Last error", s.connect_error || "–"],
    ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    table($("#errors"), [
      { h: "Time", l: 1, f: (r) => esc(r.time.slice(11)) }, { h: "Code", f: (r) => esc(r.code ?? "") },
      { h: "Symbol", l: 1, f: (r) => esc(r.symbol ?? "") }, { h: "Message", l: 1, f: (r) => esc(r.msg) },
    ], s.errors);
    return s.connected;
  } catch (e) {
    $("#status-text").textContent = "Dashboard server unreachable";
    banner(`Dashboard server unreachable: ${e.message}`);
    return false;
  }
}

// ---------- portfolio ----------
async function loadPortfolio() {
  const p = await api("/api/portfolio");
  const b = p.base_currency || state.base;
  const s = p.summary;
  const tile = (label, value, cls = "") => `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div></div>`;
  const unr = p.pnl.unrealized ?? s.UnrealizedPnL, rea = p.pnl.realized ?? s.RealizedPnL;
  $("#kpis").innerHTML = [
    tile("Net liquidation", money(s.NetLiquidation, b)),
    tile("Today's P&L", money(p.pnl.daily, b), signCls(p.pnl.daily)),
    tile("Unrealized P&L", money(unr, b), signCls(unr)),
    tile("Realized P&L (today)", money(rea, b), signCls(rea)),
    tile("Cash", money(s.TotalCashValue, b)), tile("Invested", money(s.GrossPositionValue, b)),
    tile("Buying power", money(s.BuyingPower, b)), tile("Maint. margin", money(s.MaintMarginReq, b)),
  ].join("");
  table($("#positions"), [
    { h: "Instrument", l: 1, f: (r) => `<strong>${esc(r.local_symbol)}</strong> <span class="tag">${esc(r.sec_type)}</span>` },
    { h: "Qty", f: (r) => fmt(r.position, r.position % 1 ? 2 : 0) },
    { h: "Avg cost", f: (r) => fmt(r.avg_cost) }, { h: "Price", f: (r) => fmt(r.market_price) },
    { h: "Value", f: (r) => money(r.market_value, r.currency) },
    { h: "Unrl. P&L", f: (r) => fmt(r.unrealized_pnl), cls: (r) => signCls(r.unrealized_pnl) },
    { h: "Unrl. %", f: (r) => pct(r.unrealized_pct), cls: (r) => signCls(r.unrealized_pct) },
    { h: "Weight", f: (r) => (r.weight == null ? "–" : `${fmt(r.weight, 1)}%`) },
  ], p.positions);
  $("#pos-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  table($("#cash"), [
    { h: "Currency", l: 1, f: (r) => esc(r.currency) }, { h: "Amount", f: (r) => fmt(r.amount) },
    { h: `In ${b}`, f: (r) => fmt(r.amount_base) },
  ], p.cash);
  const nlv = s.NetLiquidation || 0;
  const items = p.positions.filter((r) => r.weight != null).map((r) => [r.local_symbol, r.weight]);
  if (nlv && s.TotalCashValue != null) items.push(["Cash", (s.TotalCashValue / nlv) * 100]);
  hbar("alloc-chart", items.map((i) => i[0]), items.map((i) => i[1]), (v) => `${fmt(v, 1)}%`);
}

// ---------- metals ----------
function currentGroup() { return state.groups.find((g) => g.name === state.group) || state.groups[0]; }

async function loadGroups() {
  if (state.groups.length) return;
  state.groups = await fetch("/api/metals/groups").then((r) => r.json());
  if (!state.groups.find((g) => g.name === state.group)) state.group = state.groups[0].name;
  const chips = $("#metal-groups");
  chips.innerHTML = state.groups.map((g) => `<button class="chip" data-g="${esc(g.name)}">${esc(g.name)}</button>`).join("");
  chips.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => selectGroup(c.dataset.g)));
  $("#cost-group").innerHTML = state.groups.map((g) => `<option>${esc(g.name)}</option>`).join("");
  $("#cost-group").value = state.group;
  syncGroupUi();
}
function syncGroupUi() {
  $$("#metal-groups .chip").forEach((c) => c.classList.toggle("active", c.dataset.g === state.group));
  const g = currentGroup();
  $("#metal-view").innerHTML = `<option value="__compare__">Compare all (indexed)</option>` +
    g.keys.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join("");
  $("#metal-view").value = state.metalView;
}
function selectGroup(name) {
  state.group = name; state.metalView = "__compare__";
  store.set("ibdash.group", name);
  syncGroupUi();
  loadQuotes().catch((e) => banner(e.message));
  loadMetalChart(); loadTracking();
}

async function loadQuotes() {
  const g = currentGroup();
  const m = await api(`/api/metals/quotes?group=${encodeURIComponent(g.name)}`);
  state.quotes = m.quotes;
  const b = m.base_currency || state.base;
  const dtype = { 1: "live", 2: "frozen", 3: "delayed", 4: "delayed/frozen" };
  $("#quotes").innerHTML = m.quotes.map((q, i) => {
    const rows = [
      ["Bid / Ask", q.bid && q.ask ? `${fmtPx(q.bid)} / ${fmtPx(q.ask)}` : "–"],
      ["Spread", q.spread_bps == null ? "–" : `${fmt(q.spread_bps, 1)} bps${q.spread_source === "assumed" ? " (assumed)" : ""}`],
      [`Unit value (${b})`, money(q.notional_unit_base, "", 2)],
    ];
    if (m.unit && m.reference && q.key !== m.reference) rows.push([`Metal per unit`, q.underlying_per_unit == null ? "–" : `${fmt(q.underlying_per_unit, 4)} ${esc(m.unit)}`]);
    if (q.kind === "future") rows.push(["Contract", `${esc(q.local_symbol)} (${q.days_to_expiry ?? "?"}d)`]);
    if (q.kind === "future" && q.carry_annual_pct != null) rows.push(["Carry vs spot", `${pct(q.carry_annual_pct)} /yr`]);
    const tag = !q.price ? "no data: check subscription" : q.price_source === "history" ? `close ${q.history_date || ""}` : q.price_source === "close" ? "prev close" : dtype[q.market_data_type] || "";
    return `<div class="quote ${state.metalView === q.key ? "selected" : ""}" data-key="${esc(q.key)}">
      <div class="top"><span class="sym"><span class="swatch" data-i="${i}"></span>${esc(q.key)}${q.key === m.reference ? ' <span class="tag">ref</span>' : ""}</span>
        <span class="tag ${["mid", "last"].includes(q.price_source) ? "" : "warn"}">${esc(tag)}</span></div>
      <div class="name" title="${esc(q.name)}">${esc(q.name)}</div>
      <div><span class="px">${fmtPx(q.price)}</span> <span class="muted">${esc(q.currency)}</span>
        <span class="${signCls(q.change_pct)}">${pct(q.change_pct)}</span></div>
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
  }).join("");
  $$(".swatch").forEach((s) => (s.style.background = color(Number(s.dataset.i))));
  $$(".quote").forEach((el) => el.addEventListener("click", () => {
    state.metalView = state.metalView === el.dataset.key ? "__compare__" : el.dataset.key;
    $("#metal-view").value = state.metalView;
    loadQuotes().catch(() => {}); loadMetalChart();
  }));
}

async function loadMetalChart() {
  const g = currentGroup(), range = state.metalRange, note = $("#metal-chart-note");
  note.textContent = "Loading historical data from IB…";
  try {
    if (state.metalView === "__compare__") {
      $("#metal-chart-title").textContent = `${g.name} products, indexed to 100`;
      const results = [];
      for (const k of g.keys) { // sequential: IB paces historical requests
        try { results.push([k, (await api(`/api/metals/history?key=${encodeURIComponent(k)}&range=${range}`)).bars]); }
        catch (e) { results.push([k, []]); }
      }
      const datasets = results.map(([k, bars]) => {
        const i = g.keys.indexOf(k), first = bars.length ? bars[0].c : 1;
        return { label: k, data: bars.map((b) => ({ x: b.t, y: (b.c / first) * 100 })), borderColor: color(i), backgroundColor: color(i), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 };
      }).filter((d) => d.data.length);
      lineChart(datasets, (v) => fmt(v, 1), datasets.length > 1);
      const missing = results.filter(([, b]) => !b.length).map(([k]) => k);
      note.textContent = "Each line rebased to 100 at the start of the window, in its own currency. Futures are continuous front-month (roll jumps possible)."
        + (missing.length ? ` No history for: ${missing.join(", ")}.` : "");
    } else {
      const k = state.metalView, q = state.quotes.find((x) => x.key === k) || {};
      $("#metal-chart-title").textContent = `${k}: ${q.name || ""} (${q.currency || ""})`;
      const bars = (await api(`/api/metals/history?key=${encodeURIComponent(k)}&range=${range}`)).bars;
      const i = g.keys.indexOf(k);
      lineChart([{ label: k, data: bars.map((b) => ({ x: b.t, y: b.c })), borderColor: color(i), backgroundColor: color(i), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 }], fmtPx, false);
      const first = bars[0]?.c, last = bars[bars.length - 1]?.c;
      note.textContent = first ? `Change over ${range}: ${pct((last / first - 1) * 100)}` : "No history available.";
    }
  } catch (e) { note.textContent = `Could not load history: ${e.message}`; }
}

function lineChart(datasets, tickFmt, legend) {
  draw("metal-chart", {
    type: "line", data: { datasets },
    options: {
      maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: legend, position: "top", align: "start" }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${tickFmt(c.parsed.y)}` } } },
      scales: {
        x: { type: "category", labels: [...new Set(datasets.flatMap((d) => d.data.map((p) => p.x)))].sort(), ticks: { maxTicksLimit: 8, callback: function (v) { return String(this.getLabelForValue(v)).slice(0, 10); } }, grid: { display: false } },
        y: { grid: { color: css("--grid") }, ticks: { callback: tickFmt } },
      },
    },
  });
}

async function loadTracking() {
  const g = currentGroup();
  const range = ["5D", "1M"].includes(state.metalRange) ? "1Y" : state.metalRange;
  $("#tracking-range").textContent = `window: ${range}`;
  $("#tracking-title").textContent = `${g.name} ETF / ETC tracking vs ${g.reference || "–"}`;
  if (!g.reference) {
    $("#tracking-note").textContent = "No reference price available for this metal (IBKR has no LME access), so tracking can't be measured.";
    $("#tracking").innerHTML = ""; return;
  }
  try {
    const t = await api(`/api/metals/tracking?group=${encodeURIComponent(g.name)}&range=${range}`);
    $("#tracking-note").textContent = t.reference_kind === "spot"
      ? "Product return minus spot return in the same currency. The gap is roughly fund fees plus tracking error, i.e. what holding the product cost you."
      : "Reference is the continuous front-month future (no spot available), so the gap also contains roll effects. Treat as indicative.";
    table($("#tracking"), [
      { h: "Product", l: 1, f: (r) => `<strong>${esc(r.key)}</strong> <span class="muted">${esc(r.name || "")}</span>` },
      { h: "Period", f: (r) => (r.error ? esc(r.error) : `${esc(r.from)} → ${esc(r.to)}`) },
      { h: "Product return", f: (r) => pct(r.product_return_pct) },
      { h: `${t.reference} return`, f: (r) => pct(r.ref_return_pct) },
      { h: "Difference", f: (r) => pct(r.tracking_diff_pct), cls: (r) => signCls(r.tracking_diff_pct) },
      { h: "Diff. per year", f: (r) => pct(r.tracking_diff_annual_pct), cls: (r) => signCls(r.tracking_diff_annual_pct) },
      { h: "Stated fee", f: (r) => (r.expense_ratio_pct == null ? "–" : `${fmt(r.expense_ratio_pct, 2)}%`) },
    ], t.rows);
  } catch (e) { $("#tracking").innerHTML = `<tr><td class="muted l">${esc(e.message)}</td></tr>`; }
}

// ---------- trade ----------
async function loadInstruments() {
  const x = await api("/api/instruments");
  const sel = $("#t-instrument"), prev = sel.value;
  const opt = (v, label, disabled) => `<option value="${esc(v)}" ${disabled ? "disabled" : ""}>${esc(label)}</option>`;
  let html = `<option value="">Select an instrument…</option>`;
  if (x.positions.length) html += `<optgroup label="Your positions">${x.positions.map((p) => opt(`con:${p.con_id}`, `${p.label} (${p.sec_type}, pos ${fmt(p.position, 0)})`)).join("")}</optgroup>`;
  for (const g of state.groups) {
    const items = x.metals.filter((m) => m.group === g.name && m.kind !== "spot");
    if (items.length) html += `<optgroup label="${esc(g.name)}">${items.map((m) => opt(`key:${m.key}`, m.label, !m.tradable)).join("")}</optgroup>`;
  }
  html += `<optgroup label="Other">${opt("custom", "Other stock / ETF…")}</optgroup>`;
  sel.innerHTML = html;
  if (prev) sel.value = prev;
}

function instrumentRef() {
  const v = $("#t-instrument").value;
  if (!v) return null;
  if (v.startsWith("key:")) return { key: v.slice(4) };
  if (v.startsWith("con:")) return { con_id: Number(v.slice(4)) };
  const sym = $("#t-symbol").value.trim();
  return sym ? { symbol: sym, exchange: $("#t-exchange").value.trim().toUpperCase() || "SMART", currency: $("#t-currency").value.trim().toUpperCase() || "USD" } : null;
}

function ticketNote() {
  const s = state.orderStatus;
  const note = $("#ticket-note"), pill = $("#ticket-session");
  pill.textContent = state.session.toUpperCase();
  pill.className = `pill ${state.session === "live" ? "live" : ""}`;
  if (!s) return;
  if (!s.connected) { note.className = "note bad"; note.textContent = `Session not connected: ${s.connect_error || ""}`; }
  else if (s.orders_enabled) {
    note.className = state.session === "live" ? "note bad" : "note ok";
    note.textContent = state.session === "live" ? "LIVE session: orders use real money. Every order needs a preview and a typed confirmation." : "PAPER session: orders go to your paper account (simulated fills).";
  } else { note.className = "note"; note.textContent = `Orders disabled for this session: ${s.orders_blocked_reason}`; }
}

async function loadBook() {
  const ref = instrumentRef();
  if (!ref) { $("#l1").innerHTML = ""; $("#book").innerHTML = ""; $("#book-title").textContent = "Market"; return; }
  try {
    const b = await post("/api/book", ref);
    state.book = b;
    $("#book-title").textContent = `${b.label} · ${b.exchange} · ${b.currency}`;
    $("#book-type").textContent = { 1: "live", 2: "frozen", 3: "delayed", 4: "delayed/frozen" }[b.market_data_type] || "";
    const cell = (k, v) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`;
    $("#l1").innerHTML = cell("Bid", `${fmtPx(b.bid)}<span class="muted small"> ×${fmt(b.bid_size, 0)}</span>`) + cell("Ask", `${fmtPx(b.ask)}<span class="muted small"> ×${fmt(b.ask_size, 0)}</span>`) + cell("Last", fmtPx(b.last)) + cell("Close", fmtPx(b.close));
    const n = Math.max(b.bids.length, b.asks.length);
    const rows = Array.from({ length: n }, (_, i) => ({ b: b.bids[i], a: b.asks[i] }));
    table($("#book"), [
      { h: "Bid size", f: (r) => (r.b ? fmt(r.b.size, 0) : "") },
      { h: "Bid", f: (r) => (r.b ? fmtPx(r.b.price) : ""), cls: () => "bidp" },
      { h: "Ask", hc: "askh", f: (r) => (r.a ? fmtPx(r.a.price) : ""), cls: () => "askp" },
      { h: "Ask size", f: (r) => (r.a ? fmt(r.a.size, 0) : "") },
    ], rows);
    $("#book-msg").textContent = n ? "" : (b.messages.length ? `No depth: ${b.messages.join(" | ")}` : "No depth data (needs a depth-of-book subscription for this exchange, and the market must be open).");
    updateValue();
  } catch (e) { $("#book-msg").textContent = e.message; }
}

function updateValue() {
  const q = Number($("#t-qty").value), p = Number($("#t-price").value), m = state.book?.multiplier || 1;
  $("#t-value").textContent = q && p ? `Order value ≈ ${fmt(q * p * m, 2)} ${state.book?.currency || ""}${m !== 1 ? ` (multiplier ${m})` : ""}` : "";
}

async function previewOrder() {
  const ref = instrumentRef();
  if (!ref) return banner("Select an instrument first.");
  const body = {
    instrument: ref, action: state.side, quantity: Number($("#t-qty").value), limit_price: Number($("#t-price").value),
    tif: $("#t-tif").value, outside_rth: $("#t-rth").checked,
  };
  const btn = $("#t-preview");
  btn.disabled = true; btn.textContent = "Checking with IB…";
  try {
    const p = await post("/api/orders/preview", body);
    showPreview(p);
  } catch (e) { banner(`Preview failed: ${e.message}`); }
  finally { btn.disabled = false; btn.textContent = "Preview order"; }
}

function showPreview(p) {
  state.preview = p;
  $("#preview").hidden = false;
  const w = p.what_if || {};
  $("#preview-kv").innerHTML = [
    ["Session", p.session.toUpperCase()], ["Order", `${p.action} ${fmt(p.quantity, p.quantity % 1 ? 2 : 0)} ${p.label} @ ${fmtPx(p.limit_price)} LMT ${p.tif}`],
    ["Exchange", `${p.exchange} (${p.sec_type})`], ["Order value", `${money(p.notional, p.currency, 2)}${p.notional_base != null ? ` ≈ ${money(p.notional_base, p.base_currency, 2)}` : ""}`],
    ["Est. commission", w.commission != null ? money(w.commission, w.commission_currency, 2) : (w.min_commission != null ? `${fmt(w.min_commission)}–${fmt(w.max_commission)} ${w.commission_currency || ""}` : "–")],
    ["Init. margin change", w.init_margin_change != null ? fmt(w.init_margin_change) : "–"],
    ["Equity after", w.equity_with_loan_after != null ? fmt(w.equity_with_loan_after) : "–"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const msg = $("#preview-msg");
  const parts = [];
  if (p.blocked) parts.push(`Blocked: ${p.blocked}`);
  if (w.warning) parts.push(`IB warning: ${w.warning}`);
  if (p.what_if_error) parts.push(p.what_if_error);
  msg.textContent = parts.join("  ");
  msg.className = `note ${p.blocked ? "bad" : ""}`;
  $("#t-submit").disabled = !p.preview_id;
  $("#t-submit").textContent = p.session === "live" ? "Submit LIVE order" : "Submit paper order";
  $("#confirm-live").hidden = !p.confirm_word;
  $("#t-confirm").value = "";
  clearInterval(state.previewTimer);
  let left = p.expires_in;
  const tick = () => {
    $("#preview-timer").textContent = p.preview_id ? `valid for ${left}s` : "";
    if (left-- <= 0) { clearInterval(state.previewTimer); $("#t-submit").disabled = true; $("#preview-timer").textContent = "expired: preview again"; }
  };
  tick(); state.previewTimer = setInterval(tick, 1000);
}

async function submitOrder() {
  const p = state.preview;
  if (!p?.preview_id) return;
  if (p.confirm_word && $("#t-confirm").value.trim() !== p.confirm_word) return banner(`Type ${p.confirm_word} to confirm.`);
  $("#t-submit").disabled = true;
  try {
    const r = await post("/api/orders/submit", { preview_id: p.preview_id, confirm: $("#t-confirm").value.trim() || null });
    clearInterval(state.previewTimer);
    $("#preview").hidden = true;
    banner(`Order ${r.order_id} sent: ${r.status}${r.last_message ? ` (${r.last_message})` : ""}`);
    loadOrders();
  } catch (e) { banner(`Submit failed: ${e.message}`); $("#t-submit").disabled = false; }
}

async function loadOrders() {
  const o = await api("/api/orders");
  const cols = [
    { h: "Time", l: 1, f: (r) => esc((r.time || "").slice(11, 19)) }, { h: "Id", f: (r) => r.order_id },
    { h: "Instrument", l: 1, f: (r) => `<strong>${esc(r.label)}</strong>` },
    { h: "Side", l: 1, f: (r) => esc(r.action), cls: (r) => (r.action === "BUY" ? "up" : "down") },
    { h: "Qty", f: (r) => fmt(r.quantity, 0) }, { h: "Type", l: 1, f: (r) => esc(`${r.type} ${r.tif}`) },
    { h: "Limit", f: (r) => fmtPx(r.limit) }, { h: "Status", l: 1, f: (r) => esc(r.status) },
    { h: "Filled", f: (r) => `${fmt(r.filled, 0)}${r.avg_fill ? ` @ ${fmtPx(r.avg_fill)}` : ""}` },
  ];
  table($("#open-orders"), [...cols, { h: "", f: (r) => (r.cancellable ? `<button class="btn" data-cancel="${r.order_id}">Cancel</button>` : "") }], o.open);
  table($("#recent-orders"), [...cols, { h: "Message", l: 1, f: (r) => esc(r.last_message) }], o.recent);
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Cancel order ${b.dataset.cancel}?`)) return;
    try { await post(`/api/orders/${b.dataset.cancel}/cancel`); loadOrders(); } catch (e) { banner(e.message); }
  }));
}

async function loadTrade() {
  ticketNote();
  if (!$("#t-instrument").options.length || $("#t-instrument").options.length < 2) await loadInstruments();
  await Promise.all([loadBook(), loadOrders()]);
}

// ---------- costs ----------
async function loadEstimates() {
  const target = Number($("#target").value) || 10000;
  const e = await api(`/api/costs/estimates?group=${encodeURIComponent($("#cost-group").value || state.group)}&target=${target}`);
  const b = e.base_currency || state.base;
  table($("#estimates"), [
    { h: "Product", l: 1, f: (r) => `<strong>${esc(r.key)}</strong> <span class="tag">${esc(r.kind)}</span>` },
    { h: "Units", f: (r) => (r.qty ? fmt(r.qty, 0) : `<span class="muted" title="${esc(r.note)}">0 ⓘ</span>`) },
    { h: `Exposure (${b})`, f: (r) => fmt(r.exposure_base, 0) },
    { h: "Commission (buy+sell)", f: (r) => fmt(r.commission_rt_base) },
    { h: "Spread", f: (r) => (r.spread_rt_base == null ? "–" : `${fmt(r.spread_rt_base)}${r.spread_source === "assumed" ? "*" : ""}`) },
    { h: "Round trip", f: (r) => fmt(r.round_trip_base) }, { h: "RT bps", f: (r) => fmt(r.round_trip_bps, 1) },
    { h: "Holding / yr", f: (r) => fmt(r.annual_hold_base) },
    { h: "Year-1 total", f: (r) => `<strong>${fmt(r.first_year_base)}</strong>` },
    { h: "Year-1 bps", f: (r) => (r.exposure_base ? fmt((r.first_year_base / r.exposure_base) * 1e4, 0) : "–") },
    { h: "Futures carry", f: (r) => (r.carry_annual_pct == null ? "–" : `${pct(r.carry_annual_pct)}/yr`) },
    { h: "Note", l: 1, f: (r) => esc(r.note || "") },
  ], e.rows);
  const rows = e.rows.filter((r) => r.qty).map((r) => [r.key, (r.first_year_base / r.exposure_base) * 1e4]).sort((x, y) => x[1] - y[1]);
  hbar("est-chart", rows.map((r) => r[0]), rows.map((r) => r[1]), (v) => `${fmt(v, 0)} bps`);
}

function costSummaryHtml(sum, b) {
  const t = sum.total;
  return `<div class="tiles">
    <div class="tile"><div class="label">Trades</div><div class="value">${fmt(t.trades, 0)}</div></div>
    <div class="tile"><div class="label">Traded value</div><div class="value">${money(t.notional, b)}</div></div>
    <div class="tile"><div class="label">Commissions</div><div class="value">${money(t.commission, b, 2)}</div></div>
    <div class="tile"><div class="label">Avg cost</div><div class="value">${t.bps == null ? "–" : `${fmt(t.bps, 1)} bps`}</div></div>
  </div>`;
}
const symCols = [
  { h: "Symbol", l: 1, f: (r) => esc(r.key) }, { h: "Trades", f: (r) => fmt(r.trades, 0) },
  { h: "Traded value", f: (r) => fmt(r.notional, 0) }, { h: "Commission", f: (r) => fmt(r.commission) },
  { h: "bps", f: (r) => fmt(r.bps, 1) },
];
async function loadExecutions() {
  const x = await api("/api/costs/executions");
  $("#exec-summary").innerHTML = x.rows.length ? costSummaryHtml(x.summary, state.base) : `<p class="muted small">No executions in the Gateway's recent window. Set up Flex (right) for your full history.</p>`;
  table($("#exec-symbols"), symCols, x.summary.by_symbol);
}
async function loadFlex(refresh = false) {
  const btn = $("#flex-refresh"), body = $("#flex-body");
  try {
    btn.disabled = refresh;
    if (refresh) body.textContent = "Requesting statement from IBKR (can take up to a minute)…";
    const f = refresh ? await post("/api/costs/flex/refresh") : await api("/api/costs/flex");
    if (!f.configured) {
      btn.hidden = true;
      body.innerHTML = `Not configured. In Client Portal: <em>Performance &amp; Reports → Flex Queries</em>, create an Activity query with <em>Trades</em> and <em>Cash Transactions</em>, enable Flex Web Service, then set <code>IB_FLEX_TOKEN</code> and <code>IB_FLEX_QUERY_ID</code> in <code>.env</code>.`;
      return;
    }
    if (!f.data) { body.textContent = "No statement downloaded yet. Click Refresh."; return; }
    const d = f.data;
    body.innerHTML = `Statement ${esc(d.first || "?")} → ${esc(d.last || "?")}, downloaded ${esc(d.as_of)}` + costSummaryHtml(d.summary, state.base);
    const months = d.summary.by_month;
    draw("flex-chart", {
      type: "bar",
      data: { labels: months.map((m) => `${m.key.slice(0, 4)}-${m.key.slice(4)}`), datasets: [{ label: `Commission (${state.base})`, data: months.map((m) => m.commission), backgroundColor: color(0), borderRadius: 4, maxBarThickness: 28 }] },
      options: { maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { grid: { color: css("--grid") } } } },
    });
    table($("#flex-symbols"), symCols, d.summary.by_symbol.slice(0, 20));
    table($("#flex-fees"), [{ h: "Other fees & interest", l: 1, f: (r) => esc(r.type) }, { h: `Amount (${state.base})`, f: (r) => fmt(r.amount) }], d.fees);
  } catch (e) { body.textContent = `Flex error: ${e.message}`; }
  finally { btn.disabled = false; }
}

// ---------- wiring ----------
const loaders = {
  portfolio: loadPortfolio,
  metals: async () => { await loadGroups(); await loadQuotes(); },
  trade: async () => { await loadGroups(); await loadTrade(); },
  costs: async () => { await loadGroups(); await loadEstimates(); },
  log: async () => {},
};
let loadedOnce = new Set();

async function refresh() {
  const connected = await loadStatus();
  if (state.tab === "trade") ticketNote();
  if (!connected) return;
  try { await loaders[state.tab](); } catch (e) { banner(e.message); }
  if (!loadedOnce.has(state.tab)) {
    loadedOnce.add(state.tab);
    if (state.tab === "metals") { loadMetalChart(); loadTracking(); }
    if (state.tab === "costs") { loadExecutions().catch(() => {}); loadFlex(); }
  }
}

function switchSession(s) {
  if (s === state.session) return;
  state.session = s;
  store.set("ibdash.session", s);
  loadedOnce = new Set();
  state.book = null; state.preview = null;
  $("#preview").hidden = true;
  $("#t-instrument").innerHTML = "";
  refresh();
}

function init() {
  chartDefaults();
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
    state.tab = b.dataset.tab;
    store.set("ibdash.tab", state.tab);
    refresh();
  }));
  $$("#session-switch button").forEach((b) => b.addEventListener("click", () => switchSession(b.dataset.s)));
  $$("#metal-range button").forEach((b) => b.addEventListener("click", () => {
    $$("#metal-range button").forEach((x) => x.classList.toggle("active", x === b));
    state.metalRange = b.dataset.r;
    loadMetalChart(); loadTracking();
  }));
  $("#metal-view").addEventListener("change", (e) => { state.metalView = e.target.value; loadQuotes().catch(() => {}); loadMetalChart(); });
  let t;
  $("#target").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => loadEstimates().catch((e) => banner(e.message)), 400); });
  $("#cost-group").addEventListener("change", () => loadEstimates().catch((e) => banner(e.message)));
  $("#flex-refresh").addEventListener("click", () => loadFlex(true));

  // trade ticket
  $("#t-instrument").addEventListener("change", (e) => {
    $("#t-custom").hidden = e.target.value !== "custom";
    $("#t-price").value = ""; $("#preview").hidden = true; state.book = null;
    loadBook().then(() => { const b = state.book; if (b) $("#t-price").value = b.bid && b.ask ? ((b.bid + b.ask) / 2).toFixed(4) * 1 : b.last || b.close || ""; updateValue(); });
  });
  $("#t-lookup").addEventListener("click", () => loadBook());
  $$("#t-side button").forEach((b) => b.addEventListener("click", () => {
    $$("#t-side button").forEach((x) => x.classList.toggle("active", x === b));
    state.side = b.dataset.v; $("#preview").hidden = true;
  }));
  $$("#t-fill button").forEach((b) => b.addEventListener("click", () => {
    const k = state.book; if (!k) return;
    const v = { bid: k.bid, ask: k.ask, last: k.last || k.close, mid: k.bid && k.ask ? (k.bid + k.ask) / 2 : null }[b.dataset.f];
    if (v) { $("#t-price").value = Number(v.toFixed(4)); updateValue(); }
  }));
  ["#t-qty", "#t-price"].forEach((s) => $(s).addEventListener("input", () => { updateValue(); $("#preview").hidden = true; }));
  $("#t-preview").addEventListener("click", previewOrder);
  $("#t-submit").addEventListener("click", submitOrder);
  $("#t-cancel-preview").addEventListener("click", () => { clearInterval(state.previewTimer); $("#preview").hidden = true; });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());

  const saved = store.get("ibdash.tab");
  const btn = saved && document.querySelector(`.tabs button[data-tab="${saved}"]`);
  if (btn) btn.click(); else refresh();
  setInterval(refresh, 3000);
}

document.addEventListener("DOMContentLoaded", init);
