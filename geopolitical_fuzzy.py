#!/usr/bin/env python3
"""
KalshiBaby Geopolitical Fuzzy Classifier
=========================================
A fuzzy logic expert system for classifying geopolitical news
into oil price trading signals.

Architecture:
    Layer 1: Entity-aware keyword extraction → actor sentiment scores
    Layer 2: State vector (who wants what, to what degree)
    Layer 3: Fuzzy rule engine → weighted signal aggregation
    Output:  Trading signal with confidence and explanation

Tuning:
    - Add/remove keywords in ACTOR_PATTERNS
    - Adjust actor weights in ACTOR_WEIGHTS
    - Add/modify rules in FUZZY_RULES
    - Adjust thresholds in FuzzyClassifier.classify()

Usage:
    from geopolitical_fuzzy import FuzzyClassifier
    clf = FuzzyClassifier()
    result = clf.classify("Trump cancels Iran strikes", "Deal approved by both sides")
    # {"signal": "DEAL_SIGNAL", "confidence": 0.91, "reason": "...", "state": {...}}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Layer 1: Actor sentiment patterns
# ---------------------------------------------------------------------------

# For each actor, keywords that indicate bullish (pro-deal/de-escalation)
# or bearish (anti-deal/escalation) sentiment.
# Score: +1.0 = strongly pro-deal, -1.0 = strongly pro-escalation

ACTOR_PATTERNS: Dict[str, Dict[str, List[Tuple[str, float]]]] = {

    "trump": {
        # Note: Hegseth statements treated as Trump proxy
        "positive": [   # pro-deal / de-escalation
            ("let's not blow it", 0.95),
            ("not blow it", 0.95),
            ("canceled strikes", 0.95),
            ("cancelled strikes", 0.95),
            ("cancels strikes", 0.95),
            ("cancels iran strikes", 0.95),
            ("canceled iran strikes", 0.95),
            ("cancel strikes", 0.90),
            ("calls off strikes", 0.95),
            ("no strikes", 0.80),
            ("peace deal", 0.90),
            ("beautiful peace", 0.90),
            ("close to a deal", 0.90),
            ("sign a deal", 0.85),
            ("deal will be signed", 0.85),
            ("approved discussions", 0.85),
            ("should not have happened", 0.80),  # condemning escalation
            ("on track", 0.80),
            ("stand down", 0.80),
            ("two to three hours", 0.75),
            ("great settlement", 0.90),
            ("no more attacks", 0.80),
            ("all sides should stand down", 0.85),
            ("deal with iran", 0.75),
            ("hormuz open", 0.85),
            ("hormuz immediately", 0.85),
        ],
        "negative": [   # escalation / anti-deal
            ("very hard tonight", 1.00),
            ("hit them hard", 0.95),
            ("hitting iran", 0.95),
            ("resume bombing", 0.95),
            ("pay the price", 0.90),
            ("total control", 0.85),
            ("kharg island", 0.90),
            ("playing us for suckers", 0.85),
            ("taken too long", 0.80),
            ("attack iran", 0.90),
            ("strike iran", 0.90),
            ("power plants", 0.85),
            ("bridges", 0.70),
            ("assume total control", 0.90),
            # Hegseth as Trump proxy
            ("centcom will be busy", 1.00),
            ("going to hit iran", 0.95),
            ("hit iran hard", 0.95),
            ("negotiate with bombs", 0.90),
            ("bombs dropping", 0.90),
            ("hegseth", 0.55),
            # Apache/must respond
            ("must respond", 0.85),
            ("must, of necessity, respond", 0.90),
            ("necessity respond", 0.90),
        ],
    },

    "iran": {
        "positive": [   # pro-deal
            ("islamic republic of iran", 0.85),  # appears in deal announcements
            ("with iran", 0.65),
            ("finalise deal", 0.90),
            ("finalize deal", 0.90),
            ("negotiations", 0.60),
            ("talks continuing", 0.75),
            ("approved deal", 0.90),
            ("reached a deal", 0.95),
            ("reached deal", 0.95),
            ("peace deal", 0.90),
            ("signed deal", 0.95),
            ("open hormuz", 0.90),
            ("dilute uranium", 0.85),
            ("enriched uranium dilut", 0.85),
            ("mediators", 0.65),
            ("agreement", 0.70),
            ("iran and us", 0.65),
            ("us and iran", 0.65),
            ("president defends negotiations", 0.80),
            ("establishment united", 0.75),
            ("common vision", 0.70),
            ("within framework of negotiations", 0.80),
        ],
        "negative": [   # escalation / anti-deal
            ("no point", 0.90),
            ("red lines", 0.85),
            ("upcoming response", 0.95),
            ("warriors of islam", 0.90),
            ("will not be tolerated", 0.85),
            ("not fulfilling commitments", 0.85),
            ("ceasefire meaningless", 0.90),
            ("lacks the will", 0.80),
            ("heavy responses", 0.90),
            ("retaliate", 0.75),
            ("lifeblood", 0.70),
            ("reassess", 0.80),
            ("harming diplomatic", 0.80),
            ("violating ceasefire", 0.85),
            ("irgc", 0.55),
            # Nuclear facility strikes on Iran = major escalation
            ("nuclear facility", 0.85),
            ("nuclear site", 0.85),
            ("natanz", 0.90),
            ("fordow", 0.90),
            ("iran nuclear", 0.85),
            # Deal collapse / conditional rejection — TODAY'S scenario
            ("threatened to sink", 0.92),
            ("sink the agreement", 0.92),
            ("sink the deal", 0.92),
            ("resumption of all-out war", 0.95),
            ("resumption of war", 0.90),
            ("reject the deal", 0.90),
            ("reject deal", 0.88),
            ("will reject", 0.85),
            ("contradictory interpretations", 0.82),
            ("walk away from", 0.82),
            ("requires israel to withdraw", 0.88),
            ("condition israel", 0.85),
            ("scupper", 0.88),
            ("derail the deal", 0.88),
            ("derailing the deal", 0.88),
            ("collapse the deal", 0.90),
            ("deal at risk", 0.85),
            ("deal in jeopardy", 0.88),
            ("deal could collapse", 0.90),
        ],
    },

    "israel": {
        "positive": [   # pro-deal / standing down
            ("stand down", 0.80),
            ("halt operations", 0.75),
            ("ceasefire", 0.70),
            ("trump told netanyahu", 0.65),
            ("not conduct additional strikes", 0.85),
        ],
        "negative": [   # sabotaging deal / escalating
            ("struck beirut", 0.90),
            ("strikes beirut", 0.90),
            ("strike on beirut", 0.90),
            ("idf strikes", 0.85),
            ("hezbollah command", 0.80),
            ("military operation", 0.70),
            ("catastrophic deal", 0.90),
            ("very bad deal", 0.90),
            ("goals not met", 0.85),
            ("no coincidence", 0.75),
            ("dahieh", 0.80),
            ("southern suburbs of beirut", 0.80),
            ("airstrike", 0.65),
            # Gaza strikes — lower weight, still regional tension
            ("strikes gaza", 0.45),
            ("strike on gaza", 0.45),
            ("gaza mourns", 0.40),
            ("killed in israeli strike", 0.45),
            ("israeli airstrike", 0.50),
            # Deal rejection / Lebanon occupation — TODAY'S scenario
            ("rejected the deal", 0.92),
            ("rejects the deal", 0.92),
            ("rejects deal", 0.90),
            ("rejected deal", 0.90),
            ("already rejected", 0.88),
            ("israel has rejected", 0.90),
            ("israel rejected", 0.88),
            ("withdraw from lebanon", 0.85),  # condition Israel refuses
            ("occupation of southern lebanon", 0.88),
            ("occupation of lebanon", 0.85),
            ("violate the deal", 0.88),
            ("bombing lebanon", 0.82),
            ("bombs lebanon", 0.82),
            ("bombed lebanon", 0.82),
            ("strikes lebanon", 0.80),
            ("struck lebanon", 0.80),
            ("keeps bombing", 0.85),
            ("rules out withdrawal", 0.90),
            ("will not withdraw", 0.90),
            ("refuses to withdraw", 0.90),
            ("declares victory", 0.80),
            ("achieved its goals", 0.82),
            ("scupper", 0.88),
            ("derail", 0.82),
            ("slams israeli", 0.75),
            ("netanyahu must", 0.78),
            ("more responsible", 0.70),
        ],
    },

    "hezbollah": {
        "positive": [
            ("ceasefire", 0.65),
            ("stand down", 0.70),
        ],
        "negative": [
            ("fires rockets", 0.85),
            ("launches aerial", 0.85),
            ("attacks israel", 0.80),
            ("unprovoked attack", 0.75),
            ("missile", 0.60),
            ("drone attack", 0.65),
        ],
    },

    "qatar": {
        "positive": [
            ("mediating", 0.80),
            ("finalising", 0.85),
            ("electronic signing", 0.90),
            ("travelled to tehran", 0.75),
            ("bridge remaining gaps", 0.80),
            ("qatari negotiators", 0.75),
        ],
        "negative": [
            ("talks collapsed", 0.90),
            ("left tehran", 0.70),
            ("failed", 0.75),
        ],
    },

    "pakistan": {
        "positive": [
            ("confirmed wording", 0.90),
            ("24 hours", 0.80),
            ("likely expected", 0.85),
            ("preparing for signing", 0.90),
            ("pakistan prime minister", 0.65),
            ("mediator", 0.65),
        ],
        "negative": [
            ("deep concern", 0.60),
            ("urges ceasefire", 0.55),
        ],
    },

    "hormuz": {
        "positive": [   # opening / positive flow
            ("hormuz open", 1.00),
            ("strait open", 0.95),
            ("reopening hormuz", 0.95),
            ("hormuz reopen", 0.95),
            ("strait reopen", 0.95),
            ("traffic resuming", 0.90),
            ("ships moving", 0.85),
            ("oil flowing", 0.85),
            ("immediately after signing", 0.85),
            ("open to all", 0.90),
            ("sign the deal", 0.85),
            ("deal signed", 0.85),
            ("sign a deal", 0.80),
            ("war ending", 0.85),
            ("war over", 0.85),
            ("end the iran war", 0.80),
            ("ended the iran war", 0.85),
            ("end the gulf war", 0.80),
            ("ended the gulf war", 0.85),
            ("washington and tehran", 0.70),
            ("tehran and washington", 0.70),
            ("agree to terms", 0.85),
            ("agreed to terms", 0.85),
            ("deal is now complete", 1.00),
            ("deal with iran is now", 1.00),
            ("now complete", 0.85),
            ("sign historic deal", 1.00),
            ("signed historic deal", 1.00),
            ("historic deal", 0.90),
            ("us and iran sign", 0.95),
            ("iran and us sign", 0.95),
            ("removal of the naval blockade", 1.00),
            ("remove the naval blockade", 1.00),
            ("removal of the blockade", 0.95),
            ("lifting the blockade", 0.95),
            ("toll free opening", 1.00),
            ("opening of the strait", 0.95),
            ("authorize the toll", 0.95),
            ("ships of the world", 0.90),
            ("peace and security", 0.85),
            ("great deal with iran", 0.90),
            ("iran deal signed", 0.95),
            ("iran deal complete", 0.95),
            ("the iran deal is", 0.95),
            ("the iran deal signed", 0.95),
            ("congratulations to all", 0.75),
        ],
        "negative": [   # closing / threat
            ("hormuz closed", 1.00),
            ("strait closed", 0.95),
            ("block hormuz", 0.90),
            ("close hormuz", 0.90),
            ("hormuz disruption", 0.65),
            ("strait disruption", 0.65),
            ("blockade", 0.70),
            ("tanker attacked", 0.80),
            ("ship disabled", 0.80),
            ("impose naval blockade", 0.75),
            ("enforcing naval blockade", 0.75),
        ],
    },
}

# ---------------------------------------------------------------------------
# Layer 2: Actor importance weights for oil price impact
# ---------------------------------------------------------------------------

# How much does each actor's sentiment influence the final oil signal?
# Must sum to 1.0 across the "deal" dimension
ACTOR_WEIGHTS = {
    "trump":     0.30,   # Controls US military, biggest single mover
    "iran":      0.25,   # Controls Hormuz, second biggest
    "israel":    0.20,   # Can kill deal unilaterally — increased from 0.15
    "hezbollah": 0.06,   # Less direct but Iran proxy
    "qatar":     0.09,   # Mediator confidence matters
    "pakistan":  0.05,   # Secondary mediator
    "hormuz":    0.05,   # Direct supply signal
}

# ---------------------------------------------------------------------------
# Layer 3: Fuzzy rules
# ---------------------------------------------------------------------------

@dataclass
class FuzzyRule:
    """
    A single fuzzy inference rule.
    conditions: dict of {actor.dimension: (operator, threshold)}
    output: signal type this rule fires
    weight: confidence when this rule fires (0-1)
    label: human-readable explanation
    """
    conditions: Dict[str, Tuple[str, float]]
    output: str
    weight: float
    label: str


FUZZY_RULES: List[FuzzyRule] = [

    # -----------------------------------------------------------------------
    # Strong DEAL signals
    # -----------------------------------------------------------------------
    FuzzyRule(
        conditions={"trump.net": (">", 0.80)},
        output="DEAL_SIGNAL",
        weight=0.82,
        label="Trump strongly signaling de-escalation",
    ),
    FuzzyRule(
        conditions={"trump.net": (">", 0.60), "iran.net": (">", 0.40)},
        output="DEAL_SIGNAL",
        weight=0.92,
        label="Both Trump and Iran strongly pro-deal",
    ),
    FuzzyRule(
        conditions={"trump.net": (">", 0.70), "qatar.net": (">", 0.50)},
        output="DEAL_SIGNAL",
        weight=0.85,
        label="Trump committed, Qatar mediating actively",
    ),
    FuzzyRule(
        conditions={"hormuz.net": (">", 0.70)},
        output="DEAL_SIGNAL",
        weight=0.95,
        label="Hormuz reopening signal — direct supply positive",
    ),
    FuzzyRule(
        conditions={"pakistan.net": (">", 0.70), "qatar.net": (">", 0.50)},
        output="DEAL_SIGNAL",
        weight=0.80,
        label="Both mediators confident — deal imminent",
    ),
    FuzzyRule(
        conditions={"trump.net": (">", 0.50), "iran.net": (">", 0.30),
                    "pakistan.net": (">", 0.50)},
        output="DEAL_SIGNAL",
        weight=0.88,
        label="Three-way deal signal confirmed",
    ),
    FuzzyRule(
        conditions={"israel.net": ("<", -0.40), "hormuz.net": (">", 0.30)},
        output="NEUTRAL",
        weight=0.82,
        label="Israel escalating while deal context present — uncertain",
    ),

    # Israel kills the deal — oil bullish (deal collapses, war risk returns)
    # Override: when Iran AND Israel both strongly anti-deal, suppress hormuz deal signal
    FuzzyRule(
        conditions={"iran.net": ("<", -0.50), "israel.net": ("<", -0.50), "hormuz.net": (">", 0.50)},
        output="ESCALATION_SIGNAL",
        weight=0.90,
        label="Deal collapse risk overrides Hormuz context — actors moving against deal",
    ),
    FuzzyRule(
        conditions={"israel.net": ("<", -0.60), "iran.net": ("<", -0.30)},
        output="ESCALATION_SIGNAL",
        weight=0.85,
        label="Israel rejecting deal conditions, Iran threatening to walk — deal collapse risk",
    ),
    FuzzyRule(
        conditions={"israel.net": ("<", -0.50), "trump.net": (">", 0.30)},
        output="NEUTRAL",
        weight=0.80,
        label="Israel sabotaging deal Trump wants — uncertain, watch price",
    ),
    FuzzyRule(
        conditions={"iran.net": ("<", -0.50), "israel.net": ("<", -0.40)},
        output="ESCALATION_SIGNAL",
        weight=0.87,
        label="Both Iran and Israel moving against deal — collapse likely",
    ),

    # -----------------------------------------------------------------------
    # Strong ESCALATION signals
    # -----------------------------------------------------------------------
    FuzzyRule(
        conditions={"trump.net": ("<", -0.60), "iran.net": ("<", -0.40)},
        output="ESCALATION_SIGNAL",
        weight=0.92,
        label="Both Trump and Iran escalating",
    ),
    FuzzyRule(
        conditions={"trump.net": ("<", -0.70)},
        output="ESCALATION_SIGNAL",
        weight=0.88,
        label="Trump strongly signaling strikes",
    ),
    FuzzyRule(
        conditions={"trump.net": ("<", -0.50)},
        output="ESCALATION_SIGNAL",
        weight=0.78,
        label="Trump signaling military action",
    ),
    FuzzyRule(
        conditions={"hormuz.net": ("<", -0.70)},
        output="ESCALATION_SIGNAL",
        weight=0.95,
        label="Hormuz closure signal — direct supply negative",
    ),
    FuzzyRule(
        conditions={"iran.net": ("<", -0.70), "hezbollah.net": ("<", -0.40)},
        output="ESCALATION_SIGNAL",
        weight=0.85,
        label="Iran and Hezbollah both escalating",
    ),
    FuzzyRule(
        conditions={"iran.net": ("<", -0.60)},
        output="ESCALATION_SIGNAL",
        weight=0.80,
        label="Iran strongly threatening retaliation",
    ),
    FuzzyRule(
        conditions={"trump.net": ("<", -0.50), "iran.net": ("<", -0.50)},
        output="ESCALATION_SIGNAL",
        weight=0.90,
        label="Mutual escalation confirmed",
    ),

    # -----------------------------------------------------------------------
    # NEUTRAL — conflicting signals (hold, don't trade)
    # -----------------------------------------------------------------------
    FuzzyRule(
        conditions={"trump.net": (">", 0.60), "israel.net": ("<", -0.60)},
        output="NEUTRAL",
        weight=0.85,
        label="Trump wants deal but Israel actively sabotaging — uncertain",
    ),
    FuzzyRule(
        conditions={"trump.net": (">", 0.50), "iran.net": ("<", -0.50), "israel.net": ("<", -0.50)},
        output="NEUTRAL",
        weight=0.88,
        label="Trump pro-deal but Iran and Israel both escalating — hold",
    ),
    # Removed: logically impossible condition (net > 0.30 AND net < 0.30)
    FuzzyRule(
        conditions={"trump.net": (">", 0.40), "hezbollah.net": ("<", -0.50)},
        output="NEUTRAL",
        weight=0.72,
        label="Deal progress undermined by Hezbollah activity",
    ),

    # -----------------------------------------------------------------------
    # BULLISH (supply threat, not necessarily full escalation)
    # -----------------------------------------------------------------------
    FuzzyRule(
        conditions={"hormuz.net": ("<", -0.40), "trump.net": (">", -0.20)},
        output="BULLISH",
        weight=0.78,
        label="Hormuz under threat but no full US escalation yet",
    ),
    FuzzyRule(
        conditions={"israel.net": ("<", -0.70), "iran.net": ("<", -0.30)},
        output="BULLISH",
        weight=0.75,
        label="Israel escalating, Iran responding — regional risk premium",
    ),
    FuzzyRule(
        conditions={"israel.net": ("<", -0.30)},
        output="BULLISH",
        weight=0.65,
        label="Israeli military action — modest regional risk premium",
    ),

    # -----------------------------------------------------------------------
    # BEARISH (supply restoration, not necessarily full deal)
    # -----------------------------------------------------------------------
    FuzzyRule(
        conditions={"trump.net": (">", 0.40), "hormuz.net": (">", 0.30)},
        output="BEARISH",
        weight=0.78,
        label="Deal progress + Hormuz reopening expected",
    ),
    FuzzyRule(
        conditions={"iran.net": (">", 0.50), "qatar.net": (">", 0.40)},
        output="BEARISH",
        weight=0.72,
        label="Iran engaging, Qatar mediating — de-escalation trend",
    ),
]


# ---------------------------------------------------------------------------
# State vector and classifier
# ---------------------------------------------------------------------------

@dataclass
class ActorState:
    positive_score: float = 0.0
    negative_score: float = 0.0
    match_count: int = 0
    matched_keywords: List[str] = field(default_factory=list)

    @property
    def net(self) -> float:
        """Net sentiment: +1 = strongly pro-deal, -1 = strongly anti-deal."""
        if self.positive_score == 0 and self.negative_score == 0:
            return 0.0
        total = self.positive_score + self.negative_score
        return (self.positive_score - self.negative_score) / total if total > 0 else 0.0


@dataclass
class GeopoliticalState:
    actors: Dict[str, ActorState] = field(default_factory=dict)

    def __post_init__(self):
        for actor in ACTOR_PATTERNS:
            if actor not in self.actors:
                self.actors[actor] = ActorState()

    def get_net(self, actor: str) -> float:
        return self.actors.get(actor, ActorState()).net

    def to_dict(self) -> Dict[str, float]:
        return {
            f"{actor}.net": state.net
            for actor, state in self.actors.items()
        }

    def summary(self) -> str:
        lines = []
        for actor, state in self.actors.items():
            if state.match_count > 0:
                direction = "PRO-DEAL" if state.net > 0.1 else "ESCALATING" if state.net < -0.1 else "NEUTRAL"
                lines.append(
                    f"  {actor:<12} net={state.net:+.2f} ({direction}) "
                    f"kw={state.matched_keywords[:3]}"
                )
        return "\n".join(lines) if lines else "  No actor signals detected"


@dataclass
class FuzzyResult:
    signal: str
    confidence: float
    reason: str
    state: GeopoliticalState
    fired_rules: List[Tuple[FuzzyRule, float]]
    weighted_score: float

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "weighted_score": round(self.weighted_score, 3),
            "actor_states": {
                actor: round(self.state.get_net(actor), 3)
                for actor in ACTOR_PATTERNS
            },
            "fired_rules": [
                {"label": r.label, "output": r.output, "weight": round(w, 3)}
                for r, w in self.fired_rules
            ],
        }


class FuzzyClassifier:
    """
    Fuzzy logic classifier for geopolitical oil price signals.
    
    Tuning:
        clf.actor_patterns["trump"]["positive"].append(("new phrase", 0.85))
        clf.actor_weights["trump"] = 0.35
        clf.rules.append(FuzzyRule(...))
    """

    def __init__(self) -> None:
        self.actor_patterns = ACTOR_PATTERNS
        self.actor_weights = ACTOR_WEIGHTS
        self.rules = FUZZY_RULES

        # Thresholds for defuzzification
        self.deal_threshold = 0.28         # net score above this → DEAL_SIGNAL
        self.escalation_threshold = -0.28  # net score below this → ESCALATION_SIGNAL
        self.bullish_threshold = 0.20
        self.bearish_threshold = -0.20

        # Minimum keyword matches to trust the signal
        self.min_keyword_matches = 1

    # -----------------------------------------------------------------------
    # Layer 1: Extract actor sentiments from text
    # -----------------------------------------------------------------------

    def _extract_state(self, text: str) -> GeopoliticalState:
        """
        Scan text for actor-specific keywords and build state vector.
        Returns GeopoliticalState with sentiment scores per actor.
        """
        text_lower = text.lower()
        state = GeopoliticalState()

        for actor, patterns in self.actor_patterns.items():
            actor_state = state.actors[actor]

            for phrase, score in patterns.get("positive", []):
                if phrase in text_lower:
                    actor_state.positive_score += score
                    actor_state.match_count += 1
                    actor_state.matched_keywords.append(f"+{phrase}")

            for phrase, score in patterns.get("negative", []):
                if phrase in text_lower:
                    actor_state.negative_score += score
                    actor_state.match_count += 1
                    actor_state.matched_keywords.append(f"-{phrase}")

        return state

    # -----------------------------------------------------------------------
    # Layer 2: Compute weighted net score across actors
    # -----------------------------------------------------------------------

    def _weighted_score(self, state: GeopoliticalState) -> float:
        """
        Aggregate actor sentiments into a single weighted net score.
        Positive = bullish on deal / oil price falling.
        Negative = escalation / oil price rising.
        """
        score = 0.0
        for actor, weight in self.actor_weights.items():
            net = state.get_net(actor)
            score += net * weight
        return score

    # -----------------------------------------------------------------------
    # Layer 3: Apply fuzzy rules
    # -----------------------------------------------------------------------

    def _apply_rules(
        self, state: GeopoliticalState
    ) -> List[Tuple[FuzzyRule, float]]:
        """
        Evaluate all fuzzy rules against the current state.
        Returns list of (rule, activation_strength) for rules that fire.
        """
        state_dict = state.to_dict()
        fired = []

        for rule in self.rules:
            activation = rule.weight
            all_conditions_met = True

            for condition_key, (operator, threshold) in rule.conditions.items():
                value = state_dict.get(condition_key, 0.0)
                if operator == ">" and not (value > threshold):
                    all_conditions_met = False
                    break
                elif operator == "<" and not (value < threshold):
                    all_conditions_met = False
                    break

            if all_conditions_met:
                fired.append((rule, activation))

        return fired

    # -----------------------------------------------------------------------
    # Defuzzification: aggregate fired rules into output signal
    # -----------------------------------------------------------------------

    def _defuzzify(
        self,
        fired_rules: List[Tuple[FuzzyRule, float]],
        weighted_score: float,
        total_keywords: int,
    ) -> Tuple[str, float, str]:
        """
        Convert fired rules + weighted score into final signal.
        Returns (signal, confidence, reason).
        """
        if total_keywords < self.min_keyword_matches:
            return "NEUTRAL", 0.50, "Insufficient keyword matches"

        # Tally votes from fired rules
        vote_totals: Dict[str, float] = {}
        for rule, activation in fired_rules:
            vote_totals[rule.output] = vote_totals.get(rule.output, 0) + activation

        # Also vote from weighted score
        if weighted_score > self.deal_threshold:
            vote_totals["DEAL_SIGNAL"] = vote_totals.get("DEAL_SIGNAL", 0) + weighted_score
        elif weighted_score < self.escalation_threshold:
            vote_totals["ESCALATION_SIGNAL"] = (
                vote_totals.get("ESCALATION_SIGNAL", 0) + abs(weighted_score)
            )
        elif weighted_score > self.bullish_threshold:
            vote_totals["BEARISH"] = vote_totals.get("BEARISH", 0) + weighted_score * 0.5
        elif weighted_score < -self.bullish_threshold:
            vote_totals["BULLISH"] = vote_totals.get("BULLISH", 0) + abs(weighted_score) * 0.5

        if not vote_totals:
            return "NEUTRAL", 0.50, f"No signals detected (score={weighted_score:+.2f})"

        # Winner takes signal, confidence from vote share
        total_votes = sum(vote_totals.values())
        winner = max(vote_totals, key=vote_totals.get)
        winner_votes = vote_totals[winner]
        confidence = min(0.98, winner_votes / max(total_votes, 1.0))

        # Find best fired rule for the winner
        winner_rules = [
            (r, a) for r, a in fired_rules if r.output == winner
        ]
        if winner_rules:
            best_rule = max(winner_rules, key=lambda x: x[1])
            reason = best_rule[0].label
        else:
            reason = f"Weighted score {weighted_score:+.2f}"

        return winner, confidence, reason

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def classify(self, title: str, summary: str = "") -> FuzzyResult:
        """
        Classify a news headline + summary into a trading signal.
        
        Returns FuzzyResult with signal, confidence, reason, and full state.
        """
        # Strip HTML tags from summary (RSS feeds often include raw HTML)
        import re as _re
        clean_summary = _re.sub(r"<[^>]+>", " ", summary)
        text = title + " " + clean_summary

        # Layer 1: Extract actor sentiments
        state = self._extract_state(text)

        # Layer 2: Weighted net score
        weighted = self._weighted_score(state)

        # Layer 3: Apply fuzzy rules
        fired = self._apply_rules(state)

        # Defuzzify
        total_kw = sum(s.match_count for s in state.actors.values())
        signal, confidence, reason = self._defuzzify(fired, weighted, total_kw)

        return FuzzyResult(
            signal=signal,
            confidence=confidence,
            reason=reason,
            state=state,
            fired_rules=fired,
            weighted_score=weighted,
        )

    def explain(self, title: str, summary: str = "") -> str:
        """
        Full human-readable explanation of classification.
        Useful for tuning and debugging.
        """
        result = self.classify(title, summary)
        lines = [
            f"INPUT:    {title[:80]}",
            f"SIGNAL:   {result.signal} (confidence={result.confidence:.0%})",
            f"REASON:   {result.reason}",
            f"SCORE:    {result.weighted_score:+.3f}",
            f"",
            f"ACTOR STATES:",
            result.state.summary(),
        ]
        if result.fired_rules:
            lines.append(f"")
            lines.append(f"FIRED RULES:")
            for rule, activation in result.fired_rules:
                lines.append(f"  [{activation:.0%}] {rule.output}: {rule.label}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standalone test / tuning mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    clf = FuzzyClassifier()

    # Test headlines — add your own to tune the system
    test_cases = [
        # Thursday 12:25pm — the $3.07 loss event
        (
            "Trump cancels Iran strikes, says Tehran has approved talks",
            "Trump said he has canceled planned strikes on Iran after Tehran leadership approved discussions and final points",
        ),
        # Monday morning Apache incident
        (
            "Trump: Iran shot down US Apache helicopter, US must respond",
            "Trump said the United States must respond to the downing of the helicopter over the Strait of Hormuz",
        ),
        # Wednesday escalation
        (
            "Trump says US will hit Iran VERY HARD TONIGHT, threatens to take total control of its oil industry",
            "Hegseth: CENTCOM will be busy tonight because we are going to hit Iran hard",
        ),
        # Today's complex situation
        (
            "Let's not blow it! Trump says after Tehran warns Israeli strike on Beirut risks US-Iran deal",
            "Iran's chief negotiator says there's no point in continuing talks after Israel launched a strike on Beirut",
        ),
        # Pakistan confirmation
        (
            "Pakistan PM says US and Iran have agreed to wording of deal to end war",
            "Mediators preparing for electronic signing within 24 hours, Hormuz reopening expected",
        ),
        # Iran threatening
        (
            "Iran's top security body warns of upcoming response to Israeli attack",
            "The response of the warriors of Islam is upcoming. Red lines will not be tolerated",
        ),
        # Hegseth escalation
        (
            "Hegseth: CENTCOM will be busy tonight, we are going to hit Iran hard",
            "Negotiate with bombs. They are going to have bombs dropping on key facilities",
        ),
        # Deal signed
        (
            "US and Iran sign historic deal, Hormuz strait to open immediately",
            "Ceasefire agreement reached, sanctions to be lifted in phases, uranium to be diluted",
        ),
    ]

    print("=" * 70)
    print("KalshiBaby Fuzzy Classifier — Test Mode")
    print("=" * 70)

    for title, summary in test_cases:
        print()
        print(clf.explain(title, summary))
        print("-" * 70)

    # Interactive mode
    if "--interactive" in sys.argv:
        print("\nInteractive mode — enter headlines to classify (Ctrl+C to quit)")
        while True:
            try:
                title = input("\nHeadline: ").strip()
                summary = input("Summary (optional): ").strip()
                if title:
                    print(clf.explain(title, summary))
            except KeyboardInterrupt:
                print("\nDone.")
                break
