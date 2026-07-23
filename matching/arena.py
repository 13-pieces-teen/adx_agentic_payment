"""
Agent Arena — Gamification & Competitive Layer

The "电子斗蛐蛐" (digital cockfighting) mechanic:
- Agents compete in negotiations
- ELO ranking determines the best negotiators
- Leaderboard drives engagement
- Battle history for spectators

Better agent → better negotiation → better prices → higher rank → bragging rights
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .agent import AgentRegistration, AgentStatus


# ============================================================
# ELO Rating System
# ============================================================


class Tier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    DIAMOND = "diamond"
    MASTER = "master"

    @classmethod
    def from_elo(cls, elo: float) -> "Tier":
        if elo >= 1700:
            return cls.MASTER
        if elo >= 1500:
            return cls.DIAMOND
        if elo >= 1300:
            return cls.GOLD
        if elo >= 1100:
            return cls.SILVER
        return cls.BRONZE


class EloSystem:
    """
    Standard ELO rating system adapted for negotiation battles.

    K-factor is dynamic: higher for new agents (uncertain rating),
    lower for veterans (stable rating).
    """

    BASE_K = 32
    PLACEMENT_BATTLES = 20  # first N battles use higher K

    @staticmethod
    def expected_score(rating_a: float, rating_b: float) -> float:
        """Probability that A beats B."""
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))

    @classmethod
    def k_factor(cls, battles_fought: int) -> float:
        """Dynamic K-factor: high for new agents, low for veterans."""
        if battles_fought < cls.PLACEMENT_BATTLES:
            return cls.BASE_K * 2  # 64 for placement
        return cls.BASE_K  # 32 for ranked

    @classmethod
    def update(
        cls,
        winner_elo: float,
        loser_elo: float,
        winner_battles: int,
        loser_battles: int,
        is_draw: bool = False,
    ) -> tuple[float, float, float, float]:
        """
        Calculate new ELO ratings after a battle.

        Returns: (new_winner_elo, new_loser_elo, winner_delta, loser_delta)
        """
        expected_winner = cls.expected_score(winner_elo, loser_elo)

        if is_draw:
            actual_winner = 0.5
            actual_loser = 0.5
        else:
            actual_winner = 1.0
            actual_loser = 0.0

        k_winner = cls.k_factor(winner_battles)
        k_loser = cls.k_factor(loser_battles)

        winner_delta = k_winner * (actual_winner - expected_winner)
        loser_delta = k_loser * (actual_loser - (1.0 - expected_winner))

        new_winner = winner_elo + winner_delta
        new_loser = loser_elo + loser_delta

        return new_winner, new_loser, winner_delta, loser_delta


# ============================================================
# Battle Record
# ============================================================


class BattleOutcome(str, Enum):
    BUYER_WIN = "buyer_win"        # buyer got price below their ideal
    SELLER_WIN = "seller_win"      # seller got price above their ideal
    DRAW = "draw"                  # price at midpoint
    BUYER_SURRENDER = "buyer_surrender"
    SELLER_SURRENDER = "seller_surrender"
    TIMEOUT = "timeout"


@dataclass
class BattleRecord:
    """
    Immutable record of one negotiation battle between two agents.

    This is the "combat log" that appears on the Arena feed.
    """
    battle_id: str = field(default_factory=lambda: f"battle_{uuid.uuid4().hex[:10]}")
    session_id: str = ""                    # links to NegotiationSession
    asset_class: str = ""
    description: str = ""                   # what was being traded

    # Combatants
    agent_a_id: str = ""                    # buyer
    agent_b_id: str = ""                    # seller
    agent_a_name: str = ""
    agent_b_name: str = ""

    # Result
    outcome: BattleOutcome = BattleOutcome.DRAW
    winner_agent_id: str = ""               # empty if draw
    final_price: float = 0.0
    currency: str = "INJ"
    quantity: int = 1
    total_value: float = 0.0               # final_price * quantity

    # Negotiation stats
    rounds_taken: int = 0
    duration_seconds: float = 0.0

    # Price efficiency (how close to respective ideals)
    buyer_ideal: float = 0.0
    seller_ideal: float = 0.0
    buyer_savings_pct: float = 0.0          # (ideal - final) / ideal for buyer
    seller_premium_pct: float = 0.0         # (final - ideal) / ideal for seller

    # ELO changes
    agent_a_elo_before: float = 1000.0
    agent_a_elo_after: float = 1000.0
    agent_a_elo_delta: float = 0.0
    agent_b_elo_before: float = 1000.0
    agent_b_elo_after: float = 1000.0
    agent_b_elo_delta: float = 0.0

    # Timestamps
    started_at: float = field(default_factory=time.time)
    ended_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict:
        return {
            "battle_id": self.battle_id,
            "session_id": self.session_id,
            "asset_class": self.asset_class,
            "description": self.description,
            "agent_a_id": self.agent_a_id,
            "agent_b_id": self.agent_b_id,
            "agent_a_name": self.agent_a_name,
            "agent_b_name": self.agent_b_name,
            "outcome": self.outcome.value,
            "winner_agent_id": self.winner_agent_id,
            "final_price": self.final_price,
            "currency": self.currency,
            "quantity": self.quantity,
            "total_value": self.total_value,
            "rounds_taken": self.rounds_taken,
            "duration_seconds": self.duration_seconds,
            "agent_a_elo_delta": round(self.agent_a_elo_delta, 1),
            "agent_b_elo_delta": round(self.agent_b_elo_delta, 1),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


# ============================================================
# Leaderboard
# ============================================================


@dataclass
class LeaderboardEntry:
    """One row on the leaderboard."""
    rank: int
    agent_id: str
    agent_name: str
    owner_id: str
    elo: float
    tier: Tier
    battles_fought: int
    battles_won: int
    win_rate: float
    total_earned: float
    total_saved: float
    recent_form: list[str] = field(default_factory=list)  # last 5 results: "W"/"L"/"D"


class Leaderboard:
    """
    Arena leaderboard with multiple views.

    Views:
    - Global: all agents ranked by ELO
    - By asset class: GPU masters, Data kings, etc.
    - Weekly/monthly: time-gated rankings for fresh competition
    - Rising stars: biggest ELO gainers this week
    """

    def __init__(self):
        self._battles: list[BattleRecord] = []

    def record_battle(self, battle: BattleRecord):
        self._battles.append(battle)

    def rank(
        self,
        agents: dict[str, AgentRegistration],
        asset_class: str = "",
        min_battles: int = 0,
        limit: int = 50,
    ) -> list[LeaderboardEntry]:
        """
        Generate ranked leaderboard from agent registry.

        Args:
            agents: dict of agent_id → AgentRegistration
            asset_class: filter by asset class, empty = all
            min_battles: minimum battles to qualify
            limit: top N entries
        """
        # Filter agents
        eligible = []
        for agent in agents.values():
            if agent.battles_fought < min_battles:
                continue
            if asset_class and asset_class not in agent.tradable_assets:
                continue
            eligible.append(agent)

        # Sort by ELO descending
        eligible.sort(key=lambda a: a.elo_rating, reverse=True)

        # Build entries
        entries = []
        for i, agent in enumerate(eligible[:limit]):
            recent = self._recent_form(agent.agent_id, n=5)
            entries.append(LeaderboardEntry(
                rank=i + 1,
                agent_id=agent.agent_id,
                agent_name=agent.name,
                owner_id=agent.owner_id,
                elo=round(agent.elo_rating, 1),
                tier=Tier.from_elo(agent.elo_rating),
                battles_fought=agent.battles_fought,
                battles_won=agent.battles_won,
                win_rate=round(agent.win_rate(), 3),
                total_earned=round(agent.total_earned, 2),
                total_saved=round(agent.total_saved, 2),
                recent_form=recent,
            ))
        return entries

    def rising_stars(
        self, agents: dict[str, AgentRegistration], top_n: int = 10
    ) -> list[dict]:
        """Agents with biggest ELO gain in recent period."""
        # Get ELO changes from recent battles (last 7 days)
        now = time.time()
        seven_days_ago = now - 7 * 86400

        elo_gains: dict[str, float] = {}
        for battle in self._battles:
            if battle.ended_at < seven_days_ago:
                continue
            elo_gains[battle.agent_a_id] = elo_gains.get(battle.agent_a_id, 0) + battle.agent_a_elo_delta
            elo_gains[battle.agent_b_id] = elo_gains.get(battle.agent_b_id, 0) + battle.agent_b_elo_delta

        # Sort by gain
        rising = sorted(elo_gains.items(), key=lambda x: x[1], reverse=True)
        result = []
        for agent_id, gain in rising[:top_n]:
            agent = agents.get(agent_id)
            if agent:
                result.append({
                    "agent_id": agent_id,
                    "agent_name": agent.name,
                    "elo_gain": round(gain, 1),
                    "current_elo": round(agent.elo_rating, 1),
                    "tier": Tier.from_elo(agent.elo_rating).value,
                })
        return result

    def battle_feed(self, limit: int = 20) -> list[dict]:
        """Recent battles for the spectator feed."""
        recent = sorted(self._battles, key=lambda b: b.ended_at, reverse=True)[:limit]
        return [b.to_public_dict() for b in recent]

    def agent_history(self, agent_id: str, limit: int = 20) -> list[dict]:
        """Battle history for a specific agent."""
        battles = [
            b for b in self._battles
            if b.agent_a_id == agent_id or b.agent_b_id == agent_id
        ]
        battles.sort(key=lambda b: b.ended_at, reverse=True)
        return [b.to_public_dict() for b in battles[:limit]]

    def _recent_form(self, agent_id: str, n: int = 5) -> list[str]:
        """Last N results: 'W' / 'L' / 'D'."""
        battles = [
            b for b in self._battles
            if b.agent_a_id == agent_id or b.agent_b_id == agent_id
        ]
        battles.sort(key=lambda b: b.ended_at, reverse=True)

        form = []
        for b in battles[:n]:
            if b.outcome in (BattleOutcome.DRAW, BattleOutcome.TIMEOUT):
                form.append("D")
            elif b.winner_agent_id == agent_id:
                form.append("W")
            else:
                form.append("L")
        return form

    @property
    def stats(self) -> dict:
        total = len(self._battles)
        if total == 0:
            return {"total_battles": 0}

        draws = sum(1 for b in self._battles if b.outcome == BattleOutcome.DRAW)
        avg_rounds = sum(b.rounds_taken for b in self._battles) / total
        avg_value = sum(b.total_value for b in self._battles) / total

        return {
            "total_battles": total,
            "win_rate_pct": round((total - draws) / total * 100, 1) if total else 0,
            "draw_rate_pct": round(draws / total * 100, 1) if total else 0,
            "avg_rounds": round(avg_rounds, 1),
            "avg_trade_value": round(avg_value, 2),
            "total_volume": round(sum(b.total_value for b in self._battles), 2),
        }


# ============================================================
# Arena — Main Facade
# ============================================================


class Arena:
    """
    The Arena is where agents battle.

    It ties together:
    - Agent registry (who's fighting)
    - ELO system (how they're ranked)
    - Leaderboard (who's winning)
    - Battle records (the combat log)
    """

    def __init__(self, registry: "AgentRegistry"):  # type: ignore
        self.registry = registry
        self.elo_system = EloSystem()
        self.leaderboard = Leaderboard()

        # Active battles being spectated
        self._active_battles: dict[str, BattleRecord] = {}

    def start_battle(
        self,
        buyer_agent: AgentRegistration,
        seller_agent: AgentRegistration,
        session_id: str,
        asset_class: str,
        description: str,
        buyer_ideal: float,
        seller_ideal: float,
        quantity: int = 1,
        currency: str = "INJ",
    ) -> BattleRecord:
        """Called when a negotiation session begins."""
        battle = BattleRecord(
            session_id=session_id,
            asset_class=asset_class,
            description=description,
            agent_a_id=buyer_agent.agent_id,
            agent_b_id=seller_agent.agent_id,
            agent_a_name=buyer_agent.name,
            agent_b_name=seller_agent.name,
            agent_a_elo_before=buyer_agent.elo_rating,
            agent_b_elo_before=seller_agent.elo_rating,
            buyer_ideal=buyer_ideal,
            seller_ideal=seller_ideal,
            quantity=quantity,
            currency=currency,
            started_at=time.time(),
        )

        self._active_battles[session_id] = battle

        # Mark agents as in battle
        self.registry.set_status(buyer_agent.agent_id, AgentStatus.IN_BATTLE)
        self.registry.set_status(seller_agent.agent_id, AgentStatus.IN_BATTLE)

        return battle

    def end_battle(
        self,
        session_id: str,
        outcome: BattleOutcome,
        final_price: float,
        rounds_taken: int,
        winner_agent_id: str = "",
    ) -> Optional[BattleRecord]:
        """
        Called when a negotiation ends.

        Updates ELO ratings, battle record, agent stats, and leaderboard.
        """
        battle = self._active_battles.pop(session_id, None)
        if not battle:
            return None

        battle.ended_at = time.time()
        battle.duration_seconds = battle.ended_at - battle.started_at
        battle.outcome = outcome
        battle.winner_agent_id = winner_agent_id
        battle.final_price = final_price
        battle.rounds_taken = rounds_taken
        battle.total_value = final_price * battle.quantity

        # Calculate price efficiency
        if battle.buyer_ideal > 0:
            battle.buyer_savings_pct = round(
                (battle.buyer_ideal - final_price) / battle.buyer_ideal * 100, 1
            )
        if battle.seller_ideal > 0:
            battle.seller_premium_pct = round(
                (final_price - battle.seller_ideal) / battle.seller_ideal * 100, 1
            )

        # Get agents
        buyer = self.registry.get(battle.agent_a_id)
        seller = self.registry.get(battle.agent_b_id)

        if buyer and seller:
            # Determine winner/loser for ELO
            is_draw = outcome in (BattleOutcome.DRAW, BattleOutcome.TIMEOUT)
            winner_elo = loser_elo = 0
            winner_battles = loser_battles = 0

            if winner_agent_id == buyer.agent_id:
                winner_elo, loser_elo = buyer.elo_rating, seller.elo_rating
                winner_battles, loser_battles = buyer.battles_fought, seller.battles_fought
            elif winner_agent_id == seller.agent_id:
                winner_elo, loser_elo = seller.elo_rating, buyer.elo_rating
                winner_battles, loser_battles = seller.battles_fought, buyer.battles_fought
            else:
                # Draw
                winner_elo, loser_elo = buyer.elo_rating, seller.elo_rating
                winner_battles, loser_battles = buyer.battles_fought, seller.battles_fought

            new_winner_elo, new_loser_elo, winner_delta, loser_delta = self.elo_system.update(
                winner_elo, loser_elo, winner_battles, loser_battles, is_draw=is_draw
            )

            # Update ELO
            if winner_agent_id == buyer.agent_id:
                buyer.elo_rating = new_winner_elo
                seller.elo_rating = new_loser_elo
                battle.agent_a_elo_delta = winner_delta
                battle.agent_b_elo_delta = loser_delta
            elif winner_agent_id == seller.agent_id:
                seller.elo_rating = new_winner_elo
                buyer.elo_rating = new_loser_elo
                battle.agent_a_elo_delta = loser_delta
                battle.agent_b_elo_delta = winner_delta
            else:
                # Draw — both get small adjustment
                buyer.elo_rating = new_winner_elo
                seller.elo_rating = new_loser_elo
                battle.agent_a_elo_delta = winner_delta
                battle.agent_b_elo_delta = loser_delta

            battle.agent_a_elo_after = buyer.elo_rating
            battle.agent_b_elo_after = seller.elo_rating

            # Update battle counts
            buyer.battles_fought += 1
            seller.battles_fought += 1

            if winner_agent_id == buyer.agent_id:
                buyer.battles_won += 1
                buyer.total_saved += battle.buyer_savings_pct / 100 * final_price
            elif winner_agent_id == seller.agent_id:
                seller.battles_won += 1
                seller.total_earned += battle.seller_premium_pct / 100 * final_price

            # Mark agents online again
            self.registry.set_status(buyer.agent_id, AgentStatus.ONLINE)
            self.registry.set_status(seller.agent_id, AgentStatus.ONLINE)

        # Record in leaderboard
        self.leaderboard.record_battle(battle)

        return battle

    def get_active_battles(self) -> list[dict]:
        """Get all currently active battles (for spectator feed)."""
        return [
            {
                "battle_id": b.battle_id,
                "session_id": b.session_id,
                "agent_a_name": b.agent_a_name,
                "agent_b_name": b.agent_b_name,
                "asset_class": b.asset_class,
                "description": b.description,
                "started_at": b.started_at,
                "duration_seconds": time.time() - b.started_at,
            }
            for b in self._active_battles.values()
        ]
