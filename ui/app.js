// Formatters
const fmtMoney = (x) => x == null ? "—" : (Number(x) >= 0 ? "+" : "") + "$" + Math.abs(Number(x)).toFixed(2);
const fmt = (x, n = 2) => x == null ? "—" : Number(x).toFixed(n);
const clsPL = (x) => Number(x) >= 0 ? "positive" : "negative";
function cssId(s) { return s.replace(/[^a-zA-Z0-9]/g, "_"); }

// Per-event chart instances and rolling history.
// charts: Map<eventTicker, Chart>
// chartHistory: Map<eventTicker, {labels, datasets: {source: [values]}}>
const charts = new Map();
const chartHistory = new Map();
const MAX_CHART_POINTS = 120;

let latestStatus = null;
let paramsInitialized = false;

// ---------------------------------------------------------------------------
// Event section DOM creation
// ---------------------------------------------------------------------------

function createEventSection(et) {
  const id = cssId(et);
  const div = document.createElement("section");
  div.className = "event-section card wide";
  div.dataset.event = et;
  div.id = "event_" + id;

  div.innerHTML = `
    <div class="event-header">
      <div class="event-header-left">
        <span class="event-ticker-label">${et}</span>
        <span class="state-pill" id="state_${id}">—</span>
      </div>
      <div class="event-header-right">
        <label class="switch">
          <input type="checkbox" id="armed_${id}" onchange="onArmToggle('${et}', this.checked)" />
          <span>ARMED</span>
        </label>
        <button class="danger small" onclick="sellEvent('${et}')">Sell Event</button>
      </div>
    </div>
    <div class="event-body">
      <div class="event-price-row">
        <div>
          <div class="big" id="consensus_${id}">—</div>
          <div class="muted">consensus price</div>
        </div>
        <div id="readout_${id}" class="readout"></div>
      </div>
      <canvas id="chart_${id}" height="70" style="margin: 8px 0"></canvas>
      <div class="card-title" style="margin-top:10px">Positions</div>
      <table id="positions_${id}"></table>
      <div class="risk-row" id="risk_${id}"></div>
      <div style="margin-top:8px">
        <button class="small muted-btn" onclick="toggleSettlement('${et}')">Settlement Map ▾</button>
      </div>
      <div id="settlement_wrap_${id}" style="display:none;margin-top:6px">
        <table id="settlement_${id}"></table>
      </div>
    </div>
  `;
  return div;
}

function initEventChart(et) {
  const id = cssId(et);
  const canvas = document.getElementById("chart_" + id);
  if (!canvas || charts.has(et)) return;

  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Spot",           data: [], borderColor: "#2563eb", tension: 0.25, pointRadius: 0 },
        { label: "Yahoo",          data: [], borderColor: "#16a34a", tension: 0.25, pointRadius: 0 },
        { label: "Kalshi implied", data: [], borderColor: "#f97316", tension: 0.25, pointRadius: 0 },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      plugins: { legend: { labels: { color: "#e5e7eb", boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: "#94a3b8", maxTicksLimit: 6 }, grid: { color: "#1f2937" } },
        y: { ticks: { color: "#94a3b8" }, grid: { color: "#1f2937" } },
      },
    },
  });
  charts.set(et, chart);
  chartHistory.set(et, { labels: [], oilprice: [], yahoo: [], kalshi_implied: [] });
}

function updateEventChart(et, eventStatus) {
  if (!charts.has(et)) return;
  const chart = charts.get(et);
  const hist = chartHistory.get(et);

  const t = new Date(latestStatus.ts * 1000).toLocaleTimeString();
  hist.labels.push(t);

  const bySource = {};
  eventStatus.prices.forEach((p) => { bySource[p.source] = p.price; });
  hist.oilprice.push(bySource["oilprice"] ?? bySource["goldprice"] ?? null);
  hist.yahoo.push(bySource["yahoo"] ?? null);
  hist.kalshi_implied.push(bySource["kalshi_implied"] ?? null);

  // Trim to max points
  for (const key of ["labels", "oilprice", "yahoo", "kalshi_implied"]) {
    if (hist[key].length > MAX_CHART_POINTS) hist[key].shift();
  }

  chart.data.labels = hist.labels;
  chart.data.datasets[0].data = hist.oilprice;
  chart.data.datasets[1].data = hist.yahoo;
  chart.data.datasets[2].data = hist.kalshi_implied;

  // Sync visibility to global source checkboxes
  chart.data.datasets[0].hidden = !document.getElementById("src_oilprice")?.checked;
  chart.data.datasets[1].hidden = !document.getElementById("src_yahoo")?.checked;
  chart.data.datasets[2].hidden = !document.getElementById("src_kalshi_implied")?.checked;

  chart.update();
}

// ---------------------------------------------------------------------------
// Per-event rendering
// ---------------------------------------------------------------------------

function renderEventStatus(et, es) {
  const id = cssId(et);

  // State pill
  const pill = document.getElementById("state_" + id);
  if (pill) {
    pill.textContent = es.state;
    pill.className = "state-pill";
    if (["REGIME_BREAK", "FLATTENING"].includes(es.state)) pill.classList.add("danger");
    else if (es.state === "SHOCK_WATCH") pill.classList.add("warn");
    else if (["NORMAL", "RECOVERY"].includes(es.state)) pill.classList.add("ok");
  }

  // Armed toggle (avoid triggering onchange)
  const armedEl = document.getElementById("armed_" + id);
  if (armedEl && armedEl.checked !== es.armed) armedEl.checked = es.armed;

  // Consensus price
  const cp = document.getElementById("consensus_" + id);
  if (cp) cp.textContent = fmt(es.consensus_price, 2);

  // Price readout
  const readout = document.getElementById("readout_" + id);
  if (readout) {
    readout.innerHTML = es.prices.map((p) => {
      const stale = p.stale ? ` <span class="negative">STALE</span>` : "";
      const err = p.error ? ` <span class="negative">(${p.error.slice(0, 80)})</span>` : "";
      return `<span><b>${p.source}</b>: ${fmt(p.price, 2)}${stale}${err}</span>`;
    }).join("");
  }

  // Positions table
  const posTable = document.getElementById("positions_" + id);
  if (posTable) {
    let html = `<tr><th>Ticker</th><th>Side</th><th>Strike</th><th>Count</th><th>Avg</th><th>Bid</th><th>Mid</th><th>Peak</th><th>P/L</th><th></th></tr>`;
    es.positions.forEach((p) => {
      const pl = p.count * ((p.current_bid || p.current_mid) - p.avg_price);
      html += `<tr>
        <td>${p.ticker}</td>
        <td>${p.side.toUpperCase()}</td>
        <td>${fmt(p.strike)}</td>
        <td>${p.count}</td>
        <td>${fmt(p.avg_price, 3)}</td>
        <td>${fmt(p.current_bid, 2)}</td>
        <td>${fmt(p.current_mid, 2)}</td>
        <td>${fmt(p.peak_mid, 2)}</td>
        <td class="${clsPL(pl)}">${fmtMoney(pl)}</td>
        <td><button class="small" onclick="sellLeg('${p.ticker}')">Sell</button></td>
      </tr>`;
    });
    posTable.innerHTML = html;
  }

  // Risk summary row
  const r = es.risk;
  const riskEl = document.getElementById("risk_" + id);
  if (riskEl) {
    riskEl.innerHTML = `
      <span>Cost: <b>${fmtMoney(r.cost_basis)}</b></span>
      <span>Mark: <b>${fmtMoney(r.mark_value)}</b></span>
      <span>P/L: <b class="${clsPL(r.unrealized_pl)}">${fmtMoney(r.unrealized_pl)}</b></span>
      <span>Max profit: <b class="${clsPL(r.max_profit)}">${fmtMoney(r.max_profit)}</b></span>
      <span>Worst settle: <b class="${clsPL(r.worst_settlement_loss)}">${fmtMoney(r.worst_settlement_loss)}</b></span>
      <span>YES/NO: <b>${r.yes_count}/${r.no_count}</b></span>
    `;
  }

  // Settlement map (only re-render if visible)
  const settleWrap = document.getElementById("settlement_wrap_" + id);
  if (settleWrap && settleWrap.style.display !== "none") {
    renderSettlementMap(et, r.settlement_map);
  }

  updateEventChart(et, es);
}

function renderSettlementMap(et, rows) {
  const id = cssId(et);
  const tbl = document.getElementById("settlement_" + id);
  if (!tbl) return;
  let html = `<tr><th>Price</th><th>Settlement Value</th><th>P/L</th></tr>`;
  rows.forEach((r) => {
    html += `<tr><td>${fmt(r.price)}</td><td>${fmtMoney(r.settlement_value)}</td><td class="${clsPL(r.pl)}">${fmtMoney(r.pl)}</td></tr>`;
  });
  tbl.innerHTML = html;
}

function toggleSettlement(et) {
  const id = cssId(et);
  const wrap = document.getElementById("settlement_wrap_" + id);
  if (!wrap) return;
  const showing = wrap.style.display !== "none";
  wrap.style.display = showing ? "none" : "block";
  if (!showing && latestStatus) {
    const es = latestStatus.events[et];
    if (es) renderSettlementMap(et, es.risk.settlement_map);
  }
}

// ---------------------------------------------------------------------------
// Main render
// ---------------------------------------------------------------------------

function renderStatus(status) {
  latestStatus = status;

  // Total P/L across all events
  const totalPL = Object.values(status.events).reduce((sum, es) => sum + (es.risk?.unrealized_pl ?? 0), 0);
  const plEl = document.getElementById("headerPL");
  if (plEl) {
    plEl.textContent = fmtMoney(totalPL);
    plEl.className = "big " + clsPL(totalPL);
    plEl.style.fontSize = "20px";
  }

  // Mode badge
  const badge = document.getElementById("modeBadge");
  badge.textContent = status.mode.toUpperCase();
  badge.className = "mode-badge " + status.mode;
  document.getElementById("modeDisplay").textContent = status.mode.toUpperCase();

  // Sync/create event sections
  const container = document.getElementById("eventsContainer");
  const currentEvents = new Set(
    [...container.querySelectorAll(".event-section")].map((el) => el.dataset.event)
  );

  for (const [et, es] of Object.entries(status.events)) {
    if (!currentEvents.has(et)) {
      container.appendChild(createEventSection(et));
      // Chart must init after the DOM element exists
      setTimeout(() => initEventChart(et), 0);
    }
    currentEvents.delete(et);
    renderEventStatus(et, es);
  }

  // Remove sections for events that are no longer in the portfolio
  for (const et of currentEvents) {
    const el = document.getElementById("event_" + cssId(et));
    if (el) el.remove();
    if (charts.has(et)) {
      charts.get(et).destroy();
      charts.delete(et);
      chartHistory.delete(et);
    }
  }

  // Global params (init once)
  if (!paramsInitialized) {
    document.getElementById("pollSeconds").value = status.params.poll_seconds;
    document.getElementById("shockThreshold").value = status.params.shock_logic.shock_threshold_pct;
    document.getElementById("minRecovery").value = status.params.shock_logic.min_recovery_pct;
    document.getElementById("armAt").value = status.params.profit_harvest.arm_at;
    document.getElementById("firstTrim").value = status.params.profit_harvest.first_trim_at;
    document.getElementById("trailAfter90").value = status.params.profit_harvest.trail_after_90;
    document.getElementById("drawdownLimit").value = status.params.safety?.global_drawdown_limit ?? -100;

    ["oilprice", "yahoo", "kalshi_implied"].forEach((name) => {
      const el = document.getElementById("src_" + name);
      if (el) el.checked = !!status.params.sources[name]?.enabled;
    });
    paramsInitialized = true;
  }

  renderActions(status.actions);
  renderLogs(status.logs);
}

function renderActions(actions) {
  document.getElementById("actions").innerHTML = actions.slice(0, 20).map((a) => `
    <div class="item">
      <div class="${a.severity}">
        <b>${a.action}</b>
        ${a.event_ticker ? `<span class="muted">[${a.event_ticker}]</span>` : ""}
        ${a.ticker ? a.ticker.split("-T")[1] || "" : ""}
        ${a.qty ? "×" + a.qty : ""}
      </div>
      <div>${a.reason}</div>
      <div class="muted">${new Date(a.ts * 1000).toLocaleTimeString()}</div>
    </div>
  `).join("");
}

function renderLogs(logs) {
  document.getElementById("logs").innerHTML = logs.slice(0, 30).map(
    (x) => `<div class="item muted">${x}</div>`
  ).join("");
}

// ---------------------------------------------------------------------------
// User actions
// ---------------------------------------------------------------------------

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) alert(await r.text());
  return r.json();
}

async function onArmToggle(eventTicker, armed) {
  await postJSON("/api/arm_event", { event_ticker: eventTicker, armed });
  await refresh();
}

async function sellEvent(eventTicker) {
  if (!confirm(`Confirm: flatten ALL positions in ${eventTicker}?`)) return;
  await postJSON("/api/sell_event", { event_ticker: eventTicker, confirm: true });
  await refresh();
}

async function sellAll() {
  if (!confirm("Confirm: flatten ALL positions in ALL events?")) return;
  await postJSON("/api/sell_all", { confirm: true });
  await refresh();
}

async function sellLeg(ticker) {
  if (!confirm("Confirm sell leg: " + ticker + "?")) return;
  await postJSON("/api/sell_leg", { ticker, confirm: true });
  await refresh();
}

async function saveParams() {
  const sources = {};
  ["oilprice", "yahoo", "kalshi_implied"].forEach((name) => {
    const src = latestStatus?.params.sources[name] || {};
    sources[name] = { ...src, enabled: document.getElementById("src_" + name).checked };
  });

  await postJSON("/api/params", {
    poll_seconds: Number(document.getElementById("pollSeconds").value),
    sources,
    shock_logic: {
      shock_threshold_pct: Number(document.getElementById("shockThreshold").value),
      min_recovery_pct: Number(document.getElementById("minRecovery").value),
    },
    profit_harvest: {
      arm_at: Number(document.getElementById("armAt").value),
      first_trim_at: Number(document.getElementById("firstTrim").value),
      trail_after_90: Number(document.getElementById("trailAfter90").value),
    },
    safety: {
      global_drawdown_limit: Number(document.getElementById("drawdownLimit").value),
    },
  });
  await refresh();
}

async function setMode(mode) {
  const msg = mode === "live"
    ? "Switch to LIVE mode? Real orders will be sent when events are armed."
    : "Switch to PAPER mode?";
  if (!confirm(msg)) return;
  await postJSON("/api/params", { mode });
  await refresh();
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function refresh() {
  const r = await fetch("/api/status");
  renderStatus(await r.json());
}

document.getElementById("saveParamsBtn").addEventListener("click", saveParams);
document.getElementById("sellAllBtn").addEventListener("click", sellAll);
document.getElementById("setPaperBtn").addEventListener("click", () => setMode("paper"));
document.getElementById("setLiveBtn").addEventListener("click", () => setMode("live"));

["src_oilprice", "src_yahoo", "src_kalshi_implied"].forEach((id) => {
  document.getElementById(id).addEventListener("change", saveParams);
});

refresh();
setInterval(refresh, 3000);
