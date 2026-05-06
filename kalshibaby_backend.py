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
import json
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

import logging
import logging.handlers
import os

import requests
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


_config_env = os.environ.get("KALSHI_CONFIG", "config.yaml")
CONFIG_PATH = Path(_config_env)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path("config.example.yaml")

SESSION_PATH = Path("session.json")

# Persistent rotating log — survives server restarts, keeps 7 days of history.
_log_stem = CONFIG_PATH.stem  # e.g. "config.demo" → "kalshibaby.config.demo.log"
_file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=f"kalshibaby.{_log_stem}.log",
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_flog = logging.getLogger("kalshibaby")
_flog.setLevel(logging.DEBUG)
_flog.addHandler(_file_handler)
_flog.propagate = False


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

# Instrument prefix → price source symbol mapping.
# OilPrice only covers oil products — omit oilprice_symbol for non-oil instruments.
INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "KXWTI":     {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",   "use_goldprice": False},
    "KXWTIMAX":  {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",   "use_goldprice": False},
    "KXBRENTW":  {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude", "use_goldprice": False},
    "KXGOLDW":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True},
    "KXGOLD":    {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True},
    "KXGOLDMAX": {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True},
    "KXGOLDR":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True},
    "KXGOLDD":   {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,          "use_goldprice": True},
    "KXBRENTD":  {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude", "use_goldprice": False},
    "KXNG":      {"yahoo_symbol": "NG=F",  "oilprice_symbol": "Natural-Gas", "use_goldprice": False},
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
        return {"yahoo_symbol": "GC=F",  "oilprice_symbol": None,           "use_goldprice": True}
    if "WTI" in p:
        return {"yahoo_symbol": "CL=F",  "oilprice_symbol": "WTI-Crude",    "use_goldprice": False}
    if "BRENT" in p or "BRT" in p:
        return {"yahoo_symbol": "BZ=F",  "oilprice_symbol": "Brent-Crude",  "use_goldprice": False}
    if p.startswith("KXNG") or "NATGAS" in p or "NGAS" in p:
        return {"yahoo_symbol": "NG=F",  "oilprice_symbol": "Natural-Gas",  "use_goldprice": False}
    return {}


def now_ts() -> float:
    return time.time()


def clamp_price(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def kalshi_fee(qty: float, price: float) -> float:
    """Taker fee estimate: 0.07 × qty × price × (1 − price)."""
    return round(0.07 * qty * clamp_price(price) * (1.0 - clamp_price(price)), 4)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Position(BaseModel):
    ticker: str
    side: Side
    strike: float
    count: float
    avg_price: float
    current_bid: float = 0.0
    current_ask: float = 0.0
    current_mid: float = 0.0
    peak_mid: float = 0.0
    profit_armed: bool = False


class BuyCandidate(BaseModel):
    ticker: str
    event_ticker: str
    side: Side
    strike: float
    mid: float
    spot: Optional[float] = None
    distance: Optional[float] = None  # spot - strike for YES; strike - spot for NO
    fee_per_contract: float = 0.0
    fee_3_contracts: float = 0.0


class SessionInfo(BaseModel):
    start_balance: Optional[float] = None
    current_balance: Optional[float] = None
    realized_pl: float = 0.0
    unrealized_pl: float = 0.0
    net_pl: float = 0.0
    net_pct: Optional[float] = None


class RuntimeParams(BaseModel):
    mode: str = "paper"
    armed: bool = False
    poll_seconds: int = 3

    profit_harvest: Dict[str, float]
    shock_logic: Dict[str, float]
    source_consensus: Dict[str, float]
    structure: Dict[str, float]
    time_risk: Dict[str, float]
    sources: Dict[str, Dict[str, Any]]
    safety: Dict[str, float] = Field(default_factory=lambda: {"global_drawdown_limit": -50.0})

    max_deploy_pct: float = 0.50
    entry_zone: Dict[str, float] = Field(default_factory=lambda: {"min": 0.70, "max": 0.80})
    alert_thresholds: Dict[str, float] = Field(default_factory=lambda: {"position_drop_warning": 0.69})


class PricePoint(BaseModel):
    source: str
    price: Optional[float]
    ts: float
    stale: bool = False
    error: Optional[str] = None


class Action(BaseModel):
    ts: float
    severity: Literal["info", "warn", "danger"]
    action: Literal["HOLD", "TRIM", "SELL_LEG", "SELL_ALL", "ALERT", "BUY"]
    reason: str
    event_ticker: Optional[str] = None
    ticker: Optional[str] = None
    side: Optional[Side] = None
    qty: Optional[float] = None


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
    yes_count: float
    no_count: float
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
    session: SessionInfo = Field(default_factory=SessionInfo)
    buy_candidates: List[BuyCandidate] = Field(default_factory=list)


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
    max_deploy_pct: Optional[float] = None
    entry_zone: Optional[Dict[str, float]] = None
    alert_thresholds: Optional[Dict[str, float]] = None


class BuyRequest(BaseModel):
    ticker: str
    side: Side
    qty: int
    limit_price_cents: int  # 1-99
    confirm: bool = False


class ArmEventRequest(BaseModel):
    event_ticker: str
    armed: bool


class SellRequest(BaseModel):
    ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    qty: Optional[float] = None
    confirm: bool = False


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
    Gold spot price (USD/troy oz) via Stooq XAUUSD CSV feed (no auth required).
    Used as the secondary spot source for gold events instead of oilprice.com.
    """
    _URL = "https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv"

    def __init__(self, min_refresh_seconds: int = 30) -> None:
        self.name = "goldprice"
        self.min_refresh_seconds = min_refresh_seconds
        self._last_price: Optional[float] = None
        self._last_fetch_ts: float = 0.0

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
            r = requests.get(
                self._URL,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            r.raise_for_status()
            # Response: Symbol,Date,Time,Open,High,Low,Close,Volume\nXAUUSD,2026-05-03,...
            lines = r.text.strip().split("\n")
            if len(lines) < 2:
                raise ValueError("empty Stooq response")
            row = lines[1].split(",")
            price = float(row[6])  # Close column
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

    async def get_balance(self) -> Optional[float]:
        path = "/trade-api/v2/portfolio/balance"
        url = self.base_url + path
        try:
            r = requests.get(url, headers=self._auth_headers("GET", path), timeout=10)
            if not r.ok:
                return None
            data = r.json()
            bal = data.get("balance")
            if bal is None:
                return None
            val = self._money_to_float(bal)
            # Kalshi returns balance in cents (integer); convert to dollars
            if val is not None and isinstance(bal, int) and bal > 200:
                val = val / 100.0
            return val
        except Exception:
            return None

    async def get_open_markets_for_series(self, series_ticker: str) -> List[Dict]:
        url = self.base_url + "/trade-api/v2/markets"
        r = requests.get(
            url,
            params={"series_ticker": series_ticker, "status": "open", "limit": 200},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("markets", [])

    async def place_order(
        self, ticker: str, side: Side, action: str, qty: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place a buy or sell order. limit_price is in dollars (0.0–1.0)."""
        path = "/trade-api/v2/portfolio/orders"
        url = self.base_url + path
        price_key = "yes_price" if side == "yes" else "no_price"
        price_cents = max(1, min(99, round(limit_price * 100)))
        payload = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(qty),
            "client_order_id": str(uuid.uuid4()),
            "time_in_force": "fill_or_kill",
            price_key: price_cents,
        }
        try:
            r = requests.post(url, headers=self._auth_headers("POST", path), json=payload, timeout=10)
            return {"ok": r.ok, "status_code": r.status_code, "response": r.json() if r.text else {}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    async def sell_position(self, ticker: str, side: Side, qty: float, bid: float = 0.0) -> Dict[str, Any]:
        path = "/trade-api/v2/portfolio/orders"
        url = self.base_url + path
        if qty % 1 != 0:
            return {"ok": False, "error": f"Fractional position ({qty} contracts) cannot be sold via API — close manually on Kalshi."}
        price_key = "yes_price" if side == "yes" else "no_price"
        payload = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "count": int(qty),
            "client_order_id": str(uuid.uuid4()),
            "time_in_force": "immediate_or_cancel",
            "reduce_only": True,
            price_key: 1,
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
    ) -> None:
        self.event_ticker = event_ticker
        self.settlement_time = settlement_time
        self.params = params       # shared reference — global param updates apply automatically
        self.kalshi = kalshi
        self._actions = actions_queue
        self._logs = logs_queue

        self.positions: List[Position] = []
        self.realized_pl: float = 0.0
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

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{self.event_ticker}] {stamp} {msg}"
        self._logs.appendleft(line)
        _flog.info(line)

    def _add_action(
        self,
        severity: Literal["info", "warn", "danger"],
        action: Literal["HOLD", "TRIM", "SELL_LEG", "SELL_ALL", "ALERT", "BUY"],
        reason: str,
        ticker: Optional[str] = None,
        side: Optional[Side] = None,
        qty: Optional[float] = None,
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
        vals = [
            pp.price for name, pp in self.last_prices.items()
            if name != "kalshi_implied" and pp.price is not None and not pp.stale
        ]
        if not vals:
            pp = self.last_prices.get("kalshi_implied")
            return pp.price if pp and pp.price is not None else None
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
        if self.state == "OBSERVE_ONLY":
            self.state = "NORMAL"

        self._check_endangered()
        self._detect_shock()
        self._classify_shock()
        self._structure_drift()
        self._time_risk()

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
            self._add_action("danger", "ALERT", f"Regime break: recovery only {recovery:.2f}% after {elapsed_min:.1f} min. Review positions.")

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
            # YES wins if settlement > strike. Pre-emptive exit when approaching from above.
            return consensus < (p.strike + buf_lo)
        # NO wins if settlement < strike. Pre-emptive exit when approaching from below.
        return consensus > (p.strike - buf_hi)

    def _exit_endangered_legs(self, reason: str) -> None:
        consensus = self.consensus_price()
        for p in self.positions:
            if p.count <= 0:
                continue
            if self.position_endangered(p, consensus):
                self._add_action("danger", "SELL_LEG", reason, p.ticker, p.side, p.count)
                if self.params.mode == "live" and self.armed:
                    asyncio.create_task(self._execute_sell(p, p.count))

    def _check_endangered(self) -> None:
        """Per-tick stop-loss: exits any leg whose consensus has crossed its strike buffer."""
        consensus = self.consensus_price()
        if consensus is None:
            return
        for p in self.positions:
            if p.count <= 0:
                continue
            if self.position_endangered(p, consensus):
                self.state = "FLATTENING"
                sell_price = p.current_bid or p.current_mid
                fee = kalshi_fee(p.count, sell_price)
                reason = (
                    f"Stop-loss: {p.side.upper()} T{p.strike} endangered — "
                    f"consensus {consensus:.2f}, sell ~{sell_price:.2f}, "
                    f"est. fee ${fee:.3f}"
                )
                self._add_action("danger", "SELL_LEG", reason, p.ticker, p.side, p.count)
                if self.params.mode == "live" and self.armed:
                    asyncio.create_task(self._execute_sell(p, p.count))

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

    # -----------------------------------------------------------------------
    # Execution
    # -----------------------------------------------------------------------

    async def _execute_sell(self, p: Position, qty: float) -> None:
        if qty <= 0 or p.count <= 0:
            return
        if self.params.mode != "live":
            self.log(f"PAPER: would sell {qty} {p.side.upper()} {p.ticker}")
            return
        qty = min(qty, p.count)
        bid = p.current_bid or p.current_mid
        result = await self.kalshi.sell_position(p.ticker, p.side, qty, bid=bid)
        if result.get("ok"):
            pl = qty * (bid - p.avg_price)
            self.realized_pl += pl
            p.count -= qty
            self.log(f"SOLD {qty} {p.side.upper()} {p.ticker} @ ~{bid:.3f}  P/L: {pl:+.2f}")
        else:
            self.log(f"SELL FAILED {p.ticker}: {result}")

    async def flatten(self) -> List[Dict]:
        self.state = "FLATTENING"
        results = []
        for p in self.positions:
            if p.count > 0:
                qty = p.count
                if self.params.mode == "live" and self.armed:
                    bid = p.current_bid or p.current_mid
                    result = await self.kalshi.sell_position(p.ticker, p.side, qty, bid=bid)
                    if result.get("ok"):
                        p.count = 0
                        self.log(f"SOLD {qty} {p.side.upper()} {p.ticker}")
                    else:
                        self.log(f"SELL FAILED {p.ticker}: {result}")
                    results.append(result)
                else:
                    self.log(f"PAPER: would flatten {qty} {p.side.upper()} {p.ticker}")
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
        self._safety_triggered = False

        # Session tracking
        self.session_start_balance: Optional[float] = None
        self.session_realized_pl: float = 0.0  # accumulates from closed event engines
        self._balance_cache: Optional[float] = None
        self._balance_cache_ts: float = 0.0
        self._load_session()

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
        )
        positions = [Position(**p) for p in config.get("positions", [])]
        for p in positions:
            p.current_mid = p.avg_price
            p.peak_mid = p.avg_price
        engine.positions = positions
        self.event_engines[event_ticker] = engine
        self.log(f"Bootstrap: loaded {len(positions)} config positions for {event_ticker}")

    def _load_session(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if SESSION_PATH.exists():
            try:
                data = json.loads(SESSION_PATH.read_text())
                if data.get("date") == today:
                    self.session_start_balance = data.get("start_balance")
                    self.session_realized_pl = float(data.get("realized_pl", 0.0))
                    return
            except Exception:
                pass
        self.session_start_balance = None
        self.session_realized_pl = 0.0

    def _save_session(self) -> None:
        try:
            SESSION_PATH.write_text(json.dumps({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "start_balance": self.session_start_balance,
                "realized_pl": self.session_realized_pl,
            }, indent=2))
        except Exception as e:
            self.log(f"Session save failed: {e}")

    async def _refresh_balance(self) -> None:
        if not self.kalshi.api_key_id:
            return
        now = now_ts()
        if now - self._balance_cache_ts < 60:
            return
        try:
            bal = await self.kalshi.get_balance()
            if bal is not None:
                self._balance_cache = bal
                self._balance_cache_ts = now
                if self.session_start_balance is None:
                    self.session_start_balance = bal
                    self._save_session()
        except Exception as e:
            self.log(f"Balance refresh failed: {e}")

    async def scan_buy_candidates(self) -> List[BuyCandidate]:
        zone_min = self.params.entry_zone.get("min", 0.70)
        zone_max = self.params.entry_zone.get("max", 0.80)

        # Spot price per instrument prefix from active engines
        spot_by_prefix: Dict[str, Optional[float]] = {}
        for et, eng in self.event_engines.items():
            prefix = instrument_prefix(et)
            if prefix not in spot_by_prefix:
                spot_by_prefix[prefix] = eng.consensus_price()

        candidates: List[BuyCandidate] = []
        for prefix, spot in spot_by_prefix.items():
            try:
                markets = await self.kalshi.get_open_markets_for_series(prefix)
            except Exception as e:
                self.log(f"Buy scan {prefix}: {e}")
                continue
            for m in markets:
                ticker = m.get("ticker", "")
                if not ticker or "-T" not in ticker:
                    continue
                event_ticker = ticker.split("-T")[0]
                try:
                    strike = float(ticker.split("-T")[-1])
                except Exception:
                    continue
                yes_bid = self.kalshi._money_to_float(m.get("yes_bid_dollars"))
                yes_ask = self.kalshi._money_to_float(m.get("yes_ask_dollars"))
                no_bid  = self.kalshi._money_to_float(m.get("no_bid_dollars"))
                no_ask  = self.kalshi._money_to_float(m.get("no_ask_dollars"))

                if yes_bid is not None and yes_ask is not None:
                    yes_mid = (yes_bid + yes_ask) / 2
                    if zone_min <= yes_mid <= zone_max:
                        dist = round(spot - strike, 2) if spot is not None else None
                        candidates.append(BuyCandidate(
                            ticker=ticker, event_ticker=event_ticker, side="yes",
                            strike=strike, mid=round(yes_mid, 3),
                            spot=round(spot, 2) if spot is not None else None,
                            distance=dist,
                            fee_per_contract=kalshi_fee(1, yes_mid),
                            fee_3_contracts=kalshi_fee(3, yes_mid),
                        ))
                if no_bid is not None and no_ask is not None:
                    no_mid = (no_bid + no_ask) / 2
                    if zone_min <= no_mid <= zone_max:
                        dist = round(strike - spot, 2) if spot is not None else None
                        candidates.append(BuyCandidate(
                            ticker=ticker, event_ticker=event_ticker, side="no",
                            strike=strike, mid=round(no_mid, 3),
                            spot=round(spot, 2) if spot is not None else None,
                            distance=dist,
                            fee_per_contract=kalshi_fee(1, no_mid),
                            fee_3_contracts=kalshi_fee(3, no_mid),
                        ))

        candidates.sort(key=lambda c: abs(c.distance) if c.distance is not None else 9999)
        return candidates[:20]

    async def execute_buy(self, ticker: str, side: Side, qty: int, limit_price_cents: int) -> Dict:
        limit_price = limit_price_cents / 100.0
        cost = qty * limit_price

        if self._balance_cache is not None:
            max_invest = self._balance_cache * self.params.max_deploy_pct
            current_deployed = sum(
                p.count * p.avg_price
                for e in self.event_engines.values()
                for p in e.positions
            )
            if current_deployed + cost > max_invest:
                return {
                    "ok": False,
                    "error": (
                        f"Bankroll cap: ${current_deployed:.2f} deployed, "
                        f"adding ${cost:.2f} exceeds {self.params.max_deploy_pct*100:.0f}% "
                        f"of ${self._balance_cache:.2f} balance"
                    ),
                }

        fee = kalshi_fee(qty, limit_price)
        if self.params.mode != "live":
            msg = f"PAPER BUY: {qty} {side.upper()} {ticker} @ {limit_price_cents}¢  est.fee ${fee:.3f}"
            self.log(msg)
            self._add_action("info", "BUY", msg)
            return {"ok": True, "paper": True}

        result = await self.kalshi.place_order(ticker, side, "buy", qty, limit_price)
        if result.get("ok"):
            self.log(f"BOUGHT {qty} {side.upper()} {ticker} @ {limit_price_cents}¢  fee ~${fee:.3f}")
            self._add_action("info", "BUY", f"Bought {qty} {side.upper()} {ticker} @ {limit_price_cents}¢")
        else:
            self.log(f"BUY FAILED {ticker}: {result}")
        return result

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {msg}"
        self.logs.appendleft(line)
        _flog.info(line)

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
            count = abs(pos_float)
            if count % 1 != 0:
                self.log(f"WARNING: {ticker} has fractional count {count} — bot cannot sell this, close manually on Kalshi.")

            # Cost basis = contracts cost + fees (matches what Kalshi shows as avg price).
            def _flt(val: Any) -> float:
                try:
                    return float(val or 0)
                except (TypeError, ValueError):
                    return 0.0

            total_traded = _flt(p.get("total_traded_dollars"))
            fees_paid    = _flt(p.get("fees_paid_dollars"))
            avg_price    = (total_traded + fees_paid) / count if count else 0.0

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
            self.session_realized_pl += self.event_engines[et].realized_pl
            self._save_session()
            self.log(f"Event closed/removed: {et}")
            del self.event_engines[et]

    # -----------------------------------------------------------------------
    # Safety
    # -----------------------------------------------------------------------

    def _check_global_safety(self) -> None:
        # Reset flag once all positions are flat so the switch can re-arm.
        if self._safety_triggered:
            all_flat = all(p.count <= 0 for e in self.event_engines.values() for p in e.positions)
            if all_flat:
                self._safety_triggered = False
            return
        snapshots = [e.risk_snapshot() for e in self.event_engines.values()]
        total_cost = sum(s.cost_basis for s in snapshots)
        total_pl   = sum(s.unrealized_pl for s in snapshots)
        if total_cost <= 0:
            return
        pct = (total_pl / total_cost) * 100.0
        limit = self.params.safety.get("global_drawdown_limit", -50.0)
        if pct <= limit:
            self._safety_triggered = True
            self.log(f"SAFETY ALERT: Global P/L {pct:.1f}% <= drawdown limit {limit:.1f}%. Use SELL EVERYTHING in the UI.")
            self._add_action("danger", "ALERT", f"KILL SWITCH THRESHOLD BREACHED: {pct:.1f}% — use SELL EVERYTHING button to flatten.")

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    async def tick(self) -> None:
        await self.sync_positions()
        await self._refresh_balance()
        for eng in list(self.event_engines.values()):
            await eng.update_prices()
            eng.evaluate_state_machine()
        self._check_global_safety()

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
        snapshots = [e.risk_snapshot() for e in self.event_engines.values()]
        unrealized = sum(s.unrealized_pl for s in snapshots)
        realized = self.session_realized_pl + sum(e.realized_pl for e in self.event_engines.values())
        net = realized + unrealized
        start = self.session_start_balance
        net_pct = round(net / start * 100.0, 2) if start else None
        session = SessionInfo(
            start_balance=round(start, 2) if start is not None else None,
            current_balance=round(self._balance_cache, 2) if self._balance_cache is not None else None,
            realized_pl=round(realized, 2),
            unrealized_pl=round(unrealized, 2),
            net_pl=round(net, 2),
            net_pct=net_pct,
        )
        return MultiStatus(
            ts=now_ts(),
            mode=self.params.mode,
            events={et: eng.event_status() for et, eng in self.event_engines.items()},
            actions=list(self.actions),
            params=self.params,
            logs=list(self.logs),
            session=session,
        )

    def update_params(self, req: UpdateParamsRequest) -> None:
        data = req.model_dump(exclude_unset=True)
        for key, value in data.items():
            if value is None:
                continue
            if key in ("mode", "armed", "poll_seconds", "max_deploy_pct"):
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
    yield


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


@app.get("/api/buy_candidates")
async def api_buy_candidates():
    candidates = await engine.scan_buy_candidates()
    return {"candidates": [c.model_dump() for c in candidates]}


@app.post("/api/execute_buy")
async def api_execute_buy(req: BuyRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    return await engine.execute_buy(req.ticker, req.side, req.qty, req.limit_price_cents)


@app.get("/api/session")
async def api_session():
    s = engine.status().session
    return s.model_dump()


@app.get("/api/debug/balance")
async def api_debug_balance():
    k = engine.kalshi
    if not k.api_key_id:
        return {"error": "No API credentials configured"}
    path = "/trade-api/v2/portfolio/balance"
    url = k.base_url + path
    try:
        r = requests.get(url, headers=k._auth_headers("GET", path), timeout=10)
        try:
            body = r.json()
        except Exception:
            body = r.text[:500]
        return {"url": url, "status_code": r.status_code, "body": body, "cached": engine._balance_cache}
    except Exception as e:
        return {"error": str(e)}


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
