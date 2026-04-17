# KalshiBaby 🍼

A lightweight Python “babysitter” bot for managing open positions on Kalshi.

This tool is intentionally simple:

- You **enter trades manually** based on your own analysis  
- The bot **monitors your portfolio**
- It **automatically exits positions** based on predefined rules  
- It **notifies you** when actions are taken  

This is designed for **risk control and consistency**, not full automation.

---

## Core Philosophy

KalshiBaby is built around a key distinction:

- **Triggers decide *when* to act**
- **Execution decides *how* to exit**

Unlike the UI, the API requires explicit pricing. This bot simulates “market sell” behavior by using **aggressive limit orders based on the live orderbook**.

---

## Features (v1)

### 1. Downside Protection (`--sell_low`)
If any position falls to or below a threshold:
- The bot **immediately exits the position**
- Uses an **aggressive limit order** at the current best bid (or slightly below)
- Designed to **avoid getting stuck in fast drops**

---

### 2. Upside Exit (`--sell_high`)
If any position rises above a threshold:
- The bot exits the position
- Can either:
  - mimic UI-style immediate sell, or
  - complement existing manual limit orders (e.g., 95¢)

---

### 3. Portfolio Take-Profit (`--take_profit`)
If total portfolio value increases by a target percentage:
- The bot **liquidates all open positions**
- Locks in daily gains (e.g., 3–5%)

---

### 4. Notifications
- Sends **email or SMS (via email gateway)** when:
  - a sell is triggered
  - an order is placed
  - (optionally) when an order fills

---

### 5. Config File Support (`--config`)
Load API credentials and settings from a YAML file:

```bash
python kalshibaby.py --config demoapi.yaml
