// ---------------------------------------------------------------------------
// Wapner Window — render helpers for /api/wapner_candidates
// Paste into app.js. Function declarations, so order doesn't matter.
// ---------------------------------------------------------------------------

function renderWapnerResults(byEvent) {
  const events = Object.keys(byEvent);
  if (!events.length) {
    return '<span class="muted">No events being tracked.</span>';
  }
  let html = "";
  for (const et of events) {
    const cands = byEvent[et] || [];
    html += '<div class="wapner-event"><h4 style="margin:10px 0 4px">' + esc(et) + "</h4>";
    if (!cands.length) {
      html += '<div class="muted">No strikes near the zone.</div></div>';
      continue;
    }
    // Event-level rejection (e.g. no settlement_time)
    if (cands.length === 1 && cands[0].ticker === undefined) {
      html += '<div class="muted">REJECT — ' + esc((cands[0].reasons || []).join("; ")) + "</div></div>";
      continue;
    }
    const passes = cands.filter(c => c.grade === "PASS");
    const rejects = cands.filter(c => c.grade !== "PASS");
    if (!passes.length) {
      html += '<div class="muted" style="margin-bottom:4px">No trade. ' +
              rejects.length + " strike(s) evaluated, all rejected — that is the system working.</div>";
    }
    for (const c of passes.concat(rejects)) html += renderWapnerCard(c);
    html += "</div>";
  }
  return html;
}

function renderWapnerCard(c) {
  const pass = c.grade === "PASS";
  const border = pass ? "#1baf7a" : "#555";
  const badge = pass
    ? '<span style="background:#1baf7a;color:#fff;padding:1px 8px;border-radius:10px;font-weight:600">PASS</span>'
    : '<span style="background:#444;color:#bbb;padding:1px 8px;border-radius:10px">REJECT</span>';

  const gates = c.checks
    ? Object.entries(c.checks).map(([name, ok]) =>
        '<span title="' + esc(name) + '" style="margin-right:6px;color:' +
        (ok ? "#1baf7a" : "#e34948") + '">' + (ok ? "✓" : "✗") + " " + esc(name) + "</span>"
      ).join("")
    : "";

  const s = c.sizing || {};
  const sizing = pass
    ? '<div style="margin-top:3px"><b>Size cap: ' + s.max_contracts + " contracts</b> " +
      "(max loss $" + s.max_loss_dollars + ") · win " + cts(s.win_net_per_contract) +
      "/ct · one loss erases ~" + s.wins_erased_by_one_loss + " wins · " +
      "<b>exit if spot crosses " + c.exit_trigger_spot + "</b></div>"
    : "";

  const reasons = (c.reasons && c.reasons.length)
    ? '<div class="muted" style="margin-top:2px">' + c.reasons.map(esc).join(" · ") + "</div>"
    : "";

  const cushion = (c.cushion != null && c.expected_remaining_move != null)
    ? " · cushion " + c.cushion + " vs move " + c.expected_remaining_move
    : "";

  // Show the actual book so a liquidity REJECT is diagnosable at a glance.
  const book = (c.bid != null && c.ask != null)
    ? ' <span class="muted">· bid ' + c.bid + ' / ask ' + c.ask +
      (c.spread != null ? ' (spread ' + c.spread + ')' : '') + '</span>'
    : '';

  return (
    '<div style="border-left:3px solid ' + border + ';padding:5px 8px;margin:4px 0;background:rgba(255,255,255,0.03)">' +
      '<div>' + badge + " <b>" + esc(c.ticker || "") + "</b> " +
        esc((c.side || "").toUpperCase()) + " @ " + c.mid +
        ' <span class="muted">· ' + c.minutes_left + " min left · spot " + c.spot + cushion + "</span>" +
        book + "</div>" +
      '<div style="margin-top:3px;font-size:0.85em">' + gates + "</div>" +
      reasons + sizing +
    "</div>"
  );
}

function cts(x) { return x == null ? "?" : Math.round(x * 100) + "¢"; }

function esc(s) {
  return String(s).replace(/[&<>"']/g, m =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}
