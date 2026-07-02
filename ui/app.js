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
// Stores bot_config per ticker so openBotPanel can look it up safely
const botConfigStore = new Map();

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
        <button class="small muted-btn" id="dismiss_${id}" style="display:none" onclick="dismissEvent('${et}')">Dismiss</button>
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
      const stale   = p.stale   ? ` <span class="negative">STALE</span>`   : "";
      const outlier = p.outlier ? ` <span class="negative">OUTLIER</span>` : "";
      const err     = p.error   ? ` <span class="negative">(${p.error.slice(0, 80)})</span>` : "";
      const cls     = p.outlier ? " style=\"opacity:0.45\"" : "";
      return `<span${cls}><b>${p.source}</b>: ${fmt(p.price, 2)}${stale}${outlier}${err}</span>`;
    }).join("");
  }

  // Dismiss button: show only for SETTLED events
  const dismissBtn = document.getElementById("dismiss_" + id);
  if (dismissBtn) dismissBtn.style.display = es.state === "SETTLED" ? "" : "none";

  // Positions table (open + today's closed)
  const posTable = document.getElementById("positions_" + id);
  if (posTable) {
    let html = `<tr><th>Ticker</th><th>Side</th><th>Strike</th><th>Qty</th><th>Avg</th><th>Exit</th><th>Bid</th><th>Mid</th><th>P/L</th><th colspan="3">Sell</th><th>Bot</th></tr>`;
    es.positions.forEach((p) => {
      if (p.closed) {
        const pl = p.closed_pl;
        html += `<tr style="opacity:0.45">
          <td>${p.ticker}</td>
          <td>${p.side.toUpperCase()}</td>
          <td>${fmt(p.strike)}</td>
          <td><span style="color:#94a3b8;font-size:11px">CLOSED</span></td>
          <td>${fmt(p.avg_price, 3)}</td>
          <td>${fmt(p.exit_price, 3)}</td>
          <td>—</td>
          <td>—</td>
          <td class="${clsPL(pl)}">${fmtMoney(pl)}</td>
          <td colspan="4"></td>
        </tr>`;
      } else {
        const pl = p.count * ((p.current_bid || p.current_mid) - p.avg_price);
        const midCents = Math.round((p.current_bid || p.current_mid) * 100);
        const hasBot = !!p.bot_config;
        const botLabel = hasBot ? "Bot ✓" : "Bot";
        const botCls = hasBot ? "small bot-active" : "small muted-btn";
        const safeTicker = p.ticker.replace(/'/g, "\'");
        const safeEvent = et.replace(/'/g, "\'");
        // Store config in map so onclick doesn't need inline JSON
        botConfigStore.set(p.ticker, p.bot_config || null);
        html += `<tr>
          <td>${p.ticker}</td>
          <td>${p.side.toUpperCase()}</td>
          <td>${fmt(p.strike)}</td>
          <td>${p.count}</td>
          <td>${fmt(p.avg_price, 3)}</td>
          <td>—</td>
          <td>${fmt(p.current_bid, 2)}</td>
          <td>${fmt(p.current_mid, 2)}</td>
          <td class="${clsPL(pl)}">${fmtMoney(pl)}</td>
          <td><button class="small danger" title="Sell immediately at any price (IOC)" onclick="sellLeg('${safeTicker}')">Now</button></td>
          <td><button class="small muted-btn" title="Place a day limit order at a specific price" onclick="sellLegLimit('${safeTicker}', ${midCents})">Limit</button></td>
          <td><button class="small muted-btn" title="Auto-reprice 1¢ below best ask every 30s until filled" onclick="sellLegAdjusted('${safeTicker}')">Auto</button></td>
          <td><button class="${botCls}" onclick="openBotPanel('${safeEvent}', '${safeTicker}')">${botLabel}</button></td>
        </tr>
        <tr id="botpanel_${cssId(p.ticker)}" style="display:none">
          <td colspan="13" style="padding:0">
            <div class="bot-panel">
              <div class="bot-panel-title">⚙ Position Bot: ${p.ticker}</div>
              <div class="bot-panel-row">
                <label><input type="checkbox" id="bp_stoploss_on_${cssId(p.ticker)}"> Stop loss
                <input type="number" id="bp_stoploss_${cssId(p.ticker)}" step="0.01" min="0.01" max="0.99" placeholder="e.g. 0.45 or 45" style="width:80px">
		</label>
                <label>Strike cushion $<input type="number" step="0.25" min="0" max="10" id="bp_cushion_${cssId(p.ticker)}" placeholder="e.g. 1.00" style="width:70px"></label>
                <span class="muted" style="font-size:11px">Sell immediately if price drops to or below this (dollars or cents)</span>
              </div>
              <div class="bot-panel-row">
                <label><input type="checkbox" id="bp_limit_on_${cssId(p.ticker)}"> Limit sell</label>
                <input type="number" id="bp_limit_${cssId(p.ticker)}" step="0.01" min="0.01" max="0.99" placeholder="e.g. 0.75 or 75" style="width:80px">
                <span class="muted" style="font-size:11px">Sell when price reaches this target (dollars or cents)</span>
              </div>
              <div class="bot-panel-row">
                <label><input type="checkbox" id="bp_harvest_${cssId(p.ticker)}"> Momentum harvest</label>
                <span class="muted" style="font-size:11px">Sell on reversal after peak (noise-resistant)</span>
                <label style="margin-left:12px">Sensitivity
                  <input type="number" id="bp_sensitivity_${cssId(p.ticker)}" step="1" min="1" max="10" value="3" style="width:50px">
                </label>
              </div>
              <div class="bot-panel-row">
                <label><input type="checkbox" id="bp_timeexit_on_${cssId(p.ticker)}"> Time exit</label>
                <input type="time" id="bp_timeexit_${cssId(p.ticker)}" style="width:110px">
                <span class="muted" style="font-size:11px">Sell at this time regardless of price</span>
              </div>
              <div class="bot-panel-actions">
                <button class="small" onclick="saveBotPanel('${safeEvent}', '${safeTicker}')">Save Bot</button>
                <button class="small muted-btn" onclick="clearBotPanel('${safeEvent}', '${safeTicker}')">Clear Bot</button>
                <button class="small muted-btn" onclick="closeBotPanel('${safeTicker}')">Cancel</button>
              </div>
            </div>
          </td>
        </tr>`;
      }
    });
    // Before re-rendering, snapshot which panels are open AND their current
    // field values so we can restore without overwriting in-progress edits.
    const openPanelSnapshots = new Map(); // panelId -> { ticker, fieldValues }
    posTable.querySelectorAll('[id^="botpanel_"]').forEach(el => {
      if (el.style.display === 'none') return;
      const panelId = el.id;
      // Find matching ticker
      let matchTicker = null;
      for (const [t] of botConfigStore.entries()) {
        if (cssId(t) === panelId.replace('botpanel_', '')) { matchTicker = t; break; }
      }
      if (!matchTicker) return;
      const tid = cssId(matchTicker);
      // Snapshot current field values as-typed by user
      const snap = {
        ticker: matchTicker,
        stoploss_on:   document.getElementById('bp_stoploss_on_'  + tid)?.checked,
        stoploss:      document.getElementById('bp_stoploss_'     + tid)?.value,
        limit_on:      document.getElementById('bp_limit_on_'     + tid)?.checked,
        limit:         document.getElementById('bp_limit_'        + tid)?.value,
        harvest:       document.getElementById('bp_harvest_'      + tid)?.checked,
        sensitivity:   document.getElementById('bp_sensitivity_'  + tid)?.value,
        timeexit_on:   document.getElementById('bp_timeexit_on_'  + tid)?.checked,
        timeexit:      document.getElementById('bp_timeexit_'     + tid)?.value,
      };
      openPanelSnapshots.set(panelId, snap);
    });

    posTable.innerHTML = html;

    // Restore open panels with user's in-progress values (not stored config)
    openPanelSnapshots.forEach((snap, panelId) => {
      const el = document.getElementById(panelId);
      if (!el) return;
      el.style.display = '';
      const tid = cssId(snap.ticker);
      const setC = (id, v) => { const e = document.getElementById(id); if (e && v != null) e.checked = v; };
      const setV = (id, v) => { const e = document.getElementById(id); if (e && v != null) e.value  = v; };
      setC('bp_stoploss_on_'  + tid, snap.stoploss_on);
      setV('bp_stoploss_'     + tid, snap.stoploss);
      setC('bp_limit_on_'     + tid, snap.limit_on);
      setV('bp_limit_'        + tid, snap.limit);
      setC('bp_harvest_'      + tid, snap.harvest);
      setV('bp_sensitivity_'  + tid, snap.sensitivity);
      setC('bp_timeexit_on_'  + tid, snap.timeexit_on);
      setV('bp_timeexit_'     + tid, snap.timeexit);
    });
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

  // Session scoreboard
  const s = status.session || {};
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set("sbStart",      s.start_balance != null ? "$" + s.start_balance.toFixed(2) : "—");
  set("sbCash",       s.current_balance != null ? "$" + s.current_balance.toFixed(2) : "—");
  const rlzEl = document.getElementById("sbRealized");
  if (rlzEl) { rlzEl.textContent = fmtMoney(s.realized_pl); rlzEl.className = clsPL(s.realized_pl); }
  const urlzEl = document.getElementById("sbUnrealized");
  if (urlzEl) { urlzEl.textContent = fmtMoney(s.unrealized_pl); urlzEl.className = clsPL(s.unrealized_pl); }
  const netEl = document.getElementById("sbNet");
  if (netEl) { netEl.textContent = fmtMoney(s.net_pl); netEl.className = clsPL(s.net_pl); }
  set("sbPct", s.net_pct != null ? `(${s.net_pct > 0 ? "+" : ""}${s.net_pct.toFixed(1)}%)` : "");

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

    document.getElementById("maxDeployPct").value = status.params.max_deploy_pct ?? 0.50;
    document.getElementById("entryZoneMin").value = status.params.entry_zone?.min ?? 0.70;
    document.getElementById("entryZoneMax").value = status.params.entry_zone?.max ?? 0.80;

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
  document.getElementById("actions").innerHTML = actions.slice(0, 30).map((a) => `
    <div class="item">
      <div class="${a.severity}">
        <b>${a.action}</b>
        ${a.event_ticker ? `<span class="muted">[${a.event_ticker}]</span>` : ""}
        ${a.ticker ? a.ticker.split("-T")[1] || "" : ""}
        ${a.qty ? "×" + a.qty : ""}
      </div>
      <div>${a.reason}</div>
      ${a.headline ? `
        <div class="news-headline">
          ${a.url
            ? `<a href="${a.url}" target="_blank" class="news-link">📰 ${a.headline}</a>`
            : `<span class="news-text">📰 ${a.headline}</span>`
          }
          ${a.source ? `<span class="muted news-source">[${a.source}]</span>` : ""}
        </div>` : ""}
      <div class="muted">${new Date(a.ts * 1000).toLocaleTimeString()}</div>
    </div>
  `).join("");
}

function renderLogs(logs) {
  document.getElementById("logs").innerHTML = logs.slice(0, 50).map(
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
  if (!confirm(`Confirm: sell ${ticker} immediately at market price (IOC)?`)) return;
  await postJSON("/api/sell_leg", { ticker, confirm: true });
  await refresh();
}

async function sellLegLimit(ticker, suggestedCents) {
  const cents = prompt(
    `Limit sell ${ticker}\nEnter your limit price in CENTS (1–99):\n(Current mid ~${suggestedCents}¢)`,
    suggestedCents
  );
  if (cents === null) return;
  if (!confirm(`Place day limit sell: ${ticker} @ ${cents}¢?`)) return;
  const r = await postJSON("/api/sell_leg_limit", {
    ticker, limit_price_cents: Number(cents), confirm: true,
  });
  if (r.ok || r.paper) {
    alert(r.paper ? "PAPER: limit sell logged." : `Limit sell placed. Order ID: ${r.order_id || "?"}`);
  } else {
    alert(`Failed: ${r.error || JSON.stringify(r)}`);
  }
  await refresh();
}

async function sellLegAdjusted(ticker) {
  if (!confirm(
    `Start adjusted limit sell for ${ticker}?\n\n` +
    `Will reprice 1¢ below best ask every 30 seconds until filled.\n` +
    `Runs in background — check logs for progress.`
  )) return;
  const r = await postJSON("/api/sell_leg_adjusted", { ticker, confirm: true });
  alert(r.started ? "Adjusted limit sell started — watch the Logs panel." : `Error: ${JSON.stringify(r)}`);
  await refresh();
}

async function dismissEvent(eventTicker) {
  const r = await postJSON("/api/dismiss_event", { event_ticker: eventTicker });
  if (!r.ok) { alert(`Error: ${r.error}`); return; }
  const id = cssId(eventTicker);
  const el = document.getElementById("event_" + id);
  if (el) el.remove();
}

function exportCSV() {
  window.location.href = "/api/export_csv";
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
    safety: {
      global_drawdown_limit: Number(document.getElementById("drawdownLimit").value),
    },
    max_deploy_pct: Number(document.getElementById("maxDeployPct").value),
    entry_zone: {
      min: Number(document.getElementById("entryZoneMin").value),
      max: Number(document.getElementById("entryZoneMax").value),
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
// Buy candidates
// ---------------------------------------------------------------------------

async function refreshCandidates() {
  const container = document.getElementById("candidatesContainer");
  container.innerHTML = '<span class="muted">Scanning...</span>';

  // Use the manually entered ticker first, then fall back to first active event
  let et = (document.getElementById("scanEventTicker")?.value || "").trim();
  if (!et && window._lastStatus) {
    const eventKeys = Object.keys(window._lastStatus.events || {});
    if (eventKeys.length > 0) et = eventKeys[0];
  }

  const url = et ? `/api/buy_candidates?event_ticker=${encodeURIComponent(et)}` : "/api/buy_candidates";
  try {
    const r = await fetch(url);
    const data = await r.json();
    if (data.hint) {
      container.innerHTML = `<span class="muted">${data.hint}</span>`;
      return;
    }
    renderCandidates(data.candidates || []);
  } catch (e) {
    container.innerHTML = `<span class="negative">Error: ${e}</span>`;
  }
}

function renderCandidates(candidates) {
  const container = document.getElementById("candidatesContainer");
  if (!candidates.length) {
    container.innerHTML = '<span class="muted">No candidates in entry zone.</span>';
    return;
  }
  let html = `<table><tr>
    <th>Ticker</th><th>Side</th><th>Strike</th><th>Mid</th>
    <th>Spot</th><th>Distance</th><th>Fee×1</th><th>Fee×3</th><th></th>
  </tr>`;
  candidates.forEach((c) => {
    const dist = c.distance != null ? (c.distance >= 0 ? "+" : "") + c.distance.toFixed(2) : "—";
    const distCls = c.distance != null && c.distance >= 0 ? "positive" : "negative";
    html += `<tr>
      <td>${c.ticker.split("-T")[1] ? c.event_ticker : c.ticker}</td>
      <td>${c.side.toUpperCase()}</td>
      <td>${fmt(c.strike)}</td>
      <td><b>${fmt(c.mid, 3)}</b></td>
      <td>${c.spot != null ? fmt(c.spot) : "—"}</td>
      <td class="${distCls}">${dist}</td>
      <td class="muted">$${c.fee_per_contract.toFixed(3)}</td>
      <td class="muted">$${c.fee_3_contracts.toFixed(3)}</td>
      <td><button class="small" onclick="promptBuy('${c.ticker}','${c.side}',${c.mid})">Buy</button></td>
    </tr>`;
  });
  html += "</table>";
  container.innerHTML = html;
}

async function promptBuy(ticker, side, mid) {
  const limitCents = prompt(
    `Buy ${side.toUpperCase()} ${ticker}\nMid: ${(mid * 100).toFixed(1)}¢\nEnter limit price in CENTS (1-99):`,
    Math.round(mid * 100)
  );
  if (limitCents === null) return;
  const qty = prompt("How many contracts?", "3");
  if (qty === null) return;
  if (!confirm(`Confirm: Buy ${qty} ${side.toUpperCase()} ${ticker} @ ${limitCents}¢`)) return;
  const r = await postJSON("/api/execute_buy", {
    ticker, side, qty: Number(qty), limit_price_cents: Number(limitCents), confirm: true,
  });
  alert(r.ok ? (r.paper ? "PAPER order logged." : "Order placed!") : `Error: ${r.error || JSON.stringify(r)}`);
  await refresh();
}

// ---------------------------------------------------------------------------
// Position Bot panel
// ---------------------------------------------------------------------------

function populateBotPanel(ticker, cfg) {
  const id = cssId(ticker);
  const setCheck = (elId, val) => { const el = document.getElementById(elId); if (el) el.checked = !!val; };
  const setVal   = (elId, val) => { const el = document.getElementById(elId); if (el && val != null) el.value = val; };
  setCheck('bp_stoploss_on_'  + id, cfg.stop_loss  != null);
  setVal  ('bp_stoploss_'     + id, cfg.stop_loss);
  setVal  ('bp_cushion_'      + id, cfg.strike_cushion);
  setCheck('bp_limit_on_'     + id, cfg.limit_sell != null);
  setVal  ('bp_limit_'        + id, cfg.limit_sell);
  setCheck('bp_harvest_'      + id, cfg.harvest);
  setVal  ('bp_sensitivity_'  + id, cfg.harvest_sensitivity ?? 3);
  setCheck('bp_timeexit_on_'  + id, cfg.time_exit  != null);
  setVal  ('bp_timeexit_'     + id, cfg.time_exit);
}

function openBotPanel(eventTicker, ticker) {
  // Close any other open panels first
  document.querySelectorAll('[id^="botpanel_"]').forEach(el => el.style.display = 'none');

  const id = cssId(ticker);
  const panel = document.getElementById('botpanel_' + id);
  if (!panel) return;

  // Look up config from store (safe — no inline JSON in onclick)
  const cfg = botConfigStore.get(ticker) || {};
  populateBotPanel(ticker, cfg);

  panel.style.display = '';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeBotPanel(ticker) {
  const panel = document.getElementById('botpanel_' + cssId(ticker));
  if (panel) panel.style.display = 'none';
}

async function saveBotPanel(eventTicker, ticker) {
  const id = cssId(ticker);

  const getCheck = (elId) => document.getElementById(elId)?.checked ?? false;
  const getStr   = (elId) => document.getElementById(elId)?.value?.trim() || null;
  // Auto-convert cents to dollars if user enters > 1 (e.g. 50 → 0.50, 0.50 → 0.50)
  const getPrice = (elId) => {
    const v = parseFloat(document.getElementById(elId)?.value);
    if (isNaN(v)) return null;
    return v > 1 ? v / 100 : v;
  };

  const config = {
    stop_loss:           getCheck('bp_stoploss_on_'  + id) ? getPrice('bp_stoploss_' + id)   : null,
    strike_cushion:      getPrice('bp_cushion_' + id) || null,
    limit_sell:          getCheck('bp_limit_on_'     + id) ? getPrice('bp_limit_'    + id)   : null,
    harvest:             getCheck('bp_harvest_'      + id),
    harvest_sensitivity: parseInt(document.getElementById('bp_sensitivity_' + id)?.value ?? '3'),
    time_exit:           getCheck('bp_timeexit_on_'  + id) ? getStr('bp_timeexit_' + id)   : null,
  };

  // Validate at least one mechanism is enabled
  if (!config.stop_loss && !config.limit_sell && !config.harvest && !config.time_exit && !config.strike_cushion) {
    alert('Enable at least one bot mechanism, or use Clear Bot to remove.');
    return;
  }

  const r = await postJSON('/api/set_position_bot', { ticker, event_ticker: eventTicker, config });
  if (r.ok) {
    closeBotPanel(ticker);
    await refresh();
  } else {
    alert('Error saving bot: ' + JSON.stringify(r));
  }
}

async function clearBotPanel(eventTicker, ticker) {
  if (!confirm('Remove bot from ' + ticker + '?')) return;
  const r = await postJSON('/api/clear_position_bot', { ticker, event_ticker: eventTicker });
  if (r.ok) {
    closeBotPanel(ticker);
    await refresh();
  } else {
    alert('Error clearing bot: ' + JSON.stringify(r));
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// Pause auto-refresh while a bot panel is open so user edits aren't disrupted
function isBotPanelOpen() {
  return !!document.querySelector('[id^="botpanel_"]:not([style*="display: none"]):not([style*="display:none"])');
}

async function refresh() {
  if (isBotPanelOpen()) return;   // don't re-render while user is editing a bot
  const r = await fetch("/api/status");
  const status = await r.json();
  window._lastStatus = status;   // cache for scan auto-detection
  renderStatus(status);
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

// ---------------------------------------------------------------------------
// Inject event ticker input next to Scan button (no HTML change needed)
// ---------------------------------------------------------------------------
(function injectScanInput() {
  function doInject() {
    const scanBtn = Array.from(document.querySelectorAll("button")).find(
      b => b.textContent.trim() === "Scan"
    );
    if (!scanBtn) return;
    if (document.getElementById("scanEventTicker")) return; // already injected

    const input = document.createElement("input");
    input.id = "scanEventTicker";
    input.type = "text";
    input.placeholder = "Event ticker (auto)";
    input.style.cssText = "width:220px; margin-right:6px; font-size:0.85em; padding:2px 6px;";
    input.title = "Leave blank to auto-detect from active events, or type e.g. KXBRENTD-26JUN2917";
    scanBtn.parentNode.insertBefore(input, scanBtn);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", doInject);
  } else {
    doInject();
    // Retry after a short delay in case the button is rendered dynamically
    setTimeout(doInject, 500);
  }
})();
