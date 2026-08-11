#!/usr/bin/env python3
"""
KalshiBaby Paper Trading Backend — Multi-event portfolio trading engine with local simulation.

This is a paper trading version that:
- Pulls live contract data (prices, availability) from Kalshi API
- Simulates buys/sells against a local SQLite database
- Tracks your virtual portfolio, P/L, and fill history
- Supports partial fills with interactive confirmation for remaining quantity

Run:
    uvicorn kalshibaby_paper:app --host 0.0.0.0 --port 8766 --reload

Open:
    http://127.0.0.1:8766
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import math
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple
import base64
import uuid
import sqlite3
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import requests
import yaml

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_config_env = __import__("os").environ.get("KALSHI_CONFIG", "config.yaml")
CONFIG_PATH = Path(_config_env)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path("config.example.yaml")

DB_PATH = Path("paper_trading.db")


# ---------------------------------------------------------------------------
# Type aliases and constants
# ---------------------------------------------------------------------------

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
    """Look up instrument config by prefix, with keyword fallback."""
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
# Database layer for paper trading
# ---------------------------------------------------------------------------

@dataclass
class PaperPosition:
    ticker: str
    event_ticker: str
    side: Side
    strike: float
    count: int
    avg_price: float
    created_ts: float = field(default_factory=now_ts)
    current_bid: float = 0.0
    current_ask: float = 0.0
    current_mid: float = 0.0
    peak_mid: float = 0.0


@dataclass 
class PaperFill:
    ticker: str
    event_ticker: str
    side: Side
    action: Literal["buy", "sell"]
    qty: int
    price: float
    total_cost: float
    ts: float
    order_id: str
    note: str = ""


class PaperDatabase:
    """SQLite-backed storage for paper trading positions and fills."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # Positions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strike REAL NOT NULL,
                    count INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    UNIQUE(ticker, side)
                )
            """)
            # Fills history table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    ts REAL NOT NULL,
                    order_id TEXT NOT NULL,
                    note TEXT DEFAULT ''
                )
            """)
            # Account balance / stats
            cur.execute("""
                CREATE TABLE IF NOT EXISTS account (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
            """)
            # Initialize starting balance if not exists
            cur.execute("SELECT COUNT(*) FROM account WHERE key = 'starting_balance'")
            if cur.fetchone()[0] == 0:
                cur.execute("INSERT INTO account (key, value) VALUES ('starting_balance', 10000.0)")
                cur.execute("INSERT INTO account (key, value) VALUES ('cash_balance', 10000.0)")
            conn.commit()
        finally:
            conn.close()

    def get_positions(self) -> List[PaperPosition]:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM positions ORDER BY event_ticker, ticker, side")
            rows = cur.fetchall()
            return [
                PaperPosition(
                    ticker=row["ticker"],
                    event_ticker=row["event_ticker"],
                    side=row["side"],
                    strike=row["strike"],
                    count=row["count"],
                    avg_price=row["avg_price"],
                    created_ts=row["created_ts"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def upsert_position(self, pos: PaperPosition):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO positions (ticker, event_ticker, side, strike, count, avg_price, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, side) DO UPDATE SET
                    count = excluded.count,
                    avg_price = excluded.avg_price,
                    updated_ts = excluded.updated_ts
            """, (
                pos.ticker, pos.event_ticker, pos.side, pos.strike,
                pos.count, pos.avg_price, pos.created_ts, now_ts()
            ))
            conn.commit()
        finally:
            conn.close()

    def delete_position(self, ticker: str, side: Side):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM positions WHERE ticker = ? AND side = ?", (ticker, side))
            conn.commit()
        finally:
            conn.close()

    def record_fill(self, fill: PaperFill):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO fills (ticker, event_ticker, side, action, qty, price, total_cost, ts, order_id, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fill.ticker, fill.event_ticker, fill.side, fill.action,
                fill.qty, fill.price, fill.total_cost, fill.ts, fill.order_id, fill.note
            ))
            conn.commit()
        finally:
            conn.close()

    def get_fills(self, limit: int = 100) -> List[PaperFill]:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            return [
                PaperFill(
                    ticker=row["ticker"],
                    event_ticker=row["event_ticker"],
                    side=row["side"],
                    action=row["action"],
                    qty=row["qty"],
                    price=row["price"],
                    total_cost=row["total_cost"],
                    ts=row["ts"],
                    order_id=row["order_id"],
                    note=row["note"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_cash_balance(self) -> float:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM account WHERE key = 'cash_balance'")
            row = cur.fetchone()
            return row["value"] if row else 0.0
        finally:
            conn.close()

    def update_cash_balance(self, delta: float):
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE account SET value = value + ? WHERE key = 'cash_balance'")
            cur.execute("UPDATE account SET value = value + ? WHERE key = 'realized_pl'")
            conn.commit()
        finally:
            conn.close()

    def get_starting_balance(self) -> float:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM account WHERE key = 'starting_balance'")
            row = cur.fetchone()
            return row["value"] if row else 10000.0
        finally:
            conn.close()

    def get_account_stats(self) -> Dict[str, float]:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM account")
            stats = {row["key"]: row["value"] for row in cur.fetchall()}
            # Calculate realized P/L from fills
            cur.execute("""
                SELECT SUM(CASE WHEN action = 'sell' THEN total_cost ELSE -total_cost END) as realized_pl
                FROM fills
            """)
            row = cur.fetchone()
            stats["realized_pl"] = row["realized_pl"] or 0.0
            return stats
        finally:
            conn.close()


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
    bot_config: Optional[Dict[str, Any]] = None


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
    action: Literal["HOLD", "TRIM", "SELL_LEG", "SELL_ALL", "ALERT", "PARTIAL_FILL"]
    reason: str
    event_ticker: Optional[str] = None
    ticker: Optional[str] = None
    side: Optional[Side] = None
    qty: Optional[int] = None
    headline: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None
    remaining_qty: Optional[int] = None  # for partial fills
    next_available_price: Optional[float] = None


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
    account: Optional[Dict[str, float]] = None
    recent_fills: Optional[List[Dict]] = None


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


class OrderRequest(BaseModel):
    ticker: Optional[str] = None
    event_ticker: Optional[str] = None
    side: Optional[Side] = None
    qty: int
    price: Optional[float] = None  # limit price, None = market
    confirm: bool = False
    order_type: Literal["buy", "sell"] = "buy"


class PartialFillConfirm(BaseModel):
    order_id: str
    sell_remaining: bool = False
    next_price: Optional[float] = None


# ---------------------------------------------------------------------------
# News signal model
# ---------------------------------------------------------------------------

class NewsSignal(BaseModel):
    source: str
    headline: str
    signal: str
    confidence: float
    reason: str
    ts: float
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# News regime tracker
# ---------------------------------------------------------------------------

class NewsRegimeTracker:
    FLIP_THRESHOLD = 2
    PRICE_CONFIRM_PCT = 0.30
    PRICE_CONFIRM_MINUTES = 10.0

    def __init__(self, actions_queue: Deque[Action], logs_queue: Deque[str]) -> None:
        self._actions = actions_queue
        self._logs = logs_queue
        self.regime: NewsRegime = "NEUTRAL"
        self.pending_signal: Optional[str] = None
        self.pending_count: int = 0
        self.last_transition_ts: float = 0.0
        self.recent_signals: Deque[NewsSignal] = deque(maxlen=50)
        self.min_flip_interval: float = 300.0

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._logs.appendleft(f"[NewsRegime] {stamp} {msg}")

    def _add_action(self, severity: str, action: str, reason: str,
                    headline: Optional[str] = None, url: Optional[str] = None,
                    source: Optional[str] = None) -> None:
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
        if signal in ("DEAL_SIGNAL", "BEARISH"):
            return "DEAL_MODE"
        if signal in ("ESCALATION_SIGNAL", "BULLISH"):
            return "ESCALATION_MODE"
        return None

    def ingest(self, signal: NewsSignal, price_history: Deque[Tuple[float, float]],
               event_engines: Dict[str, "EventEngine"], params: "RuntimeParams") -> None:
        self.recent_signals.appendleft(signal)
        self.log(f"Signal: {signal.signal} ({signal.confidence:.0%}) [{signal.source}] {signal.headline[:60]}")

        target_regime = self._signal_to_regime(signal.signal)
        if target_regime is None:
            return

        if target_regime == self.regime:
            self.pending_signal = None
            self.pending_count = 0
            return

        if self.pending_signal == target_regime:
            self.pending_count += 1
        else:
            self.pending_signal = target_regime
            self.pending_count = 1

        if self.pending_count < self.FLIP_THRESHOLD:
            return

        if now_ts() - self.last_transition_ts < self.min_flip_interval:
            return

        old_regime = self.regime
        self.regime = target_regime
        self.pending_signal = None
        self.pending_count = 0
        self.last_transition_ts = now_ts()

        self.log(f"REGIME TRANSITION: {old_regime} → {self.regime}")
        severity = "danger" if self.regime == "ESCALATION_MODE" else "info"
        self._add_action(
            severity, "ALERT",
            f"News regime shifted {old_regime} → {self.regime}",
            headline=signal.headline, url=signal.url, source=signal.source,
        )


# ---------------------------------------------------------------------------
# Kalshi API client (read-only for paper trading)
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

        if private_key_path and os.path.exists(private_key_path):
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            with open(private_key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        # For paper trading, we only need public endpoints (no auth required)
        # If credentials are provided, use them; otherwise return empty headers for public access
        if not self.api_key_id or not self.private_key:
            return {"Content-Type": "application/json"}
        
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

    async def get_events(self, status: str = "open") -> List[Dict]:
        """Fetch all open events."""
        url = f"{self.base_url}/trade-api/v2/events"
        try:
            r = requests.get(url, params={"status": status, "limit": 200}, timeout=10)
            if not r.ok:
                return []
            return r.json().get("events", [])
        except Exception:
            return []

    async def get_markets_for_event(self, event_ticker: str) -> List[Dict]:
        """Fetch all markets for an event."""
        url = f"{self.base_url}/trade-api/v2/markets"
        try:
            r = requests.get(url, params={"event_ticker": event_ticker, "status": "open", "limit": 200}, timeout=10)
            if not r.ok:
                return []
            return r.json().get("markets", [])
        except Exception:
            return []

    async def get_market_quote(self, ticker: str) -> Tuple[float, float, float, int, int]:
        """
        Get bid/ask/mid and available quantities for a market.
        Returns: (bid, ask, mid, bid_qty, ask_qty)
        """
        url = f"{self.base_url}/trade-api/v2/markets/{ticker}"
        try:
            r = requests.get(url, timeout=10)
            if not r.ok:
                return 0.0, 0.0, 0.0, 0, 0
            m = r.json().get("market", r.json())
            
            yes_bid = self._money_to_float(m.get("yes_bid_dollars")) or 0.0
            yes_ask = self._money_to_float(m.get("yes_ask_dollars")) or 0.0
            no_bid = self._money_to_float(m.get("no_bid_dollars")) or 0.0
            no_ask = self._money_to_float(m.get("no_ask_dollars")) or 0.0
            
            # Get available quantities from orderbook if available
            yes_bid_qty = int(m.get("yes_bid_count", 0) or m.get("open_interest", 0))
            yes_ask_qty = int(m.get("yes_ask_count", 0) or m.get("open_interest", 0))
            
            mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else (yes_bid or yes_ask or 0.0)
            return yes_bid, yes_ask, mid, yes_bid_qty, yes_ask_qty
        except Exception:
            return 0.0, 0.0, 0.0, 0, 0

    async def get_implied_event_price(self, event_ticker: str) -> Optional[float]:
        """Calculate implied probability from market prices."""
        markets = await self.get_markets_for_event(event_ticker)
        if not markets:
            return None

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
        db: PaperDatabase,
        actions_queue: Deque[Action],
        logs_queue: Deque[str],
        armed: bool = False,
        coordinator: Optional["Engine"] = None,
    ) -> None:
        self.event_ticker = event_ticker
        self.settlement_time = settlement_time
        self.params = params
        self.kalshi = kalshi
        self.db = db
        self._actions = actions_queue
        self._logs = logs_queue
        self.coordinator = coordinator

        self.positions: List[Position] = []
        self.armed = armed
        self.state: BotState = "NORMAL" if armed else "OBSERVE_ONLY"
        self.last_prices: Dict[str, PricePoint] = {}
        self.price_history: Deque[Tuple[float, float]] = deque(maxlen=2000)
        self.shock_start_ts: Optional[float] = None
        self.shock_extreme: Optional[float] = None
        self.shock_direction: Optional[Literal["up", "down"]] = None

        # Pending orders awaiting partial-fill confirmation
        self.pending_orders: Dict[str, Dict] = {}

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._logs.appendleft(f"[{self.event_ticker}] {stamp} {msg}")

    def _add_action(self, severity: str, action: str, reason: str,
                    ticker: Optional[str] = None, side: Optional[Side] = None,
                    qty: Optional[int] = None, remaining_qty: Optional[int] = None,
                    next_price: Optional[float] = None) -> None:
        self._actions.appendleft(Action(
            ts=now_ts(), severity=severity, action=action, reason=reason,
            event_ticker=self.event_ticker, ticker=ticker, side=side,
            qty=qty, remaining_qty=remaining_qty, next_available_price=next_price,
        ))
        self.log(f"{severity.upper()} {action}: {reason}")

    async def sync_positions_from_db(self):
        """Load positions from local database and enrich with live prices."""
        db_positions = self.db.get_positions()
        self.positions = []
        
        for dp in db_positions:
            if dp.event_ticker != self.event_ticker:
                continue
            
            # Get live quote
            bid, ask, mid, _, _ = await self.kalshi.get_market_quote(dp.ticker)
            
            pos = Position(
                ticker=dp.ticker,
                side=dp.side,
                strike=dp.strike,
                count=dp.count,
                avg_price=dp.avg_price,
                current_bid=bid,
                current_ask=ask,
                current_mid=mid,
                peak_mid=max(dp.avg_price, mid),
            )
            self.positions.append(pos)
            
            # Update price history
            if mid > 0:
                self.price_history.append((now_ts(), mid))

    def consensus_price(self) -> Optional[float]:
        """Return consensus price from available sources."""
        if not self.price_history:
            return None
        recent = [p for t, p in list(self.price_history)[-10:]]
        if not recent:
            return None
        return sum(recent) / len(recent)

    def position_endangered(self, p: Position, consensus: Optional[float]) -> bool:
        """Check if position is endangered based on consensus price."""
        if consensus is None:
            return False
        
        buffer_lower = self.params.structure.get("lower_boundary_buffer", 0.75)
        buffer_upper = self.params.structure.get("upper_boundary_buffer", 0.75)
        
        if p.side == "yes":
            return consensus < (p.strike + buffer_lower / 100.0)
        else:
            return consensus > (p.strike - buffer_upper / 100.0)

    async def simulate_buy(self, ticker: str, side: Side, qty: int, 
                           limit_price: Optional[float] = None) -> Dict[str, Any]:
        """
        Simulate a buy order against live Kalshi liquidity.
        In paper mode, we check what's available and execute virtually.
        """
        bid, ask, mid, bid_qty, ask_qty = await self.kalshi.get_market_quote(ticker)
        
        if mid <= 0:
            return {"ok": False, "error": "No liquidity available"}
        
        # For buys, we hit the ask
        exec_price = limit_price if limit_price else ask
        available = ask_qty if side == "yes" else bid_qty
        
        # Simulate partial fill
        fill_qty = min(qty, available) if available > 0 else qty
        
        if fill_qty <= 0:
            return {"ok": False, "error": "No contracts available at this price"}
        
        total_cost = fill_qty * exec_price
        
        # Update cash balance
        self.db.update_cash_balance(-total_cost)
        
        # Record fill
        fill = PaperFill(
            ticker=ticker,
            event_ticker=self.event_ticker,
            side=side,
            action="buy",
            qty=fill_qty,
            price=exec_price,
            total_cost=total_cost,
            ts=now_ts(),
            order_id=str(uuid.uuid4()),
            note=f"Paper buy @ {exec_price:.2f}"
        )
        self.db.record_fill(fill)
        
        # Update or create position
        existing = None
        for p in self.positions:
            if p.ticker == ticker and p.side == side:
                existing = p
                break
        
        if existing:
            # Average into existing position
            total_contracts = existing.count + fill_qty
            new_avg = ((existing.count * existing.avg_price) + (fill_qty * exec_price)) / total_contracts
            existing.count = total_contracts
            existing.avg_price = new_avg
        else:
            new_pos = Position(
                ticker=ticker,
                side=side,
                strike=parse_strike(ticker),
                count=fill_qty,
                avg_price=exec_price,
                current_bid=bid,
                current_ask=ask,
                current_mid=mid,
            )
            self.positions.append(new_pos)
        
        # Save to DB
        from kalshibaby_paper import PaperPosition
        dp = PaperPosition(
            ticker=ticker,
            event_ticker=self.event_ticker,
            side=side,
            strike=parse_strike(ticker),
            count=fill_qty if not existing else existing.count,
            avg_price=new_avg if existing else exec_price,
        )
        self.db.upsert_position(dp)
        
        result = {
            "ok": True,
            "filled_qty": fill_qty,
            "requested_qty": qty,
            "price": exec_price,
            "total_cost": total_cost,
            "paper": True,
        }
        
        # Check for partial fill
        if fill_qty < qty:
            remaining = qty - fill_qty
            result["partial_fill"] = True
            result["remaining_qty"] = remaining
            
            # Store pending order info
            order_id = str(uuid.uuid4())
            self.pending_orders[order_id] = {
                "ticker": ticker,
                "side": side,
                "remaining_qty": remaining,
                "original_qty": qty,
                "avg_filled_price": exec_price,
            }
            
            # Add action for user confirmation
            self._add_action(
                "warn", "PARTIAL_FILL",
                f"Bought {fill_qty}/{qty} @ {exec_price:.2f}. {remaining} remaining.",
                ticker=ticker, side=side, qty=fill_qty,
                remaining_qty=remaining, next_price=mid * 1.02  # Estimate next price
            )
            result["order_id"] = order_id
        
        self.log(f"PAPER BUY: {fill_qty} {side.upper()} {ticker} @ {exec_price:.2f} (total: ${total_cost:.2f})")
        return result

    async def simulate_sell(self, ticker: str, side: Side, qty: int,
                            limit_price: Optional[float] = None) -> Dict[str, Any]:
        """
        Simulate a sell order against live Kalshi liquidity.
        Handles partial fills with user confirmation flow.
        """
        # Find position
        pos = None
        for p in self.positions:
            if p.ticker == ticker and p.side == side:
                pos = p
                break
        
        if not pos or pos.count <= 0:
            return {"ok": False, "error": "No position to sell"}
        
        qty_to_sell = min(qty, pos.count)
        
        bid, ask, mid, bid_qty, ask_qty = await self.kalshi.get_market_quote(ticker)
        
        if mid <= 0:
            return {"ok": False, "error": "No liquidity available"}
        
        # For sells, we hit the bid
        exec_price = limit_price if limit_price else bid
        available = bid_qty if side == "yes" else ask_qty
        
        # Simulate partial fill based on available liquidity
        fill_qty = min(qty_to_sell, available) if available > 0 else qty_to_sell
        fill_qty = min(fill_qty, pos.count)  # Can't sell more than we have
        
        if fill_qty <= 0:
            return {"ok": False, "error": "No buyers available at this price"}
        
        proceeds = fill_qty * exec_price
        
        # Calculate P/L for this portion
        cost_basis = fill_qty * pos.avg_price
        pl = proceeds - cost_basis
        
        # Update cash balance
        self.db.update_cash_balance(proceeds)
        
        # Record fill
        fill = PaperFill(
            ticker=ticker,
            event_ticker=self.event_ticker,
            side=side,
            action="sell",
            qty=fill_qty,
            price=exec_price,
            total_cost=proceeds,
            ts=now_ts(),
            order_id=str(uuid.uuid4()),
            note=f"Paper sell @ {exec_price:.2f}, P/L: ${pl:.2f}"
        )
        self.db.record_fill(fill)
        
        # Update position
        pos.count -= fill_qty
        
        if pos.count <= 0:
            # Remove position
            self.positions.remove(pos)
            self.db.delete_position(ticker, side)
        else:
            # Update DB with remaining count
            from kalshibaby_paper import PaperPosition
            dp = PaperPosition(
                ticker=ticker,
                event_ticker=self.event_ticker,
                side=side,
                strike=pos.strike,
                count=pos.count,
                avg_price=pos.avg_price,
            )
            self.db.upsert_position(dp)
        
        result = {
            "ok": True,
            "filled_qty": fill_qty,
            "requested_qty": qty,
            "price": exec_price,
            "proceeds": proceeds,
            "pl": pl,
            "paper": True,
        }
        
        # Check for partial fill
        if fill_qty < qty_to_sell:
            remaining = qty_to_sell - fill_qty
            result["partial_fill"] = True
            result["remaining_qty"] = remaining
            
            # Store pending order
            order_id = str(uuid.uuid4())
            self.pending_orders[order_id] = {
                "ticker": ticker,
                "side": side,
                "remaining_qty": remaining,
                "filled_qty": fill_qty,
                "avg_filled_price": exec_price,
            }
            
            # Get next available price level (estimate)
            next_price = exec_price * 0.98 if side == "yes" else exec_price * 1.02
            
            # Add action requiring user decision
            self._add_action(
                "warn", "PARTIAL_FILL",
                f"Sold {fill_qty}/{qty_to_sell} @ {exec_price:.2f}. "
                f"{remaining} contracts remaining. Next available price ~{next_price:.2f}",
                ticker=ticker, side=side, qty=fill_qty,
                remaining_qty=remaining, next_price=next_price
            )
            result["order_id"] = order_id
            result["next_available_price"] = next_price
        
        self.log(f"PAPER SELL: {fill_qty} {side.upper()} {ticker} @ {exec_price:.2f} (proceeds: ${proceeds:.2f}, P/L: ${pl:.2f})")
        return result

    async def confirm_partial_fill(self, order_id: str, 
                                   sell_remaining: bool = True,
                                   next_price: Optional[float] = None) -> Dict[str, Any]:
        """
        User confirmed they want to sell/buy remaining quantity at next price.
        """
        if order_id not in self.pending_orders:
            return {"ok": False, "error": "Order not found"}
        
        order = self.pending_orders.pop(order_id)
        
        if not sell_remaining:
            # User cancelled - just remove pending order
            self.log(f"Partial fill cancelled for order {order_id}")
            return {"ok": True, "cancelled": True}
        
        # Execute remaining at next available price
        remaining_qty = order["remaining_qty"]
        ticker = order["ticker"]
        side = order["side"]
        
        # Use provided next_price or get fresh quote
        exec_price = next_price
        if not exec_price:
            bid, ask, mid, _, _ = await self.kalshi.get_market_quote(ticker)
            exec_price = bid if side == "yes" else ask
        
        if order.get("action") == "sell":
            # Continue selling remaining
            return await self.simulate_sell(ticker, side, remaining_qty, exec_price)
        else:
            # Continue buying remaining
            return await self.simulate_buy(ticker, side, remaining_qty, exec_price)

    def risk_snapshot(self) -> RiskSnapshot:
        """Calculate risk metrics for this event."""
        _empty = RiskSnapshot(
            cost_basis=0, mark_value=0, unrealized_pl=0, max_payout=0,
            max_profit=0, worst_settlement_value=0, worst_settlement_loss=0,
            modeled_stop_loss_value=0, modeled_stop_loss=0,
            yes_count=0, no_count=0, imbalance_ratio=0, settlement_map=[],
        )
        if not self.positions:
            return _empty

        cost = sum(p.count * p.avg_price for p in self.positions)
        mark = sum(p.count * p.current_mid for p in self.positions)
        max_payout = sum(p.count for p in self.positions)
        yes = sum(p.count for p in self.positions if p.side == "yes")
        no = sum(p.count for p in self.positions if p.side == "no")
        ratio = max(yes / no, no / yes) if yes and no else float("inf") if yes or no else 0.0

        strikes = sorted({p.strike for p in self.positions})
        test_pts = sorted({s + d for s in strikes for d in (-1, 0, 1)})
        settlement_map = []
        worst_val = float("inf")
        for x in test_pts:
            v = sum(p.count * (1.0 if (x > p.strike) == (p.side == "yes") else 0.0) for p in self.positions)
            worst_val = min(worst_val, v)
            settlement_map.append({"price": round(x, 2), "settlement_value": round(v, 2), "pl": round(v - cost, 2)})

        consensus = self.consensus_price()
        modeled = sum(
            p.count * (max(0.01, p.current_bid) if self.position_endangered(p, consensus) else p.current_mid)
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
            kalshi_implied_price=None,  # Would need to fetch
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

        api_cfg = config.get("api") or config.get("kalshi") or {}
        base_url = api_cfg.get("base_url", "https://api.elections.kalshi.com")
        if "/trade-api" in base_url:
            base_url = base_url[: base_url.index("/trade-api")]

        self.kalshi = KalshiClient(
            api_key_id=api_cfg.get("key_id") or api_cfg.get("api_key_id"),
            private_key_path=api_cfg.get("private_key_path"),
            base_url=base_url,
        )

        # Initialize paper trading database
        self.db = PaperDatabase(DB_PATH)

        self.params = RuntimeParams(
            mode="paper",  # Always paper mode
            armed=bool(config.get("armed", False)),
            poll_seconds=int(config.get("poll_seconds", 3)),
            profit_harvest=config.get("profit_harvest", {}),
            shock_logic=config.get("shock_logic", {}),
            source_consensus=config.get("source_consensus", {}),
            structure=config.get("structure", {}),
            time_risk=config.get("time_risk", {}),
            sources=config.get("sources", {}),
            safety=config.get("safety", {"global_drawdown_limit": -100.0}),
        )

        self.event_engines: Dict[str, EventEngine] = {}
        self.position_bots: Dict[str, Dict[str, Any]] = {}

        self.news_regime = NewsRegimeTracker(
            actions_queue=self.actions,
            logs_queue=self.logs,
        )

        self._running = False

    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.appendleft(f"{stamp} {msg}")

    def _add_action(self, severity: str, action: str, reason: str) -> None:
        self.actions.appendleft(Action(ts=now_ts(), severity=severity, action=action, reason=reason))
        self.log(f"{severity.upper()} {action}: {reason}")

    async def refresh_events(self):
        """Fetch all open events from Kalshi and create/update engines."""
        events = await self.kalshi.get_events()
        
        for evt in events:
            event_ticker = evt.get("ticker", "")
            if not event_ticker:
                continue
            
            if event_ticker not in self.event_engines:
                # Create new event engine
                settlement_time = None
                if evt.get("settlement_time"):
                    try:
                        settlement_time = datetime.fromisoformat(evt["settlement_time"].replace("Z", "+00:00"))
                    except:
                        pass
                
                engine = EventEngine(
                    event_ticker=event_ticker,
                    settlement_time=settlement_time,
                    params=self.params,
                    kalshi=self.kalshi,
                    db=self.db,
                    actions_queue=self.actions,
                    logs_queue=self.logs,
                    armed=self.params.armed,
                    coordinator=self,
                )
                self.event_engines[event_ticker] = engine
                self.log(f"Tracking new event: {event_ticker}")
            
            # Sync positions from DB
            await self.event_engines[event_ticker].sync_positions_from_db()

    async def loop(self):
        """Main polling loop."""
        self._running = True
        self.log("Paper trading engine started")
        
        while self._running:
            try:
                await self.refresh_events()
                
                # Update all positions with live prices
                for eng in self.event_engines.values():
                    await eng.sync_positions_from_db()
                
            except Exception as e:
                self.log(f"Error in main loop: {e}")
            
            await asyncio.sleep(self.params.poll_seconds)

    def status(self) -> MultiStatus:
        """Get current status of all events."""
        events = {}
        for event_ticker, eng in self.event_engines.items():
            if eng.positions:  # Only show events with positions
                events[event_ticker] = eng.event_status()
        
        # Get account stats
        account_stats = self.db.get_account_stats()
        starting = account_stats.get("starting_balance", 10000.0)
        cash = account_stats.get("cash_balance", starting)
        realized_pl = account_stats.get("realized_pl", 0.0)
        
        # Calculate portfolio value
        portfolio_value = sum(
            p.count * p.current_mid 
            for eng in self.event_engines.values() 
            for p in eng.positions
        )
        
        account_stats["portfolio_value"] = portfolio_value
        account_stats["total_value"] = cash + portfolio_value
        account_stats["total_return"] = (cash + portfolio_value - starting) / starting * 100
        
        # Get recent fills
        recent_fills = [
            {
                "ticker": f.ticker,
                "event_ticker": f.event_ticker,
                "side": f.side,
                "action": f.action,
                "qty": f.qty,
                "price": f.price,
                "total": f.total_cost,
                "ts": f.ts,
                "note": f.note,
            }
            for f in self.db.get_fills(limit=20)
        ]
        
        return MultiStatus(
            ts=now_ts(),
            mode="paper",
            events=events,
            actions=list(self.actions)[:50],
            params=self.params,
            logs=list(self.logs)[:100],
            news_regime=self.news_regime.regime,
            position_bots=self.position_bots,
            account=account_stats,
            recent_fills=recent_fills,
        )

    def arm_event(self, event_ticker: str, armed: bool) -> None:
        if event_ticker in self.event_engines:
            self.event_engines[event_ticker].armed = armed
            self.log(f"Event {event_ticker} armed={armed}")

    def update_params(self, req: UpdateParamsRequest) -> None:
        if req.mode is not None:
            self.params.mode = "paper"  # Force paper mode
        if req.armed is not None:
            self.params.armed = req.armed
        if req.poll_seconds is not None:
            self.params.poll_seconds = req.poll_seconds


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


app = FastAPI(title="KalshiBaby Paper Trading", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")


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


@app.post("/api/order")
async def api_order(req: OrderRequest):
    """Place a buy or sell order (simulated in paper mode)."""
    if not req.ticker and not req.event_ticker:
        raise HTTPException(status_code=400, detail="ticker or event_ticker required")
    
    # Find the event engine
    eng = None
    if req.event_ticker:
        eng = engine.event_engines.get(req.event_ticker)
    else:
        # Find by ticker
        for e in engine.event_engines.values():
            if any(p.ticker == req.ticker for p in e.positions):
                eng = e
                break
    
    if not eng:
        raise HTTPException(status_code=404, detail="Event not found")
    
    if req.order_type == "buy":
        if not req.side:
            raise HTTPException(status_code=400, detail="side required for buy")
        return await eng.simulate_buy(req.ticker, req.side, req.qty, req.price)
    else:  # sell
        if not req.ticker:
            raise HTTPException(status_code=400, detail="ticker required for sell")
        # Infer side from position
        pos = next((p for p in eng.positions if p.ticker == req.ticker), None)
        if not pos:
            raise HTTPException(status_code=404, detail="Position not found")
        return await eng.simulate_sell(req.ticker, pos.side, req.qty, req.price)


@app.post("/api/confirm_partial_fill")
async def api_confirm_partial_fill(req: PartialFillConfirm):
    """Confirm what to do with remaining quantity after partial fill."""
    # Find which event has this pending order
    for eng in engine.event_engines.values():
        if req.order_id in eng.pending_orders:
            return await eng.confirm_partial_fill(
                req.order_id,
                req.sell_remaining,
                req.next_price
            )
    
    raise HTTPException(status_code=404, detail="Pending order not found")


@app.get("/api/fills")
async def api_fills(limit: int = 50):
    """Get recent fill history."""
    fills = engine.db.get_fills(limit)
    return {
        "fills": [
            {
                "ticker": f.ticker,
                "event_ticker": f.event_ticker,
                "side": f.side,
                "action": f.action,
                "qty": f.qty,
                "price": f.price,
                "total": f.total_cost,
                "ts": datetime.fromtimestamp(f.ts).isoformat(),
                "note": f.note,
            }
            for f in fills
        ]
    }


@app.get("/api/account")
async def api_account():
    """Get account balance and stats."""
    stats = engine.db.get_account_stats()
    starting = stats.get("starting_balance", 10000.0)
    
    # Calculate current portfolio value
    portfolio_value = sum(
        p.count * p.current_mid
        for eng in engine.event_engines.values()
        for p in eng.positions
    )
    
    cash = stats.get("cash_balance", starting)
    realized_pl = stats.get("realized_pl", 0.0)
    
    return {
        "starting_balance": starting,
        "cash_balance": cash,
        "portfolio_value": portfolio_value,
        "total_value": cash + portfolio_value,
        "realized_pl": realized_pl,
        "total_return_pct": (cash + portfolio_value - starting) / starting * 100,
    }


@app.post("/api/reset_account")
async def api_reset_account():
    """Reset paper trading account to starting balance (for testing)."""
    # This would require implementing a reset method in PaperDatabase
    return {"ok": True, "message": "Account reset (implement in DB)"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8766)
