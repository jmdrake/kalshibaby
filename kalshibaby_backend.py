#!/usr/bin/env python3
"""
KalshiBaby v3 backend — multi-event portfolio trading engine.

Run:
    uvicorn kalshibaby_backend:app --host 0.0.0.0 --port 8765 --reload

Open:
    http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import math
import time
from bs4 import BeautifulSoup
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple
import base64
import uuid
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import os

import requests
import yaml
from wapner_checklist import evaluate_event_wapner
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


_config_env = os.environ.get("KALSHI_CONFIG", "config.yaml")
CONFIG_PATH = Path(_config_env)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path("config.example.yaml")


Side = Literal["yes", "no"]
BotState = Literal[
    "NORMAL",
    "PROFIT_PROTECTION",
    "SHOCK_WATCH",
    "RECOVERY",
    "REGIME_BREAK",
    "FLATTENING",
    "OBSERVE_ONLY",
]
NewsRegime = Literal["NEUTRAL", "DEAL_MODE", "ESCALATION_MODE"]

# Instrument prefix → price source symbol mapping.
# OilPrice only covers oil products — omit oilprice_symbol for non-oil instruments.
INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "KXWTI":     {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",   "use_goldprice": False, "use_silverprice": False},
    "KXWTIMAX":  {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",   "use_goldprice": False, "use_silverprice": False},
    "KXBRENTW":  {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude", "use_goldprice": False, "use_silverprice": False},
    "KXGOLDW":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True,  "use_silverprice": False},
    "KXGOLD":    {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True,  "use_silverprice": False},
    "KXGOLDMAX": {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True,  "use_silverprice": False},
    "KXGOLDR":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True,  "use_silverprice": False},
    "KXGOLDD":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True,  "use_silverprice": False},
    "KXBRENTD":  {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude", "use_goldprice": False, "use_silverprice": False},
    "KXNG":      {"yahoo_symbol": "NG=F",  "oilprice_symbol": "Natural-Gas", "use_goldprice": False, "use_silverprice": False},
    "KXSILVER":  {"yahoo_symbol": "SI=F",  "oilprice_symbol": None,          "use_goldprice": False, "use_silverprice": True},
    "KXSILVERD": {"yahoo_symbol": "SI=F",  "oilprice_symbol": None,          "use_goldprice": False, "use_silverprice": True},
    "KXSILVERW": {"yahoo_symbol": "SI=F",  "oilprice_symbol": None,          "use_goldprice": False, "use_silverprice": True},
}


def parse_strike(ticker: str) -> float:
    try:
        return float(ticker.split("-T")[-1])
    except Exception:
        return 0.0


def instrument_prefix(event_ticker: str) -> str:
    return event_ticker.split("-")[0]


def detect_instrument(event_ticker: str) -> Dict[str, Any]:
    """
    Look up instrument config by prefix, with keyword fallback for unknown tickers.
    Logs a warning if falling back so the prefix can be added to INSTRUMENTS.
    """
    prefix = instrument_prefix(event_ticker)
    if prefix in INSTRUMENTS:
        return INSTRUMENTS[prefix]

    p = prefix.upper()
    if "GOLD" in p or p.startswith("KXAU"):
        return {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,           "use_goldprice": True,  "use_silverprice": False}
    if "SILVER" in p or p.startswith("KXAG") or p.startswith("KXSLV"):
        return {"yahoo_symbol": "SI=F",  "oilprice_symbol": None,           "use_goldprice": False, "use_silverprice": True}
    if "WTI" in p:
        return {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",    "use_goldprice": False, "use_silverprice": False}
    if "BRENT" in p or "BRT" in p:
        return {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude",  "use_goldprice": False, "use_silverprice": False}
    if p.startswith("KXNG") or "NATGAS" in p or "NGAS" in p:
        return {"yahoo_symbol": "NG=F",  "oilprice_symbol": "Natural-Gas",  "use_goldprice": False, "use_silverprice": False}
    return {}


def now_ts() -> float:
    return time.time()


def clamp_price(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Position(BaseModel):
    ticker: str
    side: Side
    strike: float
    count: int
    avg_price: float
    current_bid: float = 0.0
    current_ask: float = 0.0
    current_mid: float = 0.0
    peak_mid: float = 0.0
    profit_armed: bool = False
    bot_config: Optional[Dict[str, Any]] = None  # per-position bot set via UI


class RuntimeParams(BaseModel):
    mode: str = "paper"          # "paper" or "live" — enforces execution gate
    armed: bool = False           # global default arm state for new events
    poll_seconds: int = 3

    profit_harvest: Dict[str, float]
    shock_logic: Dict[str, float]
    source_consensus: Dict[str, float]
    structure: Dict[str, float]
    time_risk: Dict[str, float]
    sources: Dict[str, Dict[str, Any]]
    safety: Dict[str, float] = Field(default_factory=lambda: {"global_drawdown_limit": -50.0})
    entry_zone: Dict[str, float] = Field(default_factory=lambda: {"min": 0.70, "max": 0.80})


class PricePoint(BaseModel):
    source: str
    price: Optional[float]
    ts: float
    stale: bool = False
    error: Optional[str] = None


class Action(BaseModel):
    ts: float
    severity: Literal["info", "warn", "danger"]
    action: Literal["HOLD", "TRIM", "SELL_LEG", "SELL_ALL", "ALERT"]
    reason: str
    event_ticker: Optional[str] = None
    ticker: Optional[str] = None
    side: Optional[Side] = None
    qty: Optional[int] = None
    headline: Optional[str] = None   # news headline for display in UI
    url: Optional[str] = None        # link to source article
    source: Optional[str] = None     # feed name e.g. "gnews_iran_israel"


class RiskSnapshot(BaseModel):
    cost_basis: float
    mark_value: float
    unrealized_pl: float
    max_payout: float
    max_profit: float
    worst_settlement_value: float
    worst_settlement_loss: float
    modeled_stop_loss_value: float
    modeled_stop_loss: float
    yes_count: int
    no_count: int
    imbalance_ratio: float
    settlement_map: List[Dict[str, float]]


class EventStatus(BaseModel):
    event_ticker: str
    state: BotState
    armed: bool
    consensus_price: Optional[float]
    kalshi_implied_price: Optional[float]
    prices: List[PricePoint]
    positions: List[Position]
    risk: RiskSnapshot


class MultiStatus(BaseModel):
    ts: float
    mode: str
    events: Dict[str, EventStatus]
    actions: List[Action]
    params: RuntimeParams
    logs: List[str]
    news_regime: str = "NEUTRAL"
    position_bots: Dict[str, Any] = Field(default_factory=dict)


class UpdateParamsRequest(BaseModel):
    mode: Optional[str] = None
    armed: Optional[bool] = None
    poll_seconds: Optional[int] = None
    profit_harvest: Optional[Dict[str, float]] = None
    shock_logic: Optional[Dict[str, float]] = None
    source_consensus: Optional[Dict[str, float]] = None
    structure: Optional[Dict[str, float]] = None
    time_risk: Optional[Dict[str, float]] = None
    sources: Optional[Dict[str, Dict[str, Any]]] = None
    safety: Optional[Dict[str, float]] = None


class PositionBotRequest(BaseModel):
    ticker: str
    event_ticker: str
    config: Dict[str, Any] = Field(default_factory=dict)


class ClearPositionBotRequest(BaseModel):
    ticker: str
    event_ticker: str


class ArmEventRequest(BaseModel):
    event_ticker: str
    armed: bool


class SellRequest(BaseModel):
    ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    qty: Optional[int] = None
    confirm: bool = False


# ---------------------------------------------------------------------------
# News signal model (received from newsagent.py)
# ---------------------------------------------------------------------------

class NewsSignal(BaseModel):
    source: str
    headline: str
    signal: str          # DEAL_SIGNAL | ESCALATION_SIGNAL | BULLISH | BEARISH | NEUTRAL
    confidence: float
    reason: str
    ts: float
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# News regime tracker — acts only on TRANSITIONS, not sustained signals
# ---------------------------------------------------------------------------

class NewsRegimeTracker:
    """
    Maintains a news narrative regime state machine.

    States:
        NEUTRAL        — no strong signal established yet
        DEAL_MODE      — sustained deal/de-escalation narrative
        ESCALATION_MODE — narrative has shifted to conflict/deal collapse

    Key design: only acts when the regime CHANGES, not on repeated same-regime signals.
    Also requires price confirmation before firing sells — news alone is not enough.

    Price confirmation logic:
        DEAL_MODE → ESCALATION_MODE: price must be moving UP (bad for NO, good for YES)
        ESCALATION_MODE → DEAL_MODE: price must be moving DOWN (bad for YES, good for NO)
    """

    # How many consecutive signals of the new type needed to flip regime
    FLIP_THRESHOLD = 2

    # Price must have moved at least this % in the confirming direction
    # over the price_confirm_minutes window
    PRICE_CONFIRM_PCT = 0.30
    PRICE_CONFIRM_MINUTES = 10.0

    def __init__(
        self,
        actions_queue: Deque[Action],
        logs_queue: Deque[str],
    ) -> None:
        self._actions = actions_queue
        self._logs = logs_queue
        self.regime: NewsRegime = "NEUTRAL"
        self.pending_signal: Optional[str] = None   # signal type accumulating
        self.pending_count: int = 0
        self.last_transition_ts: float = 0.0
        self.recent_signals: Deque[NewsSignal] = deque(maxlen=50)

        # Min seconds between regime flips — prevents thrashing on noisy feeds
        self.min_flip_interval: float = 300.0  # 5 minutes

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._logs.appendleft(f"[NewsRegime] {stamp} {msg}")

    def _add_action(
        self,
        severity: str,
        action: str,
        reason: str,
        headline: Optional[str] = None,
        url: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        # Dedup — don't spam the same alert repeatedly
        recent = list(self._actions)[:5]
        for a in recent:
            if a.action == action and a.reason == reason:
                return
        self._actions.appendleft(Action(
            ts=now_ts(), severity=severity, action=action, reason=reason,
            headline=headline, url=url, source=source,
        ))
        self.log(f"{severity.upper()} {action}: {reason}")

    def _signal_to_regime(self, signal: str) -> Optional[NewsRegime]:
        """Map classifier signal to regime direction."""
        if signal in ("DEAL_SIGNAL", "BEARISH"):
            return "DEAL_MODE"
        if signal in ("ESCALATION_SIGNAL", "BULLISH"):
            return "ESCALATION_MODE"
        return None  # NEUTRAL signals don't count toward a flip

    def _price_confirms_transition(
        self,
        new_regime: NewsRegime,
        price_history: Deque[Tuple[float, float]],
    ) -> bool:
        """
        Check if recent price movement confirms the news regime transition.
        DEAL_MODE→ESCALATION_MODE: need price rising (bad for NO positions).
        ESCALATION_MODE→DEAL_MODE: need price falling (bad for YES positions).
        """
        if len(price_history) < 2:
            return False  # Not enough data — be cautious, don't act

        t_now, p_now = price_history[-1]
        target_t = t_now - self.PRICE_CONFIRM_MINUTES * 60
        p_old = next(
            (p for t, p in reversed(price_history) if t <= target_t),
            price_history[0][1],
        )
        if not p_old:
            return False

        change_pct = (p_now - p_old) / p_old * 100.0

        if new_regime == "ESCALATION_MODE":
            # Price needs to be rising (supply fear returning)
            confirmed = change_pct >= self.PRICE_CONFIRM_PCT
            self.log(
                f"Price confirm check ESCALATION: {change_pct:+.2f}% "
                f"(need +{self.PRICE_CONFIRM_PCT}%) → {'✓' if confirmed else '✗'}"
            )
            return confirmed
        else:  # DEAL_MODE
            # Price needs to be falling (supply restored)
            confirmed = change_pct <= -self.PRICE_CONFIRM_PCT
            self.log(
                f"Price confirm check DEAL: {change_pct:+.2f}% "
                f"(need -{self.PRICE_CONFIRM_PCT}%) → {'✓' if confirmed else '✗'}"
            )
            return confirmed

    def ingest(
        self,
        signal: NewsSignal,
        price_history: Deque[Tuple[float, float]],
        event_engines: Dict[str, "EventEngine"],
        params: "RuntimeParams",
    ) -> None:
        """
        Process an incoming news signal. Only fires protective sells on regime transitions
        confirmed by price movement.
        """
        self.recent_signals.appendleft(signal)
        self.log(
            f"Signal: {signal.signal} ({signal.confidence:.0%}) "
            f"[{signal.source}] {signal.headline[:60]}"
        )

        # Map to regime direction
        target_regime = self._signal_to_regime(signal.signal)
        if target_regime is None:
            self.log("NEUTRAL signal — no regime impact")
            return

        # Same as current regime — just reinforce, no transition
        if target_regime == self.regime:
            self.pending_signal = None
            self.pending_count = 0
            self.log(f"Sustained {self.regime} — no transition")
            return

        # Accumulate toward a flip
        if self.pending_signal == target_regime:
            self.pending_count += 1
        else:
            self.pending_signal = target_regime
            self.pending_count = 1

        self.log(
            f"Pending {target_regime}: {self.pending_count}/{self.FLIP_THRESHOLD} "
            f"(current: {self.regime})"
        )

        if self.pending_count < self.FLIP_THRESHOLD:
            return  # Not enough signals yet

        # Check flip interval throttle
        if now_ts() - self.last_transition_ts < self.min_flip_interval:
            self.log(f"Flip throttled — too soon since last transition")
            return

        # Require price confirmation
        if not self._price_confirms_transition(target_regime, price_history):
            self._add_action(
                "warn", "ALERT",
                f"News regime shifting {self.regime}→{target_regime} "
                f"but price not yet confirming — watching.",
                headline=signal.headline,
                url=signal.url,
                source=signal.source,
            )
            # Reset pending so we need fresh signals after price catches up
            self.pending_count = 0
            return

        # --- Regime transition confirmed ---
        old_regime = self.regime
        self.regime = target_regime
        self.pending_signal = None
        self.pending_count = 0
        self.last_transition_ts = now_ts()

        self.log(f"REGIME TRANSITION: {old_regime} → {self.regime}")
        self._add_action(
            "danger", "ALERT",
            f"News regime transition: {old_regime} → {self.regime}.",
            headline=signal.headline,
            url=signal.url,
            source=signal.source,
        )

        # Fire protective sells on endangered positions
        self._protect_positions(event_engines, params)

    def _protect_positions(
        self,
        event_engines: Dict[str, "EventEngine"],
        params: "RuntimeParams",
    ) -> None:
        """
        On regime transition, sell positions that are now endangered by the new narrative.

        DEAL_MODE → ESCALATION_MODE: price rising → NO positions at risk
        ESCALATION_MODE → DEAL_MODE: price falling → YES positions at risk
        """
        endangered_side: Side = "no" if self.regime == "ESCALATION_MODE" else "yes"

        for event_ticker, eng in event_engines.items():
            if not eng.armed:
                continue
            consensus = eng.consensus_price()
            for p in eng.positions:
                if p.count <= 0:
                    continue
                if p.side != endangered_side:
                    continue
                if eng.position_endangered(p, consensus):
                    reason = f"News regime {self.regime}: endangered {p.side.upper()} position."
                    eng._add_action(
                        "danger", "SELL_LEG", reason,
                        p.ticker, p.side, p.count,
                    )
                    if params.mode == "live" and eng.armed and eng.coordinator is not None:
                        asyncio.create_task(
                            eng.coordinator.request_auto_sell(eng, p, p.count, reason)
                        )
                else:
                    # Not yet endangered but warn — cushion may be thin
                    eng._add_action(
                        "warn", "ALERT",
                        f"News regime {self.regime}: {p.side.upper()} {p.ticker} "
                        f"not yet endangered but watch closely.",
                    )


# ---------------------------------------------------------------------------
# Price sources
# ---------------------------------------------------------------------------

class PriceSource:
    name: str

    async def get_price(self) -> PricePoint:
        raise NotImplementedError


class YahooPriceSource(PriceSource):
    def __init__(self, symbol: str = "CL=F") -> None:
        self.name = "yahoo"
        self.symbol = symbol

    async def get_price(self) -> PricePoint:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{self.symbol}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        try:
            r = requests.get(url, headers=headers, timeout=4)
            r.raise_for_status()
            data = r.json()
            result = data["chart"]["result"][0]
            price = result["meta"].get("regularMarketPrice")
            if price is None:
                quotes = result["indicators"]["quote"][0]["close"]
                price = next((x for x in reversed(quotes) if x is not None), None)
            if price is None:
                raise ValueError("No price in Yahoo response")
            return PricePoint(source=self.name, price=float(price), ts=now_ts())
        except Exception as e:
            return PricePoint(source=self.name, price=None, ts=now_ts(), error=str(e))


class OilPriceSource(PriceSource):
    def __init__(self, symbol: str = "WTI-Crude", min_refresh_seconds: int = 15) -> None:
        self.name = "oilprice"
        self.symbol = symbol
        self.min_refresh_seconds = min_refresh_seconds
        self._last_price: Optional[float] = None
        self._last_fetch_ts: float = 0.0

    def _fetch(self) -> float:
        html = requests.get(
            "https://oilprice.com/oil-price-charts/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        ).text
        soup = BeautifulSoup(html, "html.parser")
        row = soup.find("tr", {"data-name": self.symbol})
        if not row:
            raise ValueError(f"Row not found for {self.symbol}")
        cell = row.find("td", class_="last_price")
        if not cell:
            raise ValueError(f"Price cell not found for {self.symbol}")
        price = cell.get("data-price") or cell.get_text(strip=True)
        return float(price)

    async def get_price(self) -> PricePoint:
        now = now_ts()
        if now - self._last_fetch_ts < self.min_refresh_seconds:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts,
                stale=self._last_price is None,
            )
        try:
            price = self._fetch()
            self._last_price = price
            self._last_fetch_ts = now
            return PricePoint(source=self.name, price=price, ts=now)
        except Exception as e:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts or now,
                stale=True,
                error=str(e),
            )


class GoldPriceSource(PriceSource):
    """
    Gold spot price (USD/troy oz) — primary: gold-api.com.
    """

    _GOLDAPI_URL = "https://api.gold-api.com/price/XAU"
    _SANITY_LO   = 1000.0
    _SANITY_HI   = 8000.0

    def __init__(self, min_refresh_seconds: int = 30) -> None:
        self.name = "goldprice"
        self.min_refresh_seconds = min_refresh_seconds
        self._last_price: Optional[float] = None
        self._last_fetch_ts: float = 0.0

    def _fetch_goldapi(self) -> float:
        r = requests.get(self._GOLDAPI_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        r.raise_for_status()
        price = float(r.json()["price"])
        if not (self._SANITY_LO <= price <= self._SANITY_HI):
            raise ValueError(f"gold-api: price {price} outside sanity range")
        return price

    async def get_price(self) -> PricePoint:
        now = now_ts()
        if now - self._last_fetch_ts < self.min_refresh_seconds:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts,
                stale=self._last_price is None,
            )
        errors = []
        try:
            price = self._fetch_goldapi()
            self._last_price = price
            self._last_fetch_ts = now
            return PricePoint(source=self.name, price=price, ts=now)
        except Exception as e:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts or now,
                stale=True,
                error=str(e),
            )

class SilverPriceSource(PriceSource):
    """
    Silver spot price (USD/troy oz) — primary: gold-api.com

    Mirrors GoldPriceSource exactly except for sanity range and API URL.

    Sanity range: silver has traded $10-$50 since 1980 with brief spikes;
    $8-$100 gives room without accepting garbage.
    """
    _SILVER_API = "https://api.gold-api.com/price/XAG"
    _SANITY_LO = 8.0
    _SANITY_HI = 100.0

    def __init__(self, min_refresh_seconds: int = 30) -> None:
        self.name = "silverprice"
        self.min_refresh_seconds = min_refresh_seconds
        self._last_price: Optional[float] = None
        self._last_fetch_ts: float = 0.0

    def _fetch_silverapi(self) -> float:
        r = requests.get(self._SILVER_API, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "price" not in data:
            raise ValueError("Silver API: unexpected response format")
        price = float(data["price"])
        if not (self._SANITY_LO <= price <= self._SANITY_HI):
            raise ValueError(f"Silver API: price {price} outside sanity range")
        return price

    async def get_price(self) -> PricePoint:
        now = now_ts()
        if now - self._last_fetch_ts < self.min_refresh_seconds:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts,
                stale=self._last_price is None,
            )
        try:
            price = self._fetch_silverapi()
            self._last_price = price
            self._last_fetch_ts = now
            return PricePoint(source=self.name, price=price, ts=now)
        except Exception as e:
            return PricePoint(
                source=self.name,
                price=self._last_price,
                ts=self._last_fetch_ts or now,
                stale=True,
                error=str(e),
            )


# ---------------------------------------------------------------------------
# Kalshi API client
# ---------------------------------------------------------------------------

class KalshiClient:
    def __init__(
        self,
        api_key_id: Optional[str] = None,
        private_key_path: Optional[str] = None,
        base_url: str = "https://api.elections.kalshi.com",
    ) -> None:
        self.api_key_id = api_key_id
        self.base_url = base_url.rstrip("/")
        self.private_key = None

        if private_key_path:
            with open(private_key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        if not self.api_key_id or not self.private_key:
            raise RuntimeError("Kalshi API credentials not configured")
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{path}".encode("utf-8")
        sig = self.private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "Content-Type": "application/json",
        }

    def _money_to_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        value = str(value).replace("$", "").strip()
        return float(value) if value else None

    async def get_portfolio_positions(self) -> List[Dict]:
        path = "/trade-api/v2/portfolio/positions"
        url = self.base_url + path
        r = requests.get(url, headers=self._auth_headers("GET", path), timeout=10)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        return r.json().get("market_positions", [])

    async def get_implied_event_price(self, event_ticker: str) -> Optional[float]:
        url = self.base_url + "/trade-api/v2/markets"
        r = requests.get(url, params={"event_ticker": event_ticker, "status": "open", "limit": 200}, timeout=10)
        r.raise_for_status()
        markets = r.json().get("markets", [])

        points = []
        for m in markets:
            ticker = m.get("ticker", "")
            try:
                strike = float(ticker.split("-T")[-1])
            except Exception:
                continue
            yes_bid = self._money_to_float(m.get("yes_bid_dollars"))
            yes_ask = self._money_to_float(m.get("yes_ask_dollars"))
            if yes_bid is None or yes_ask is None:
                continue
            mid = (yes_bid + yes_ask) / 2 if (yes_bid > 0 and yes_ask > 0) else (yes_bid or yes_ask)
            points.append((strike, mid))

        if len(points) < 2:
            return None
        points.sort()
        for (s1, p1), (s2, p2) in zip(points, points[1:]):
            if (p1 >= 0.5 and p2 <= 0.5) or (p1 <= 0.5 and p2 >= 0.5):
                if p1 == p2:
                    return (s1 + s2) / 2
                return s1 + (0.5 - p1) * (s2 - s1) / (p2 - p1)
        return None

    async def get_market_quote_for_side(self, ticker: str, side: Side) -> Tuple[float, float, float]:
        url = f"{self.base_url}/trade-api/v2/markets/{ticker}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        m = r.json().get("market", r.json())

        if side == "yes":
            bid = self._money_to_float(m.get("yes_bid_dollars")) or 0.0
            ask = self._money_to_float(m.get("yes_ask_dollars")) or 0.0
        else:
            bid = self._money_to_float(m.get("no_bid_dollars")) or 0.0
            ask = self._money_to_float(m.get("no_ask_dollars")) or 0.0

        mid = (bid + ask) / 2 if bid and ask else bid or ask or 0.0
        return bid, ask, mid

    async def sell_position(self, ticker: str, side: Side, qty: int) -> Dict[str, Any]:
        # V2 events/orders endpoint — migrated from deprecated /portfolio/orders
        path = "/trade-api/v2/portfolio/events/orders"
        url = self.base_url + path
        # V2 uses bid/ask from YES perspective only
        # sell YES = ask side at floor price 0.01
        # sell NO = bid side at ceiling price 0.99
        v2_side = "ask" if side == "yes" else "bid"
        floor_price = "0.0100" if side == "yes" else "0.9900"
        payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": v2_side,
            "count": str(float(qty)),
            "price": floor_price,
            "time_in_force": "immediate_or_cancel",
            "reduce_only": False,
            "self_trade_prevention_type": "taker_at_cross",
        }
        try:
            r = requests.post(url, headers=self._auth_headers("POST", path), json=payload, timeout=10)
            return {"ok": r.ok, "status_code": r.status_code, "response": r.json() if r.text else {}}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Per-event state machine
# ---------------------------------------------------------------------------

class EventEngine:
    """State machine and price tracking for one Kalshi event."""

    def __init__(
        self,
        event_ticker: str,
        settlement_time: Optional[datetime],
        params: RuntimeParams,
        kalshi: KalshiClient,
        actions_queue: Deque[Action],
        logs_queue: Deque[str],
        armed: bool = False,
        coordinator: Optional["Engine"] = None,
    ) -> None:
        self.event_ticker = event_ticker
        self.settlement_time = settlement_time
        self.params = params       # shared reference — global param updates apply automatically
        self.kalshi = kalshi
        self._actions = actions_queue
        self._logs = logs_queue
        # Back-reference to the Engine, used to route auto-sells through the
        # Telegram confirmation gate. Stop losses bypass this and execute
        # directly via _execute_sell; everything else must ask first.
        self.coordinator = coordinator

        self.positions: List[Position] = []
        self.armed = armed
        self.state: BotState = "NORMAL" if armed else "OBSERVE_ONLY"
        self.last_prices: Dict[str, PricePoint] = {}
        self.price_history: Deque[Tuple[float, float]] = deque(maxlen=2000)
        self.shock_start_ts: Optional[float] = None
        self.shock_extreme: Optional[float] = None
        self.shock_direction: Optional[Literal["up", "down"]] = None

        # Persistent per-engine spot sources preserve throttle cache across ticks.
        self._oilprice_source: Optional[OilPriceSource] = None
        self._goldprice_source: Optional[GoldPriceSource] = None
        self._silverprice_source: Optional[SilverPriceSource] = None

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._logs.appendleft(f"[{self.event_ticker}] {stamp} {msg}")

    def _add_action(
        self,
        severity: Literal["info", "warn", "danger"],
        action: Literal["HOLD", "TRIM", "SELL_LEG", "SELL_ALL", "ALERT"],
        reason: str,
        ticker: Optional[str] = None,
        side: Optional[Side] = None,
        qty: Optional[int] = None,
    ) -> None:
        recent = list(self._actions)[:5]
        for a in recent:
            if a.action == action and a.ticker == ticker and a.reason == reason and a.event_ticker == self.event_ticker:
                return
        self._actions.appendleft(Action(
            ts=now_ts(), severity=severity, action=action, reason=reason,
            event_ticker=self.event_ticker, ticker=ticker, side=side, qty=qty,
        ))
        self.log(f"{severity.upper()} {action}: {reason} {ticker or ''} {qty or ''}")

    def set_armed(self, armed: bool) -> None:
        self.armed = armed
        if armed and self.state == "OBSERVE_ONLY":
            self.state = "NORMAL"
        elif not armed:
            self.state = "OBSERVE_ONLY"

    # -----------------------------------------------------------------------
    # Price fetching
    # -----------------------------------------------------------------------

    async def update_prices(self) -> None:
        src_cfg = self.params.sources
        prefix = instrument_prefix(self.event_ticker)
        instrument = detect_instrument(self.event_ticker)
        if not instrument:
            self.log(f"WARNING: unknown instrument prefix '{prefix}' — add to INSTRUMENTS")
        source_objects: List[PriceSource] = []

        if src_cfg.get("yahoo", {}).get("enabled"):
            symbol = instrument.get("yahoo_symbol") or src_cfg["yahoo"].get("symbol", "CL=F")
            source_objects.append(YahooPriceSource(symbol))

        if src_cfg.get("oilprice", {}).get("enabled"):
            if instrument.get("use_goldprice"):
                # Gold instrument — goldprice.org instead of oilprice.com.
                refresh = src_cfg["oilprice"].get("min_refresh_seconds", 30)
                if self._goldprice_source is None:
                    self._goldprice_source = GoldPriceSource(min_refresh_seconds=refresh)
                source_objects.append(self._goldprice_source)
            elif instrument.get("use_silverprice"):
                # Silver instrument — XAGUSD, mirroring the gold path.
                # Reuses the `oilprice.enabled` config toggle intentionally: it's
                # the "third-party spot source" gate, whatever the underlying feed.
                refresh = src_cfg["oilprice"].get("min_refresh_seconds", 30)
                if self._silverprice_source is None:
                    self._silverprice_source = SilverPriceSource(min_refresh_seconds=refresh)
                source_objects.append(self._silverprice_source)
            else:
                oilprice_sym = instrument.get("oilprice_symbol") or src_cfg["oilprice"].get("symbol")
                if oilprice_sym:
                    cfg = src_cfg["oilprice"]
                    if self._oilprice_source is None or self._oilprice_source.symbol != oilprice_sym:
                        self._oilprice_source = OilPriceSource(
                            symbol=oilprice_sym,
                            min_refresh_seconds=cfg.get("min_refresh_seconds", 15),
                        )
                    source_objects.append(self._oilprice_source)

        results: List[PricePoint] = []
        if source_objects:
            results = list(await asyncio.gather(*[s.get_price() for s in source_objects]))

        await self._refresh_position_quotes()

        if src_cfg.get("kalshi_implied", {}).get("enabled"):
            try:
                implied = await self.kalshi.get_implied_event_price(self.event_ticker)
                results.append(PricePoint(source="kalshi_implied", price=implied, ts=now_ts()))
            except Exception as e:
                results.append(PricePoint(source="kalshi_implied", price=None, ts=now_ts(), stale=True, error=str(e)))

        stale_after = self.params.source_consensus.get("stale_after_seconds", 90)
        t = now_ts()
        for pp in results:
            pp.stale = (t - pp.ts) > stale_after or pp.price is None
            self.last_prices[pp.source] = pp

        consensus = self.consensus_price()
        if consensus is not None:
            self.price_history.append((t, consensus))

    async def _refresh_position_quotes(self) -> None:
        for p in self.positions:
            bid, ask, mid = await self.kalshi.get_market_quote_for_side(p.ticker, p.side)
            if mid <= 0:
                mid = p.current_mid or p.avg_price
                bid = max(0.0, mid - 0.02)
                ask = min(1.0, mid + 0.02)
            p.current_bid = clamp_price(bid)
            p.current_ask = clamp_price(ask)
            p.current_mid = clamp_price(mid)
            p.peak_mid = max(p.peak_mid, p.current_mid)

    def consensus_price(self) -> Optional[float]:
        # Include kalshi_implied in the median — for gold events it is the
        # most relevant price source (Pyth-based, settles at 5pm EDT spot).
        # Previously excluded, which caused stop/endangered decisions to rely
        # solely on Yahoo futures price (~$15 divergence observed Jul 1 2026).
        vals = [
            pp.price for pp in self.last_prices.values()
            if pp.price is not None and not pp.stale
        ]
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    def kalshi_implied_price(self) -> Optional[float]:
        pp = self.last_prices.get("kalshi_implied")
        return pp.price if pp else None

    def _price_change_pct(self, minutes: float) -> Optional[float]:
        if not self.price_history:
            return None
        t_now, p_now = self.price_history[-1]
        target = t_now - minutes * 60
        old = next((price for ts, price in reversed(self.price_history) if ts <= target), None)
        if old is None:
            old = self.price_history[0][1]
        return None if not old else (p_now - old) / old * 100.0

    # -----------------------------------------------------------------------
    # State machine
    # -----------------------------------------------------------------------

    def evaluate_state_machine(self) -> None:
        if self.armed and self.state == "OBSERVE_ONLY":
            self.state = "NORMAL"
        elif not self.armed:
            self.state = "OBSERVE_ONLY"
            return  # No logic when disarmed

        self._detect_shock()
        self._classify_shock()
        self._structure_drift()
        self._time_risk()
        self._late_day_thin_cushion()

        if self.state == "RECOVERY":
            self.state = "NORMAL"

    def _detect_shock(self) -> None:
        price = self.consensus_price()
        if price is None:
            return
        window = self.params.shock_logic.get("shock_window_minutes", 10)
        threshold = self.params.shock_logic.get("shock_threshold_pct", 2.0)
        change = self._price_change_pct(window)
        if change is None:
            return
        if abs(change) >= threshold and self.state not in ("SHOCK_WATCH", "REGIME_BREAK", "FLATTENING"):
            self.state = "SHOCK_WATCH"
            self.shock_start_ts = now_ts()
            self.shock_extreme = price
            self.shock_direction = "down" if change < 0 else "up"
            self._add_action("warn", "ALERT", f"Shock detected: {change:.2f}% over {window} min.")
        if self.state == "SHOCK_WATCH":
            if self.shock_direction == "down":
                self.shock_extreme = min(self.shock_extreme or price, price)
            else:
                self.shock_extreme = max(self.shock_extreme or price, price)

    def _classify_shock(self) -> None:
        if self.state != "SHOCK_WATCH" or self.shock_start_ts is None:
            return
        price = self.consensus_price()
        if price is None or self.shock_extreme is None:
            return
        elapsed_min = (now_ts() - self.shock_start_ts) / 60.0
        recovery_window = self.params.shock_logic.get("recovery_window_minutes", 15)
        min_recovery = self.params.shock_logic.get("min_recovery_pct", 0.75)
        if self.shock_direction == "down":
            recovery = (price - self.shock_extreme) / self.shock_extreme * 100.0
        else:
            recovery = (self.shock_extreme - price) / self.shock_extreme * 100.0
        if recovery >= min_recovery:
            self.state = "RECOVERY"
            self._add_action("info", "HOLD", f"Shock recovery confirmed: {recovery:.2f}% rebound.")
            self._clear_shock()
            return
        if elapsed_min >= recovery_window:
            self.state = "REGIME_BREAK"
            self._add_action("danger", "ALERT", f"Regime break: recovery only {recovery:.2f}% after {elapsed_min:.1f} min.")
            self._exit_endangered_legs("Regime break confirmed after shock failed to recover.")

    def _clear_shock(self) -> None:
        self.shock_start_ts = None
        self.shock_extreme = None
        self.shock_direction = None

    def position_endangered(self, p: Position, consensus: Optional[float]) -> bool:
        if consensus is None:
            return False
        buf_lo = self.params.structure.get("lower_boundary_buffer", 0.75)
        buf_hi = self.params.structure.get("upper_boundary_buffer", 0.75)
        if p.side == "yes":
            return consensus < (p.strike + buf_lo)
        return consensus > (p.strike - buf_hi)

    def _exit_endangered_legs(self, reason: str) -> None:
        consensus = self.consensus_price()
        for p in self.positions:
            if p.count <= 0:
                continue
            if self.position_endangered(p, consensus):
                self._add_action("danger", "SELL_LEG", reason, p.ticker, p.side, p.count)
                if self.params.mode == "live" and self.armed and self.coordinator is not None:
                    # Confirmation gate — no direct execution for non-stop sells.
                    asyncio.create_task(
                        self.coordinator.request_auto_sell(self, p, p.count, reason)
                    )

    def _structure_drift(self) -> None:
        yes = sum(p.count for p in self.positions if p.side == "yes")
        no = sum(p.count for p in self.positions if p.side == "no")
        if not yes or not no:
            return
        ratio = max(yes / no, no / yes)
        max_ratio = self.params.structure.get("max_yes_no_imbalance", 1.75)
        if ratio > max_ratio:
            self._add_action("warn", "ALERT", f"Structure drift: imbalance ratio {ratio:.2f} > {max_ratio:.2f}.")

    def _time_risk(self) -> None:
        if self.settlement_time is None:
            return
        now = datetime.now(self.settlement_time.tzinfo or timezone.utc)
        minutes_left = (self.settlement_time - now).total_seconds() / 60.0
        flatten_under = self.params.time_risk.get("flatten_unstable_under_minutes", 60)
        if minutes_left <= flatten_under:
            if any(self.position_endangered(p, self.consensus_price()) for p in self.positions):
                self._add_action("danger", "ALERT", f"Late-day unstable structure: {minutes_left:.0f} min to settlement.")
                self._exit_endangered_legs("Late-day unstable structure.")

    def _late_day_thin_cushion(self) -> None:
        """
        Late day protection: if we're within thin_cushion_window_minutes of settlement
        and a position has a small price cushion but high market value, sell it.

        Rationale: a $0.93 position with $0.30 cushion is not worth holding into
        settlement when a small move against you can wipe the profit.

        Config keys (under time_risk):
            thin_cushion_window_minutes  — how close to settlement (default 90)
            thin_cushion_threshold       — max cushion in $ to trigger (default 0.50)
            thin_cushion_min_value       — min position mid to trigger (default 0.90)
        """
        if self.settlement_time is None:
            return
        now = datetime.now(self.settlement_time.tzinfo or timezone.utc)
        minutes_left = (self.settlement_time - now).total_seconds() / 60.0
        window = self.params.time_risk.get("thin_cushion_window_minutes", 90)
        if minutes_left > window:
            return

        consensus = self.consensus_price()
        if consensus is None:
            return

        cushion_threshold = self.params.time_risk.get("thin_cushion_threshold", 0.50)
        min_value = self.params.time_risk.get("thin_cushion_min_value", 0.90)

        for p in self.positions:
            if p.count <= 0:
                continue
            mid = p.current_bid or p.current_mid
            if mid < min_value:
                continue  # Position not valuable enough to trigger
            cushion = abs(consensus - p.strike)
            if cushion < cushion_threshold:
                reason = (
                    f"Late-day thin cushion: {cushion:.2f} cushion at {mid:.2f} "
                    f"with {minutes_left:.0f} min left."
                )
                self._add_action(
                    "warn", "SELL_LEG", reason,
                    p.ticker, p.side, p.count,
                )
                if self.params.mode == "live" and self.armed and self.coordinator is not None:
                    asyncio.create_task(
                        self.coordinator.request_auto_sell(self, p, p.count, reason)
                    )

    # -----------------------------------------------------------------------
    # Execution
    # -----------------------------------------------------------------------

    async def _execute_sell(self, p: Position, qty: int) -> None:
        if qty <= 0 or p.count <= 0:
            return
        if self.params.mode != "live":
            self.log(f"PAPER: would sell {qty} {p.side.upper()} {p.ticker}")
            return
        qty = min(qty, p.count)
        # Decrement immediately so subsequent ticks don't re-fire on the same position
        # before sync_positions() catches up. Restored on failure.
        p.count -= qty
        result = await self.kalshi.sell_position(p.ticker, p.side, qty)
        if result.get("ok"):
            self.log(f"SOLD {qty} {p.side.upper()} {p.ticker}")
        else:
            p.count += qty  # Restore on failure so it can retry
            self.log(f"SELL FAILED {p.ticker}: {result}")

    async def flatten(self) -> List[Dict]:
        self.state = "FLATTENING"
        results = []
        for p in self.positions:
            if p.count > 0:
                qty = p.count
                if self.params.mode == "live" and self.armed:
                    result = await self.kalshi.sell_position(p.ticker, p.side, qty)
                    if result.get("ok"):
                        p.count = 0
                    results.append(result)
                else:
                    results.append({"ok": True, "paper": True, "ticker": p.ticker, "qty": qty})
        return results

    # -----------------------------------------------------------------------
    # Risk / status
    # -----------------------------------------------------------------------

    def settlement_value_at(self, price: float) -> float:
        total = 0.0
        for p in self.positions:
            win = (price > p.strike) if p.side == "yes" else (price <= p.strike)
            total += p.count * (1.0 if win else 0.0)
        return total

    def risk_snapshot(self) -> RiskSnapshot:
        _empty = RiskSnapshot(
            cost_basis=0, mark_value=0, unrealized_pl=0, max_payout=0,
            max_profit=0, worst_settlement_value=0, worst_settlement_loss=0,
            modeled_stop_loss_value=0, modeled_stop_loss=0,
            yes_count=0, no_count=0, imbalance_ratio=0, settlement_map=[],
        )
        if not self.positions:
            return _empty

        cost = sum(p.count * p.avg_price for p in self.positions)
        mark = sum(p.count * (p.current_bid or p.current_mid) for p in self.positions)
        max_payout = sum(p.count for p in self.positions)
        yes = sum(p.count for p in self.positions if p.side == "yes")
        no = sum(p.count for p in self.positions if p.side == "no")
        ratio = max(yes / no, no / yes) if yes and no else float("inf") if yes or no else 0.0

        strikes = sorted({p.strike for p in self.positions})
        test_pts = sorted({s + d for s in strikes for d in (-1, 0, 1)})
        settlement_map = []
        worst_val = float("inf")
        for x in test_pts:
            v = self.settlement_value_at(x)
            worst_val = min(worst_val, v)
            settlement_map.append({"price": round(x, 2), "settlement_value": round(v, 2), "pl": round(v - cost, 2)})

        consensus = self.consensus_price()
        modeled = sum(
            p.count * (max(0.01, p.current_bid or p.current_mid) if self.position_endangered(p, consensus)
                       else (p.current_bid or p.current_mid))
            for p in self.positions
        )

        return RiskSnapshot(
            cost_basis=round(cost, 2),
            mark_value=round(mark, 2),
            unrealized_pl=round(mark - cost, 2),
            max_payout=round(max_payout, 2),
            max_profit=round(max_payout - cost, 2),
            worst_settlement_value=round(worst_val if math.isfinite(worst_val) else 0.0, 2),
            worst_settlement_loss=round((worst_val if math.isfinite(worst_val) else 0.0) - cost, 2),
            modeled_stop_loss_value=round(modeled, 2),
            modeled_stop_loss=round(modeled - cost, 2),
            yes_count=yes,
            no_count=no,
            imbalance_ratio=round(ratio, 2) if math.isfinite(ratio) else 999.0,
            settlement_map=settlement_map,
        )

    def event_status(self) -> EventStatus:
        return EventStatus(
            event_ticker=self.event_ticker,
            state=self.state,
            armed=self.armed,
            consensus_price=self.consensus_price(),
            kalshi_implied_price=self.kalshi_implied_price(),
            prices=list(self.last_prices.values()),
            positions=self.positions,
            risk=self.risk_snapshot(),
        )


# ---------------------------------------------------------------------------
# Multi-event coordinator
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.actions: Deque[Action] = deque(maxlen=100)
        self.logs: Deque[str] = deque(maxlen=200)

        # Support both "api:" (current config.yaml) and "kalshi:" key layouts.
        api_cfg = config.get("api") or config.get("kalshi") or {}
        base_url = api_cfg.get("base_url", "https://api.elections.kalshi.com")
        # Strip any /trade-api path suffix so paths are never doubled.
        if "/trade-api" in base_url:
            base_url = base_url[: base_url.index("/trade-api")]

        self.kalshi = KalshiClient(
            api_key_id=api_cfg.get("key_id") or api_cfg.get("api_key_id"),
            private_key_path=api_cfg.get("private_key_path"),
            base_url=base_url,
        )

        self.params = RuntimeParams(
            mode=config.get("mode", "paper"),
            armed=bool(config.get("armed", False)),
            poll_seconds=int(config.get("poll_seconds", 3)),
            profit_harvest=config["profit_harvest"],
            shock_logic=config["shock_logic"],
            source_consensus=config["source_consensus"],
            structure=config["structure"],
            time_risk=config["time_risk"],
            sources=config["sources"],
            safety=config.get("safety", {"global_drawdown_limit": -100.0}),
        )

        self.event_engines: Dict[str, EventEngine] = {}

        # Per-position stop loss / take profit bots
        # key: ticker, value: config dict from UI
        self.position_bots: Dict[str, Dict[str, Any]] = {}

        # News regime tracker — shared across all events
        self.news_regime = NewsRegimeTracker(
            actions_queue=self.actions,
            logs_queue=self.logs,
        )

        # Bootstrap from config positions (bootstrap survives even if portfolio sync fails).
        if config.get("event_ticker") and config.get("positions"):
            self._bootstrap_from_config(config)

        self._running = False

    def _bootstrap_from_config(self, config: Dict) -> None:
        event_ticker = config["event_ticker"]
        settlement_time = self._parse_dt(config.get("settlement_time"))
        engine = EventEngine(
            event_ticker=event_ticker,
            settlement_time=settlement_time,
            params=self.params,
            kalshi=self.kalshi,
            actions_queue=self.actions,
            logs_queue=self.logs,
            armed=self.params.armed,
            coordinator=self,
        )
        positions = [Position(**p) for p in config.get("positions", [])]
        for p in positions:
            p.current_mid = p.avg_price
            p.peak_mid = p.avg_price
        engine.positions = positions
        self.event_engines[event_ticker] = engine
        self.log(f"Bootstrap: loaded {len(positions)} config positions for {event_ticker}")

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.appendleft(f"{stamp} {msg}")

    def _add_action(self, severity: str, action: str, reason: str) -> None:
        self.actions.appendleft(Action(ts=now_ts(), severity=severity, action=action, reason=reason))
        self.log(f"{severity.upper()} {action}: {reason}")

    # -----------------------------------------------------------------------
    # Portfolio sync
    # -----------------------------------------------------------------------

    async def sync_positions(self) -> None:
        """Replace config positions with live portfolio from Kalshi API."""
        if not self.kalshi.api_key_id:
            return  # No credentials — rely on bootstrap
        try:
            raw = await self.kalshi.get_portfolio_positions()
        except Exception as e:
            self.log(f"Portfolio sync failed: {e}")
            return

        if not raw:
            self.log("Portfolio sync: no open positions returned")
            # Clear all event engines since portfolio is empty
            self.event_engines.clear()
            return

        self.log(f"Portfolio sync: {len(raw)} position(s) received")

        # Parse raw positions into a dict keyed by event_ticker.
        # Kalshi API v2 uses:
        #   position_fp  — signed fixed-point string: "+100.00" = 100 YES, "-50.00" = 50 NO
        #   total_traded_dollars  — cost of contracts (ex-fees), string
        #   fees_paid_dollars     — fees paid, string
        #   market_exposure_dollars — current mark value, string (NOT cost basis)
        live: Dict[str, List[Position]] = defaultdict(list)
        for p in raw:
            ticker = p.get("ticker", "")
            if not ticker:
                continue
            event_ticker = ticker.split("-T")[0]

            # Determine side and count from signed position_fp.
            pos_raw = p.get("position_fp") or p.get("position") or "0"
            try:
                pos_float = float(pos_raw)
            except (TypeError, ValueError):
                self.log(f"Skip {ticker}: unparseable position_fp={pos_raw!r}")
                continue
            if pos_float == 0:
                continue

            side: Side = "yes" if pos_float > 0 else "no"
            count = int(abs(pos_float))
            count_fp = abs(pos_float)  # fractional count for avg_price calc

            # Cost basis = contracts cost + fees (matches what Kalshi shows as avg price).
            def _flt(val: Any) -> float:
                try:
                    return float(val or 0)
                except (TypeError, ValueError):
                    return 0.0

            total_traded = _flt(p.get("total_traded_dollars"))
            fees_paid    = _flt(p.get("fees_paid_dollars"))
            avg_price    = (total_traded + fees_paid) / count_fp if count_fp else 0.0

            try:
                live[event_ticker].append(Position(
                    ticker=ticker,
                    side=side,
                    strike=parse_strike(ticker),
                    count=count,
                    avg_price=round(avg_price, 4),
                ))
                self.log(f"  {ticker} {side.upper()} x{count} @ {avg_price:.4f}")
            except Exception as e:
                self.log(f"Skip position {ticker}: {e}")

        # Update/create EventEngines from live data.
        for event_ticker, new_positions in live.items():
            if event_ticker not in self.event_engines:
                self.event_engines[event_ticker] = EventEngine(
                    event_ticker=event_ticker,
                    settlement_time=None,
                    params=self.params,
                    kalshi=self.kalshi,
                    actions_queue=self.actions,
                    logs_queue=self.logs,
                    armed=False,
                    coordinator=self,
                )
                self.log(f"New event discovered: {event_ticker}")

            # Merge: preserve quote/peak data for existing positions.
            eng = self.event_engines[event_ticker]
            existing = {pos.ticker: pos for pos in eng.positions}
            merged = []
            for np in new_positions:
                if np.ticker in existing:
                    ex = existing[np.ticker]
                    ex.count = np.count
                    merged.append(ex)
                else:
                    np.current_mid = np.avg_price
                    np.peak_mid = np.avg_price
                    merged.append(np)
            eng.positions = merged

        # Remove engines whose event no longer has open positions.
        stale = [et for et in list(self.event_engines) if et not in live]
        for et in stale:
            self.log(f"Event closed/removed: {et}")
            del self.event_engines[et]

    # -----------------------------------------------------------------------
    # Safety
    # -----------------------------------------------------------------------

    def _check_global_safety(self) -> None:
        pass  # PERMANENTLY DISABLED — unreliable due to avg_price calculation issues

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    async def tick(self) -> None:
        await self.sync_positions()
        for eng in list(self.event_engines.values()):
            await eng.update_prices()
            eng.evaluate_state_machine()
        # self._check_global_safety()  # PERMANENTLY DISABLED — avg_price bugs make this unreliable
        await self._execute_position_bots()

    async def _execute_position_bots(self) -> None:
        """
        Evaluate per-position bot configs every tick.
        Reads directly from position_bots dict — not p.bot_config which
        gets wiped on every sync_positions() call.

        Sell routing:
          - STOP LOSS auto-executes via _execute_sell (safety net; user
            explicitly wants this to bypass confirmation).
          - Everything else (limit sell, harvest, time exit) routes through
            request_auto_sell → Telegram confirmation. No answer = no sale,
            which is what the user asked for.
        """
        now_dt = datetime.now()
        for eng in list(self.event_engines.values()):
            for p in eng.positions:
                if p.count <= 0:
                    continue
                bot = self.position_bots.get(p.ticker)
                if not bot:
                    continue
                cfg = bot["config"]
                mid = p.current_bid or p.current_mid
                if not mid:
                    continue

                # ── Stop loss (auto-executes, no confirmation) ─────────────
                stop = cfg.get("stop_loss")
                if stop is not None and mid <= stop:
                    # Strike cushion gate: only fire if spot is close to strike.
                    # Prevents firing on illiquid overnight prices when position is safe.
                    strike_cushion = cfg.get("strike_cushion")
                    spot = eng.consensus_price()
                    cushion_ok = True
                    actual_cushion = None
                    if strike_cushion is not None and spot is not None:
                        if p.side == "no":
                            actual_cushion = p.strike - spot
                        else:
                            actual_cushion = spot - p.strike
                        cushion_ok = actual_cushion <= strike_cushion
                        if not cushion_ok:
                            self.log(
                                f"Stop loss BLOCKED: {p.ticker} "
                                f"mid={mid:.2f} but spot cushion "
                                f"{actual_cushion:.2f} > required {strike_cushion:.2f}"
                            )
                    if cushion_ok:
                        reason = f"Bot stop loss: mid {mid:.2f} <= stop {stop:.2f}"
                        if actual_cushion is not None:
                            reason += f" (spot cushion {actual_cushion:.2f})"
                        eng._add_action("danger", "SELL_LEG", reason,
                            p.ticker, p.side, p.count)
                        if self.params.mode == "live" and eng.armed:
                            await eng._execute_sell(p, p.count)
                        # Remove bot config immediately so portfolio sync
                        # restoring count before fill is confirmed cannot
                        # cause the stop to re-fire on the next poll cycle.
                        self.position_bots.pop(p.ticker, None)
                        continue  # Don't check other conditions once stop fires

                # ── Limit sell (take profit — requires confirmation) ───────
                limit = cfg.get("limit_sell")
                if limit is not None and mid >= limit:
                    reason = f"Bot limit sell: mid {mid:.2f} >= target {limit:.2f}"
                    eng._add_action(
                        "info", "SELL_LEG", reason,
                        p.ticker, p.side, p.count,
                    )
                    if self.params.mode == "live" and eng.armed:
                        await self.request_auto_sell(eng, p, p.count, reason)
                    continue

                # ── Momentum harvest (hard floor — requires confirmation) ──
                #
                # User's rule (July 6 harvest bug): "I don't want profit
                # harvest at 88c just because it went to 90c and down to 87c."
                # Fix: harvest never fires unless mid is CURRENTLY at or above
                # harvest_floor. Optional min_profit adds a second floor
                # relative to avg_price. Both are hard gates checked live —
                # no peak-triggered, drop-tolerant behavior.
                if cfg.get("harvest"):
                    floor = float(cfg.get("harvest_floor", 0.95))
                    min_profit = float(cfg.get("harvest_min_profit", 0.0))
                    # Second floor: avg_price + min_profit ensures we never
                    # harvest at a loss (min_profit=0 defaults to breakeven).
                    profit_floor = p.avg_price + min_profit if min_profit > 0 else 0.0
                    effective_floor = max(floor, profit_floor)
                    if mid >= effective_floor:
                        reason = (
                            f"Bot harvest: mid {mid:.2f} >= floor {effective_floor:.2f}"
                        )
                        eng._add_action(
                            "warn", "SELL_LEG", reason,
                            p.ticker, p.side, p.count,
                        )
                        if self.params.mode == "live" and eng.armed:
                            await self.request_auto_sell(eng, p, p.count, reason)
                    continue

                # ── Time exit (requires confirmation) ──────────────────────
                time_exit = cfg.get("time_exit")
                if time_exit:
                    try:
                        exit_h, exit_m = map(int, time_exit.split(":"))
                        exit_dt = now_dt.replace(
                            hour=exit_h, minute=exit_m, second=0, microsecond=0
                        )
                        if now_dt >= exit_dt:
                            reason = f"Bot time exit: {time_exit} reached"
                            eng._add_action(
                                "warn", "SELL_LEG", reason,
                                p.ticker, p.side, p.count,
                            )
                            if self.params.mode == "live" and eng.armed:
                                await self.request_auto_sell(eng, p, p.count, reason)
                    except Exception as e:
                        self.log(f"Bot time_exit parse error {p.ticker}: {e}")

    async def request_auto_sell(
        self,
        eng: "EventEngine",
        position: Position,
        qty: int,
        reason: str,
    ) -> None:
        """
        Central gate for every non-stop automated sell.

        - If a Telegram bot is available and healthy, ask the user; the sell
          only executes when the user taps APPROVE.
        - If Telegram is unavailable, DO NOT SELL. Log the intent. Per the
          user's rule: "I'd rather no sale than a bad sale (except stop loss)."
        """
        tg = getattr(self, "telegram", None)
        if tg is not None:
            try:
                await tg.request_sell_confirmation(eng, position, qty, reason)
                return
            except Exception as e:
                self.log(
                    f"Telegram confirm request failed for {position.ticker}: {e} — "
                    f"NOT selling (fail-safe: no sale > bad sale)."
                )
                return
        self.log(
            f"AUTO-SELL BLOCKED (no Telegram confirm path available): "
            f"{position.ticker} — {reason}"
        )

    async def loop(self) -> None:
        self._running = True
        self.log("KalshiBaby v3 backend started.")
        while self._running:
            try:
                await self.tick()
            except Exception as e:
                self.log(f"ERROR tick failed: {e}")
            await asyncio.sleep(max(1, int(self.params.poll_seconds)))

    # -----------------------------------------------------------------------
    # API helpers
    # -----------------------------------------------------------------------

    def status(self) -> MultiStatus:
        # Inject bot_config into each position so UI can render Bot ✓ button
        for eng in self.event_engines.values():
            for p in eng.positions:
                bot = self.position_bots.get(p.ticker)
                p.bot_config = bot["config"] if bot else None

        return MultiStatus(
            ts=now_ts(),
            mode=self.params.mode,
            events={et: eng.event_status() for et, eng in self.event_engines.items()},
            actions=list(self.actions),
            params=self.params,
            logs=list(self.logs),
            news_regime=self.news_regime.regime,
            position_bots=self.position_bots,
        )

    def update_params(self, req: UpdateParamsRequest) -> None:
        data = req.model_dump(exclude_unset=True)
        for key, value in data.items():
            if value is None:
                continue
            if key in ("mode", "armed", "poll_seconds"):
                setattr(self.params, key, value)
            else:
                current = getattr(self.params, key)
                current.update(value)
        self.log(f"Params updated: {data}")

    def arm_event(self, event_ticker: str, armed: bool) -> None:
        if event_ticker not in self.event_engines:
            raise HTTPException(status_code=404, detail="Event not found")
        self.event_engines[event_ticker].set_armed(armed)
        self.log(f"Event {event_ticker} {'ARMED' if armed else 'DISARMED'}")

    async def sell_all(self, confirm: bool = False) -> Dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm=true required")
        self._add_action("danger", "SELL_ALL", "Manual sell everything now.")
        results = {}
        for et, eng in self.event_engines.items():
            results[et] = await eng.flatten()
        return {"ok": True, "mode": self.params.mode, "results": results}

    async def sell_event(self, event_ticker: str, confirm: bool = False) -> Dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm=true required")
        if event_ticker not in self.event_engines:
            raise HTTPException(status_code=404, detail="Event not found")
        self._add_action("danger", "SELL_ALL", f"Manual flatten: {event_ticker}")
        results = await self.event_engines[event_ticker].flatten()
        return {"ok": True, "mode": self.params.mode, "results": results}

    async def sell_leg(self, ticker: str, qty: Optional[int], confirm: bool) -> Dict:
        if not confirm:
            raise HTTPException(status_code=400, detail="confirm=true required")
        for eng in self.event_engines.values():
            p = next((x for x in eng.positions if x.ticker == ticker), None)
            if p:
                await eng._execute_sell(p, qty or p.count)
                return {"ok": True}
        raise HTTPException(status_code=404, detail="Position not found")


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


engine = Engine(load_config())


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(engine.loop())

    # Start Telegram bot if configured
    tg_cfg = engine.config.get("telegram")
    if tg_cfg and tg_cfg.get("token") and tg_cfg["token"] != "YOUR_TOKEN_HERE":
        try:
            from kalshibaby_telegram import KalshiBabyBot
            tg_bot = KalshiBabyBot(engine, tg_cfg)
            engine.telegram = tg_bot   # expose so engine can call send_stop_loss_alert
            await tg_bot.start()
        except Exception as e:
            engine.log(f"Telegram bot failed to start: {e}")
    else:
        engine.telegram = None

    yield

    if getattr(engine, "telegram", None):
        await engine.telegram.stop()


app = FastAPI(title="KalshiBaby v3", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory="ui"), name="ui")


@app.get("/")
async def index():
    return FileResponse("ui/index.html")


@app.get("/api/status", response_model=MultiStatus)
async def api_status():
    return engine.status()


@app.post("/api/params")
async def api_params(req: UpdateParamsRequest):
    engine.update_params(req)
    return {"ok": True}


@app.post("/api/set_position_bot")
async def api_set_position_bot(req: PositionBotRequest):
    """Save a per-position stop loss / take profit bot config from the UI."""
    engine.position_bots[req.ticker] = {
        "ticker": req.ticker,
        "event_ticker": req.event_ticker,
        "config": req.config,
        "created_ts": now_ts(),
    }
    engine.log(f"Position bot set: {req.ticker} config={req.config}")
    return {"ok": True, "ticker": req.ticker}


@app.post("/api/clear_position_bot")
async def api_clear_position_bot(req: ClearPositionBotRequest):
    """Remove a per-position bot."""
    removed = engine.position_bots.pop(req.ticker, None)
    engine.log(f"Position bot cleared: {req.ticker}")
    return {"ok": True, "ticker": req.ticker, "was_set": removed is not None}


@app.post("/api/arm_event")
async def api_arm_event(req: ArmEventRequest):
    engine.arm_event(req.event_ticker, req.armed)
    return {"ok": True}


@app.post("/api/sell_all")
async def api_sell_all(req: SellRequest):
    return await engine.sell_all(confirm=req.confirm)


@app.post("/api/sell_event")
async def api_sell_event(req: SellRequest):
    if not req.event_ticker:
        raise HTTPException(status_code=400, detail="event_ticker required")
    return await engine.sell_event(req.event_ticker, confirm=req.confirm)


@app.post("/api/sell_leg")
async def api_sell_leg(req: SellRequest):
    if not req.ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    return await engine.sell_leg(req.ticker, req.qty, req.confirm)


@app.post("/api/news_signal")
async def api_news_signal(signal: NewsSignal):
    """
    Receive a classified news signal from newsagent.py.
    1. Creates a direct Action so the headline/link appears in the UI immediately.
    2. Ingests into NewsRegimeTracker for regime transition logic.
    """
    # Always create a visible action with headline and link for the UI
    if signal.signal not in ("NEUTRAL",) and signal.confidence >= 0.65:
        severity = "danger" if signal.signal == "ESCALATION_SIGNAL" else \
                   "warn" if signal.signal == "BULLISH" else "info"
        engine.actions.appendleft(Action(
            ts=signal.ts,
            severity=severity,
            action="ALERT",
            reason=f"News {signal.signal} ({signal.confidence:.0%}): {signal.reason}",
            headline=signal.headline,
            url=signal.url,
            source=signal.source,
        ))

    # Use the most data-rich event engine's price history for confirmation
    price_history: Deque[Tuple[float, float]] = deque()
    best_len = 0
    for eng in engine.event_engines.values():
        if len(eng.price_history) > best_len:
            price_history = eng.price_history
            best_len = len(eng.price_history)

    engine.news_regime.ingest(
        signal=signal,
        price_history=price_history,
        event_engines=engine.event_engines,
        params=engine.params,
    )
    return {
        "ok": True,
        "regime": engine.news_regime.regime,
        "pending": engine.news_regime.pending_signal,
        "pending_count": engine.news_regime.pending_count,
    }


@app.get("/api/news_regime")
async def api_news_regime():
    """Current news regime state and recent signals."""
    nr = engine.news_regime
    return {
        "regime": nr.regime,
        "pending_signal": nr.pending_signal,
        "pending_count": nr.pending_count,
        "flip_threshold": nr.FLIP_THRESHOLD,
        "price_confirm_pct": nr.PRICE_CONFIRM_PCT,
        "price_confirm_minutes": nr.PRICE_CONFIRM_MINUTES,
        "last_transition_ts": nr.last_transition_ts,
        "recent_signals": [
            {
                "source": s.source,
                "headline": s.headline[:80],
                "signal": s.signal,
                "confidence": round(s.confidence, 2),
                "ts": s.ts,
            }
            for s in list(nr.recent_signals)[:10]
        ],
    }


@app.get("/api/debug/goldprice")
async def api_debug_goldprice():
    """Test api.metals.live endpoint and show raw response for troubleshooting."""
    url = GoldPriceSource._URL
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        return {"url": url, "status_code": r.status_code, "body": body}
    except Exception as e:
        return {"url": url, "error": str(e)}


@app.get("/api/debug/events")
async def api_debug_events():
    """Show active event tickers and their detected instrument mapping."""
    result = {}
    for et, eng in engine.event_engines.items():
        prefix = instrument_prefix(et)
        instr = detect_instrument(et)
        result[et] = {
            "prefix": prefix,
            "in_instruments_map": prefix in INSTRUMENTS,
            "yahoo_symbol": instr.get("yahoo_symbol"),
            "use_goldprice": instr.get("use_goldprice", False),
            "oilprice_symbol": instr.get("oilprice_symbol"),
            "position_count": len(eng.positions),
        }
    return result


@app.get("/api/debug/portfolio")
async def api_debug_portfolio():
    """Raw Kalshi portfolio API call — shows full response for auth troubleshooting."""
    k = engine.kalshi
    if not k.api_key_id:
        return {"error": "No API credentials configured"}

    path = "/trade-api/v2/portfolio/positions"
    url = k.base_url + path

    # Also test an unauthenticated market endpoint to confirm network/domain is reachable.
    probe_url = k.base_url + "/trade-api/v2/markets?limit=1"
    probe_ok = False
    probe_status = None
    try:
        pr = requests.get(probe_url, timeout=5)
        probe_ok = pr.ok
        probe_status = pr.status_code
    except Exception as pe:
        probe_status = str(pe)

    try:
        headers = k._auth_headers("GET", path)
        r = requests.get(url, headers=headers, timeout=10)
        try:
            body = r.json()
        except Exception:
            body = r.text[:1000]
        return {
            "base_url": k.base_url,
            "api_key_id": k.api_key_id,
            "key_loaded": k.private_key is not None,
            "url": url,
            "status_code": r.status_code,
            "body": body,
            "probe_url": probe_url,
            "probe_reachable": probe_ok,
            "probe_status": probe_status,
        }
    except Exception as e:
        return {
            "error": str(e),
            "base_url": k.base_url,
            "api_key_id": k.api_key_id,
            "key_loaded": k.private_key is not None,
            "probe_url": probe_url,
            "probe_reachable": probe_ok,
            "probe_status": probe_status,
        }

@app.get("/api/wapner_candidates")
async def api_wapner_candidates(event_ticker: Optional[str] = None):
    if event_ticker and event_ticker not in engine.event_engines:
        raise HTTPException(status_code=404, detail="Event not found")
    engines = (
        {event_ticker: engine.event_engines[event_ticker]}
        if event_ticker else engine.event_engines
    )
    results = {}
    for et, eng in engines.items():
        try:
            results[et] = evaluate_event_wapner(engine, eng)
        except Exception as e:
            results[et] = [{"event_ticker": et, "grade": "REJECT",
                            "reasons": [f"evaluation error: {e}"]}]
    return {"ts": time.time(), "candidates": results}

# ---------------------------------------------------------------------------
# Buy candidates scan — finds open contracts in the entry price zone
# ---------------------------------------------------------------------------

KALSHI_FEE_RATE = 0.07   # 7% of max(yes_price, no_price), capped at $0.07/contract


@app.get("/api/buy_candidates")
async def api_buy_candidates(event_ticker: Optional[str] = None):
    """
    Scans open markets for contracts in the configured entry_zone (default 70-80c).
    Pass ?event_ticker=KXBRENTD-26JUN2917 to scan a specific event even with no
    active positions.  Without it, scans all events the engine is currently tracking.
    """
    zone_min = engine.params.entry_zone.get("min", 0.70)
    zone_max = engine.params.entry_zone.get("max", 0.80)

    candidates = []

    # Build (et, spot) pairs — direct ticker takes priority, else all tracked events
    if event_ticker:
        eng = engine.event_engines.get(event_ticker)
        scan_list = [(event_ticker, eng.consensus_price() if eng else None)]
    else:
        scan_list = [(et, eng.consensus_price()) for et, eng in engine.event_engines.items()]

    if not scan_list:
        return {
            "candidates": [],
            "zone_min": zone_min,
            "zone_max": zone_max,
            "hint": "No active events tracked and no event_ticker param supplied. "
                    "Try /api/buy_candidates?event_ticker=KXBRENTD-26JUN2917",
        }

    for event_ticker, spot in scan_list:
        # Fetch all open markets for this event from Kalshi
        url = engine.kalshi.base_url + "/trade-api/v2/markets"
        try:
            r = requests.get(
                url,
                params={"event_ticker": event_ticker, "status": "open", "limit": 200},
                timeout=8,
            )
            if not r.ok:
                continue
            markets = r.json().get("markets", [])
        except Exception:
            continue

        def _flt(v: Any) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        for m in markets:
            ticker = m.get("ticker", "")
            strike = parse_strike(ticker)

            for side in ("yes", "no"):
                bid_key = f"{side}_bid_dollars"
                ask_key = f"{side}_ask_dollars"
                bid = _flt(m.get(bid_key))
                ask = _flt(m.get(ask_key))
                if bid is None or ask is None:
                    continue
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else bid or ask
                if mid <= 0:
                    continue
                if not (zone_min <= mid <= zone_max):
                    continue

                # Fee estimate: 7% of the contract price, min $0.01, max $0.07
                fee = round(min(0.07, max(0.01, mid * KALSHI_FEE_RATE)), 4)

                distance: Optional[float] = None
                if spot is not None:
                    # For Yes: positive distance = strike is above spot (riskier)
                    # For No:  positive distance = strike is below spot (safer)
                    distance = round(
                        (strike - spot) if side == "yes" else (spot - strike), 2
                    )

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": event_ticker,
                    "side": side,
                    "strike": strike,
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "mid": round(mid, 4),
                    "spot": round(spot, 2) if spot is not None else None,
                    "distance": distance,
                    "fee_per_contract": fee,
                    "fee_3_contracts": round(fee * 3, 4),
                })

    # Sort: closest to spot first (smallest absolute distance), then by mid desc
    candidates.sort(key=lambda c: (
        abs(c["distance"]) if c["distance"] is not None else 999,
        -c["mid"],
    ))

    return {"candidates": candidates, "zone_min": zone_min, "zone_max": zone_max}


# ---------------------------------------------------------------------------
# Wapner Window scanner — high-probability late-settlement arb candidates
# ---------------------------------------------------------------------------

@app.get("/api/wapner_candidates")
async def api_wapner_candidates(
    event_ticker: Optional[str] = None,
    min_mid: float = 0.85,
    max_mid: float = 0.97,
    min_cushion: float = 8.0,
    max_minutes: float = 60.0,
):
    """
    Scans open markets for late-settlement arb candidates (the 'Wapner Window').

    Criteria:
    - Settlement within max_minutes (default 60)
    - Contract mid between min_mid and max_mid (default 85-97¢)
    - Spot cushion >= min_cushion from strike (default $8)
    - Spot moving AWAY from strike (safe direction)

    Returns candidates sorted by expected value (probability × net payout),
    best opportunities first.

    Query params:
        event_ticker  — scan one specific event (optional)
        min_mid       — minimum contract mid price (default 0.85)
        max_mid       — maximum contract mid price (default 0.97)
        min_cushion   — minimum spot distance from strike in $ (default 8.0)
        max_minutes   — maximum minutes to settlement (default 60)
    """
    candidates = []
    now = datetime.now(timezone.utc)

    # Build scan list
    if event_ticker:
        eng = engine.event_engines.get(event_ticker)
        scan_list = [(event_ticker, eng)]
    else:
        scan_list = list(engine.event_engines.items())

    if not scan_list:
        return {
            "candidates": [],
            "params": {
                "min_mid": min_mid, "max_mid": max_mid,
                "min_cushion": min_cushion, "max_minutes": max_minutes,
            },
            "hint": "No active events tracked. Pass ?event_ticker=KXGOLDD-26JUL0217",
        }

    def _flt(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _parse_settlement_minutes(et: str) -> Optional[float]:
        """
        Parse settlement time from event ticker suffix and return minutes remaining.
        Format: PREFIX-YYMONDDHH  e.g. KXGOLDD-26JUL0217 = Jul 2 2026 17:00 UTC
        Returns None if unparseable or already past settlement.
        """
        import re
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        # Try YYMONDDH H format (e.g. 26JUL0217)
        m = re.search(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})$", et)
        if not m:
            return None
        try:
            from datetime import timedelta
            # Settlement hour is EDT (UTC-4)
            settle_utc = datetime(
                2000 + int(m.group(1)),
                month_map[m.group(2)],
                int(m.group(3)),
                int(m.group(4)),
                0, 0,
                tzinfo=timezone(timedelta(hours=-4)),
            ).astimezone(timezone.utc)
            delta = (settle_utc - now).total_seconds() / 60.0
            return round(delta, 1) if delta > 0 else None
        except Exception:
            return None

    for et, eng in scan_list:
        minutes_left = _parse_settlement_minutes(et)
        if minutes_left is None or minutes_left > max_minutes:
            continue

        spot = eng.consensus_price() if eng else None

        url = engine.kalshi.base_url + "/trade-api/v2/markets"
        try:
            r = requests.get(
                url,
                params={"event_ticker": et, "status": "open", "limit": 200},
                timeout=8,
            )
            if not r.ok:
                continue
            markets = r.json().get("markets", [])
        except Exception:
            continue

        for m in markets:
            ticker = m.get("ticker", "")
            strike = parse_strike(ticker)

            for side in ("yes", "no"):
                bid_key = f"{side}_bid_dollars"
                ask_key = f"{side}_ask_dollars"
                bid = _flt(m.get(bid_key))
                ask = _flt(m.get(ask_key))
                if bid is None or ask is None:
                    continue
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else bid or ask
                if mid <= 0:
                    continue

                # Price zone filter
                if not (min_mid <= mid <= max_mid):
                    continue

                # Spot cushion filter
                if spot is None:
                    continue
                if side == "yes":
                    cushion = spot - strike   # positive = spot above strike = winning
                else:
                    cushion = strike - spot   # positive = spot below strike = winning

                if cushion < min_cushion:
                    continue

                # Expected value: mid is the market's implied probability.
                # Net payout per contract = $1 - ask (cost to buy at ask).
                # EV = mid * (1 - ask) — simplified, ignores fee.
                fee = round(min(0.07, max(0.01, ask * KALSHI_FEE_RATE)), 4)
                cost = round((ask or mid) + fee, 4)
                net_payout = round(1.0 - cost, 4)
                ev = round(mid * net_payout, 4)

                # Suggested limit buy: 1-2¢ below current ask for fill probability
                suggested_limit = round(max(min_mid, (ask or mid) - 0.02), 2)

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": et,
                    "side": side,
                    "strike": strike,
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "mid": round(mid, 4),
                    "spot": round(spot, 2),
                    "cushion": round(cushion, 2),
                    "minutes_left": minutes_left,
                    "fee_per_contract": fee,
                    "cost_per_contract": cost,
                    "net_payout_per_contract": net_payout,
                    "expected_value": ev,
                    "suggested_limit": suggested_limit,
                })

    # Sort by expected value descending — best opportunities first
    candidates.sort(key=lambda c: -c["expected_value"])

    return {
        "candidates": candidates,
        "params": {
            "min_mid": min_mid,
            "max_mid": max_mid,
            "min_cushion": min_cushion,
            "max_minutes": max_minutes,
        },
        "scanned_events": len(scan_list),
    }


# ---------------------------------------------------------------------------
# Execute buy order
# ---------------------------------------------------------------------------

class BuyRequest(BaseModel):
    ticker: str
    side: Side
    qty: int
    limit_price_cents: int   # 1–99 cents
    confirm: bool = False


@app.post("/api/execute_buy")
async def api_execute_buy(req: BuyRequest):
    """
    Places a buy order for the given contract.  In paper mode logs the intent
    without touching the API.  In live mode sends an IOC limit order to Kalshi.
    Requires confirm=true.
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    if not (1 <= req.limit_price_cents <= 99):
        raise HTTPException(status_code=400, detail="limit_price_cents must be 1–99")
    if req.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")

    limit_price = req.limit_price_cents / 100.0

    if engine.params.mode != "live":
        engine.log(
            f"PAPER BUY: {req.qty} {req.side.upper()} {req.ticker} "
            f"@ {req.limit_price_cents}¢"
        )
        return {"ok": True, "paper": True, "ticker": req.ticker,
                "side": req.side, "qty": req.qty,
                "limit_price": limit_price}

    # Live execution
    k = engine.kalshi
    if not k.api_key_id:
        raise HTTPException(status_code=503, detail="Kalshi API credentials not configured")

    path = "/trade-api/v2/portfolio/orders"
    url = k.base_url + path
    price_key = "yes_price" if req.side == "yes" else "no_price"
    payload = {
        "ticker": req.ticker,
        "action": "buy",
        "side": req.side,
        "count": req.qty,
        "client_order_id": str(uuid.uuid4()),
        "time_in_force": "ioc",
        price_key: req.limit_price_cents,   # Kalshi expects integer cents
    }
    try:
        r = requests.post(
            url,
            headers=k._auth_headers("POST", path),
            json=payload,
            timeout=10,
        )
        body = r.json() if r.text else {}
        ok = r.ok
        if ok:
            engine.log(
                f"BUY PLACED: {req.qty} {req.side.upper()} {req.ticker} "
                f"@ {req.limit_price_cents}¢  order_id={body.get('order', {}).get('order_id','?')}"
            )
        else:
            engine.log(f"BUY FAILED: {req.ticker} HTTP {r.status_code}: {r.text[:200]}")
        return {"ok": ok, "status_code": r.status_code, "response": body}
    except Exception as e:
        engine.log(f"BUY ERROR: {req.ticker}: {e}")
        return {"ok": False, "error": str(e)}
