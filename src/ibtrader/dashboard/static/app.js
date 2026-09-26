"use strict";

// ---------- helpers ----------
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
const color = (i) => css(SERIES[i % SERIES.length]);

function fmt(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}
const money = (x, ccy, d = 0) => (x === null || x === undefined ? "–" : `${fmt(x, d)} ${ccy || ""}`.trim());
const pct = (x, d = 2) => (x === null || x === undefined ? "–" : `${x > 0 ? "+" : ""}${fmt(x, d)}%`);
const signCls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Requested-With": "ibdash", ...(opts.headers || {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}

function table(el, cols, rows, totalRow) {
  const head = `<thead><tr>${cols.map((c) => `<th class="${c.l ? "l" : ""}">${esc(c.h)}</th>`).join("")}</tr></thead>`;
  const tr = (r, cls = "") => `<tr class="${cls}">${cols.map((c) => {
    const v = c.f(r);
    return `<td class="${c.l ? "l" : ""} ${c.cls ? c.cls(r) : ""}">${v}</td>`;
  }).join("")}</tr>`;
  const body = rows.length ? rows.map((r) => tr(r)).join("") : `<tr><td colspan="${cols.length}" class="muted l">No data</td></tr>`;
  el.innerHTML = `${head}<tbody>${body}${totalRow ? tr(totalRow, "total") : ""}</tbody>`;
}

function banner(msg) {
  const b = $("#banner");
  b.hidden = !msg;
  b.textContent = msg || "";
}

// ---------- charts ----------
const charts = {};
function chartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--grid");
  Chart.defaults.plugins.tooltip.backgroundColor = css("--surface");
  Chart.defaults.plugins.tooltip.titleColor = css("--ink");
  Chart.defaults.plugins.tooltip.bodyColor = css("--ink-2");
  Chart.defaults.plugins.tooltip.borderColor = css("--border");
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.legend.labels.color = css("--ink-2");
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
}
function draw(id, config) {
  if (!window.Chart) return;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), config);
}
function hbar(id, labels, values, fmtFn, barColor) {
  draw(id, {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: barColor || color(0), borderRadius: 4, borderSkipped: "start", maxBarThickness: 18 }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmtFn(c.parsed.x) } } },
      scales: { x: { grid: { color: css("--grid") }, ticks: { callback: (v) => fmtFn(v) } }, y: { grid: { display: false } } },
    },
  });
}

// ---------- state ----------
const state = { tab: "portfolio", base: "EUR", goldRange: "1Y", goldView: "__compare__", quotes: [], keys: [] };

// ---------- status ----------
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.base = s.base_currency || state.base;
    document.querySelectorAll(".ccy").forEach((e) => (e.textContent = state.base));
    const el = $("#status");
    el.className = `status ${s.connected ? "ok" : "bad"}`;
    const dataType = { 1: "live data", 2: "frozen", 3: "delayed*", 4: "delayed frozen" }[s.market_data_type] || "";
    $("#status-text").innerHTML = s.connected
      ? `Connected · <span class="pill ${s.mode === "live" ? "live" : ""}">${esc(s.mode.toUpperCase())}</span> <span class="pill">READ-ONLY</span> ${esc(s.account)} · ${esc(dataType)}`
      : `Disconnected`;
    banner(s.connected ? "" : `Not connected to IB Gateway (${s.host}). ${s.connect_error || "Retrying…"}`);
    $("#conn").innerHTML = [
      ["Status", s.connected ? "Connected" : "Disconnected"], ["Gateway", s.host], ["Mode", s.mode],
      ["Orders", "Disabled in dashboard (read-only connection)"], ["Account", s.account], ["Base currency", s.base_currency],
      ["Market data", `${dataType} (*delayed mode returns live data where you have a subscription)`],
      ["Flex history", s.flex_configured ? "Configured" : "Not configured (see README)"], ["Last error", s.connect_error || "–"],
    ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    table($("#errors"), [
      { h: "Time", l: 1, f: (r) => esc(r.time.slice(11)) }, { h: "Code", f: (r) => esc(r.code ?? "") },
      { h: "Symbol", l: 1, f: (r) => esc(r.symbol ?? "") }, { h: "Message", l: 1, f: (r) => esc(r.msg) },
    ], s.errors);
    return s.connected;
  } catch (e) {
    $("#status").className = "status bad";
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
  const tile = (label, value, sub = "", cls = "") =>
    `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div><div class="sub">${sub}</div></div>`;
  $("#kpis").innerHTML = [
    tile("Net liquidation", money(s.NetLiquidation, b)),
    tile("Today's P&L", money(p.pnl.daily, b), "", signCls(p.pnl.daily)),
    tile("Unrealized P&L", money(p.pnl.unrealized ?? s.UnrealizedPnL, b), "", signCls(p.pnl.unrealized ?? s.UnrealizedPnL)),
    tile("Realized P&L (today)", money(p.pnl.realized ?? s.RealizedPnL, b), "", signCls(p.pnl.realized ?? s.RealizedPnL)),
    tile("Cash", money(s.TotalCashValue, b)),
    tile("Invested", money(s.GrossPositionValue, b)),
    tile("Buying power", money(s.BuyingPower, b)),
    tile("Maint. margin", money(s.MaintMarginReq, b)),
  ].join("");

  table($("#positions"), [
    { h: "Instrument", l: 1, f: (r) => `<strong>${esc(r.local_symbol)}</strong> <span class="tag">${esc(r.sec_type)}</span>` },
    { h: "Qty", f: (r) => fmt(r.position, r.position % 1 ? 2 : 0) },
    { h: "Avg cost", f: (r) => fmt(r.avg_cost) },
    { h: "Price", f: (r) => fmt(r.market_price) },
    { h: "Value", f: (r) => money(r.market_value, r.currency) },
    { h: "Unrl. P&L", f: (r) => fmt(r.unrealized_pnl), cls: (r) => signCls(r.unrealized_pnl) },
    { h: "Unrl. %", f: (r) => pct(r.unrealized_pct), cls: (r) => signCls(r.unrealized_pct) },
    { h: "Weight", f: (r) => (r.weight == null ? "–" : `${fmt(r.weight, 1)}%`) },
  ], p.positions);
  $("#pos-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;

  table($("#cash"), [
    { h: "Currency", l: 1, f: (r) => esc(r.currency) },
    { h: "Amount", f: (r) => fmt(r.amount) },
    { h: `In ${b}`, f: (r) => fmt(r.amount_base) },
  ], p.cash);

  const nlv = s.NetLiquidation || 0;
  const items = p.positions.filter((r) => r.weight != null).map((r) => [r.local_symbol, r.weight]);
  if (nlv && s.TotalCashValue != null) items.push(["Cash", (s.TotalCashValue / nlv) * 100]);
  hbar("alloc-chart", items.map((i) => i[0]), items.map((i) => i[1]), (v) => `${fmt(v, 1)}%`);
}

// ---------- gold ----------
async function loadQuotes() {
  const g = await api("/api/gold/quotes");
  state.quotes = g.quotes;
  if (!state.keys.length) {
    state.keys = g.quotes.map((q) => q.key);
    const sel = $("#gold-view");
    g.quotes.forEach((q) => sel.insertAdjacentHTML("beforeend", `<option value="${esc(q.key)}">${esc(q.key)}: ${esc(q.name)}</option>`));
  }
  const b = g.base_currency || state.base;
  const dtype = { 1: "live", 2: "frozen", 3: "delayed", 4: "delayed/frozen" };
  $("#quotes").innerHTML = g.quotes.map((q, i) => {
    const rows = [
      ["Bid / Ask", q.bid && q.ask ? `${fmt(q.bid)} / ${fmt(q.ask)}` : "–"],
      ["Spread", q.spread_bps == null ? "–" : `${fmt(q.spread_bps, 1)} bps${q.spread_source === "assumed" ? " (assumed)" : ""}`],
      [`Unit value (${b})`, money(q.notional_unit_base, "", 2)],
      ["Gold per unit", q.oz_per_unit == null ? "–" : `${fmt(q.oz_per_unit, 4)} oz`],
    ];
    if (q.kind === "future") rows.push(["Contract", `${esc(q.local_symbol)} (${q.days_to_expiry ?? "?"}d)`], ["Carry vs spot", q.carry_annual_pct == null ? "–" : `${pct(q.carry_annual_pct)} /yr`]);
    return `<div class="quote ${state.goldView === q.key ? "selected" : ""}" data-key="${esc(q.key)}">
      <div class="top"><span class="sym"><span class="swatch" data-i="${i}"></span>${esc(q.key)}</span>
        <span class="tag ${q.price_source === "mid" || q.price_source === "last" ? "" : "warn"}">${esc(
          !q.price ? "no data: check subscription" : q.price_source === "history" ? `close ${q.history_date || ""}` : q.price_source === "close" ? "prev close" : dtype[q.market_data_type] || "")}</span></div>
      <div class="name" title="${esc(q.name)}">${esc(q.name)}</div>
      <div><span class="px">${fmt(q.price)}</span> <span class="muted">${esc(q.currency)}</span>
        <span class="${signCls(q.change_pct)}">${pct(q.change_pct)}</span></div>
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl></div>`;
  }).join("");
  document.querySelectorAll(".swatch").forEach((s) => (s.style.background = color(Number(s.dataset.i))));
  document.querySelectorAll(".quote").forEach((el) => el.addEventListener("click", () => {
    const k = el.dataset.key;
    state.goldView = state.goldView === k ? "__compare__" : k;
    $("#gold-view").value = state.goldView;
    loadQuotes().catch(() => {});
    loadGoldChart();
  }));
}

async function loadGoldChart() {
  const range = state.goldRange;
  const note = $("#gold-chart-note");
  note.textContent = "Loading historical data from IB…";
  try {
    if (state.goldView === "__compare__") {
      $("#gold-chart-title").textContent = "Gold products, indexed to 100";
      const results = [];
      for (const k of state.keys) { // sequential: IB paces historical requests
        try { results.push([k, (await api(`/api/gold/history?key=${encodeURIComponent(k)}&range=${range}`)).bars]); }
        catch (e) { results.push([k, []]); }
      }
      const datasets = results.map(([k, bars], i) => {
        const base = bars.length ? bars[0].c : 1;
        return {
          label: k, data: bars.map((b) => ({ x: b.t, y: (b.c / base) * 100 })),
          borderColor: color(state.keys.indexOf(k)), backgroundColor: color(state.keys.indexOf(k)),
          borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0,
        };
      }).filter((d) => d.data.length);
      lineChart(datasets, (v) => fmt(v, 1), true);
      note.textContent = "Each line rebased to 100 at the start of the window (own currency; 4GLD is in EUR). Futures series are continuous front-month (roll jumps possible).";
    } else {
      const k = state.goldView;
      const q = state.quotes.find((x) => x.key === k) || {};
      $("#gold-chart-title").textContent = `${k}: ${q.name || ""} (${q.currency || ""})`;
      const bars = (await api(`/api/gold/history?key=${encodeURIComponent(k)}&range=${range}`)).bars;
      const i = state.keys.indexOf(k);
      lineChart([{ label: k, data: bars.map((b) => ({ x: b.t, y: b.c })), borderColor: color(i), backgroundColor: color(i), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 }], (v) => fmt(v, 2), false);
      const first = bars[0]?.c, last = bars[bars.length - 1]?.c;
      note.textContent = first ? `Change over ${range}: ${pct((last / first - 1) * 100)}` : "";
    }
  } catch (e) {
    note.textContent = `Could not load history: ${e.message}`;
  }
}

function lineChart(datasets, tickFmt, legend) {
  draw("gold-chart", {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false, animation: false, parsing: true,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: legend, position: "top", align: "start" }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${tickFmt(c.parsed.y)}` } } },
      scales: {
        x: { type: "category", labels: [...new Set(datasets.flatMap((d) => d.data.map((p) => p.x)))].sort(), ticks: { maxTicksLimit: 8, callback: function (v) { return String(this.getLabelForValue(v)).slice(0, 10); } }, grid: { display: false } },
        y: { grid: { color: css("--grid") }, ticks: { callback: tickFmt } },
      },
    },
  });
}

async function loadTracking() {
  const range = ["5D", "1M"].includes(state.goldRange) ? "1Y" : state.goldRange;
  $("#tracking-range").textContent = `window: ${range}`;
  try {
    const t = await api(`/api/gold/tracking?range=${range}`);
    table($("#tracking"), [
      { h: "Product", l: 1, f: (r) => `<strong>${esc(r.key)}</strong> <span class="muted">${esc(r.name || "")}</span>` },
      { h: "Period", f: (r) => (r.error ? esc(r.error) : `${esc(r.from)} → ${esc(r.to)}`) },
      { h: "Product return", f: (r) => pct(r.product_return_pct) },
      { h: "Gold return", f: (r) => pct(r.gold_return_pct) },
      { h: "Difference", f: (r) => pct(r.tracking_diff_pct), cls: (r) => signCls(r.tracking_diff_pct) },
      { h: "Diff. per year", f: (r) => pct(r.tracking_diff_annual_pct), cls: (r) => signCls(r.tracking_diff_annual_pct) },
      { h: "Stated fee", f: (r) => `${fmt(r.expense_ratio_pct, 2)}%` },
    ], t.rows);
  } catch (e) {
    $("#tracking").innerHTML = `<tr><td class="muted l">${esc(e.message)}</td></tr>`;
  }
}

// ---------- costs ----------
async function loadEstimates() {
  const target = Number($("#target").value) || 10000;
  const e = await api(`/api/costs/estimates?target=${target}`);
  const b = e.base_currency || state.base;
  table($("#estimates"), [
    { h: "Product", l: 1, f: (r) => `<strong>${esc(r.key)}</strong> <span class="tag">${esc(r.kind)}</span>` },
    { h: "Units", f: (r) => (r.qty ? fmt(r.qty, 0) : `<span class="muted" title="${esc(r.note)}">0 ⓘ</span>`) },
    { h: `Exposure (${b})`, f: (r) => fmt(r.exposure_base, 0) },
    { h: "Commission (buy+sell)", f: (r) => fmt(r.commission_rt_base) },
    { h: "Spread", f: (r) => `${fmt(r.spread_rt_base)}${r.spread_source === "assumed" ? "*" : ""}` },
    { h: "Round trip", f: (r) => fmt(r.round_trip_base) },
    { h: "RT bps", f: (r) => fmt(r.round_trip_bps, 1) },
    { h: "Holding / yr", f: (r) => fmt(r.annual_hold_base) },
    { h: "Year-1 total", f: (r) => `<strong>${fmt(r.first_year_base)}</strong>` },
    { h: "Year-1 bps", f: (r) => (r.exposure_base ? fmt((r.first_year_base / r.exposure_base) * 1e4, 0) : "–") },
    { h: "Futures carry", f: (r) => (r.carry_annual_pct == null ? "–" : `${pct(r.carry_annual_pct)}/yr`) },
  ], e.rows);
  const rows = e.rows.filter((r) => r.qty).map((r) => [r.key, (r.first_year_base / r.exposure_base) * 1e4]).sort((a, b2) => a[1] - b2[1]);
  hbar("est-chart", rows.map((r) => r[0]), rows.map((r) => r[1]), (v) => `${fmt(v, 0)} bps`);
}

function costSummaryHtml(sum, b) {
  const t = sum.total;
  return `<div class="tiles">
    <div class="tile"><div class="label">Trades</div><div class="value">${fmt(t.trades, 0)}</div></div>
    <div class="tile"><div class="label">Traded value</div><div class="value">${money(t.notional, b)}</div></div>
    <div class="tile"><div class="label">Commissions</div><div class="value">${money(t.commission, b, 2)}</div></div>
    <div class="tile"><div class="label">Avg cost</div><div class="value">${t.bps == null ? "–" : `${fmt(t.bps, 1)} bps`}</div><div class="sub">commission / traded value</div></div>
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
  const btn = $("#flex-refresh");
  const body = $("#flex-body");
  try {
    btn.disabled = refresh;
    if (refresh) body.textContent = "Requesting statement from IBKR (can take up to a minute)…";
    const f = refresh ? await api("/api/costs/flex/refresh", { method: "POST" }) : await api("/api/costs/flex");
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
  } catch (e) {
    body.textContent = `Flex error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ---------- wiring ----------
const loaders = {
  portfolio: loadPortfolio,
  gold: loadQuotes,
  costs: loadEstimates,
  log: async () => {},
};
const loadedOnce = new Set();

async function refresh() {
  const connected = await loadStatus();
  if (!connected) return;
  try { await loaders[state.tab](); } catch (e) { banner(e.message); }
  if (!loadedOnce.has(state.tab)) {
    loadedOnce.add(state.tab);
    if (state.tab === "gold") { loadGoldChart(); loadTracking(); }
    if (state.tab === "costs") { loadExecutions().catch(() => {}); loadFlex(); }
  }
}

function init() {
  chartDefaults();
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
    state.tab = b.dataset.tab;
    try { localStorage.setItem("ibdash.tab", state.tab); } catch (e) { /* storage unavailable */ }
    refresh();
  }));
  document.querySelectorAll("#gold-range button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#gold-range button").forEach((x) => x.classList.toggle("active", x === b));
    state.goldRange = b.dataset.r;
    loadGoldChart();
    loadTracking();
  }));
  $("#gold-view").addEventListener("change", (e) => { state.goldView = e.target.value; loadQuotes().catch(() => {}); loadGoldChart(); });
  let t;
  $("#target").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => loadEstimates().catch((e) => banner(e.message)), 400); });
  $("#flex-refresh").addEventListener("click", () => loadFlex(true));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());

  let saved = null;
  try { saved = localStorage.getItem("ibdash.tab"); } catch (e) { /* ignore */ }
  const btn = saved && document.querySelector(`.tabs button[data-tab="${saved}"]`);
  if (btn) btn.click(); else refresh();
  setInterval(refresh, 5000);
}

document.addEventListener("DOMContentLoaded", init);
