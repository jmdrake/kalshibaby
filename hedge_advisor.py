#!/usr/bin/env python3
"""
hedge_advisor.py — Phase 2 hedge advisor for KalshiBaby.

Watches every tracked event for the two situations from the manual
playbook and produces "cards" (proposal dicts) that the Telegram bot
presents for one-tap approval. NEVER executes anything on its own:
execution happens only via execute_card(), which is only called from
the Telegram callback after the user taps a button.

Card kinds:

  "reposition" — the harvest-and-reposition pattern:
      one wing fat (mid >= harvest_at, default 0.90 per user's rule)
      while an opposite-side leg bleeds (mid <= bleed_at, default 0.60).
      Proposes: sell the fat leg, replace with a near-spot hedge on the
      same side. Card carries locked P/L, new both-win zone, green
      floors, and worst/best — the spreadsheet numbers.

  "hold" — the free-ride advisory (the Y4050 card):
      a leg looks scary (mid <= scared_at, default 0.30) but the
      structure already covers its worst case (green floor is a free
      ride or already below current mid). Pure information: HOLD.

Config lives in params.hedge_advisor (all optional):
  enabled            1/0                     default 1
  harvest_at         fat-wing trigger        default 0.90
  bleed_at           opposite-wing trigger   default 0.60
  scared_at          hold-advisory trigger   default 0.30
  hedge_zone_min/max ask range for hedges    default 0.40 / 0.75
  realert_seconds    per-event throttle      default 600
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

import hedge_math as hm


def _flt(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class HedgeAdvisor:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        # card_id -> card dict (awaiting user decision via Telegram)
        self.pending_cards: Dict[str, Dict[str, Any]] = {}
        # throttles: key -> last alert ts
        self._last_alert: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: float) -> float:
        try:
            return float(getattr(self.engine.params, "hedge_advisor", {}).get(key, default))
        except Exception:
            return default

    def _throttled(self, key: str) -> bool:
        window = self._cfg("realert_seconds", 600)
        last = self._last_alert.get(key, 0.0)
        if time.time() - last < window:
            return True
        self._last_alert[key] = time.time()
        return False

    # ------------------------------------------------------------------
    # Leg / ledger extraction
    # ------------------------------------------------------------------

    @staticmethod
    def legs_from_engine(eng: Any) -> List[hm.Leg]:
        return [
            {
                "ticker": p.ticker,
                "side": p.side,
                "strike": float(p.strike),
                "count": int(p.count),
                "avg_price": float(p.avg_price),
                "mid": float(p.current_bid or p.current_mid or 0.0),
            }
            for p in eng.positions
            if p.count > 0
        ]

    @staticmethod
    def ledger_from_engine(eng: Any) -> Tuple[float, float]:
        """
        (cash_in, cash_out) for the whole campaign on this event.
        cash_out = cost of open legs + cost basis of legs already closed.
        cash_in  = estimated proceeds of executed sells (recorded by
                   _execute_sell at the bid in effect when it fired).
        """
        open_cost = sum(p.count * p.avg_price for p in eng.positions if p.count > 0)
        cash_in = float(getattr(eng, "realized_cash_in", 0.0))
        closed_cost = float(getattr(eng, "realized_cost_closed", 0.0))
        return cash_in, open_cost + closed_cost

    # ------------------------------------------------------------------
    # Market data for candidate hedges
    # ------------------------------------------------------------------

    def _fetch_event_quotes(self, event_ticker: str) -> List[Dict[str, Any]]:
        """All open contracts for the event with strike + yes/no bid/ask."""
        url = self.engine.kalshi.base_url + "/trade-api/v2/markets"
        try:
            r = requests.get(
                url,
                params={"event_ticker": event_ticker, "status": "open", "limit": 200},
                timeout=8,
            )
            if not r.ok:
                return []
            markets = r.json().get("markets", [])
        except Exception:
            return []
        out = []
        for m in markets:
            ticker = m.get("ticker", "")
            if "-T" not in ticker:
                continue
            try:
                strike = float(ticker.split("-T")[-1])
            except Exception:
                continue
            out.append({
                "ticker": ticker,
                "strike": strike,
                "yes_bid": _flt(m.get("yes_bid_dollars")),
                "yes_ask": _flt(m.get("yes_ask_dollars")),
                "no_bid": _flt(m.get("no_bid_dollars")),
                "no_ask": _flt(m.get("no_ask_dollars")),
            })
        out.sort(key=lambda x: x["strike"])
        return out

    def _hedge_candidates(
        self,
        quotes: List[Dict[str, Any]],
        side: str,
        spot: float,
        exclude_tickers: set,
        max_candidates: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Near-spot hedge candidates on the given side, per the user's style:
        hug spot first (max responsiveness, cheap), then one strike further
        out as the conservative alternative.

        NO wins if settle <= strike -> near-spot NO = first strikes ABOVE spot.
        YES wins if settle > strike -> near-spot YES = first strikes BELOW spot.
        """
        zone_min = self._cfg("hedge_zone_min", 0.40)
        zone_max = self._cfg("hedge_zone_max", 0.75)

        if side == "no":
            pool = [q for q in quotes if q["strike"] >= spot]
            pool.sort(key=lambda q: q["strike"])            # nearest above first
        else:
            pool = [q for q in quotes if q["strike"] <= spot]
            pool.sort(key=lambda q: -q["strike"])           # nearest below first

        picks = []
        for q in pool:
            if q["ticker"] in exclude_tickers:
                continue
            ask = q[f"{side}_ask"]
            if ask is None or not (zone_min <= ask <= zone_max):
                continue
            picks.append({"ticker": q["ticker"], "strike": q["strike"],
                          "side": side, "ask": ask})
            if len(picks) >= max_candidates:
                break
        return picks

    # ------------------------------------------------------------------
    # Main check — called from Engine.tick (self-throttled)
    # ------------------------------------------------------------------

    async def check(self) -> None:
        if not self._cfg("enabled", 1):
            return
        for event_ticker, eng in list(self.engine.event_engines.items()):
            try:
                await self._check_event(event_ticker, eng)
            except Exception as e:
                self.engine.log(f"HedgeAdvisor error on {event_ticker}: {e}")

    async def _check_event(self, event_ticker: str, eng: Any) -> None:
        legs = self.legs_from_engine(eng)
        if len(legs) < 2:
            return
        cash_in, cash_out = self.ledger_from_engine(eng)

        # ── 1. Free-ride HOLD advisories (the Y4050 card) ────────────────
        scared_at = self._cfg("scared_at", 0.30)
        for leg in legs:
            if leg["mid"] and leg["mid"] <= scared_at:
                floor = hm.green_floor(legs, str(leg["ticker"]), cash_in, cash_out)
                if floor is None or (leg["mid"] and floor <= leg["mid"]):
                    if not self._throttled(f"hold_{leg['ticker']}"):
                        await self._emit_hold_card(event_ticker, eng, legs,
                                                   leg, floor, cash_in, cash_out)

        # ── 2. Harvest-and-reposition ────────────────────────────────────
        harvest_at = self._cfg("harvest_at", 0.90)
        bleed_at = self._cfg("bleed_at", 0.60)

        fat = [l for l in legs if l["mid"] >= harvest_at]
        if not fat:
            return
        # Harvest the leg with the most profit to lock (count × run-up),
        # not the highest mid — a 0.97 leg entered at 0.95 locks pennies.
        fat_leg = max(fat, key=lambda l: l["count"] * (l["mid"] - l["avg_price"]))
        opposite = "yes" if fat_leg["side"] == "no" else "no"
        bleeding = [l for l in legs if l["side"] == opposite and 0 < l["mid"] <= bleed_at]
        if not bleeding:
            return
        if self._throttled(f"repo_{event_ticker}_{fat_leg['ticker']}"):
            return

        spot = eng.consensus_price()
        if spot is None:
            return

        quotes = self._fetch_event_quotes(event_ticker)
        held = {l["ticker"] for l in legs}
        candidates = self._hedge_candidates(quotes, fat_leg["side"], spot, held)

        card = self.build_reposition_card(
            event_ticker, legs, fat_leg, bleeding, candidates,
            spot, cash_in, cash_out,
        )
        await self._dispatch(card, eng)

    # ------------------------------------------------------------------
    # Card construction
    # ------------------------------------------------------------------

    def build_reposition_card(
        self,
        event_ticker: str,
        legs: List[hm.Leg],
        fat_leg: hm.Leg,
        bleeding: List[hm.Leg],
        candidates: List[Dict[str, Any]],
        spot: float,
        cash_in: float,
        cash_out: float,
    ) -> Dict[str, Any]:
        harvest = {"ticker": fat_leg["ticker"], "price": fat_leg["mid"]}
        options = []
        for cand in candidates:
            hedge_leg = {
                "ticker": cand["ticker"], "side": cand["side"],
                "strike": cand["strike"], "count": fat_leg["count"],
                "avg_price": cand["ask"],
            }
            res = hm.evaluate_reposition(legs, harvest, hedge_leg,
                                         prior_cash_in=cash_in,
                                         prior_cash_out=cash_out)
            after = res["after"]
            options.append({
                "hedge": hedge_leg,
                "harvested_pl": after["harvested_pl"],
                "both_win_zones": after["both_win_zones"],
                "best_net": after["best_net"],
                "worst_net": after["worst_net"],
                "green_floors": after["green_floors"],
            })

        # Harvest-only branch for comparison / the "Harvest only" button.
        res_h = hm.evaluate_reposition(legs, harvest, None,
                                       prior_cash_in=cash_in,
                                       prior_cash_out=cash_out)

        card = {
            "card_id": uuid.uuid4().hex[:10],
            "kind": "reposition",
            "ts": time.time(),
            "event_ticker": event_ticker,
            "spot": spot,
            "harvest": {**harvest, "side": fat_leg["side"],
                        "qty": fat_leg["count"],
                        "avg_price": fat_leg["avg_price"]},
            "bleeding": [{"ticker": b["ticker"], "mid": b["mid"]} for b in bleeding],
            "options": options,
            "harvest_only": {
                "harvested_pl": res_h["after"]["harvested_pl"],
                "best_net": res_h["after"]["best_net"],
                "worst_net": res_h["after"]["worst_net"],
            },
        }
        self.pending_cards[card["card_id"]] = card
        return card

    async def _emit_hold_card(
        self, event_ticker: str, eng: Any, legs: List[hm.Leg],
        leg: hm.Leg, floor: Optional[float], cash_in: float, cash_out: float,
    ) -> None:
        wb = hm.worst_best(legs, cash_in, cash_out)
        card = {
            "card_id": uuid.uuid4().hex[:10],
            "kind": "hold",
            "ts": time.time(),
            "event_ticker": event_ticker,
            "leg": {"ticker": leg["ticker"], "side": leg["side"],
                    "qty": leg["count"], "mid": leg["mid"],
                    "avg_price": leg["avg_price"]},
            "green_floor": floor,          # None = free ride
            "worst_net": wb["worst_net"],
            "best_net": wb["best_net"],
        }
        await self._dispatch(card, eng)

    # ------------------------------------------------------------------
    # Dispatch & execution
    # ------------------------------------------------------------------

    async def _dispatch(self, card: Dict[str, Any], eng: Any) -> None:
        tg = getattr(self.engine, "telegram", None)
        if tg is not None:
            try:
                await tg.send_hedge_card(card)
                self.engine.log(
                    f"HedgeAdvisor: {card['kind']} card sent for {card['event_ticker']}"
                )
                return
            except Exception as e:
                self.engine.log(f"HedgeAdvisor telegram send failed: {e}")
        self.engine.log(
            f"HedgeAdvisor (no telegram): {card['kind']} card for "
            f"{card['event_ticker']} — {card.get('card_id')}"
        )

    async def execute_card(self, card_id: str, mode: str,
                           option_index: int = 0) -> Dict[str, Any]:
        """
        Execute an approved reposition card.
        mode: "full" (harvest + hedge buy) or "harvest_only".
        Called ONLY from the Telegram callback after a user tap.
        """
        card = self.pending_cards.pop(card_id, None)
        if card is None:
            return {"ok": False, "error": "card expired or already handled"}
        if card["kind"] != "reposition":
            return {"ok": False, "error": "not an executable card"}

        eng = self.engine.event_engines.get(card["event_ticker"])
        if eng is None:
            return {"ok": False, "error": "event no longer tracked"}

        h = card["harvest"]
        pos = next((p for p in eng.positions if p.ticker == h["ticker"]), None)
        if pos is None or pos.count <= 0:
            return {"ok": False, "error": f"harvest position {h['ticker']} gone"}

        await eng._execute_sell(pos, int(h["qty"]))
        result: Dict[str, Any] = {"ok": True, "harvested": h["ticker"]}

        if mode == "full" and card["options"]:
            idx = min(option_index, len(card["options"]) - 1)
            hedge = card["options"][idx]["hedge"]
            # Small buffer over the quoted ask so the IOC doesn't miss on a tick.
            limit_cents = min(99, int(round(float(hedge["avg_price"]) * 100)) + 2)
            buy = await self.engine.execute_buy(
                ticker=hedge["ticker"], side=hedge["side"],
                qty=int(hedge["count"]), limit_price_cents=limit_cents,
            )
            result["hedge"] = {"ticker": hedge["ticker"], "buy": buy}
        return result

    # ------------------------------------------------------------------
    # Sandbox / API evaluation
    # ------------------------------------------------------------------

    def evaluate_for_api(
        self,
        event_ticker: str,
        harvest: Optional[Dict[str, Any]] = None,
        hedge: Optional[Dict[str, Any]] = None,
        extra_legs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Backend for /api/hedge_eval — the live spreadsheet. Evaluates the
        current book plus any hypotheticals and returns the full math.
        """
        eng = self.engine.event_engines.get(event_ticker)
        if eng is None:
            raise KeyError(f"event not tracked: {event_ticker}")
        legs = self.legs_from_engine(eng)
        for xl in (extra_legs or []):
            legs.append({
                "ticker": xl.get("ticker") or f"HYPO-{xl['side']}-{xl['strike']}",
                "side": xl["side"], "strike": float(xl["strike"]),
                "count": int(xl["count"]), "avg_price": float(xl["price"]),
            })
        cash_in, cash_out = self.ledger_from_engine(eng)
        if extra_legs:
            cash_out += sum(int(x["count"]) * float(x["price"]) for x in extra_legs)

        hedge_leg = None
        if hedge:
            hedge_leg = {
                "ticker": hedge.get("ticker") or f"HEDGE-{hedge['side']}-{hedge['strike']}",
                "side": hedge["side"], "strike": float(hedge["strike"]),
                "count": int(hedge["count"]), "avg_price": float(hedge["price"]),
            }
        return hm.evaluate_reposition(
            legs,
            harvest if harvest else None,
            hedge_leg,
            prior_cash_in=cash_in,
            prior_cash_out=cash_out,
        )
