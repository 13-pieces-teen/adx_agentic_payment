"""
Agent Identity Layer — BYOAgent Registry

Users bring their own agents (BYOAgent) with their own LLM API keys.
The platform registers agents, validates their capabilities, and routes
negotiation calls to the user's LLM of choice.

This is the key architectural decision: we DON'T run LLM inference ourselves.
Each user's agent is their own LLM instance, configured by them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ============================================================
# LLM Provider Registry
# ============================================================


class LLMProvider(str, Enum):
    OPENAI = "openai"           # GPT-4o, GPT-4, etc.
    ANTHROPIC = "anthropic"     # Claude Opus, Sonnet, Haiku
    DEEPSEEK = "deepseek"       # DeepSeek V3, R1
    LOCAL = "local"             # Self-hosted (vLLM, Ollama, etc.)
    CUSTOM = "custom"           # Any OpenAI-compatible endpoint


@dataclass
class LLMConfig:
    """User's LLM configuration. API key is NOT stored in plaintext."""
    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4o"
    endpoint: str = ""                      # custom endpoint URL
    api_key_hmac: str = ""                  # HMAC of the API key (not the key itself)
    max_tokens: int = 4096
    temperature: float = 0.7

    def set_api_key(self, raw_key: str, secret: str = "adx-platform-secret"):
        """Store HMAC of API key, never the raw key."""
        self.api_key_hmac = hmac.new(
            secret.encode(), raw_key.encode(), hashlib.sha256
        ).hexdigest()

    def verify_api_key(self, raw_key: str, secret: str = "adx-platform-secret") -> bool:
        """Verify an API key matches the stored HMAC."""
        expected = hmac.new(
            secret.encode(), raw_key.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.api_key_hmac, expected)


# ============================================================
# Agent Registration
# ============================================================


class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    IN_BATTLE = "in_battle"     # currently negotiating
    SUSPENDED = "suspended"     # flagged for review


@dataclass
class AgentRegistration:
    """
    A registered agent on the ADX platform.

    Each agent belongs to one user. Users can have multiple agents
    with different strategies for different asset classes.
    """
    agent_id: str = field(default_factory=lambda: f"agent_{uuid.uuid4().hex[:12]}")
    owner_id: str = ""                      # user/human who owns this agent
    name: str = ""                          # human-readable name
    description: str = ""                   # what this agent does

    # LLM config (BYOAgent — user brings their own)
    llm: LLMConfig = field(default_factory=LLMConfig)

    # Negotiation personality
    negotiation_style: str = "balanced"     # aggressive | balanced | passive
    strategy_description: str = ""          # user's custom strategy notes

    # Capabilities — what can this agent trade?
    tradable_assets: list[str] = field(default_factory=list)  # ["compute", "data"]
    trade_direction: str = "both"           # buy | sell | both

    # Status
    status: AgentStatus = AgentStatus.OFFLINE
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    # Arena stats (populated by arena.py)
    elo_rating: float = 1000.0
    battles_fought: int = 0
    battles_won: int = 0
    total_earned: float = 0.0              # total value won through negotiation
    total_saved: float = 0.0               # total discount achieved (buy side)

    def win_rate(self) -> float:
        if self.battles_fought == 0:
            return 0.0
        return self.battles_won / self.battles_fought

    def to_public_dict(self) -> dict:
        """Public profile — NEVER includes API key or HMAC."""
        return {
            "agent_id": self.agent_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "llm_provider": self.llm.provider.value,
            "llm_model": self.llm.model,
            "negotiation_style": self.negotiation_style,
            "strategy_description": self.strategy_description,
            "tradable_assets": self.tradable_assets,
            "trade_direction": self.trade_direction,
            "status": self.status.value,
            "registered_at": self.registered_at,
            "elo_rating": self.elo_rating,
            "battles_fought": self.battles_fought,
            "battles_won": self.battles_won,
            "win_rate": round(self.win_rate(), 3),
            "total_earned": self.total_earned,
            "total_saved": self.total_saved,
        }


# ============================================================
# Agent Registry (In-Memory, → Persistent DB later)
# ============================================================


class AgentRegistry:
    """
    Registry of all agents on the platform.

    In production: backed by PostgreSQL.
    For hackathon: in-memory dict.
    """

    def __init__(self):
        self._agents: dict[str, AgentRegistration] = {}
        self._owner_agents: dict[str, list[str]] = {}  # owner_id → [agent_ids]

    # ---- CRUD ----

    def register(self, agent: AgentRegistration) -> str:
        """Register a new agent. Returns agent_id."""
        if agent.agent_id in self._agents:
            raise ValueError(f"Agent {agent.agent_id} already registered")

        agent.registered_at = time.time()
        self._agents[agent.agent_id] = agent
        self._owner_agents.setdefault(agent.owner_id, []).append(agent.agent_id)
        return agent.agent_id

    def unregister(self, agent_id: str) -> bool:
        """Remove an agent."""
        agent = self._agents.pop(agent_id, None)
        if agent and agent.owner_id in self._owner_agents:
            self._owner_agents[agent.owner_id].remove(agent_id)
            return True
        return False

    def get(self, agent_id: str) -> Optional[AgentRegistration]:
        return self._agents.get(agent_id)

    def list_by_owner(self, owner_id: str) -> list[AgentRegistration]:
        ids = self._owner_agents.get(owner_id, [])
        return [self._agents[i] for i in ids if i in self._agents]

    # ---- Discovery ----

    def find_by_asset(
        self, asset_class: str, direction: str = "both", min_elo: float = 0
    ) -> list[AgentRegistration]:
        """Find agents capable of trading a specific asset class."""
        results = []
        for agent in self._agents.values():
            if agent.status == AgentStatus.SUSPENDED:
                continue
            if asset_class not in agent.tradable_assets:
                continue
            if direction != "both" and agent.trade_direction != "both":
                if agent.trade_direction != direction:
                    continue
            if agent.elo_rating < min_elo:
                continue
            results.append(agent)
        return sorted(results, key=lambda a: a.elo_rating, reverse=True)

    def find_counterparties(
        self, agent: AgentRegistration, asset_class: str
    ) -> list[AgentRegistration]:
        """Find agents who can trade with this agent (opposite direction)."""
        opposite = "sell" if agent.trade_direction == "buy" else "buy"
        if agent.trade_direction == "both":
            opposite = "both"
        return self.find_by_asset(asset_class, direction=opposite)

    # ---- Status ----

    def set_status(self, agent_id: str, status: AgentStatus):
        agent = self._agents.get(agent_id)
        if agent:
            agent.status = status
            agent.last_seen = time.time()

    def heartbeat(self, agent_id: str):
        """Update last_seen timestamp."""
        agent = self._agents.get(agent_id)
        if agent:
            agent.last_seen = time.time()
            if agent.status == AgentStatus.OFFLINE:
                agent.status = AgentStatus.ONLINE

    def cleanup_stale(self, max_age_seconds: int = 3600) -> int:
        """Mark agents offline if no heartbeat. Returns count."""
        now = time.time()
        count = 0
        for agent in self._agents.values():
            if agent.status == AgentStatus.ONLINE:
                if now - agent.last_seen > max_age_seconds:
                    agent.status = AgentStatus.OFFLINE
                    count += 1
        return count

    @property
    def stats(self) -> dict:
        online = sum(1 for a in self._agents.values() if a.status == AgentStatus.ONLINE)
        in_battle = sum(1 for a in self._agents.values() if a.status == AgentStatus.IN_BATTLE)
        return {
            "total_agents": len(self._agents),
            "online": online,
            "in_battle": in_battle,
            "offline": len(self._agents) - online - in_battle,
        }
