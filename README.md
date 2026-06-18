# KalshiBaby v3

Portfolio-aware multi-event trading engine for Kalshi commodity contracts (oil, Brent, gold, natural gas). Monitors live prices from multiple sources, detects price shocks, and automates profit harvesting via a per-event state machine.

## Files

- `kalshibaby_backend.py` — FastAPI backend, Kalshi API client, risk engine, per-event state machines.
- `ui/index.html` — dashboard shell.
- `ui/app.js` — frontend polling, per-event chart logic, and action controls.
- `ui/styles.css` — UI styling.
- `config.yaml` — production config (real Kalshi account).
- `config.demo.yaml` — demo account config.
- `requirements.txt` — Python dependencies.

## Starting the server

```bash
# Production (real account)
./startserver

# Live-mode test (production account, paper mode by default)
./startlivetest

# Demo account
./startdemotest
```

Open `http://<host>:8765` in a browser.

Each script sets `KALSHI_CONFIG` to the appropriate config file and starts uvicorn with `--reload`.

## Global Parameters

These are editable in the UI at runtime and take effect immediately without a restart.

### Poll seconds
How often (in seconds) the engine fetches fresh prices and evaluates exit logic for all events. Lower values are more responsive but increase API call volume.
Default: `3`

### Shock %
The percentage move in the spot price (relative to the reference price at arm time) that triggers a SHOCK_WATCH state. If the price moves this far in a single shock window, the engine flags the event as potentially unstable.
Default: `2.0`

### Recovery min %
After a shock is detected, the minimum percentage of the shock move that must be recovered before the engine considers the shock resolved. A value of `0.75` means 75% of the gap must close before the state returns to NORMAL.
Default: `0.75`

### Arm profit @
The YES-side mid price threshold at which the profit-harvest logic becomes active for an event. Below this level, no automatic exits are triggered even if the event is armed.
Default: `0.88` (88¢)

### First trim @
The YES-side mid price at which the engine sells the first portion of the position (one-third by default). This locks in partial profit while keeping exposure.
Default: `0.90` (90¢)

### Trail after .90
Once the mid price has crossed 90¢, this is the trailing stop distance. If the price drops by this amount from its peak, the engine exits the remaining position.
Default: `0.03` (3¢ trail)

### Drawdown limit %
The global portfolio loss floor expressed as a percentage of total cost basis. If unrealized P/L across all events drops below this percentage of what was spent to open the positions, the engine flattens everything immediately regardless of arm state or mode.
Default: `-50` (flatten everything if down 50% or more of total invested)

## Price Sources

Each event is matched to the correct price sources based on its ticker prefix (WTI, Brent, Gold, etc.).

| Source | What it provides |
|---|---|
| **Spot** | oilprice.com (oil) or Stooq XAUUSD (gold) |
| **Yahoo** | Yahoo Finance futures (CL=F, BZ=F, GC=F, NG=F) |
| **Kalshi implied** | Mid price back-calculated from the live order book |

Source checkboxes in the UI toggle each feed globally across all event charts. Disabling a source removes it from the consensus price calculation.

## Paper vs Live mode

- **PAPER** — the engine evaluates all logic and logs what it would do, but no real orders are sent.
- **LIVE** — orders are executed when an event is also individually armed.

Both conditions must be true for real execution: mode must be `live` AND the per-event ARMED toggle must be on.

## Safety defaults

Config files start with:

```yaml
mode: "paper"
armed: false
```

Do not switch to LIVE mode until you have confirmed the Kalshi API credentials are correct and position sync is showing your actual positions.

## Supported instruments

| Prefix | Yahoo symbol | Spot source |
|---|---|---|
| KXWTI, KXWTIMAX | CL=F | oilprice.com WTI-Crude |
| KXBRENTW, KXBRENTD | BZ=F | oilprice.com Brent-Crude |
| KXGOLD, KXGOLDW, KXGOLDMAX, KXGOLDR, KXGOLDD | GC=F | Stooq XAUUSD |
| KXNG | NG=F | oilprice.com Natural-Gas |

Unknown prefixes fall back to keyword detection (WTI, BRENT, GOLD, etc.) and log a warning.
