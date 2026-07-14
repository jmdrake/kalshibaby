#!/usr/bin/env python3
"""
Wapner Window checklist for KalshiBaby v3.

INTEGRATION (your local kalshibaby_backend.py may differ from the copy I can
see, so this is paste-in rather than a patched file):

1. Copy everything below the marker line into kalshibaby_backend.py
   (anywhere after the EventEngine class; the endpoint goes with the other
   @app routes, or keep this as wapner_checklist.py and add:
       from wapner_checklist import evaluate_event_wapner, WAPNER_DEFAULTS
   then paste only the @app.get route into the backend).

2. Add to config.yaml (all optional — these are the defaults):

   wapner:
     max_minutes: 60            # Gate 1: window
     min_mid: 0.85              # Gate 2: zone floor
     max_mid: 0.97              # Gate 2: zone ceiling
     cushion_vol_multiple: 3.0  # Gate 3: cushion >= 3x expected remaining move
     trend_lookback_minutes: 20 # Gate 4: trend window
     vol_lookback_minutes: 60   # Gate 3: volatility estimation window
     min_history_minutes: 15    # Gate 3: fail if less price history than this
     max_loss_dollars: 10.0     # Sizing rule: contracts = max_loss / price
     exit_cushion_fraction: 0.5 # Safety net: exit when half the cushion is gone

3. Hit GET /api/wapner_candidates            (all events)
   or  GET /api/wapner_candidates?event_ticker=KXBRENTD-26JUL0617

Every candidate returns PASS or REJECT — no maybes — with the failing gates
named, a suggested max size derived from max_loss_dollars, the win/loss
arithmetic after estimated taker fees, and the pre-committed exit trigger.
REJECT means REJECT. The gates are the edge; skipping them is June 10.
"""

# ---------------------------------------------------------------- paste below
import math
import statistics
import time
from typing import Any, Dict, List, Optional

import requests

WAPNER_DEFAULTS: Dict[str, float] = {
    "max_minutes": 60,
    "min_mid": 0.85,
    "max_mid": 0.97,
    "cushion_vol_multiple": 3.0,
    "trend_lookback_minutes": 20,
    "vol_lookback_minutes": 60,
    "min_history_minutes": 15,
    "max_loss_dollars": 10.0,
    "exit_cushion_fraction": 0.5,
    # Gate 6 (Liquidity): a candidate must show a real two-sided market on
    # its OWN side of the book. Prevents the July 6 phantom candidates where
    # a stale YES ask at 0.97 with no bid was advertised as PASS-eligible.
    "max_spread": 0.10,          # side is illiquid if ask - bid > this
    "min_bid": 0.01,             # side is illiquid if bid is at or below this
}


def _wapner_cfg(engine) -> Dict[str, float]:
    cfg = dict(WAPNER_DEFAULTS)
    try:
        cfg.update(engine.config.get("wapner") or {})
    except Exception:
        pass
    return cfg


def _taker_fee(price: float) -> float:
    """Kalshi taker fee per contract: 0.07 * P * (1-P), rounded up to the cent."""
    return math.ceil(7.0 * price * (1.0 - price)) / 100.0


def _history_window(eng, minutes: float) -> List[tuple]:
    cutoff = time.time() - minutes * 60.0
    return [(ts, p) for ts, p in eng.price_history if ts >= cutoff]


def expected_remaining_move(eng, minutes_left: float, cfg: Dict[str, float]) -> Optional[float]:
    """
    Estimate the plausible spot move between now and settlement from the
    consensus price feed: sigma of per-minute changes over the volatility
    lookback, scaled by sqrt(minutes remaining). Returns None when the feed
    hasn't run long enough to measure — which is a Gate 3 FAILURE, not a pass.
    """
    pts = _history_window(eng, cfg["vol_lookback_minutes"])
    if len(pts) < 10:
        return None
    span_min = (pts[-1][0] - pts[0][0]) / 60.0
    if span_min < cfg["min_history_minutes"]:
        return None
    per_min_moves: List[float] = []
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        dt_min = (t2 - t1) / 60.0
        if dt_min <= 0:
            continue
        per_min_moves.append((p2 - p1) / math.sqrt(dt_min))
    if len(per_min_moves) < 8:
        return None
    sigma_1min = statistics.pstdev(per_min_moves)
    if sigma_1min <= 0:
        return None
    return sigma_1min * math.sqrt(max(minutes_left, 1.0))


def trend_toward_strike(eng, strike: float, side: str, cfg: Dict[str, float]) -> Optional[bool]:
    """
    True  -> spot has moved TOWARD the strike over the lookback (Gate 4 fail).
    False -> flat or moving away (pass).
    None  -> not enough history (treated as fail, conservatively).
    Side is the side you'd BUY: yes wins above the strike, no wins at/below.
    """
    pts = _history_window(eng, cfg["trend_lookback_minutes"])
    if len(pts) < 3:
        return None
    then, now = pts[0][1], pts[-1][1]
    drift = now - then
    # Danger direction: whatever moves spot closer to flipping the outcome.
    if side == "yes":
        return drift < 0 and now > strike   # falling toward strike from above
    return drift > 0 and now < strike       # rising toward strike from below


def evaluate_wapner_candidate(
    eng,
    ticker: str,
    strike: float,
    side: str,
    bid: float,
    ask: float,
    minutes_left: float,
    cfg: Dict[str, float],
) -> Dict[str, Any]:
    """
    Grade one strike-side against all six gates.

    Note the signature change from the pre-July-6 build: this function now
    takes bid AND ask for the SPECIFIC side we're grading (not a synthetic
    mid derived from the opposite side). The mid we surface is computed
    only when both sides of the local book are real; if either is missing,
    Gate 6 fires and mid is reported for context but the candidate rejects.
    """
    spot = eng.consensus_price()
    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    # Gate 6 — liquidity (evaluated FIRST so a phantom candidate can't slip
    # through on downstream math built from a fictional mid). Requires both
    # a real bid AND a real ask on the side we plan to trade, with the
    # spread narrow enough that "mid" is a meaningful number.
    spread = (ask - bid) if (bid > 0 and ask > 0) else None
    checks["liquidity"] = (
        bid > cfg["min_bid"]
        and ask > 0
        and spread is not None
        and spread <= cfg["max_spread"]
    )
    if not checks["liquidity"]:
        if bid <= cfg["min_bid"]:
            reasons.append(f"no real bid on {side.upper()} side (bid {bid:.2f})")
        elif ask <= 0:
            reasons.append(f"no ask on {side.upper()} side")
        elif spread is not None and spread > cfg["max_spread"]:
            reasons.append(
                f"{side.upper()} spread {spread:.2f} > {cfg['max_spread']:.2f} — "
                f"mid is fictional, no fillable market"
            )

    # If liquidity failed we still compute mid for display, but from the
    # midpoint of whatever quotes exist — no synthesis from the other side.
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2
    elif ask > 0:
        mid = ask
    elif bid > 0:
        mid = bid
    else:
        mid = 0.0

    # Gate 1 — window
    checks["window"] = 0 < minutes_left <= cfg["max_minutes"]
    if not checks["window"]:
        reasons.append(f"{minutes_left:.0f} min to settlement (window is <= {cfg['max_minutes']:.0f})")

    # Gate 2 — zone
    checks["zone"] = cfg["min_mid"] <= mid <= cfg["max_mid"]
    if not checks["zone"]:
        reasons.append(f"mid {mid:.2f} outside {cfg['min_mid']:.2f}-{cfg['max_mid']:.2f} zone")

    # Gate 3 — volatility-scaled cushion
    cushion = None
    exp_move = None
    if spot is None:
        checks["cushion"] = False
        reasons.append("no consensus spot price")
    else:
        cushion = (spot - strike) if side == "yes" else (strike - spot)
        exp_move = expected_remaining_move(eng, minutes_left, cfg)
        if cushion <= 0:
            checks["cushion"] = False
            reasons.append(f"spot {spot:.2f} is on the wrong side of strike {strike:.2f}")
        elif exp_move is None:
            checks["cushion"] = False
            reasons.append("insufficient price history to measure volatility — unmeasured risk is a rejection")
        else:
            checks["cushion"] = cushion >= cfg["cushion_vol_multiple"] * exp_move
            if not checks["cushion"]:
                reasons.append(
                    f"cushion {cushion:.2f} < {cfg['cushion_vol_multiple']:.1f}x expected remaining move ({exp_move:.2f})"
                )

    # Gate 4 — trend
    toward = trend_toward_strike(eng, strike, side, cfg) if spot is not None else None
    checks["trend"] = toward is False
    if toward is True:
        reasons.append(f"spot moving TOWARD strike over last {cfg['trend_lookback_minutes']:.0f} min")
    elif toward is None:
        reasons.append("insufficient history to establish trend")

    # Gate 5 — regime
    checks["regime"] = eng.state in ("NORMAL", "RECOVERY")
    if not checks["regime"]:
        reasons.append(f"bot state is {eng.state} — calm markets only")

    grade = "PASS" if all(checks.values()) else "REJECT"

    fee = _taker_fee(mid) if mid > 0 else 0.0
    win_net = round((1.0 - mid) - fee, 4)
    loss_net = round(mid + fee, 4)
    max_contracts = int(cfg["max_loss_dollars"] // mid) if mid > 0 else 0
    exit_trigger = None
    if cushion is not None and cushion > 0 and spot is not None:
        gone = cushion * cfg["exit_cushion_fraction"]
        exit_trigger = round(spot - gone if side == "yes" else spot + gone, 2)

    return {
        "ticker": ticker,
        "side": side,
        "strike": strike,
        "mid": round(mid, 2),
        "bid": round(bid, 2),
        "ask": round(ask, 2),
        "spread": round(spread, 2) if spread is not None else None,
        "minutes_left": round(minutes_left, 1),
        "spot": round(spot, 2) if spot is not None else None,
        "cushion": round(cushion, 2) if cushion is not None else None,
        "expected_remaining_move": round(exp_move, 2) if exp_move else None,
        "checks": checks,
        "grade": grade,
        "reasons": reasons,
        "sizing": {
            "max_contracts": max_contracts,
            "max_loss_dollars": cfg["max_loss_dollars"],
            "win_net_per_contract": win_net,
            "loss_net_per_contract": loss_net,
            "wins_erased_by_one_loss": round(loss_net / win_net, 1) if win_net > 0 else None,
            "breakeven_win_rate": round(loss_net / (loss_net + win_net), 3) if win_net > 0 else None,
        },
        "exit_trigger_spot": exit_trigger,
        "rule": "Exit immediately if spot crosses exit_trigger_spot. REJECT means REJECT.",
    }


def evaluate_event_wapner(engine, eng) -> List[Dict[str, Any]]:
    """Grade every open strike (both sides) for one EventEngine."""
    cfg = _wapner_cfg(engine)

    if eng.settlement_time is None:
        return [{"event_ticker": eng.event_ticker, "grade": "REJECT",
                 "reasons": ["no settlement_time configured for this event"]}]
    from datetime import datetime, timezone
    now = datetime.now(eng.settlement_time.tzinfo or timezone.utc)
    minutes_left = (eng.settlement_time - now).total_seconds() / 60.0

    url = engine.kalshi.base_url + "/trade-api/v2/markets"
    r = requests.get(url, params={"event_ticker": eng.event_ticker,
                                  "status": "open", "limit": 200}, timeout=10)
    r.raise_for_status()
    markets = r.json().get("markets", [])

    out: List[Dict[str, Any]] = []
    for m in markets:
        ticker = m.get("ticker", "")
        try:
            strike = float(ticker.split("-T")[-1])
        except Exception:
            continue

        # Read each side's book independently. The pre-fix version derived
        # the NO mid from (1 - yes_mid), which advertised phantom candidates
        # whenever one side had only an ask with no bid (July 6 bug).
        yb = engine.kalshi._money_to_float(m.get("yes_bid_dollars")) or 0.0
        ya = engine.kalshi._money_to_float(m.get("yes_ask_dollars")) or 0.0
        nb = engine.kalshi._money_to_float(m.get("no_bid_dollars")) or 0.0
        na = engine.kalshi._money_to_float(m.get("no_ask_dollars")) or 0.0

        # Skip strikes where BOTH sides look completely dead — no display
        # value in listing a market with no quotes at all.
        if yb <= 0 and ya <= 0 and nb <= 0 and na <= 0:
            continue

        for side, bid, ask in (("yes", yb, ya), ("no", nb, na)):
            # Rough zone pre-filter so we don't grade obviously-irrelevant
            # strikes. Uses whichever quote exists to estimate mid; the
            # candidate itself will still fail Gate 6 if the book is thin.
            if bid > 0 and ask > 0:
                mid_est = (bid + ask) / 2
            elif ask > 0:
                mid_est = ask
            elif bid > 0:
                mid_est = bid
            else:
                continue  # no quotes at all on this side
            if mid_est < cfg["min_mid"] - 0.05 or mid_est > 0.99:
                continue
            out.append(evaluate_wapner_candidate(
                eng, ticker, strike, side, bid, ask, minutes_left, cfg
            ))
    out.sort(key=lambda c: (c["grade"] != "PASS", -(c.get("cushion") or 0)))
    return out


# --------------------------------------------------------------- FastAPI route
# Paste this with the other @app routes in kalshibaby_backend.py:
#
# @app.get("/api/wapner_candidates")
# async def api_wapner_candidates(event_ticker: Optional[str] = None):
#     engines = (
#         {event_ticker: engine.event_engines[event_ticker]}
#         if event_ticker and event_ticker in engine.event_engines
#         else engine.event_engines
#     )
#     if event_ticker and event_ticker not in engine.event_engines:
#         raise HTTPException(status_code=404, detail="Event not found")
#     results = {}
#     for et, eng in engines.items():
#         try:
#             results[et] = evaluate_event_wapner(engine, eng)
#         except Exception as e:
#             results[et] = [{"event_ticker": et, "grade": "REJECT",
#                             "reasons": [f"evaluation error: {e}"]}]
#     return {"ts": time.time(), "candidates": results}
