"""
ADX Agent Arena — Matching & Negotiation Engine

"Arena for agents, not an agent service."
Users bring their own agents (BYOAgent). The platform provides:
- Agent registry & discovery
- Resource listing & order book
- Matching engine with agent-aware scoring
- Negotiation protocol with rule validators
- Arena leaderboard & battle history (ELO ranking)
- Calibration tools (no fine-tuning required)

Modules:
  agent.py        — BYOAgent identity, registration, LLM config
  arena.py        — ELO ranking, leaderboard, battle records
  engine.py       — OrderBook, Intent, ResourceListing, matching
  negotiation.py  — State machine, proposal validation, Arena integration
  calibration.py  — Few-shot prompts, profiles, outcome feedback
  schemas.py      — A2A AgentCard extensions, discovery
  prompts/        — LLM prompt templates (buyer/seller)
"""

from .agent import (
    AgentRegistry, AgentRegistration, AgentStatus,
    LLMConfig, LLMProvider,
)
from .arena import (
    Arena, EloSystem, Tier,
    BattleRecord, BattleOutcome, Leaderboard, LeaderboardEntry,
)
from .engine import (
    OrderBook, Intent, CandidateMatch, ResourceListing,
    IntentType, AssetClass, NegotiationStyle,
    PriceConstraint, TradingRules,
)
from .negotiation import (
    NegotiationProtocol, NegotiationSession,
    NegotiationState, Proposal, ProposalType, ProposalValidator,
)
from .calibration import (
    NegotiationProfile, OutcomeLogger, ReviewEscalator,
    NegotiationOutcome, build_agent_context,
    PROPOSAL_JSON_SCHEMA, EVALUATION_JSON_SCHEMA, PROFILES,
)

__all__ = [
    # Agent
    "AgentRegistry", "AgentRegistration", "AgentStatus",
    "LLMConfig", "LLMProvider",
    # Arena
    "Arena", "EloSystem", "Tier",
    "BattleRecord", "BattleOutcome", "Leaderboard", "LeaderboardEntry",
    # Engine
    "OrderBook", "Intent", "CandidateMatch", "ResourceListing",
    "IntentType", "AssetClass", "NegotiationStyle",
    "PriceConstraint", "TradingRules",
    # Negotiation
    "NegotiationProtocol", "NegotiationSession",
    "NegotiationState", "Proposal", "ProposalType", "ProposalValidator",
    # Calibration
    "NegotiationProfile", "OutcomeLogger", "ReviewEscalator",
    "NegotiationOutcome", "build_agent_context",
    "PROPOSAL_JSON_SCHEMA", "EVALUATION_JSON_SCHEMA", "PROFILES",
]
