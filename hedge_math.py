#!/usr/bin/env python3
"""
hedge_math.py — Phase 1 hedge math engine for KalshiBaby.

Pure computation, no execution, no I/O. Answers the questions the
manual spreadsheet answers:

  1. Settlement map: what does the whole structure pay at every
     candidate settle price (including hypothetical legs)?
  2. Both-win zone(s): which settle ranges pay EVERY leg?
  3. Green floor: with profit already harvested, how far can the
     losing leg fall at exit before the campaign goes red?
  4. Harvest/reposition evaluation: before/after comparison of a
     proposed harvest + replacement hedge.

Conventions match kalshibaby_backend.py:
  - YES wins if settle >  strike
  - NO  wins if settle <= strike
  - Prices are dollars per contract (0.0 - 1.0)

A "leg" is a dict:
  {ticker, side ("yes"|"no"), strike, count, avg_price}
Optional: mid (current mark), for unrealized P/L display.

A "cash ledger" tracks realized flows so harvested profit is part of
every floor/settlement calculation:
  cash_out  = money spent (entries + hedge buys)
  cash_in   = money received (harvests / early exits)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

Leg = Dict[str, object]


# ---------------------------------------------------------------------------
# Core payoff
# ---------------------------------------------------------------------------

def leg_wins(leg: Leg, settle: float) -> bool:
    if leg["side"] == "yes":
        return settle > float(leg["strike"])
    return settle <= float(leg["strike"])


def settlement_value(legs: List[Leg], settle: float) -> float:
    """Total $ paid at settlement for all open legs at a settle price."""
    return sum(float(l["count"]) for l in legs if leg_wins(l, settle))


def total_cost(legs: List[Leg]) -> float:
    return sum(float(l["count"]) * float(l["avg_price"]) for l in legs)


# ---------------------------------------------------------------------------
# Settlement map & zones
# ---------------------------------------------------------------------------

def _test_points(legs: List[Leg], pad: float = 1.0) -> List[float]:
    """Evaluate just above/below every strike, plus outer bounds."""
    strikes = sorted({float(l["strike"]) for l in legs})
    if len(strikes) > 1:
        min_gap = min(b - a for a, b in zip(strikes, strikes[1:]))
        eps = max(min_gap / 10, 1e-4)
    else:
        eps = 1e-3
    pts = set()
    for s in strikes:
        pts.add(round(s - eps, 6))
        pts.add(round(s + eps, 6))
        pts.add(s)
    pts.add(strikes[0] - pad)
    pts.add(strikes[-1] + pad)
    return sorted(pts)


def settlement_map(
    legs: List[Leg],
    cash_in: float = 0.0,
    cash_out: Optional[float] = None,
) -> List[Dict[str, float]]:
    """
    P/L at each test settle price. cash_out defaults to cost of the
    open legs; pass the full campaign spend to include closed legs.
    net = settlement value + cash_in - cash_out
    """
    if cash_out is None:
        cash_out = total_cost(legs)
    out = []
    for x in _test_points(legs):
        v = settlement_value(legs, x)
        out.append({
            "settle": round(x, 6),
            "value": round(v, 2),
            "net": round(v + cash_in - cash_out, 2),
        })
    return out


def both_win_zones(legs: List[Leg]) -> List[Tuple[float, float]]:
    """
    Settle ranges where EVERY open leg pays $1.
    For a YES leg the winning range is (strike, +inf);
    for a NO leg it is (-inf, strike].
    Intersection: (max yes strike, min no strike].
    """
    yes_strikes = [float(l["strike"]) for l in legs if l["side"] == "yes"]
    no_strikes = [float(l["strike"]) for l in legs if l["side"] == "no"]
    if not yes_strikes:
        return [(float("-inf"), min(no_strikes))] if no_strikes else []
    if not no_strikes:
        return [(max(yes_strikes), float("inf"))]
    lo, hi = max(yes_strikes), min(no_strikes)
    return [(lo, hi)] if lo < hi else []


def worst_best(
    legs: List[Leg],
    cash_in: float = 0.0,
    cash_out: Optional[float] = None,
) -> Dict[str, float]:
    smap = settlement_map(legs, cash_in, cash_out)
    nets = [row["net"] for row in smap]
    return {"worst_net": min(nets), "best_net": max(nets)}


# ---------------------------------------------------------------------------
# Green floor
# ---------------------------------------------------------------------------

def green_floor(
    legs: List[Leg],
    losing_ticker: str,
    cash_in: float = 0.0,
    cash_out: Optional[float] = None,
    assume_others_win: bool = True,
) -> Optional[float]:
    """
    Lowest exit price for the losing leg at which the campaign nets >= 0,
    assuming (by default) every other open leg holds and pays $1.

    Matches the manual-spreadsheet question: "how far can I let this
    leg fall and still be green?"

    Returns None if the campaign is green even at an exit of 0.00
    (floor is 'free ride'), or the breakeven price otherwise.
    Set assume_others_win=False to assume other legs pay nothing
    (a stress floor).
    """
    if cash_out is None:
        cash_out = total_cost(legs)
    loser = next(l for l in legs if l["ticker"] == losing_ticker)
    others = [l for l in legs if l["ticker"] != losing_ticker]
    others_value = sum(float(l["count"]) for l in others) if assume_others_win else 0.0
    need = cash_out - cash_in - others_value
    if need <= 0:
        return None  # green even if loser exits at zero
    floor = need / float(loser["count"])
    return round(floor, 4) if floor <= 1.0 else float("inf")


# ---------------------------------------------------------------------------
# Harvest / reposition evaluation
# ---------------------------------------------------------------------------

def evaluate_reposition(
    open_legs: List[Leg],
    harvest: Optional[Dict[str, object]],     # {ticker, price} sell entire leg at price
    hedge: Optional[Leg],                      # new leg incl. avg_price = entry ask
    prior_cash_in: float = 0.0,
    prior_cash_out: Optional[float] = None,
) -> Dict[str, object]:
    """
    Before/after comparison for the advisor card.

    harvest: sell an existing leg in full at the given price (adds to cash_in).
    hedge:   buy a new leg at avg_price (adds to cash_out).
    Either may be None to evaluate harvest-only or hedge-only moves.
    """
    if prior_cash_out is None:
        prior_cash_out = total_cost(open_legs)

    before = {
        "both_win_zones": both_win_zones(open_legs),
        **worst_best(open_legs, prior_cash_in, prior_cash_out),
        "map": settlement_map(open_legs, prior_cash_in, prior_cash_out),
    }

    legs = [dict(l) for l in open_legs]
    cash_in, cash_out = prior_cash_in, prior_cash_out

    harvested_pl = None
    if harvest is not None:
        leg = next(l for l in legs if l["ticker"] == harvest["ticker"])
        proceeds = float(leg["count"]) * float(harvest["price"])
        cash_in += proceeds
        harvested_pl = round(
            proceeds - float(leg["count"]) * float(leg["avg_price"]), 2
        )
        legs = [l for l in legs if l["ticker"] != harvest["ticker"]]

    if hedge is not None:
        cash_out += float(hedge["count"]) * float(hedge["avg_price"])
        legs.append(dict(hedge))

    after = {
        "both_win_zones": both_win_zones(legs),
        **worst_best(legs, cash_in, cash_out),
        "map": settlement_map(legs, cash_in, cash_out),
        "harvested_pl": harvested_pl,
        "cash_in": round(cash_in, 2),
        "cash_out": round(cash_out, 2),
    }

    floors = {}
    for l in legs:
        f = green_floor(legs, str(l["ticker"]), cash_in, cash_out)
        floors[str(l["ticker"])] = f
    after["green_floors"] = floors

    return {"before": before, "after": after, "open_legs_after": legs}


# ---------------------------------------------------------------------------
# Self-test: the case study from the design conversation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 2 YES @0.75 (strike 3.220), 2 NO @0.75 (strike 3.280).
    # NO rises to 0.85, YES falls to 0.55.
    yes_leg = {"ticker": "Y", "side": "yes", "strike": 3.220, "count": 2, "avg_price": 0.75}
    no_leg  = {"ticker": "N", "side": "no",  "strike": 3.280, "count": 2, "avg_price": 0.75}

    # Harvest the NO at 0.85, replace with 2 NO @0.65 at a strike near spot (3.245).
    hedge = {"ticker": "H", "side": "no", "strike": 3.245, "count": 2, "avg_price": 0.65}

    result = evaluate_reposition(
        open_legs=[yes_leg, no_leg],
        harvest={"ticker": "N", "price": 0.85},
        hedge=hedge,
    )

    after = result["after"]
    assert after["harvested_pl"] == 0.20, after["harvested_pl"]

    # Both-win zone: settle in (3.220, 3.245] -> Y and H both pay.
    zones = after["both_win_zones"]
    assert zones == [(3.220, 3.245)], zones

    # Both win: 1.70 in + 4.00 settle - 4.30 out = +1.40
    best = after["best_net"]
    assert abs(best - 1.40) < 1e-9, best

    # Green floor on the losing YES leg (hedge assumed to win):
    # need = 4.30 - 1.70 - 2.00 = 0.60 -> floor 0.30/contract...
    # Case study said 0.35 keeps +0.10; exact breakeven is 0.30.
    floor = after["green_floors"]["Y"]
    assert abs(floor - 0.30) < 1e-9, floor

    # Sanity: exit Y at 0.35 with hedge winning -> +0.10
    net_at_035 = 1.70 + 2 * 0.35 + 2.00 - 4.30
    assert abs(net_at_035 - 0.10) < 1e-9

    # Outside the band exactly one leg pays (2.00): net = 1.70 + 2.00 - 4.30 = -0.60.
    # Below/at 3.220: Y loses, H wins. Above 3.245: Y wins, H loses.
    for row in after["map"]:
        if row["settle"] <= 3.220 or row["settle"] > 3.245:
            assert abs(row["net"] - (-0.60)) < 1e-9, row
        else:
            assert abs(row["net"] - 1.40) < 1e-9, row
    assert after["worst_net"] == -0.60

    print("All case-study assertions passed.")
    print(f"Harvested P/L locked: +${after['harvested_pl']:.2f}")
    print(f"Both-win zone: {zones[0][0]} < settle <= {zones[0][1]}  ->  net +${best:.2f}")
    print(f"Green floor on losing YES (hedge wins): {floor:.2f}")
    print(f"Worst case (settle outside band): ${after['worst_net']:.2f}")
