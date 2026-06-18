#!/usr/bin/env python3
"""
KalshiBaby News Agent v1
========================
Monitors news feeds for geopolitical events affecting commodity prices.
Classifies headlines using a local AI model (via Ollama).
Sends actionable signals to KalshiBaby backend.

Setup:
    pip install feedparser requests beautifulsoup4 pyyaml
    ollama pull hermes3        # or: mistral, llama3, phi3

Run:
    python newsagent.py
    python newsagent.py --config newsagent.yaml
    python newsagent.py --dry-run   # classify but don't send signals

Signal types:
    DEAL_SIGNAL        — ceasefire, deal announced, strikes canceled → sell YES
    ESCALATION_SIGNAL  — strikes, attacks, war escalation → sell NO
    BULLISH            — supply disruption, Hormuz threat → informational
    BEARISH            — deal progress, supply increase → informational
    NEUTRAL            — unrelated news → ignore
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import feedparser
import requests
import yaml

# Fuzzy classifier — import if available
try:
    from geopolitical_fuzzy import FuzzyClassifier
    _fuzzy_available = True
except ImportError:
    _fuzzy_available = False

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "kalshibaby_url": "http://127.0.0.1:8765",
    "ollama_url": "http://localhost:11434/api/generate",
    "model": "hermes3",
    "poll_seconds": 15,
    "min_confidence": 0.80,
    "max_seen": 500,           # dedup cache size
    "dry_run": False,
    "feeds": {
        "gnews_hormuz_oil": {
            # Google News: core commodity — Hormuz, Iran, oil supply
            "url": "https://news.google.com/rss/search?q=iran+hormuz+oil&hl=en-US&gl=US&ceid=US:en",
            "enabled": True,
            "weight": 1.0,
        },
        "gnews_iran_israel": {
            # Google News: geopolitical regime — deal/war/Israel/Lebanon
            "url": "https://news.google.com/rss/search?q=iran+israel+deal+OR+war+OR+lebanon&hl=en-US&gl=US&ceid=US:en",
            "enabled": True,
            "weight": 1.0,
        },
        "gnews_oil_price": {
            # Google News: direct price news — Brent, WTI, crude
            "url": "https://news.google.com/rss/search?q=brent+crude+OR+wti+oil+price&hl=en-US&gl=US&ceid=US:en",
            "enabled": True,
            "weight": 1.0,
        },
        "aljazeera": {
            "url": "https://www.aljazeera.com/xml/rss/all.xml",
            "enabled": True,
            "weight": 1.0,
        },
        "truthsocial": {
            "url": "https://truthsocial.com/@realDonaldTrump.rss",
            "enabled": True,
            "weight": 2.0,     # Trump posts weighted higher — direct market mover
            "fallback_url": "https://trumpstruth.org/@realDonaldTrump.rss",
        },
        "osint_defender": {
            "url": "https://nitter.privacydev.net/OSINTdefender/rss",
            "enabled": True,
            "weight": 1.5,
        },
        "ukmto": {
            # UK Maritime Trade Operations — Hormuz shipping alerts
            "url": "https://nitter.privacydev.net/UK_MTO/rss",
            "enabled": True,
            "weight": 1.5,
        },
    },
    # Keywords that force a re-check even if item was seen before
    "priority_keywords": [
        "trump", "iran", "hormuz", "strike", "bomb", "ceasefire",
        "deal", "nuclear", "kharg", "hegseth", "centcom", "irgc",
        "oil", "brent", "wti", "tanker", "blockade",
        "israel", "lebanon", "reject", "rejected", "collapse",
        "withdraw", "skeptic", "jeopard", "sink the", "walk away",
    ],
}

# ---------------------------------------------------------------------------
# System prompt for local AI classifier
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a commodity price signal detector specializing in oil markets.

Your job: classify a news headline and summary as one of these signals:

ESCALATION_SIGNAL — military strikes launched, attacks, war escalating, new hostilities
DEAL_SIGNAL       — ceasefire announced, strikes canceled, deal reached, negotiations succeeding
BULLISH           — supply threat, Hormuz disruption, sanctions, infrastructure attack
BEARISH           — deal progress, supply restoration, de-escalation, Hormuz reopening
NEUTRAL           — unrelated to oil/energy/Middle East conflict

Rules:
- If Trump cancels, pauses, or walks back strikes: DEAL_SIGNAL
- If Trump announces new strikes or threatens escalation: ESCALATION_SIGNAL
- If Iran retaliates or launches attacks: ESCALATION_SIGNAL
- If ceasefire or deal terms agreed: DEAL_SIGNAL
- If Hormuz traffic resumes: BEARISH
- If Kharg Island or oil infrastructure threatened: BULLISH

Respond ONLY in valid JSON with no markdown, no explanation:
{"signal": "SIGNAL_TYPE", "confidence": 0.85, "reason": "one sentence max 15 words"}"""


# ---------------------------------------------------------------------------
# Feed poller
# ---------------------------------------------------------------------------

class FeedPoller:
    """Fetches and parses a single RSS/Atom feed."""

    def __init__(self, name: str, config: Dict[str, Any]) -> None:
        self.name = name
        self.url = config["url"]
        self.fallback_url = config.get("fallback_url")
        self.weight = float(config.get("weight", 1.0))
        self.enabled = bool(config.get("enabled", True))

    def fetch(self) -> List[Dict[str, str]]:
        """Returns list of {id, title, summary, link, published} dicts."""
        items = self._parse(self.url)
        if not items and self.fallback_url:
            print(f"  [{self.name}] primary failed, trying fallback...")
            items = self._parse(self.fallback_url)
        return items

    def _parse(self, url: str) -> List[Dict[str, str]]:
        try:
            feed = feedparser.parse(url)
            results = []
            for entry in feed.entries[:10]:  # only latest 10
                results.append({
                    "id": entry.get("id") or entry.get("link", ""),
                    "title": entry.get("title", "").strip(),
                    "summary": self._clean(entry.get("summary", "")),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
            return results
        except Exception as e:
            print(f"  [{self.name}] fetch error: {e}")
            return []

    @staticmethod
    def _clean(text: str) -> str:
        """Strip HTML tags and truncate."""
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[:300]


# ---------------------------------------------------------------------------
# Local AI classifier
# ---------------------------------------------------------------------------

class LocalAIClassifier:
    """Classifies headlines using a local Ollama model."""

    def __init__(self, ollama_url: str, model: str) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self._available: Optional[bool] = None

    def check_available(self) -> bool:
        try:
            r = requests.get(
                self.ollama_url.replace("/api/generate", "/api/tags"),
                timeout=5,
            )
            self._available = r.ok
            return self._available
        except Exception:
            self._available = False
            return False

    def classify(self, title: str, summary: str) -> Dict[str, Any]:
        """
        Returns {"signal": str, "confidence": float, "reason": str}
        Priority: Fuzzy classifier → Ollama → keyword fallback

        Fuzzy runs FIRST always — it's fast, local, and explainable.
        Ollama only runs if fuzzy returns NEUTRAL (ambiguous headline).
        Keywords only run if both fuzzy and Ollama fail.
        """
        # Step 1: Always try fuzzy classifier first
        if _fuzzy_available:
            try:
                fuzzy = FuzzyClassifier()
                result = fuzzy.classify(title, summary)
                # Trust fuzzy if it found a non-neutral signal
                if result.signal != "NEUTRAL":
                    return {
                        "signal": result.signal,
                        "confidence": result.confidence,
                        "reason": result.reason,
                        "method": "fuzzy",
                    }
                # Fuzzy returned NEUTRAL — try Ollama for ambiguous headlines
            except Exception as e:
                pass  # fall through

        # Step 2: Try Ollama only for headlines fuzzy couldn't classify
        if self._available is False:
            return self._keyword_fallback(title, summary)

        prompt = f"Headline: {title}\nSummary: {summary[:200]}"
        try:
            r = requests.post(
                self.ollama_url,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "system": SYSTEM_PROMPT,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": 80},
                },
                timeout=60,   # longer for first load into RAM
            )
            r.raise_for_status()
            raw = r.json().get("response", "{}")
            result = json.loads(raw)
            return {
                "signal": result.get("signal", "NEUTRAL"),
                "confidence": float(result.get("confidence", 0.5)),
                "reason": result.get("reason", ""),
            }
        except Exception as e:
            print(f"  [AI] classify error: {e} — falling back to keywords")
            self._available = False
            # If fuzzy already ran and returned NEUTRAL, trust it over keywords
            # Keywords have too many false positives on domestic political content
            if _fuzzy_available:
                try:
                    fuzzy = FuzzyClassifier()
                    result = fuzzy.classify(title, summary)
                    return {
                        "signal": result.signal,
                        "confidence": result.confidence,
                        "reason": result.reason + " [fuzzy fallback]",
                        "method": "fuzzy",
                    }
                except Exception:
                    pass
            return self._keyword_fallback(title, summary)

    @staticmethod
    def _keyword_fallback(title: str, summary: str) -> Dict[str, Any]:
        """
        Simple keyword-based fallback when Ollama is unavailable.
        Less accurate but provides basic coverage.
        """
        text = (title + " " + summary).lower()

        # Deal/de-escalation keywords — broader matching
        deal_keywords = [
            "ceasefire", "deal reached", "deal agreed", "deal signed",
            "deal to be signed", "finalise deal", "finalize deal",
            "canceled strike", "cancelled strike", "cancels strike",
            "pauses strike", "suspended strike", "approved discussions",
            "peace deal", "talks progressing", "negotiations succeed",
            "agreement reached", "truce", "sign deal", "signing deal",
            "mediators", "qatar deal", "pakistan deal", "nuclear deal",
            "hormuz reopening", "strait reopening", "sanctions relief",
            "frozen assets", "memorandum", "mou signed",
        ]
        escalation_keywords = [
            "launches strike", "launched strike", "bombs iran", "bombed iran",
            "strikes iran", "struck iran", "attack iran", "attacked iran",
            "iran retaliates", "iran fires", "missiles launched",
            "very hard tonight", "hitting iran", "resume bombing",
            "new strikes", "military action", "centcom strikes",
            "irgc launches", "escalation", "war resumes",
        ]
        bullish_keywords = [
            "hormuz closed", "kharg island", "infrastructure attack",
            "oil facility", "supply disruption", "blockade tightened",
            "tanker attacked", "ship attacked", "pipeline",
        ]
        bearish_keywords = [
            "hormuz reopens", "hormuz open", "oil flows", "deal imminent",
            "sanctions lifted", "ceasefire holds", "deal sunday",
            "strait opens", "shipping resumes", "war ends", "peace",
        ]

        for kw in deal_keywords:
            if kw in text:
                return {"signal": "DEAL_SIGNAL", "confidence": 0.75,
                        "reason": f"Keyword match: '{kw}'"}

        for kw in escalation_keywords:
            if kw in text:
                return {"signal": "ESCALATION_SIGNAL", "confidence": 0.75,
                        "reason": f"Keyword match: '{kw}'"}

        for kw in bullish_keywords:
            if kw in text:
                return {"signal": "BULLISH", "confidence": 0.70,
                        "reason": f"Keyword match: '{kw}'"}

        for kw in bearish_keywords:
            if kw in text:
                return {"signal": "BEARISH", "confidence": 0.70,
                        "reason": f"Keyword match: '{kw}'"}

        # Iran + deal context catch-all
        if "iran" in text and any(w in text for w in ["deal", "agreement", "talks", "negotiate", "mediator"]):
            return {"signal": "DEAL_SIGNAL", "confidence": 0.82,
                    "reason": "Iran deal context detected"}

        # Tighter catch-all — require military-specific context not just any "fire"
        if "iran" in text and any(w in text for w in [
            "airstrike", "missile strike", "bomb iran", "attack iran",
            "strike iran", "hit iran", "missiles fired", "launched attack",
            "military strike", "centcom", "irgc attack"
        ]):
            return {"signal": "ESCALATION_SIGNAL", "confidence": 0.75,
                    "reason": "Iran military conflict detected"}
        
        # Trump + action catch-all
        if "trump" in text and any(w in text for w in ["iran", "strike", "deal", "cancel", "bomb"]):
            if any(w in text for w in ["deal", "cancel", "pause", "suspend", "ceasefire"]):
                return {"signal": "DEAL_SIGNAL", "confidence": 0.85,
                        "reason": "Trump de-escalation context"}
            return {"signal": "ESCALATION_SIGNAL", "confidence": 0.82,
                    "reason": "Trump Iran action context"}

        return {"signal": "NEUTRAL", "confidence": 0.90, "reason": "No relevant keywords"}


# ---------------------------------------------------------------------------
# KalshiBaby client
# ---------------------------------------------------------------------------

class KalshiBabyClient:
    """Sends news signals to the KalshiBaby backend."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def send_signal(
        self,
        source: str,
        headline: str,
        signal: str,
        confidence: float,
        reason: str,
        url: Optional[str] = None,
    ) -> bool:
        try:
            r = requests.post(
                f"{self.base_url}/api/news_signal",
                json={
                    "source": source,
                    "headline": headline,
                    "signal": signal,
                    "confidence": confidence,
                    "reason": reason,
                    "ts": time.time(),
                    "url": url,
                },
                timeout=5,
            )
            return r.ok
        except Exception as e:
            print(f"  [KB] send error: {e}")
            return False

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/status", timeout=3)
            return r.ok
        except Exception:
            return False


# ---------------------------------------------------------------------------
# News Agent
# ---------------------------------------------------------------------------

class NewsAgent:
    """Main agent — polls feeds, classifies, signals."""

    def __init__(self, config: Dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run or config.get("dry_run", False)
        self.poll_seconds = int(config.get("poll_seconds", 15))
        self.min_confidence = float(config.get("min_confidence", 0.80))
        self.priority_keywords = [k.lower() for k in config.get("priority_keywords", [])]

        # Dedup cache — stores item IDs we've already processed
        self._seen: Deque[str] = deque(maxlen=int(config.get("max_seen", 500)))
        self._seen_set: set = set()

        # Build pollers
        self.pollers: List[FeedPoller] = []
        for name, fcfg in config.get("feeds", {}).items():
            if fcfg.get("enabled", True):
                self.pollers.append(FeedPoller(name, fcfg))

        # AI classifier
        self.classifier = LocalAIClassifier(
            ollama_url=config.get("ollama_url", "http://localhost:11434/api/generate"),
            model=config.get("model", "hermes3"),
        )

        # KalshiBaby client
        self.kb = KalshiBabyClient(config.get("kalshibaby_url", "http://127.0.0.1:8765"))

        # Signal log for display
        self.signal_log: Deque[Dict] = deque(maxlen=50)

    def _item_id(self, item: Dict) -> str:
        key = item.get("id") or item.get("link") or item.get("title", "")
        return hashlib.md5(key.encode()).hexdigest()

    def _already_seen(self, item: Dict) -> bool:
        iid = self._item_id(item)
        if iid in self._seen_set:
            return True
        self._seen.append(iid)
        self._seen_set.add(iid)
        # Trim set to match deque
        if len(self._seen_set) > len(self._seen) + 10:
            self._seen_set = set(self._seen)
        return False

    def _is_priority(self, item: Dict) -> bool:
        text = (item.get("title", "") + " " + item.get("summary", "")).lower()
        return any(kw in text for kw in self.priority_keywords)

    def tick(self) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] Polling {len(self.pollers)} feeds...")

        for poller in self.pollers:
            items = poller.fetch()
            for item in items:
                if self._already_seen(item):
                    continue

                title = item.get("title", "")
                summary = item.get("summary", "")

                if not title:
                    continue

                # Skip if no priority keywords and not a high-weight source
                if not self._is_priority(item) and poller.weight < 1.5:
                    if self.dry_run:
                        print(f"  [{poller.name}] SKIPPED (no priority keywords): {title[:60]}")
                    continue

                print(f"  [{poller.name}] {title[:80]}")

                # Filter domestic US politics — not relevant to oil trading
                domestic_noise = [
                    "dumocrat", "fisa", "dni", "gop", "nomination",
                    "republican", "senator", "congress", "pulte",
                    "dnc", "rnc", "midterm", "election", "vote",
                    "heritage foundation", "wahl", "alabama gop",
                ]
                combined = (title + " " + summary).lower()
                if any(kw in combined for kw in domestic_noise) and                    not any(kw in combined for kw in ["iran", "hormuz", "oil", "brent", "wti"]):
                    continue  # skip domestic politics

                result = self.classifier.classify(title, summary)
                signal = result.get("signal", "NEUTRAL")
                confidence = float(result.get("confidence", 0))
                reason = result.get("reason", "")

                # Apply source weight to confidence
                weighted_confidence = min(1.0, confidence * poller.weight)

                entry = {
                    "ts": now,
                    "source": poller.name,
                    "title": title[:100],
                    "signal": signal,
                    "confidence": weighted_confidence,
                    "reason": reason,
                }
                self.signal_log.appendleft(entry)

                # In dry-run always show classification result
                if self.dry_run or signal != "NEUTRAL":
                    print(f"    → {signal} ({weighted_confidence:.0%}): {reason}")

                if signal == "NEUTRAL":
                    continue

                if weighted_confidence < self.min_confidence:
                    print(f"    → Below threshold ({self.min_confidence:.0%}), skipping")
                    continue

                if self.dry_run:
                    print(f"    → DRY RUN: would send {signal} to KalshiBaby")
                else:
                    ok = self.kb.send_signal(
                        source=poller.name,
                        headline=title,
                        signal=signal,
                        confidence=weighted_confidence,
                        reason=reason,
                        url=item.get("link"),
                    )
                    status = "✓ sent" if ok else "✗ failed"
                    print(f"    → KalshiBaby: {status}")

    def run(self) -> None:
        print("=" * 60)
        print("KalshiBaby News Agent v1")
        print(f"Model:      {self.classifier.model}")
        print(f"Poll:       every {self.poll_seconds}s")
        print(f"Threshold:  {self.min_confidence:.0%} confidence")
        print(f"Dry run:    {self.dry_run}")
        print(f"Feeds:      {[p.name for p in self.pollers]}")
        print("=" * 60)

        # Check Ollama
        if self.classifier.check_available():
            print(f"✓ Ollama available: {self.classifier.model}")
        else:
            print("⚠ Ollama unavailable — using keyword fallback")

        # Check KalshiBaby
        if self.kb.ping():
            print("✓ KalshiBaby connected")
        else:
            print("⚠ KalshiBaby not reachable — signals will fail")

        print("=" * 60)

        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"Tick error: {e}")
            time.sleep(self.poll_seconds)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        print(f"Config {path} not found — using defaults")
        return DEFAULT_CONFIG
    with open(p) as f:
        user = yaml.safe_load(f) or {}
    # Deep merge user config over defaults
    config = dict(DEFAULT_CONFIG)
    config.update(user)
    if "feeds" in user:
        config["feeds"] = {**DEFAULT_CONFIG["feeds"], **user["feeds"]}
    return config


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KalshiBaby News Agent")
    parser.add_argument("--config", default="newsagent.yaml",
                        help="Config file (default: newsagent.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify but don't send signals to KalshiBaby")
    parser.add_argument("--model", default=None,
                        help="Override Ollama model (e.g. mistral, llama3)")
    parser.add_argument("--poll", type=int, default=None,
                        help="Override poll interval in seconds")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.model:
        cfg["model"] = args.model
    if args.poll:
        cfg["poll_seconds"] = args.poll
    if args.dry_run:
        cfg["dry_run"] = True

    NewsAgent(cfg, dry_run=cfg.get("dry_run", False)).run()
