#!/usr/bin/env python3
"""
Kalshi full-history exporter — settlements + fills.

Reuses the same config.yaml / auth scheme as kalshibaby_backend.py.

Run (from the kalshibaby directory, next to config.yaml):
    python kalshi_history_export.py
    python kalshi_history_export.py --config /path/to/config.yaml

Outputs:
    kalshi_settlements.csv  — every settled position (result, cost, revenue)
    kalshi_fills.csv        — every fill, including buy/sell action
"""

from __future__ import annotations

import argparse
import base64
import csv
import sys
import time
from pathlib import Path

import requests
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_client_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    api_cfg = cfg.get("api") or cfg.get("kalshi") or {}
    base_url = api_cfg.get("base_url", "https://api.elections.kalshi.com")
    if "/trade-api" in base_url:
        base_url = base_url[: base_url.index("/trade-api")]
    key_id = api_cfg.get("key_id") or api_cfg.get("api_key_id")
    key_path = api_cfg.get("private_key_path")
    if not key_id or not key_path:
        sys.exit("No API credentials found in config (need key_id + private_key_path).")
    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    return {"base_url": base_url.rstrip("/"), "key_id": key_id, "private_key": private_key}


def auth_headers(client: dict, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode("utf-8")
    sig = client["private_key"].sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": client["key_id"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "Content-Type": "application/json",
    }


def paginate(client: dict, path: str, list_key: str, params: dict | None = None) -> list[dict]:
    """Follow cursor pagination until exhausted. Signature covers path only (no query)."""
    out: list[dict] = []
    cursor = None
    page = 0
    while True:
        p = dict(params or {})
        p["limit"] = 200
        if cursor:
            p["cursor"] = cursor
        r = requests.get(
            client["base_url"] + path,
            headers=auth_headers(client, "GET", path),
            params=p,
            timeout=15,
        )
        if r.status_code == 404:
            print(f"  {path} -> 404 (endpoint not available)")
            return out
        if not r.ok:
            print(f"  {path} -> HTTP {r.status_code}: {r.text[:300]}")
            return out
        body = r.json()
        rows = body.get(list_key, []) or []
        out.extend(rows)
        page += 1
        print(f"  page {page}: +{len(rows)} rows (total {len(out)})")
        cursor = body.get("cursor")
        if not cursor or not rows:
            return out
        time.sleep(0.25)  # be polite to rate limits


def write_csv(rows: list[dict], out_path: Path) -> None:
    if not rows:
        print(f"  nothing to write for {out_path.name}")
        return
    # Union of all keys so no field is silently dropped, whatever the API returns.
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  wrote {len(rows)} rows -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    client = load_client_config(args.config)
    outdir = Path(args.outdir)

    print("Fetching settlements...")
    settlements = paginate(client, "/trade-api/v2/portfolio/settlements", "settlements")

    print("Fetching fills...")
    fills = paginate(client, "/trade-api/v2/portfolio/fills", "fills")

    # Historical-tier fallback: records older than the live/historical cutoff
    # may live under /trade-api/v2/historical/*. Try it if it exists; harmless if 404.
    print("Checking historical tier for older fills...")
    hist_fills = paginate(client, "/trade-api/v2/historical/fills", "fills")
    if hist_fills:
        seen = {(f.get("trade_id"), f.get("order_id"), f.get("created_time")) for f in fills}
        added = [f for f in hist_fills if (f.get("trade_id"), f.get("order_id"), f.get("created_time")) not in seen]
        fills.extend(added)
        print(f"  merged {len(added)} historical fills")

    write_csv(settlements, outdir / "kalshi_settlements.csv")
    write_csv(fills, outdir / "kalshi_fills.csv")

    if settlements:
        times = [s.get("settled_time") or s.get("settled_ts") or "" for s in settlements]
        times = [t for t in times if t]
        print(f"\nSettlements: {len(settlements)} rows, {min(times)} -> {max(times)}" if times
              else f"\nSettlements: {len(settlements)} rows")
    if fills:
        print(f"Fills: {len(fills)} rows")
    print("\nDone. Upload kalshi_settlements.csv (and kalshi_fills.csv) back to the chat.")


if __name__ == "__main__":
    main()
