"""
ADX Agent Arena — REST API (FastAPI)

Endpoints for the web frontend:
- Agent registration & management
- Resource listings
- Arena leaderboard & battle feed
- Negotiation lifecycle
"""

import uuid
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from matching.agent import AgentRegistry, AgentRegistration, AgentStatus, LLMConfig, LLMProvider
from matching.engine import (
    OrderBook, Intent, IntentType, AssetClass,
    PriceConstraint, TradingRules, ResourceListing,
)
from matching.arena import Arena, Tier
from matching.negotiation import NegotiationProtocol
from matching.calibration import OutcomeLogger, ReviewEscalator


# ============================================================
# Pydantic Request/Response Models
# ============================================================


class RegisterAgentRequest(BaseModel):
    name: str
    description: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    negotiation_style: str = "balanced"
    strategy_description: str = ""
    tradable_assets: list[str] = Field(default_factory=list)
    trade_direction: str = "both"


class CreateListingRequest(BaseModel):
    seller_agent_id: str
    asset_class: str
    title: str
    description: str = ""
    quantity: int = 1
    unit: str = "hour"
    min_price: float = 0.0
    ideal_price: float = 0.0
    max_price: float = float("inf")
    currency: str = "INJ"
    tags: list[str] = Field(default_factory=list)
    ttl_hours: float = 24.0


class CreateIntentRequest(BaseModel):
    agent_id: str
    intent_type: str  # "buy" | "sell"
    asset_class: str
    description: str = ""
    quantity: int = 1
    min_price: float = 0.0
    ideal_price: float = 0.0
    max_price: float = float("inf")
    currency: str = "INJ"
    tags: list[str] = Field(default_factory=list)
    listing_id: str = ""  # for sell intents
    max_rounds: int = 5
    auto_accept_pct: float = 5.0


class SubmitProposalRequest(BaseModel):
    session_id: str
    agent_id: str
    price: float
    quantity: int = 1
    message: str = ""
    proposal_type: str = "initial_offer"  # initial_offer | counter | final_offer


# ============================================================
# App Factory
# ============================================================


def create_app() -> FastAPI:
    app = FastAPI(
        title="ADX Agent Arena",
        description="Agent-to-Agent Resource Trading Platform",
        version="0.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Singletons (in production: proper DI container) ----
    registry = AgentRegistry()
    orderbook = OrderBook()
    arena = Arena(registry)
    negotiation = NegotiationProtocol(arena=arena)
    outcome_logger = OutcomeLogger()
    escalator = ReviewEscalator()

    orderbook.configure(registry)

    # ========================================================
    # Agent Endpoints
    # ========================================================

    @app.post("/api/agents/register")
    async def register_agent(req: RegisterAgentRequest):
        """Register a new agent (BYOAgent)."""
        agent = AgentRegistration(
            owner_id=f"user_{uuid.uuid4().hex[:8]}",  # TODO: real auth
            name=req.name,
            description=req.description,
            llm=LLMConfig(
                provider=LLMProvider(req.llm_provider),
                model=req.llm_model,
            ),
            negotiation_style=req.negotiation_style,
            strategy_description=req.strategy_description,
            tradable_assets=req.tradable_assets,
            trade_direction=req.trade_direction,
            status=AgentStatus.ONLINE,
        )
        agent_id = registry.register(agent)
        return {"agent_id": agent_id, **agent.to_public_dict()}

    @app.get("/api/agents")
    async def list_agents(
        asset_class: str = "",
        min_elo: float = 0,
    ):
        """List registered agents, optionally filtered."""
        if asset_class:
            agents = registry.find_by_asset(asset_class, min_elo=min_elo)
        else:
            agents = list(registry._agents.values())
        return {
            "total": len(agents),
            "agents": [a.to_public_dict() for a in agents],
        }

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        agent = registry.get(agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent.to_public_dict()

    @app.post("/api/agents/{agent_id}/heartbeat")
    async def agent_heartbeat(agent_id: str):
        registry.heartbeat(agent_id)
        return {"status": "ok"}

    # ========================================================
    # Listing Endpoints
    # ========================================================

    @app.post("/api/listings")
    async def create_listing(req: CreateListingRequest):
        """Publish a resource listing."""
        agent = registry.get(req.seller_agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")

        listing = ResourceListing(
            seller_agent_id=req.seller_agent_id,
            seller_name=agent.name,
            asset_class=AssetClass(req.asset_class),
            title=req.title,
            description=req.description,
            quantity=req.quantity,
            unit=req.unit,
            price_per_unit=PriceConstraint(
                currency=req.currency,
                min_acceptable=req.min_price,
                ideal=req.ideal_price,
                max_acceptable=req.max_price,
            ),
            available_until=time.time() + req.ttl_hours * 3600,
            tags=req.tags,
        )
        listing_id = orderbook.publish_listing(listing)
        return {"listing_id": listing_id, **listing.to_public_dict()}

    @app.get("/api/listings")
    async def get_listings(
        asset_class: str = "",
        available_only: bool = True,
    ):
        listings = orderbook.get_listings(asset_class, available_only)
        return {
            "total": len(listings),
            "listings": [l.to_public_dict() for l in listings],
        }

    # ========================================================
    # Intent & Matching Endpoints
    # ========================================================

    @app.post("/api/intents")
    async def create_intent(req: CreateIntentRequest):
        """Post a buy or sell intent."""
        agent = registry.get(req.agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")

        intent = Intent(
            agent_id=req.agent_id,
            agent_name=agent.name,
            intent_type=IntentType(req.intent_type),
            asset_class=AssetClass(req.asset_class),
            description=req.description,
            quantity=req.quantity,
            price=PriceConstraint(
                currency=req.currency,
                min_acceptable=req.min_price,
                ideal=req.ideal_price,
                max_acceptable=req.max_price,
            ),
            rules=TradingRules(
                max_negotiation_rounds=req.max_rounds,
                auto_accept_threshold_pct=req.auto_accept_pct,
            ),
            negotiation_style=agent.negotiation_style,
            tags=req.tags,
            listing_id=req.listing_id,
        )
        intent_id = orderbook.publish(intent)

        # Find matches immediately
        matches = orderbook.find_matches(intent, top_k=5)

        return {
            "intent_id": intent_id,
            "intent_type": intent.intent_type.value,
            "matches_found": len(matches),
            "matches": [
                {
                    "match_id": m.match_id,
                    "score": round(m.score, 1),
                    "price_zone": [round(m.price_zone[0], 2), round(m.price_zone[1], 2)],
                    "counterparty_agent": m.sell_intent.agent_name if intent.intent_type == IntentType.BUY else m.buy_intent.agent_name,
                    "counterparty_elo": round(m.seller_elo if intent.intent_type == IntentType.BUY else m.buyer_elo, 1),
                }
                for m in matches
            ],
        }

    @app.get("/api/intents/{intent_id}/matches")
    async def get_matches(intent_id: str, top_k: int = 10):
        intent = orderbook.get(intent_id)
        if not intent:
            raise HTTPException(404, "Intent not found")
        matches = orderbook.find_matches(intent, top_k=top_k)
        return {
            "intent_id": intent_id,
            "matches": [
                {
                    "match_id": m.match_id,
                    "score": round(m.score, 1),
                    "price_zone": [round(m.price_zone[0], 2), round(m.price_zone[1], 2)],
                    "buyer": m.buy_intent.agent_name,
                    "seller": m.sell_intent.agent_name,
                    "buyer_elo": round(m.buyer_elo, 1),
                    "seller_elo": round(m.seller_elo, 1),
                }
                for m in matches
            ],
        }

    # ========================================================
    # Negotiation Endpoints
    # ========================================================

    @app.post("/api/negotiations/start")
    async def start_negotiation(match_id: str):
        """Start a negotiation session from a match."""
        # Find the match (simplified — in production, store matches)
        # For now: create session directly from intents
        all_matches = orderbook.find_all_matches()
        match = next((m for m in all_matches if m.match_id == match_id), None)
        if not match:
            raise HTTPException(404, "Match not found or expired")

        session = negotiation.create_session(match)
        negotiation.link_arena(session)

        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "buyer_agent_id": session.buyer.agent_id,
            "seller_agent_id": session.seller.agent_id,
            "price_zone": list(session.match.price_zone),
            "max_rounds": session.max_rounds,
        }

    @app.post("/api/negotiations/propose")
    async def submit_proposal(req: SubmitProposalRequest):
        """Submit a negotiation proposal."""
        session = negotiation.get_session(req.session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        # Determine which intent this agent is using
        if req.agent_id == session.buyer.agent_id:
            sender = session.buyer
        elif req.agent_id == session.seller.agent_id:
            sender = session.seller
        else:
            raise HTTPException(403, "Agent not part of this negotiation")

        from matching.negotiation import Proposal, ProposalType
        proposal = Proposal(
            proposal_type=ProposalType(req.proposal_type),
            price=req.price,
            quantity=req.quantity,
            message=req.message,
            sender_intent_id=sender.intent_id,
        )

        result = negotiation.process_proposal(session, proposal, sender)
        return result

    @app.post("/api/negotiations/{session_id}/accept")
    async def accept_negotiation(session_id: str, agent_id: str):
        session = negotiation.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        if agent_id == session.buyer.agent_id:
            accepter = session.buyer
        elif agent_id == session.seller.agent_id:
            accepter = session.seller
        else:
            raise HTTPException(403, "Agent not part of this negotiation")

        return negotiation.accept(session, accepter)

    @app.post("/api/negotiations/{session_id}/reject")
    async def reject_negotiation(session_id: str, agent_id: str, reason: str = ""):
        session = negotiation.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        if agent_id == session.buyer.agent_id:
            rejecter = session.buyer
        elif agent_id == session.seller.agent_id:
            rejecter = session.seller
        else:
            raise HTTPException(403, "Agent not part of this negotiation")

        return negotiation.reject(session, rejecter, reason)

    @app.get("/api/negotiations/{session_id}")
    async def get_negotiation(session_id: str):
        session = negotiation.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        last = session.last_proposal()
        return {
            "session_id": session.session_id,
            "state": session.state.value,
            "round": session.current_round,
            "max_rounds": session.max_rounds,
            "last_price": last.price if last else None,
            "last_message": last.message if last else None,
            "buyer_agent": session.buyer.agent_name,
            "seller_agent": session.seller.agent_name,
            "price_zone": list(session.match.price_zone),
            "started_at": session.started_at,
            "duration": time.time() - session.started_at,
        }

    # ========================================================
    # Arena Endpoints (the "电子斗蛐蛐" spectator view)
    # ========================================================

    @app.get("/api/arena/leaderboard")
    async def get_leaderboard(
        asset_class: str = "",
        min_battles: int = 0,
        limit: int = 50,
    ):
        entries = arena.leaderboard.rank(
            registry._agents, asset_class, min_battles, limit
        )
        return {
            "total": len(entries),
            "leaderboard": [
                {
                    "rank": e.rank,
                    "agent_name": e.agent_name,
                    "elo": e.elo,
                    "tier": e.tier.value,
                    "battles": e.battles_fought,
                    "win_rate": round(e.win_rate * 100, 1),
                    "recent_form": e.recent_form,
                }
                for e in entries
            ],
        }

    @app.get("/api/arena/battles")
    async def get_battle_feed(limit: int = 20):
        battles = arena.leaderboard.battle_feed(limit)
        return {"total": len(battles), "battles": battles}

    @app.get("/api/arena/battles/active")
    async def get_active_battles():
        """Spectator: watch live negotiations."""
        return {"active": arena.get_active_battles()}

    @app.get("/api/arena/agents/{agent_id}/history")
    async def get_agent_history(agent_id: str, limit: int = 20):
        history = arena.leaderboard.agent_history(agent_id, limit)
        agent = registry.get(agent_id)
        return {
            "agent_id": agent_id,
            "agent_name": agent.name if agent else "unknown",
            "elo": agent.elo_rating if agent else 0,
            "tier": Tier.from_elo(agent.elo_rating).value if agent else "bronze",
            "battles": agent.battles_fought if agent else 0,
            "win_rate": round(agent.win_rate() * 100, 1) if agent else 0,
            "history": history,
        }

    @app.get("/api/arena/rising")
    async def get_rising_stars(top_n: int = 10):
        rising = arena.leaderboard.rising_stars(registry._agents, top_n)
        return {"rising_stars": rising}

    @app.get("/api/arena/stats")
    async def get_arena_stats():
        return {
            "agents": registry.stats,
            "orderbook": orderbook.stats,
            "battles": arena.leaderboard.stats,
        }

    # ========================================================
    # Health
    # ========================================================

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.2.0",
            "agents": registry.stats,
            "listings": orderbook.stats,
            "battles": arena.leaderboard.stats,
        }

    return app


# ============================================================
# Run with: uvicorn web.api:create_app --factory --reload
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.api:create_app", factory=True, reload=True, port=8000)
