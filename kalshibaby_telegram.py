#!/usr/bin/env python3
"""
KalshiBaby Telegram Bot — mobile command and alert interface.

Bridges the KalshiBaby v3 backend to your phone via Telegram.

v2 CHANGES (five-gate Wapner integration):
  - /wapner now runs the same five-gate checklist as the dashboard and the
    /api/wapner_candidates endpoint (wapner_checklist.py). One evaluator,
    three views, zero drift.
  - /wapner shows PASS candidates only; /wapner all names failed gates.
  - Auto-alerts fire on exactly two transitions:
        REJECT/absent -> PASS   (window opened — includes size cap + exit)
        PASS -> REJECT          (gate broke — if you entered, exit bell)
    Candidates that vanish because the market settled clear silently.
  - telegram.wapner_min_mid / wapner_min_cushion / wapner_max_minutes are
    RETIRED. Gates live only in the top-level wapner: block of config.yaml.
    A warning is logged if the old keys are still present.
  - If an EventEngine has no settlement_time (portfolio-synced events), the
    bot backfills it by parsing the event ticker, so Gate 1 always works.

Features:
  PUSH alerts (bot → you):
    - Wapner Window PASS detected (with size cap and exit trigger)
    - Wapner gate broke on a previously passing candidate
    - Stop loss fired (with confirm/cancel buttons)
    - Settlement approaching (15-min warning)
    - Global drawdown limit approaching
    - Morning session brief (7:00am)

  PULL commands (you → bot):
    /status        — all positions and P/L
    /price         — current consensus prices per event
    /wapner        — Wapner PASS candidates ( /wapner all → include rejects )
    /stop <ticker> <price> — set stop loss on a position
    /clearstop <ticker>    — remove stop loss
    /sell <ticker>         — sell position (asks confirmation)
    /arm <event_ticker>    — arm an event
    /disarm <event_ticker> — disarm an event
    /mode paper|live       — switch execution mode
    /help          — command reference

Setup:
  1. Message @BotFather on Telegram, create a bot, copy the token.
  2. Get your Telegram chat ID: message @userinfobot or start the bot
     and check logs for your chat_id.
  3. Add to config.yaml:
       telegram:
         token: "123456:ABC-your-token-here"
         allowed_chat_ids:
           - 987654321        # your personal chat ID
         morning_brief_time: "07:00"   # local time HH:MM
       wapner:                # top-level block — shared with the dashboard
         max_minutes: 60
         min_mid: 0.85
         max_mid: 0.97
         cushion_vol_multiple: 3.0
         trend_lookback_minutes: 20
         vol_lookback_minutes: 60
         min_history_minutes: 15
         max_loss_dollars: 10.0
         exit_cushion_fraction: 0.5
  4. pip install python-telegram-bot --break-system-packages
  5. Import and start in kalshibaby_backend.py lifespan (see bottom of file).

Dependencies:
  python-telegram-bot >= 20.0  (async API)
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from wapner_checklist import evaluate_event_wapner

logger = logging.getLogger(__name__)

try:
    from telegram import (
        Bot,
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        ContextTypes,
    )
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning(
        "python-telegram-bot not installed. "
        "Run: pip install python-telegram-bot --break-system-packages"
    )

if TYPE_CHECKING:
    from kalshibaby_backend import Engine


# ---------------------------------------------------------------------------
# Emoji constants — keeps message formatting readable
# ---------------------------------------------------------------------------
EMOJI = {
    "money":    "💰",
    "warn":     "⚠️",
    "danger":   "🚨",
    "info":     "ℹ️",
    "clock":    "⏰",
    "chart":    "📊",
    "up":       "📈",
    "down":     "📉",
    "check":    "✅",
    "cross":    "❌",
    "fire":     "🔥",
    "shield":   "🛡️",
    "news":     "📰",
    "gold":     "🏅",
    "oil":      "🛢️",
    "gear":     "⚙️",
    "wall":     "🧱",
    "target":   "🎯",
}

_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _fmt_price(p: Optional[float], decimals: int = 2) -> str:
    return f"{p:.{decimals}f}" if p is not None else "—"


def _fmt_pl(pl: Optional[float]) -> str:
    if pl is None:
        return "—"
    sign = "+" if pl >= 0 else ""
    return f"{sign}${pl:.2f}"


def _cents(price: float) -> str:
    return f"{price * 100:.0f}¢"


def parse_settlement_from_ticker(event_ticker: str) -> Optional[datetime]:
    """KXBRENTD-26JUL0617 -> 2026-07-06 17:00 ET. None if unparseable."""
    m = re.search(r"(\d{2})([A-Z]{3})(\d{2})(\d{2})$", event_ticker)
    if not m:
        return None
    try:
        return datetime(
            2000 + int(m.group(1)),
            _MONTH_MAP[m.group(2)],
            int(m.group(3)),
            int(m.group(4)), 0, 0,
            tzinfo=timezone(timedelta(hours=-4)),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# KalshiBabyBot
# ---------------------------------------------------------------------------

class KalshiBabyBot:
    """
    Telegram bot that wraps the KalshiBaby Engine.

    Lifecycle:
        bot = KalshiBabyBot(engine, config["telegram"])
        await bot.start()          # call from lifespan
        # ... engine runs ...
        await bot.stop()           # call on shutdown
    """

    def __init__(self, engine: "Engine", telegram_cfg: Dict) -> None:
        self.engine              = engine
        self.token               = telegram_cfg["token"]
        self.allowed_ids: Set[int] = {
            int(cid) for cid in telegram_cfg.get("allowed_chat_ids", [])
        }
        self.morning_time        = telegram_cfg.get("morning_brief_time", "07:00")

        # Retired keys — gates live in the top-level wapner: block now.
        for dead in ("wapner_min_mid", "wapner_min_cushion", "wapner_max_minutes"):
            if dead in telegram_cfg:
                logger.warning(
                    f"config telegram.{dead} is retired and IGNORED — "
                    f"the five gates live in the top-level wapner: block."
                )

        # Wapner transition tracking: (ticker, side) -> first-PASS timestamp
        self._wapner_live: Dict[Tuple[str, str], float] = {}
        # Misc one-shot alert keys (settlement warnings, drawdown warning)
        self._alerted_misc: Set[str] = set()
        # Pending sell confirmations: callback_id → (eng, position, qty)
        self._pending_sells: Dict[str, tuple] = {}

        self._app: Optional[Application] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._morning_task: Optional[asyncio.Task] = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def start(self) -> None:
        if not TELEGRAM_AVAILABLE:
            logger.warning("Telegram bot disabled — python-telegram-bot not installed.")
            return
        if not self.token or self.token == "YOUR_TOKEN_HERE":
            logger.warning("Telegram bot disabled — no token configured.")
            return

        self._app = (
            Application.builder()
            .token(self.token)
            .build()
        )
        self._register_handlers()
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        # Background tasks
        self._poll_task    = asyncio.create_task(self._alert_loop())
        self._morning_task = asyncio.create_task(self._morning_brief_loop())
        logger.info("Telegram bot started.")

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self._morning_task:
            self._morning_task.cancel()
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Telegram bot stopped.")

    # -----------------------------------------------------------------------
    # Handler registration
    # -----------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start",   self._cmd_start))
        app.add_handler(CommandHandler("help",    self._cmd_help))
        app.add_handler(CommandHandler("status",  self._cmd_status))
        app.add_handler(CommandHandler("price",   self._cmd_price))
        app.add_handler(CommandHandler("wapner",  self._cmd_wapner))
        app.add_handler(CommandHandler("stop",    self._cmd_set_stop))
        app.add_handler(CommandHandler("clearstop", self._cmd_clear_stop))
        app.add_handler(CommandHandler("stopevent", self._cmd_stop_event))
        app.add_handler(CommandHandler("sell",    self._cmd_sell))
        app.add_handler(CommandHandler("arm",     self._cmd_arm))
        app.add_handler(CommandHandler("disarm",  self._cmd_disarm))
        app.add_handler(CommandHandler("mode",    self._cmd_mode))
        app.add_handler(CommandHandler("hedge",   self._cmd_hedge))
        app.add_handler(CallbackQueryHandler(self._on_callback))

    # -----------------------------------------------------------------------
    # Auth guard
    # -----------------------------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        cid = update.effective_chat.id
        if self.allowed_ids and cid not in self.allowed_ids:
            logger.warning(f"Unauthorized Telegram access attempt from chat_id={cid}")
            return False
        return True

    async def _send(self, chat_id: int, text: str, reply_markup=None) -> None:
        """Send a message, swallowing errors so a bad send doesn't crash the engine."""
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    async def _broadcast(self, text: str, reply_markup=None, **_ignored) -> None:
        """Send to all allowed chat IDs."""
        for cid in self.allowed_ids:
            await self._send(cid, text, reply_markup)

    # -----------------------------------------------------------------------
    # Wapner checklist bridge
    # -----------------------------------------------------------------------

    def _ensure_settlement_time(self, eng) -> None:
        """Backfill settlement_time for portfolio-synced events (Gate 1 needs it)."""
        if eng.settlement_time is None:
            st = parse_settlement_from_ticker(eng.event_ticker)
            if st is not None:
                eng.settlement_time = st

    def _minutes_left(self, eng) -> Optional[float]:
        self._ensure_settlement_time(eng)
        if eng.settlement_time is None:
            return None
        now = datetime.now(eng.settlement_time.tzinfo or timezone.utc)
        return (eng.settlement_time - now).total_seconds() / 60.0

    def _scan_wapner(self, only_in_window: bool) -> Tuple[List[dict], List[dict], List[str]]:
        """
        Run the five-gate evaluator across events.
        Returns (passes, rejects, errors). Blocking — call via asyncio.to_thread.
        only_in_window=True skips events whose window can't be open yet
        (alert loop); False evaluates everything (manual /wapner).
        """
        passes: List[dict] = []
        rejects: List[dict] = []
        errors: List[str] = []
        for et, eng in list(self.engine.event_engines.items()):
            mins = self._minutes_left(eng)
            if only_in_window and (mins is None or mins <= 0 or mins > 75):
                continue
            try:
                for c in evaluate_event_wapner(self.engine, eng):
                    if not c.get("ticker"):
                        # event-level rejection (e.g. no settlement_time)
                        rejects.append({"ticker": et, "side": "", "mid": 0,
                                        "checks": {}, "grade": "REJECT",
                                        "reasons": c.get("reasons", [])})
                    elif c.get("grade") == "PASS":
                        passes.append(c)
                    else:
                        rejects.append(c)
            except Exception as e:
                errors.append(f"{et}: scan error {e}")
        return passes, rejects, errors

    def _fmt_pass(self, c: dict) -> str:
        s = c.get("sizing", {})
        win = s.get("win_net_per_contract") or 0
        return (
            f"{EMOJI['check']} <b>PASS</b> <code>{c['ticker']}</code> "
            f"{c['side'].upper()} @ {_cents(c['mid'])}\n"
            f"  {EMOJI['clock']} {c['minutes_left']:.0f} min · spot {c['spot']} · "
            f"cushion {c['cushion']} vs move {c['expected_remaining_move']}\n"
            f"  {EMOJI['target']} Size cap <b>{s.get('max_contracts')}</b> "
            f"(max loss ${s.get('max_loss_dollars')}) · win {round(win * 100)}¢/ct · "
            f"one loss ≈ {s.get('wins_erased_by_one_loss')} wins\n"
            f"  {EMOJI['shield']} Exit if spot crosses <b>{c['exit_trigger_spot']}</b>"
            f" — set it NOW:\n"
            f"  <code>/stop {c['ticker']} …</code>"
        )

    def _fmt_reject(self, c: dict) -> str:
        failed = [k for k, v in (c.get("checks") or {}).items() if not v]
        gates = ", ".join(failed) if failed else "; ".join(c.get("reasons", [])) or "event"
        mid = f" @ {_cents(c['mid'])}" if c.get("mid") else ""
        return (
            f"{EMOJI['cross']} <code>{c['ticker']}</code> "
            f"{(c.get('side') or '').upper()}{mid} — REJECT ({gates})"
        )

    # -----------------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------------

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            f"{EMOJI['fire']} <b>KalshiBaby v3</b> connected!\n"
            f"Chat ID: <code>{update.effective_chat.id}</code>\n\n"
            f"Type /help for commands.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        text = (
            f"{EMOJI['gear']} <b>KalshiBaby Commands</b>\n\n"
            f"/status — positions and P/L\n"
            f"/price — consensus prices\n"
            f"/wapner — five-gate PASS candidates\n"
            f"/wapner all — include rejects with failed gates\n"
            f"/stop &lt;price&gt; — set stop loss (pick from list)\n"
            f"/stop &lt;ticker&gt; &lt;price&gt; — set stop loss direct\n"
            f"/stopevent &lt;price&gt; — stop on ALL legs of an event\n"
            f"/clearstop — remove a stop (pick from list)\n"
            f"/sell — sell a position (pick from list, confirms)\n"
            f"/arm — arm an event (pick from list)\n"
            f"/disarm — disarm an event (pick from list)\n"
            f"/mode paper|live — switch execution mode\n"
            f"/hedge [event] — settlement map, both-win zone, green floors\n"
            f"/help — this message\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        status = self.engine.status()
        if not status.events:
            await update.message.reply_text(
                f"{EMOJI['info']} No active events.",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [f"{EMOJI['chart']} <b>KalshiBaby Status</b> ({status.mode.upper()})\n"]
        for et, es in status.events.items():
            r = es.risk
            armed = "🟢 ARMED" if es.armed else "⚪ DISARMED"
            lines.append(f"<b>{et}</b> {armed}")
            lines.append(
                f"  Consensus: <b>{_fmt_price(es.consensus_price)}</b> | "
                f"State: {es.state}"
            )
            lines.append(
                f"  Cost: {_fmt_pl(r.cost_basis)} | "
                f"Mark: {_fmt_pl(r.mark_value)} | "
                f"P/L: <b>{_fmt_pl(r.unrealized_pl)}</b>"
            )
            if es.positions:
                lines.append("  <i>Positions:</i>")
                for p in es.positions:
                    if p.count <= 0:
                        continue
                    pl = p.count * ((p.current_bid or p.current_mid) - p.avg_price)
                    lines.append(
                        f"    {p.side.upper()} >{p.strike} ×{p.count} "
                        f"@ {_cents(p.avg_price)} | "
                        f"mid {_cents(p.current_mid)} | "
                        f"{_fmt_pl(pl)}"
                    )
            lines.append("")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_price(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        status = self.engine.status()
        if not status.events:
            await update.message.reply_text(f"{EMOJI['info']} No active events.")
            return

        lines = [f"{EMOJI['chart']} <b>Consensus Prices</b>\n"]
        for et, es in status.events.items():
            lines.append(f"<b>{et}</b>")
            lines.append(f"  Consensus: <b>{_fmt_price(es.consensus_price)}</b>")
            for p in es.prices:
                stale = " ⚠️STALE" if p.stale else ""
                lines.append(
                    f"  {p.source}: {_fmt_price(p.price)}{stale}"
                )
            lines.append("")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_wapner(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        show_all = bool(ctx.args) and ctx.args[0].lower() == "all"
        passes, rejects, errors = await asyncio.to_thread(
            self._scan_wapner, False
        )

        lines = [f"{EMOJI['clock']} <b>Wapner Window</b> — five-gate scan\n"]
        if passes:
            lines += [self._fmt_pass(c) for c in passes]
        else:
            lines.append(
                f"{EMOJI['wall']} No PASS candidates right now.\n"
                f"Rejections are the system working — no trade is a result."
            )
        if show_all and rejects:
            lines.append("")
            lines += [self._fmt_reject(c) for c in rejects[:15]]
            if len(rejects) > 15:
                lines.append(f"…+{len(rejects) - 15} more rejects.")
        elif rejects:
            lines.append(
                f"\n{EMOJI['info']} {len(rejects)} strike(s) rejected. "
                f"/wapner all to see the failed gates."
            )
        lines += errors
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # -----------------------------------------------------------------------
    # Picker helpers — one-tap lists so nothing needs to be typed by hand
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_price(s: str) -> Optional[float]:
        try:
            price = float(s)
        except ValueError:
            return None
        if price > 1:
            price = price / 100  # accept cents input e.g. 75 → 0.75
        return price if 0 < price < 1 else None

    def _open_positions(self):
        """[(eng, position), ...] for all open positions."""
        out = []
        for eng in self.engine.event_engines.values():
            for p in eng.positions:
                if p.count > 0:
                    out.append((eng, p))
        return out

    def _positions_keyboard(self, cb_prefix: str) -> Optional[InlineKeyboardMarkup]:
        rows = []
        for eng, p in self._open_positions():
            strike = p.ticker.split("-")[-1]
            label = (f"{eng.event_ticker.split('-')[0]} {strike} "
                     f"{p.side.upper()} ×{p.count} @ {_cents(p.current_mid)}")
            rows.append([InlineKeyboardButton(
                label, callback_data=f"{cb_prefix}{p.ticker}")])
        return InlineKeyboardMarkup(rows) if rows else None

    def _events_keyboard(self, cb_prefix: str, armed: Optional[bool]) -> Optional[InlineKeyboardMarkup]:
        rows = []
        for et, eng in self.engine.event_engines.items():
            if armed is not None and eng.armed != armed:
                continue
            n = sum(1 for p in eng.positions if p.count > 0)
            rows.append([InlineKeyboardButton(
                f"{et} ({n} pos)", callback_data=f"{cb_prefix}{et}")])
        return InlineKeyboardMarkup(rows) if rows else None

    def _suggest_event(self, wrong: str) -> Optional[str]:
        import difflib
        matches = difflib.get_close_matches(
            wrong.upper(), list(self.engine.event_engines.keys()), n=1, cutoff=0.6)
        return matches[0] if matches else None

    def _apply_stop(self, ticker: str, price: float) -> Optional[str]:
        """Set a stop bot for a ticker. Returns event_ticker or None if not found.
        Stores the NESTED config shape the engine reads — the old flat shape
        raised KeyError in _execute_position_bots and aborted the whole tick."""
        for eng in self.engine.event_engines.values():
            p = next((x for x in eng.positions if x.ticker == ticker), None)
            if p:
                self.engine.position_bots[ticker] = {
                    "ticker": ticker,
                    "event_ticker": eng.event_ticker,
                    "config": {"stop_loss": price, "harvest": False},
                    "created_ts": __import__("time").time(),
                }
                self.engine.log(f"Stop set via Telegram: {ticker} @ {price:.2f}")
                return eng.event_ticker
        return None

    async def _cmd_set_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []

        # /stop <price> — pick the position from a list
        if len(args) == 1:
            price = self._parse_price(args[0])
            if price is None:
                await update.message.reply_text(
                    f"Usage: /stop &lt;price&gt; (pick position)\n"
                    f"or /stop &lt;ticker&gt; &lt;price&gt;",
                    parse_mode=ParseMode.HTML,
                )
                return
            kb = self._positions_keyboard(f"stoppick_{int(round(price * 100))}_")
            if kb is None:
                await update.message.reply_text("No open positions.")
                return
            await update.message.reply_text(
                f"{EMOJI['shield']} Set stop @ {_cents(price)} on which position?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return

        if len(args) < 2:
            await update.message.reply_text(
                f"Usage:\n"
                f"/stop &lt;price&gt; — pick position from list\n"
                f"/stop &lt;ticker&gt; &lt;price&gt; — direct",
                parse_mode=ParseMode.HTML,
            )
            return

        ticker = args[0]
        price = self._parse_price(args[1])
        if price is None:
            await update.message.reply_text(f"Invalid price: {args[1]}")
            return
        et = self._apply_stop(ticker, price)
        if et:
            await update.message.reply_text(
                f"{EMOJI['shield']} Stop loss set: <b>{ticker}</b> @ {_cents(price)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                f"{EMOJI['cross']} Position not found: {ticker}\n"
                f"Tip: /stop {args[1]} shows a pick list.",
            )

    async def _cmd_stop_event(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /stopevent <price> — pick an event, set the stop on EVERY open leg.
        /stopevent <event_ticker> <price> — direct.
        """
        if not self._authorized(update):
            return
        args = ctx.args or []
        if len(args) == 1:
            price = self._parse_price(args[0])
            if price is None:
                await update.message.reply_text(
                    "Usage: /stopevent &lt;price&gt; or /stopevent &lt;event&gt; &lt;price&gt;",
                    parse_mode=ParseMode.HTML)
                return
            kb = self._events_keyboard(f"stopev_{int(round(price * 100))}_", armed=None)
            if kb is None:
                await update.message.reply_text("No events tracked.")
                return
            await update.message.reply_text(
                f"{EMOJI['shield']} Set stop @ {_cents(price)} on ALL legs of which event?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return
        if len(args) >= 2:
            et = args[0].upper()
            price = self._parse_price(args[1])
            if price is None:
                await update.message.reply_text(f"Invalid price: {args[1]}")
                return
            n = self._apply_stop_event(et, price)
            if n is None:
                sug = self._suggest_event(et)
                hint = f"\nDid you mean <b>{sug}</b>?" if sug else ""
                await update.message.reply_text(
                    f"{EMOJI['cross']} Event not found: {et}{hint}",
                    parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(
                    f"{EMOJI['shield']} Stop @ {_cents(price)} set on "
                    f"<b>{n}</b> legs of {et}", parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            "Usage: /stopevent &lt;price&gt; or /stopevent &lt;event&gt; &lt;price&gt;",
            parse_mode=ParseMode.HTML)

    def _apply_stop_event(self, event_ticker: str, price: float) -> Optional[int]:
        eng = self.engine.event_engines.get(event_ticker)
        if eng is None:
            return None
        n = 0
        for p in eng.positions:
            if p.count > 0 and self._apply_stop(p.ticker, price):
                n += 1
        return n

    async def _cmd_clear_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args:
            bots = self.engine.position_bots
            if not bots:
                await update.message.reply_text("No bot configs set.")
                return
            rows = []
            for ticker, bot in bots.items():
                cfg = bot.get("config") if isinstance(bot.get("config"), dict) else bot
                stop = cfg.get("stop_loss")
                label = ticker.split("-", 1)[-1]
                if stop is not None:
                    label += f" (stop {_cents(float(stop))})"
                rows.append([InlineKeyboardButton(
                    label, callback_data=f"clearstoppick_{ticker}")])
            await update.message.reply_text(
                f"{EMOJI['gear']} Clear which bot?",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        ticker = args[0]
        removed = self.engine.position_bots.pop(ticker, None)
        if removed:
            await update.message.reply_text(
                f"{EMOJI['check']} Stop loss cleared: <b>{ticker}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                f"{EMOJI['info']} No bot config found for {ticker}"
            )

    def _sell_confirm_payload(self, eng, p):
        """Register a pending manual sell and return (text, keyboard)."""
        callback_id = f"manual_{p.ticker}"
        self._pending_sells[callback_id] = (eng, p, p.count)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Sell {p.count} {p.side.upper()} {p.ticker}",
                callback_data=f"confirm_{callback_id}",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"cancel_{callback_id}",
            ),
        ]])
        text = (
            f"{EMOJI['warn']} <b>Confirm sell?</b>\n"
            f"Ticker: {p.ticker}\n"
            f"Side: {p.side.upper()}\n"
            f"Qty: {p.count}\n"
            f"Avg: {_cents(p.avg_price)} | Mid: {_cents(p.current_mid)}\n"
            f"Est P/L: {_fmt_pl(p.count * (p.current_mid - p.avg_price))}"
        )
        return text, keyboard

    async def _cmd_sell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args:
            kb = self._positions_keyboard("sellpick_")
            if kb is None:
                await update.message.reply_text("No open positions.")
                return
            await update.message.reply_text(
                f"{EMOJI['warn']} Sell which position?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return
        ticker = args[0]

        # Find position
        for eng in self.engine.event_engines.values():
            p = next((x for x in eng.positions if x.ticker == ticker), None)
            if p:
                text, keyboard = self._sell_confirm_payload(eng, p)
                await update.message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
                return

        await update.message.reply_text(
            f"{EMOJI['cross']} Position not found: {ticker}\n"
            f"Tip: /sell with no arguments shows a pick list."
        )

    async def _cmd_arm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args:
            kb = self._events_keyboard("armpick_", armed=False)
            if kb is None:
                await update.message.reply_text(
                    "No disarmed events — everything tracked is already armed "
                    "(or nothing is tracked)."
                )
                return
            await update.message.reply_text(
                f"{EMOJI['fire']} Arm which event?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return
        et = args[0].upper()
        try:
            self.engine.arm_event(et, True)
            await update.message.reply_text(
                f"{EMOJI['fire']} <b>{et}</b> ARMED",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            sug = self._suggest_event(et)
            hint = f"\nDid you mean <b>{sug}</b>? Try /arm with no arguments." if sug else ""
            await update.message.reply_text(
                f"{EMOJI['cross']} Error: {e}{hint}", parse_mode=ParseMode.HTML)

    async def _cmd_disarm(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args:
            kb = self._events_keyboard("disarmpick_", armed=True)
            if kb is None:
                await update.message.reply_text("No armed events.")
                return
            await update.message.reply_text(
                f"{EMOJI['shield']} Disarm which event?",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
            return
        et = args[0].upper()
        try:
            self.engine.arm_event(et, False)
            await update.message.reply_text(
                f"{EMOJI['shield']} <b>{et}</b> DISARMED",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            sug = self._suggest_event(et)
            hint = f"\nDid you mean <b>{sug}</b>? Try /disarm with no arguments." if sug else ""
            await update.message.reply_text(
                f"{EMOJI['cross']} Error: {e}{hint}", parse_mode=ParseMode.HTML)

    async def _cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args or args[0] not in ("paper", "live"):
            await update.message.reply_text(
                "Usage: /mode paper|live\n\n"
                f"Current mode: <b>{self.engine.params.mode.upper()}</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        if args[0] == "live":
            # Require explicit confirmation for live mode via inline button
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, go LIVE", callback_data="confirm_setlive"),
                InlineKeyboardButton("❌ Cancel",       callback_data="cancel_setlive"),
            ]])
            await update.message.reply_text(
                f"{EMOJI['danger']} Switch to <b>LIVE mode</b>? "
                f"Real orders will be sent when events are armed.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            self.engine.params.mode = "paper"
            await update.message.reply_text(
                f"{EMOJI['shield']} Mode set to <b>PAPER</b>",
                parse_mode=ParseMode.HTML,
            )

    # -----------------------------------------------------------------------
    # Inline keyboard callbacks
    # -----------------------------------------------------------------------

    async def _cmd_hedge(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /hedge [event_ticker] — on-demand settlement map, both-win zone,
        and per-leg green floors for an event's current structure.
        With one tracked event the argument is optional.
        """
        if not self._authorized(update):
            return
        advisor = getattr(self.engine, "hedge_advisor", None)
        if advisor is None:
            await update.message.reply_text("Hedge advisor not available.")
            return
        engines = self.engine.event_engines
        if not engines:
            await update.message.reply_text("No events tracked.")
            return
        if ctx.args:
            et = ctx.args[0].upper()
        elif len(engines) == 1:
            et = next(iter(engines))
        else:
            await update.message.reply_text(
                "Multiple events tracked — specify one:\n"
                + "\n".join(f"/hedge {e}" for e in engines)
            )
            return
        try:
            res = advisor.evaluate_for_api(et)
        except KeyError:
            await self._send(update.effective_chat.id, f"Event not tracked: {et}")
            return
        except Exception as e:
            await self._send(update.effective_chat.id, f"Hedge eval failed: {e}")
            return
        before = res["before"]
        eng = engines[et]
        legs = advisor.legs_from_engine(eng)
        cash_in, cash_out = advisor.ledger_from_engine(eng)
        import hedge_math as _hm
        lines = [f"{EMOJI['shield']} <b>Structure</b> — {et}"]
        spot = eng.consensus_price()
        if spot is not None:
            lines.append(f"Spot: {spot:g}")
        lines.append(f"Both-win: {self._fmt_zone(before['both_win_zones'])}")
        lines.append(
            f"Worst/best net: {_fmt_pl(before['worst_net'])} / {_fmt_pl(before['best_net'])}"
        )
        lines.append("")
        lines.append("<b>Green floors</b> (others hold):")
        for leg in legs:
            f = _hm.green_floor(legs, str(leg["ticker"]), cash_in, cash_out)
            strike = str(leg["ticker"]).split("-")[-1]
            lines.append(
                f"  {strike} {leg['side'].upper()} ×{leg['count']} "
                f"@ {_cents(leg['mid'])}: {self._fmt_floor(f)}"
            )
        lines.append("")
        lines.append("<b>Settlement map</b>:")
        for row in before["map"]:
            lines.append(f"  {row['settle']:g} → {_fmt_pl(row['net'])}")
        await self._send(update.effective_chat.id, "\n".join(lines))

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if not self._authorized(update):
            return

        data = query.data

        # Sell confirmation — handles both manual /sell and automated bot requests.
        # Pending tuples are (eng, position, qty) for manual, or
        # (eng, position, qty, alerted_ts) for auto — unpack tolerantly.
        if data.startswith("confirm_auto_") or data.startswith("confirm_manual_"):
            callback_id = data[len("confirm_"):]
            pending = self._pending_sells.pop(callback_id, None)
            if not pending:
                await query.edit_message_text(f"{EMOJI['cross']} Sell request expired or already handled.")
                return
            eng, p, qty = pending[0], pending[1], pending[2]
            await eng._execute_sell(p, qty)
            await query.edit_message_text(
                f"{EMOJI['check']} <b>SELL executed</b>: "
                f"{qty} {p.side.upper()} {p.ticker}",
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("cancel_auto_") or data.startswith("cancel_manual_"):
            callback_id = data[len("cancel_"):]
            self._pending_sells.pop(callback_id, None)
            await query.edit_message_text(f"{EMOJI['cross']} Sell cancelled.")
        # Stop loss confirmation (fired by bot)
        elif data.startswith("confirm_stop_"):
            ticker = data[len("confirm_stop_"):]
            for eng in self.engine.event_engines.values():
                p = next((x for x in eng.positions if x.ticker == ticker), None)
                if p:
                    await eng._execute_sell(p, p.count)
                    await query.edit_message_text(
                        f"{EMOJI['check']} <b>Stop loss executed</b>: "
                        f"{p.count} {p.side.upper()} {ticker}",
                        parse_mode=ParseMode.HTML,
                    )
                    return
            await query.edit_message_text(
                f"{EMOJI['cross']} Position no longer found: {ticker}"
            )

        elif data.startswith("cancel_stop_"):
            ticker = data[len("cancel_stop_"):]
            await query.edit_message_text(
                f"{EMOJI['cross']} Stop loss <b>SKIPPED</b> for {ticker}. "
                f"Monitor manually.",
                parse_mode=ParseMode.HTML,
            )

        # Hedge advisor cards: hedge_full_{opt}_{card_id} / hedge_harv_{card_id}
        # / hedge_skip_{card_id}
        elif data.startswith("hedge_full_"):
            rest = data[len("hedge_full_"):]
            opt_s, _, card_id = rest.partition("_")
            advisor = getattr(self.engine, "hedge_advisor", None)
            if advisor is None:
                await query.edit_message_text(f"{EMOJI['cross']} Hedge advisor not available.")
                return
            result = await advisor.execute_card(card_id, "full", int(opt_s or 0))
            if result.get("ok"):
                hedge = result.get("hedge") or {}
                buy = hedge.get("buy") or {}
                buy_note = "paper" if buy.get("paper") else ("placed" if buy.get("ok") else f"FAILED: {buy.get('error') or buy.get('status_code')}")
                await query.edit_message_text(
                    f"{EMOJI['check']} <b>Harvest + hedge executed</b>\n"
                    f"Sold: <code>{result.get('harvested')}</code>\n"
                    f"Hedge buy <code>{hedge.get('ticker','—')}</code>: {buy_note}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.edit_message_text(
                    f"{EMOJI['cross']} Hedge card failed: {result.get('error')}"
                )

        elif data.startswith("hedge_harv_"):
            card_id = data[len("hedge_harv_"):]
            advisor = getattr(self.engine, "hedge_advisor", None)
            if advisor is None:
                await query.edit_message_text(f"{EMOJI['cross']} Hedge advisor not available.")
                return
            result = await advisor.execute_card(card_id, "harvest_only")
            if result.get("ok"):
                await query.edit_message_text(
                    f"{EMOJI['check']} <b>Harvest executed</b> (no hedge): "
                    f"<code>{result.get('harvested')}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.edit_message_text(
                    f"{EMOJI['cross']} Harvest failed: {result.get('error')}"
                )

        elif data.startswith("hedge_skip_"):
            card_id = data[len("hedge_skip_"):]
            advisor = getattr(self.engine, "hedge_advisor", None)
            if advisor is not None:
                advisor.pending_cards.pop(card_id, None)
            await query.edit_message_text(
                f"{EMOJI['cross']} Hedge suggestion skipped. Positions unchanged."
            )

        elif data.startswith("hedge_ack_"):
            await query.edit_message_text(
                f"{EMOJI['check']} Hold advisory acknowledged."
            )

        # Picker callbacks — one-tap versions of the typed commands
        elif data.startswith("armpick_"):
            et = data[len("armpick_"):]
            try:
                self.engine.arm_event(et, True)
                await query.edit_message_text(
                    f"{EMOJI['fire']} <b>{et}</b> ARMED", parse_mode=ParseMode.HTML)
            except Exception as e:
                await query.edit_message_text(f"{EMOJI['cross']} Error: {e}")

        elif data.startswith("disarmpick_"):
            et = data[len("disarmpick_"):]
            try:
                self.engine.arm_event(et, False)
                await query.edit_message_text(
                    f"{EMOJI['shield']} <b>{et}</b> DISARMED", parse_mode=ParseMode.HTML)
            except Exception as e:
                await query.edit_message_text(f"{EMOJI['cross']} Error: {e}")

        elif data.startswith("sellpick_"):
            ticker = data[len("sellpick_"):]
            for eng in self.engine.event_engines.values():
                p = next((x for x in eng.positions if x.ticker == ticker and x.count > 0), None)
                if p:
                    text, keyboard = self._sell_confirm_payload(eng, p)
                    await query.edit_message_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                    return
            await query.edit_message_text(f"{EMOJI['cross']} Position no longer open: {ticker}")

        elif data.startswith("stoppick_"):
            rest = data[len("stoppick_"):]
            cents_s, _, ticker = rest.partition("_")
            try:
                price = int(cents_s) / 100.0
            except ValueError:
                await query.edit_message_text(f"{EMOJI['cross']} Bad stop callback: {data}")
                return
            et = self._apply_stop(ticker, price)
            if et:
                await query.edit_message_text(
                    f"{EMOJI['shield']} Stop loss set: <b>{ticker}</b> @ {_cents(price)}",
                    parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(
                    f"{EMOJI['cross']} Position no longer open: {ticker}")

        elif data.startswith("stopev_"):
            rest = data[len("stopev_"):]
            cents_s, _, et = rest.partition("_")
            try:
                price = int(cents_s) / 100.0
            except ValueError:
                await query.edit_message_text(f"{EMOJI['cross']} Bad stopevent callback: {data}")
                return
            n = self._apply_stop_event(et, price)
            if n is None:
                await query.edit_message_text(f"{EMOJI['cross']} Event no longer tracked: {et}")
            else:
                await query.edit_message_text(
                    f"{EMOJI['shield']} Stop @ {_cents(price)} set on <b>{n}</b> legs of {et}",
                    parse_mode=ParseMode.HTML)

        elif data.startswith("clearstoppick_"):
            ticker = data[len("clearstoppick_"):]
            removed = self.engine.position_bots.pop(ticker, None)
            if removed:
                await query.edit_message_text(
                    f"{EMOJI['check']} Bot cleared: <b>{ticker}</b>",
                    parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(f"{EMOJI['info']} No bot found for {ticker}")

        # Live mode confirmation
        elif data == "confirm_setlive":
            self.engine.params.mode = "live"
            await query.edit_message_text(
                f"{EMOJI['danger']} Mode set to <b>LIVE</b>. "
                f"Real orders will execute when events are armed.",
                parse_mode=ParseMode.HTML,
            )

        elif data == "cancel_setlive":
            await query.edit_message_text(
                f"{EMOJI['shield']} Mode change cancelled. Still in "
                f"<b>{self.engine.params.mode.upper()}</b>.",
                parse_mode=ParseMode.HTML,
            )

    # -----------------------------------------------------------------------
    # Alert loop — runs every 30 seconds alongside the engine tick
    # -----------------------------------------------------------------------

    async def _alert_loop(self) -> None:
        """
        Background loop that checks for alertable conditions every 30 seconds.
        Intentionally runs slower than the engine tick (3s) to avoid spam.
        """
        await asyncio.sleep(10)  # Let engine initialize first
        while True:
            try:
                await self._check_wapner_alerts()
                await self._check_settlement_warnings()
                await self._check_drawdown_alert()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Alert loop error: {e}")
            await asyncio.sleep(30)

    async def _check_wapner_alerts(self) -> None:
        """
        Five-gate transition alerts:
          REJECT/absent -> PASS : entry window opened (full card, once)
          PASS -> REJECT        : gate broke — if you entered, that's the bell
        Candidates that vanish entirely (market settled/closed) clear silently.
        """
        passes, rejects, _errors = await asyncio.to_thread(self._scan_wapner, True)

        evaluated: Dict[Tuple[str, str], dict] = {}
        for c in passes + rejects:
            if c.get("ticker") and c.get("side"):
                evaluated[(c["ticker"], c["side"])] = c

        # New passes
        for c in passes:
            key = (c["ticker"], c["side"])
            if key not in self._wapner_live:
                self._wapner_live[key] = datetime.now(timezone.utc).timestamp()
                await self._broadcast(
                    f"🔔 <b>Wapner Window open</b>\n\n{self._fmt_pass(c)}"
                )

        # Broken gates on previously-passing candidates
        for key in list(self._wapner_live):
            c = evaluated.get(key)
            if c is None:
                self._wapner_live.pop(key, None)   # settled/closed — silent
            elif c.get("grade") != "PASS":
                self._wapner_live.pop(key, None)
                failed = [k for k, v in (c.get("checks") or {}).items() if not v]
                await self._broadcast(
                    f"{EMOJI['warn']} <b>Gate broke</b> on "
                    f"<code>{key[0]}</code> {key[1].upper()}: "
                    f"{', '.join(failed) or 'unknown'}.\n"
                    f"If you entered this one, the setup no longer exists — "
                    f"exit, don't hope. /status to check, /sell to act."
                )

    async def _check_settlement_warnings(self) -> None:
        """Warn 15 minutes before settlement if positions are open."""
        for et, eng in self.engine.event_engines.items():
            if not eng.positions:
                continue
            mins_left = self._minutes_left(eng)
            if mins_left is None:
                continue
            warn_key = f"settle_warn_{et}"
            if 13 <= mins_left <= 17 and warn_key not in self._alerted_misc:
                self._alerted_misc.add(warn_key)
                r = eng.risk_snapshot()
                await self._broadcast(
                    f"{EMOJI['clock']} <b>Settlement in ~15 min</b>\n"
                    f"Event: {et}\n"
                    f"Mark: {_fmt_pl(r.mark_value)} | "
                    f"Max profit: {_fmt_pl(r.max_profit)}\n"
                    f"Positions: YES×{r.yes_count} NO×{r.no_count}",
                )

    async def _check_drawdown_alert(self) -> None:
        """Alert when approaching global drawdown limit."""
        snapshots = [e.risk_snapshot() for e in self.engine.event_engines.values()]
        total_cost = sum(s.cost_basis for s in snapshots)
        total_pl   = sum(s.unrealized_pl for s in snapshots)
        if total_cost <= 0:
            return
        pct = (total_pl / total_cost) * 100.0
        limit = self.engine.params.safety.get("global_drawdown_limit", -50.0)
        warn_threshold = limit * 0.75  # warn at 75% of limit
        warn_key = "drawdown_warn"
        if pct <= warn_threshold and warn_key not in self._alerted_misc:
            self._alerted_misc.add(warn_key)
            await self._broadcast(
                f"{EMOJI['danger']} <b>Drawdown warning</b>\n"
                f"Portfolio P/L: <b>{pct:.1f}%</b> "
                f"(limit: {limit:.1f}%)\n"
                f"Consider reducing exposure.",
            )
        # Reset warning if recovered above threshold
        if pct > warn_threshold and warn_key in self._alerted_misc:
            self._alerted_misc.discard(warn_key)

    # -----------------------------------------------------------------------
    # Morning brief loop
    # -----------------------------------------------------------------------

    async def _morning_brief_loop(self) -> None:
        """Send a morning session brief at configured time each day."""
        while True:
            try:
                now = datetime.now()
                h, m = self.morning_time.split(":")
                target = now.replace(
                    hour=int(h), minute=int(m), second=0, microsecond=0
                )
                if target <= now:
                    target = target + timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                await asyncio.sleep(wait_seconds)
                await self._send_morning_brief()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Morning brief error: {e}")
                await asyncio.sleep(60)

    async def _send_morning_brief(self) -> None:
        status = self.engine.status()
        today = datetime.now().strftime("%A, %b %d")
        lines = [
            f"{EMOJI['news']} <b>KalshiBaby Morning Brief</b> — {today}\n",
            f"Mode: <b>{status.mode.upper()}</b>",
            f"Active events: <b>{len(status.events)}</b>",
            "",
            f"<i>Pre-trade checklist:</i>",
            f"  1. Check gold/oil headlines (news.google.com)",
            f"  2. Check ladder thickness before building structure",
            f"  3. Define risk/reward envelope before entering",
            f"  4. Wait for 7:30am data spike to settle",
            "",
            f"<i>Wapner reminder:</i> five gates or no trade. "
            f"The window opens in the last hour before settlement — "
            f"alerts will fire automatically.",
            "",
            f"Good luck today. /status for current positions.",
        ]
        await self._broadcast("\n".join(lines))

    # -----------------------------------------------------------------------
    # Stop loss confirmation alert (called by engine when stop fires in live mode)
    # -----------------------------------------------------------------------

    async def send_stop_triggered_notice(self, ticker: str, side: str, qty: int,
                                         mid: float, stop: float, why: str) -> None:
        """
        Stop condition met but NOT auto-selling (event disarmed or paper
        mode). Pure notification with an optional one-tap manual sell —
        the bot config stays in place either way. Sent throttled (5 min)
        by the engine while the condition persists.
        """
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💰 Sell now ({qty} {side.upper()})",
                callback_data=f"confirm_stop_{ticker}",
            ),
            InlineKeyboardButton(
                "👌 Keep holding",
                callback_data=f"cancel_stop_{ticker}",
            ),
        ]])
        await self._broadcast(
            f"{EMOJI['warn']} <b>Stop TRIGGERED — not sold</b> ({why})\n"
            f"Ticker: <code>{ticker}</code>\n"
            f"Side: {side.upper()} ×{qty}\n"
            f"Mid: {_cents(mid)} | Stop: {_cents(stop)}\n\n"
            f"Stop stays armed; you'll be re-alerted every 5 min while "
            f"the condition holds. Tip: /hedge shows this leg's green floor.",
            reply_markup=keyboard,
        )

    async def send_stop_loss_alert(self, ticker: str, side: str, qty: int,
                                   mid: float, stop: float) -> None:
        """
        Called by the engine BEFORE executing a stop loss in live mode.
        Sends a Telegram message with Confirm/Cancel buttons.
        User has 60 seconds to respond; if no response, sell executes anyway.
        """
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ Execute stop sell ({qty} @ {_cents(mid)})",
                callback_data=f"confirm_stop_{ticker}",
            ),
            InlineKeyboardButton(
                "❌ Skip this time",
                callback_data=f"cancel_stop_{ticker}",
            ),
        ]])
        await self._broadcast(
            f"{EMOJI['danger']} <b>Stop loss triggered</b>\n"
            f"Ticker: {ticker}\n"
            f"Side: {side.upper()} ×{qty}\n"
            f"Mid: {_cents(mid)} | Stop: {_cents(stop)}\n\n"
            f"Respond within 60s or sell executes automatically.",
            reply_markup=keyboard,
        )

    # Suppress re-alerts on the same pending sell for this long (seconds).
    # Long enough that a still-triggered condition on every 3s tick doesn't
    # flood your phone; short enough that an ignored alert re-surfaces if the
    # condition genuinely persists past a few minutes.
    PENDING_REALERT_SECONDS = 300  # 5 minutes

    async def request_sell_confirmation(self, eng, position, qty, reason):
        """
        Called by the engine when an automated (non-stop) sell condition is met.
        Registers the sell as pending and sends a Telegram confirmation prompt.
        The engine MUST NOT execute the sell after calling this — the sell only
        happens when the user taps APPROVE (see _on_callback).

        Dedup: if a confirmation for the same ticker is already pending and was
        alerted recently, silently skip. Prevents flooding when a condition
        stays true across multiple engine ticks.
        """
        import time as _time
        ticker = position.ticker
        callback_id = f"auto_{ticker}"

        existing = self._pending_sells.get(callback_id)
        if existing:
            # existing tuples are (eng, position, qty[, alerted_ts]); newer
            # entries carry a timestamp for re-alert throttling.
            alerted_ts = existing[3] if len(existing) >= 4 else 0
            if _time.time() - alerted_ts < self.PENDING_REALERT_SECONDS:
                # Already pending, alerted recently — refresh qty/pos silently.
                self._pending_sells[callback_id] = (eng, position, qty, alerted_ts)
                return

        now = _time.time()
        self._pending_sells[callback_id] = (eng, position, qty, now)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ APPROVE SELL ({qty} {position.side.upper()})",
                callback_data=f"confirm_{callback_id}",
            ),
            InlineKeyboardButton(
                "❌ REJECT",
                callback_data=f"cancel_{callback_id}",
            ),
        ]])

        await self._broadcast(
            f"{EMOJI['warn']} <b>Automated Sell Request</b>\n"
            f"Ticker: <code>{ticker}</code>\n"
            f"Reason: {reason}\n"
            f"Qty: {qty} {position.side.upper()}\n"
            f"Mid: {_cents(position.current_mid)}\n\n"
            f"Waiting for your confirmation — no answer = no sale.",
            reply_markup=keyboard,
        )

    # -----------------------------------------------------------------------
    # Hedge advisor cards
    # -----------------------------------------------------------------------

    @staticmethod
    def _fmt_zone(zones) -> str:
        if not zones:
            return "none (one wing must lose)"
        lo, hi = zones[0]
        lo_s = "-∞" if lo == float("-inf") else f"{lo:g}"
        hi_s = "+∞" if hi == float("inf") else f"{hi:g}"
        return f"{lo_s} → {hi_s}"

    @staticmethod
    def _fmt_floor(f) -> str:
        if f is None:
            return "FREE RIDE (green even at $0)"
        if f == float("inf"):
            return "unreachable (red regardless)"
        return _cents(f)

    async def send_hedge_card(self, card: dict) -> None:
        """
        Render a hedge-advisor card. 'reposition' cards get action buttons
        (approve = execute; no answer = nothing happens). 'hold' cards are
        pure information — the free-ride advisory.
        """
        if card["kind"] == "hold":
            leg = card["leg"]
            await self._broadcast(
                f"{EMOJI['shield']} <b>HOLD advisory</b> — {card['event_ticker']}\n"
                f"<code>{leg['ticker']}</code> {leg['side'].upper()} ×{leg['qty']} "
                f"looks scary at {_cents(leg['mid'])} (entry {_cents(leg['avg_price'])})\n"
                f"Green floor: <b>{self._fmt_floor(card['green_floor'])}</b>\n"
                f"Structure worst/best: {_fmt_pl(card['worst_net'])} / {_fmt_pl(card['best_net'])}\n\n"
                f"The structure already covers this leg. Selling here forfeits "
                f"${leg['qty']:.0f} of recovery upside for {_cents(leg['mid'])} on the dollar.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👍 Got it", callback_data=f"hedge_ack_{card['card_id']}"),
                ]]),
            )
            return

        # reposition card
        h = card["harvest"]
        cid = card["card_id"]
        bleed_str = ", ".join(
            f"{b['ticker'].split('-')[-1]}@{_cents(b['mid'])}" for b in card["bleeding"]
        )
        lines = [
            f"{EMOJI['warn']} <b>Harvest &amp; Reposition</b> — {card['event_ticker']}",
            f"Spot: {card['spot']:g}",
            f"Fat wing: <code>{h['ticker']}</code> {h['side'].upper()} ×{h['qty']} "
            f"at {_cents(h['price'])} (entry {_cents(h['avg_price'])})",
            f"Bleeding: {bleed_str}",
            "",
        ]
        buttons = []
        for i, opt in enumerate(card["options"]):
            hg = opt["hedge"]
            floors = opt["green_floors"]
            worst_floor = min(
                (f for f in floors.values() if f is not None and f != float("inf")),
                default=None,
            )
            floor_note = (
                "all legs FREE RIDE" if worst_floor is None
                else f"tightest floor {_cents(worst_floor)}"
            )
            lines.append(
                f"<b>Option {i + 1}</b>: buy ×{hg['count']} {hg['side'].upper()} "
                f"T{hg['strike']:g} @ {_cents(hg['avg_price'])}\n"
                f"  Locks {_fmt_pl(opt['harvested_pl'])} | both-win "
                f"{self._fmt_zone(opt['both_win_zones'])} → {_fmt_pl(opt['best_net'])}\n"
                f"  Worst {_fmt_pl(opt['worst_net'])} | {floor_note}"
            )
            buttons.append(InlineKeyboardButton(
                f"✅ Opt {i + 1}: harvest + T{hg['strike']:g}",
                callback_data=f"hedge_full_{i}_{cid}",
            ))
        ho = card["harvest_only"]
        lines.append(
            f"<b>Harvest only</b>: locks {_fmt_pl(ho['harvested_pl'])} | "
            f"worst {_fmt_pl(ho['worst_net'])} / best {_fmt_pl(ho['best_net'])}"
        )
        lines.append("")
        lines.append("No answer = no trade.")

        keyboard_rows = [[b] for b in buttons]
        keyboard_rows.append([
            InlineKeyboardButton("🌾 Harvest only", callback_data=f"hedge_harv_{cid}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"hedge_skip_{cid}"),
        ])
        await self._broadcast(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )

# ---------------------------------------------------------------------------
# Backend integration (unchanged interface — no lifespan edits needed if you
# already wired v1):
#
#   from kalshibaby_telegram import KalshiBabyBot
#
#   @asynccontextmanager
#   async def lifespan(app: FastAPI):
#       asyncio.create_task(engine.loop())
#       tg_cfg = engine.config.get("telegram")
#       bot = KalshiBabyBot(engine, tg_cfg) if tg_cfg else None
#       if bot:
#           await bot.start()
#       yield
#       if bot:
#           await bot.stop()
# ---------------------------------------------------------------------------
