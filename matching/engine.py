"""
Matching Engine — Agent-Aware Order Book & Resource Listings

Extends the basic matching with agent identity and performance awareness.
Each Intent is now linked to an Agent, and matching considers agent quality.
"""

from __future__ import annotations

import uuid
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import AgentRegistration


# ============================================================
# Domain Primitives
# ============================================================


class IntentType(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    COMPUTE = "compute"           # GPU/CPU time
    STORAGE = "storage"           # disk / object store
    DATA = "data"                 # datasets, APIs
    SERVICE = "service"           # arbitrary agent-performed work
    TOKEN = "token"               # fungible crypto tokens
    BANDWIDTH = "bandwidth"       # network throughput


class NegotiationStyle(str, Enum):
    AGGRESSIVE = "aggressive"
    BALANCED = "balanced"
    PASSIVE = "passive"


@dataclass
class PriceConstraint:
    currency: str = "INJ"
    min_acceptable: float = 0.0
    ideal: float = 0.0
    max_acceptable: float = float("inf")

    def overlap(self, other: "PriceConstraint") -> Optional[tuple[float, float]]:
        low = max(self.min_acceptable, other.min_acceptable)
        high = min(self.max_acceptable, other.max_acceptable)
        if low <= high:
            return (low, high)
        return None


@dataclass
class TradingRules:
    max_negotiation_rounds: int = 5
    min_price_delta_pct: float = 1.0
    auto_accept_threshold_pct: float = 5.0
    require_escrow: bool = True
    timeout_seconds: int = 300


# ============================================================
# Resource Listing (seller posts this)
# ============================================================


@dataclass
class ResourceListing:
    """A concrete resource a seller puts on the market."""
    listing_id: str = field(default_factory=lambda: f"list_{uuid.uuid4().hex[:10]}")
    seller_agent_id: str = ""               # agent that sells this
    seller_name: str = ""                   # display name
    asset_class: AssetClass = AssetClass.COMPUTE
    title: str = ""
    description: str = ""
    quantity: int = 1
    unit: str = "hour"                      # hour, GB, request, etc.
    price_per_unit: PriceConstraint = field(default_factory=PriceConstraint)
    available_until: float = 0.0            # listing expiry timestamp
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def is_available(self) -> bool:
        if self.available_until <= 0:
            return True
        return time.time() < self.available_until

    def to_public_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "seller_agent_id": self.seller_agent_id,
            "seller_name": self.seller_name,
            "asset_class": self.asset_class.value,
            "title": self.title,
            "description": self.description,
            "quantity": self.quantity,
            "unit": self.unit,
            "price_range": {
                "min": self.price_per_unit.min_acceptable,
                "ideal": self.price_per_unit.ideal,
                "max": self.price_per_unit.max_acceptable,
                "currency": self.price_per_unit.currency,
            },
            "tags": self.tags,
            "created_at": self.created_at,
        }


# ============================================================
# Intent (linked to Agent)
# ============================================================


@dataclass
class Intent:
    """
    A buy or sell intent posted by an agent on behalf of their human owner.

    Now linked to an AgentRegistration for identity and performance tracking.
    """
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    agent_id: str = ""                      # links to AgentRegistration
    agent_name: str = ""                    # display name
    intent_type: IntentType = IntentType.SELL
    asset_class: AssetClass = AssetClass.SERVICE
    description: str = ""
    quantity: int = 1
    price: PriceConstraint = field(default_factory=PriceConstraint)
    rules: TradingRules = field(default_factory=TradingRules)
    negotiation_style: NegotiationStyle = NegotiationStyle.BALANCED
    tags: list[str] = field(default_factory=list)
    listing_id: str = ""                    # links to ResourceListing (for sell)
    created_at: float = field(default_factory=time.time)
    ttl_seconds: int = 3600

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


# ============================================================
# CandidateMatch (agent-aware scoring)
# ============================================================


@dataclass
class CandidateMatch:
    buy_intent: Intent
    sell_intent: Intent
    price_zone: tuple[float, float]
    score: float = 0.0
    match_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    buyer_elo: float = 1000.0               # for arena display
    seller_elo: float = 1000.0              # for arena display

    def __post_init__(self):
        if self.score == 0.0:
            self.score = self._compute_score()

    def _compute_score(self) -> float:
        score = 50.0  # baseline for price overlap

        # Price zone width bonus
        zone_width = self.price_zone[1] - self.price_zone[0]
        avg_price = (self.price_zone[0] + self.price_zone[1]) / 2
        if avg_price > 0:
            width_pct = (zone_width / avg_price) * 100
            score += min(width_pct * 2, 20)

        # Tag overlap
        buy_tags = set(self.buy_intent.tags)
        sell_tags = set(self.sell_intent.tags)
        if buy_tags and sell_tags:
            score += min(len(buy_tags & sell_tags) * 5, 15)

        # Style compatibility
        style_scores = {
            (NegotiationStyle.AGGRESSIVE, NegotiationStyle.AGGRESSIVE): -10,
            (NegotiationStyle.PASSIVE, NegotiationStyle.PASSIVE): 5,
            (NegotiationStyle.BALANCED, NegotiationStyle.BALANCED): 10,
            (NegotiationStyle.AGGRESSIVE, NegotiationStyle.PASSIVE): -5,
        }
        pair = (self.buy_intent.negotiation_style, self.sell_intent.negotiation_style)
        score += style_scores.get(pair, 0)

        # Quantity match
        if self.buy_intent.quantity == self.sell_intent.quantity:
            score += 5

        # ELO closeness bonus — close ratings = more exciting battle
        elo_diff = abs(self.buyer_elo - self.seller_elo)
        score += max(0, 10 - elo_diff / 50)  # up to +10 for close ELO

        return max(0.0, min(100.0, score))


# ============================================================
# OrderBook (with Resource Listings)
# ============================================================


class OrderBook:
    """Agent-aware order book with resource listings."""

    def __init__(self):
        self._buy_intents: dict[str, Intent] = {}
        self._sell_intents: dict[str, Intent] = {}
        self._listings: dict[str, ResourceListing] = {}
        self._agent_registry = None  # set via configure()

    def configure(self, registry: "AgentRegistry"):  # type: ignore
        self._agent_registry = registry

    # ---- Listings ----

    def publish_listing(self, listing: ResourceListing) -> str:
        self._listings[listing.listing_id] = listing
        return listing.listing_id

    def get_listings(
        self, asset_class: str = "", available_only: bool = True
    ) -> list[ResourceListing]:
        results = list(self._listings.values())
        if asset_class:
            results = [l for l in results if l.asset_class.value == asset_class]
        if available_only:
            results = [l for l in results if l.is_available()]
        return sorted(results, key=lambda l: l.created_at, reverse=True)

    def remove_listing(self, listing_id: str) -> bool:
        return self._listings.pop(listing_id, None) is not None

    # ---- Intents ----

    def publish(self, intent: Intent) -> str:
        store = self._buy_intents if intent.intent_type == IntentType.BUY else self._sell_intents
        store[intent.intent_id] = intent

        # Update agent ELO on the intent for scoring
        if self._agent_registry:
            agent = self._agent_registry.get(intent.agent_id)
            if agent:
                intent.negotiation_style = NegotiationStyle(agent.negotiation_style)

        return intent.intent_id

    def revoke(self, intent_id: str) -> bool:
        for store in (self._buy_intents, self._sell_intents):
            if intent_id in store:
                del store[intent_id]
                return True
        return False

    def get(self, intent_id: str) -> Optional[Intent]:
        for store in (self._buy_intents, self._sell_intents):
            if intent_id in store:
                return store[intent_id]
        return None

    # ---- Matching ----

    def find_matches(self, intent: Intent, top_k: int = 10) -> list[CandidateMatch]:
        """Find matching counterparties, enriched with agent ELO data."""
        if intent.intent_type == IntentType.BUY:
            counterparties = list(self._sell_intents.values())
        else:
            counterparties = list(self._buy_intents.values())

        candidates = []
        for other in counterparties:
            if other.is_expired():
                continue
            if other.asset_class != intent.asset_class:
                continue

            buyer = intent if intent.intent_type == IntentType.BUY else other
            seller = intent if intent.intent_type == IntentType.SELL else other

            if buyer.price.max_acceptable < seller.price.min_acceptable:
                continue
            if buyer.price.min_acceptable > seller.price.max_acceptable:
                continue

            zone = (
                max(buyer.price.min_acceptable, seller.price.min_acceptable),
                min(buyer.price.max_acceptable, seller.price.max_acceptable),
            )

            # Get agent ELOs for scoring
            buyer_elo = seller_elo = 1000.0
            if self._agent_registry:
                ba = self._agent_registry.get(buyer.agent_id)
                sa = self._agent_registry.get(seller.agent_id)
                if ba:
                    buyer_elo = ba.elo_rating
                if sa:
                    seller_elo = sa.elo_rating

            match = CandidateMatch(
                buy_intent=buyer,
                sell_intent=seller,
                price_zone=zone,
                buyer_elo=buyer_elo,
                seller_elo=seller_elo,
            )
            candidates.append(match)

        candidates.sort(key=lambda m: m.score, reverse=True)
        return candidates[:top_k]

    def find_all_matches(self) -> list[CandidateMatch]:
        """Batch match — find all compatible pairs across the book."""
        all_matches = []
        for buy in list(self._buy_intents.values()):
            if buy.is_expired():
                self.revoke(buy.intent_id)
                continue
            all_matches.extend(self.find_matches(buy))
        return sorted(all_matches, key=lambda m: m.score, reverse=True)

    def cleanup_expired(self) -> int:
        removed = 0
        for store in (self._buy_intents, self._sell_intents):
            expired = [iid for iid, intent in store.items() if intent.is_expired()]
            for iid in expired:
                del store[iid]
                removed += 1
        # Also cleanup listings
        expired_listings = [lid for lid, l in self._listings.items() if not l.is_available()]
        for lid in expired_listings:
            del self._listings[lid]
            removed += 1
        return removed

    @property
    def stats(self) -> dict:
        return {
            "buy_intents": len(self._buy_intents),
            "sell_intents": len(self._sell_intents),
            "listings": len(self._listings),
            "available_listings": sum(1 for l in self._listings.values() if l.is_available()),
        }
