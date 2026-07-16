#!/usr/bin/env python3
"""Advisor integration test against the July 15 gold scenario (mock engine)."""
import asyncio
from types import SimpleNamespace

from hedge_advisor import HedgeAdvisor

def P(ticker, side, strike, count, avg, mid):
    return SimpleNamespace(ticker=ticker, side=side, strike=strike, count=count,
                           avg_price=avg, current_bid=mid, current_mid=mid)

# The July 15 gold book at the moment of the panic sale:
# Y4050 down at 0.15, the 4070 NO wall fat at 0.92 (as during the peak).
positions = [
    P("KXGOLDD-Y4030", "yes", 4030, 2, 0.86, 0.80),
    P("KXGOLDD-Y4040", "yes", 4040, 1, 0.78, 0.70),
    P("KXGOLDD-Y4050", "yes", 4050, 6, 0.8017, 0.15),   # the scary leg
    P("KXGOLDD-Y4060", "yes", 4060, 4, 0.80, 0.35),
    P("KXGOLDD-N4070", "no",  4070, 16, 0.5538, 0.92),  # fat wing
    P("KXGOLDD-N4080", "no",  4080, 2, 0.78, 0.95),
    P("KXGOLDD-N4090", "no",  4090, 4, 0.8775, 0.97),
]

class MockEventEngine:
    def __init__(self):
        self.positions = positions
        self.realized_cash_in = 0.0
        self.realized_cost_closed = 0.0
        self.sold = []
    def consensus_price(self):
        return 4046.0   # gold dipped — YES side bleeding
    async def _execute_sell(self, p, qty):
        self.sold.append((p.ticker, qty))
        p.count -= qty

class MockEngine:
    def __init__(self):
        self.event_engines = {"KXGOLDD-26JUL1517": MockEventEngine()}
        self.params = SimpleNamespace(hedge_advisor={
            "enabled": 1, "harvest_at": 0.90, "bleed_at": 0.60,
            "scared_at": 0.30, "hedge_zone_min": 0.40, "hedge_zone_max": 0.75,
            "realert_seconds": 600,
        })
        self.kalshi = SimpleNamespace(base_url="https://example.invalid")
        self.logs = []
        self.buys = []
        self.cards_sent = []
        # telegram stub capturing cards
        adv_self = self
        class TG:
            async def send_hedge_card(self, card):
                adv_self.cards_sent.append(card)
        self.telegram = TG()
    def log(self, msg):
        self.logs.append(msg)
    async def execute_buy(self, ticker, side, qty, limit_price_cents):
        self.buys.append((ticker, side, qty, limit_price_cents))
        return {"ok": True, "paper": True}

engine = MockEngine()
advisor = HedgeAdvisor(engine)

# Stub market quotes: strikes around spot 4046 for candidate NO hedges.
advisor._fetch_event_quotes = lambda et: [
    {"ticker": f"KXGOLDD-N{s}", "strike": float(s),
     "yes_bid": None, "yes_ask": None,
     "no_bid": 0.50 if s == 4050 else 0.60, "no_ask": 0.55 if s == 4050 else 0.66}
    for s in (4030, 4040, 4050, 4060, 4070, 4080, 4090)
]

asyncio.run(advisor.check())

kinds = [c["kind"] for c in engine.cards_sent]
assert "hold" in kinds, kinds
assert "reposition" in kinds, kinds

hold = next(c for c in engine.cards_sent if c["kind"] == "hold")
assert hold["leg"]["ticker"] == "KXGOLDD-Y4050"
assert hold["green_floor"] is None, hold["green_floor"]   # FREE RIDE — the Y4050 card
print(f"HOLD card: {hold['leg']['ticker']} at {hold['leg']['mid']} -> floor "
      f"{'FREE RIDE' if hold['green_floor'] is None else hold['green_floor']}, "
      f"worst {hold['worst_net']}, best {hold['best_net']}")

repo = next(c for c in engine.cards_sent if c["kind"] == "reposition")
assert repo["harvest"]["ticker"] == "KXGOLDD-N4070"
assert repo["harvest"]["qty"] == 16
opts = repo["options"]
assert len(opts) == 2, len(opts)
# Near-spot first: first NO strike >= 4046 is 4050, then 4060.
assert opts[0]["hedge"]["strike"] == 4050.0
assert opts[1]["hedge"]["strike"] == 4060.0
# Harvest locks 16 * (0.92 - 0.5538) = +5.86
assert abs(opts[0]["harvested_pl"] - 5.86) < 0.01, opts[0]["harvested_pl"]
print(f"REPO card: harvest {repo['harvest']['ticker']} x{repo['harvest']['qty']} "
      f"@ {repo['harvest']['price']} locks +${opts[0]['harvested_pl']:.2f}")
for i, o in enumerate(opts):
    z = o["both_win_zones"]
    print(f"  Option {i+1}: NO T{o['hedge']['strike']:g} @ {o['hedge']['avg_price']:.2f} | "
          f"both-win {z} best {o['best_net']} worst {o['worst_net']}")

# Throttle: second check should send nothing new.
n = len(engine.cards_sent)
asyncio.run(advisor.check())
assert len(engine.cards_sent) == n, "throttle failed"

# Execute the card (option 0, full): sells the fat wing, buys the hedge.
res = asyncio.run(advisor.execute_card(repo["card_id"], "full", 0))
assert res["ok"], res
ev = engine.event_engines["KXGOLDD-26JUL1517"]
assert ("KXGOLDD-N4070", 16) in ev.sold
assert engine.buys and engine.buys[0][0] == "KXGOLDD-N4050"
assert engine.buys[0][3] == 57   # ask 0.55 + 2c buffer
print(f"EXECUTE: sold {ev.sold[0]}, bought {engine.buys[0]}")

# Card is consumed — re-execution refuses.
res2 = asyncio.run(advisor.execute_card(repo["card_id"], "full", 0))
assert not res2["ok"]
print("Card consumed after execution — replay refused.")
print("ALL ADVISOR TESTS PASSED")
