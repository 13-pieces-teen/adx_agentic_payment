"""
ADX Agent Arena — REST API (FastAPI)

Endpoints for the web frontend:
- Agent registration & management
- Resource listings
- Arena leaderboard & battle feed
- Negotiation lifecycle
"""

import time
import ipaddress
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from matching.agent import (
    AgentRegistry,
    AgentRegistration,
    AgentStatus,
    LLMConfig,
    LLMProvider,
)
from matching.engine import (
    OrderBook,
    Intent,
    IntentType,
    AssetClass,
    PriceConstraint,
    TradingRules,
    ResourceListing,
)
from matching.arena import Arena, Tier
from matching.negotiation import NegotiationProtocol
from matching.calibration import OutcomeLogger, ReviewEscalator
from connector_gateway import (
    ConnectorGateway,
    ProductionConnectorBundle,
    build_production_connector,
    create_connector_router,
)
from connector_gateway.auth import AuthError, AuthPrincipal


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


def _is_loopback_client(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    host = str(client[0]).split("%", 1)[0]
    # Starlette's in-process TestClient uses this sentinel; an ASGI server
    # supplies the real peer address and cannot be overridden by HTTP headers.
    if host == "testclient":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _ConnectorLoopbackOnlyMiddleware:
    """Fail closed when the unauthenticated demo plane is reached remotely."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_connector_request = scope["type"] in {"http", "websocket"} and scope.get(
            "path", ""
        ).startswith("/api/connectors")
        if not is_connector_request or _is_loopback_client(scope):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            response = JSONResponse(
                {"detail": "Unsafe Connector demo is restricted to loopback clients"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        await send(
            {
                "type": "websocket.close",
                # Before websocket.accept this rejects the upgrade. Uvicorn
                # exposes it to the caller as an HTTP 403 handshake.
                "code": 1008,
                "reason": "Unsafe Connector demo is restricted to loopback clients",
            }
        )


def _mount_connector_gateway(
    app: FastAPI, connector_demo_enabled: Optional[bool]
) -> None:
    """Mount the unauthenticated MVP control plane only after an explicit opt-in.

    The repository does not yet have a shared FastAPI/Supabase identity verifier,
    so exposing these routes on a remotely reachable app would let callers drive
    a paired local runtime. Production must replace this guard with authenticated
    tenant/object authorization.
    """

    if connector_demo_enabled is None:
        connector_demo_enabled = os.getenv(
            "ADX_CONNECTOR_UNSAFE_DEMO", ""
        ).strip().lower() in {"1", "true", "yes"}
    app.state.connector_gateway_enabled = connector_demo_enabled
    if not connector_demo_enabled:
        return
    app.add_middleware(_ConnectorLoopbackOnlyMiddleware)
    connector_gateway = ConnectorGateway()
    app.state.connector_gateway = connector_gateway
    app.include_router(create_connector_router(connector_gateway))


def _production_connector_enabled(
    connector_demo_enabled: Optional[bool],
) -> bool:
    if connector_demo_enabled is not None:
        return False
    connector_mode = os.getenv("ADX_CONNECTOR_MODE", "").strip().lower()
    environment = os.getenv("ADX_ENV", "").strip().lower()
    return connector_mode == "production" or environment == "production"


def _allowed_origins(production: bool) -> list[str]:
    configured = [
        value.strip().rstrip("/")
        for value in os.getenv("ADX_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    ]
    if configured:
        if production and "*" in configured:
            raise RuntimeError("ADX_ALLOWED_ORIGINS must not contain '*' in production")
        return configured

    public_app_url = os.getenv("ADX_PUBLIC_APP_URL", "").strip()
    if public_app_url:
        parsed = urlsplit(public_app_url)
        if parsed.scheme and parsed.netloc:
            return [f"{parsed.scheme}://{parsed.netloc}"]
    if production:
        raise RuntimeError(
            "ADX_ALLOWED_ORIGINS or ADX_PUBLIC_APP_URL is required in production"
        )
    return ["http://localhost:3000", "http://127.0.0.1:3000"]


def create_app(connector_demo_enabled: Optional[bool] = None) -> FastAPI:
    production_connector = _production_connector_enabled(connector_demo_enabled)
    connector_bundle: ProductionConnectorBundle | None = None
    if production_connector:
        # This validates every security-sensitive production setting before
        # the process starts accepting traffic.
        connector_bundle = build_production_connector()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if connector_bundle is not None:
            await connector_bundle.initialize()
        try:
            yield
        finally:
            if connector_bundle is not None:
                await connector_bundle.close()

    app = FastAPI(
        title="ADX Agent Arena",
        description="Agent-to-Agent Resource Trading Platform",
        version="0.2.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(production_connector),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-CSRF-Token"],
    )

    if connector_bundle is not None:
        app.state.connector_gateway_enabled = True
        app.state.connector_gateway_mode = "production"
        app.state.connector_gateway = connector_bundle.service
        app.state.connector_auth = connector_bundle.auth
        app.include_router(connector_bundle.router)
    else:
        _mount_connector_gateway(app, connector_demo_enabled)
        app.state.connector_gateway_mode = (
            "demo" if app.state.connector_gateway_enabled else "off"
        )

    # ---- Singletons (in production: proper DI container) ----
    registry = AgentRegistry()
    orderbook = OrderBook()
    arena = Arena(registry)
    negotiation = NegotiationProtocol(arena=arena)
    outcome_logger = OutcomeLogger()
    escalator = ReviewEscalator()

    orderbook.configure(registry)

    async def require_arena_principal(
        request: Request,
        *,
        csrf: bool = False,
    ) -> AuthPrincipal | None:
        """Authenticate Arena mutations on the public production plane."""

        if connector_bundle is None:
            return None
        try:
            principal = await connector_bundle.auth.authenticate(request)
            if csrf:
                await connector_bundle.auth.require_csrf(request, principal)
            return principal
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def require_owned_agent(
        agent_id: str,
        principal: AuthPrincipal | None,
    ) -> AgentRegistration:
        agent = registry.get(agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        if principal is not None and agent.owner_id != principal.user_id:
            raise HTTPException(403, "Agent is not owned by the authenticated user")
        return agent

    # ========================================================
    # Agent Endpoints
    # ========================================================

    @app.post("/api/agents/register")
    async def register_agent(req: RegisterAgentRequest, request: Request):
        """Register a new agent (BYOAgent)."""
        principal = await require_arena_principal(request, csrf=True)
        agent = AgentRegistration(
            owner_id=(
                principal.user_id
                if principal is not None
                else f"demo_user_{uuid.uuid4().hex[:8]}"
            ),
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
    async def agent_heartbeat(agent_id: str, request: Request):
        principal = await require_arena_principal(request, csrf=True)
        require_owned_agent(agent_id, principal)
        registry.heartbeat(agent_id)
        return {"status": "ok"}

    # ========================================================
    # Listing Endpoints
    # ========================================================

    @app.post("/api/listings")
    async def create_listing(req: CreateListingRequest, request: Request):
        """Publish a resource listing."""
        principal = await require_arena_principal(request, csrf=True)
        agent = require_owned_agent(req.seller_agent_id, principal)

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
    async def create_intent(req: CreateIntentRequest, request: Request):
        """Post a buy or sell intent."""
        principal = await require_arena_principal(request, csrf=True)
        agent = require_owned_agent(req.agent_id, principal)

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
                    "price_zone": [
                        round(m.price_zone[0], 2),
                        round(m.price_zone[1], 2),
                    ],
                    "counterparty_agent": (
                        m.sell_intent.agent_name
                        if intent.intent_type == IntentType.BUY
                        else m.buy_intent.agent_name
                    ),
                    "counterparty_elo": round(
                        (
                            m.seller_elo
                            if intent.intent_type == IntentType.BUY
                            else m.buyer_elo
                        ),
                        1,
                    ),
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
                    "price_zone": [
                        round(m.price_zone[0], 2),
                        round(m.price_zone[1], 2),
                    ],
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
    async def start_negotiation(match_id: str, request: Request):
        """Start a negotiation session from a match."""
        principal = await require_arena_principal(request, csrf=True)
        # Find the match (simplified — in production, store matches)
        # For now: create session directly from intents
        all_matches = orderbook.find_all_matches()
        match = next((m for m in all_matches if m.match_id == match_id), None)
        if not match:
            raise HTTPException(404, "Match not found or expired")
        if principal is not None:
            participant_ids = {
                match.buy_intent.agent_id,
                match.sell_intent.agent_id,
            }
            if not any(
                (agent := registry.get(agent_id)) is not None
                and agent.owner_id == principal.user_id
                for agent_id in participant_ids
            ):
                raise HTTPException(
                    403,
                    "Negotiation can only be started by a participant owner",
                )

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
    async def submit_proposal(req: SubmitProposalRequest, request: Request):
        """Submit a negotiation proposal."""
        principal = await require_arena_principal(request, csrf=True)
        require_owned_agent(req.agent_id, principal)
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
    async def accept_negotiation(
        session_id: str,
        agent_id: str,
        request: Request,
    ):
        principal = await require_arena_principal(request, csrf=True)
        require_owned_agent(agent_id, principal)
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
    async def reject_negotiation(
        session_id: str,
        agent_id: str,
        request: Request,
        reason: str = "",
    ):
        principal = await require_arena_principal(request, csrf=True)
        require_owned_agent(agent_id, principal)
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
                    "agent_id": e.agent_id,
                    "agent_name": e.agent_name,
                    "owner_id": e.owner_id,
                    "elo": e.elo,
                    "tier": e.tier.value,
                    "battles": e.battles_fought,
                    "wins": e.battles_won,
                    "win_rate": e.win_rate,
                    "earned": e.total_earned,
                    "saved": e.total_saved,
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
            "connector_gateway": app.state.connector_gateway_mode,
            "agents": registry.stats,
            "listings": orderbook.stats,
            "battles": arena.leaderboard.stats,
        }

    return app


# ============================================================
# Supabase-Backed Variant
# ============================================================


def create_app_with_db(db=None, connector_demo_enabled: Optional[bool] = None):
    """
    Create the FastAPI app with Supabase persistence.

    Args:
        db: ADXSupabase instance. If None, reads SUPABASE_URL/SUPABASE_ANON_KEY from env.

    Usage:
        uvicorn web.api:create_app_with_db --factory --reload

    Env vars:
        SUPABASE_URL     — https://xxx.supabase.co
        SUPABASE_ANON_KEY — eyJhbG...
    """
    if _production_connector_enabled(connector_demo_enabled):
        raise RuntimeError(
            "create_app_with_db is a legacy development factory and cannot run "
            "with ADX_ENV=production; use web.api:create_app"
        )

    if db is None:
        from db.client import ADXSupabase

        db = ADXSupabase()
        db.connect()

    app = FastAPI(
        title="ADX Agent Arena",
        description="Agent-to-Agent Resource Trading Platform — Supabase Edition",
        version="0.2.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(False),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-CSRF-Token"],
    )

    _mount_connector_gateway(app, connector_demo_enabled)

    # Use DB-backed stores
    registry = db.agents
    orderbook = db.orderbook
    # Arena still uses in-memory for negotiation sessions (DB used for persistence)
    from matching.agent import AgentRegistry as MemRegistry
    from matching.arena import Arena

    mem_registry = MemRegistry()  # Arena needs AgentRegistration objects in memory
    arena = Arena(mem_registry)
    negotiation = NegotiationProtocol(arena=arena)

    # Agent endpoints
    @app.post("/api/agents/register")
    async def register_agent(req: RegisterAgentRequest):
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        data = {
            "agent_id": agent_id,
            "owner_id": f"user_{uuid.uuid4().hex[:8]}",  # TODO: real auth
            "name": req.name,
            "description": req.description,
            "llm_provider": req.llm_provider,
            "llm_model": req.llm_model,
            "negotiation_style": req.negotiation_style,
            "strategy_description": req.strategy_description,
            "tradable_assets": req.tradable_assets,
            "trade_direction": req.trade_direction,
            "status": "online",
        }
        registry.register(data)
        return {"agent_id": agent_id, **data}

    @app.get("/api/agents")
    async def list_agents(asset_class: str = "", min_elo: float = 0):
        if asset_class:
            agents = registry.find_by_asset(asset_class, min_elo=min_elo)
        else:
            result = db.client.table("agents").select("*").execute()
            agents = result.data
        return {"total": len(agents), "agents": agents}

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        agent = registry.get(agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent

    @app.post("/api/agents/{agent_id}/heartbeat")
    async def agent_heartbeat(agent_id: str):
        registry.heartbeat(agent_id)
        return {"status": "ok"}

    # Listing endpoints
    @app.post("/api/listings")
    async def create_listing(req: CreateListingRequest):
        agent = registry.get(req.seller_agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        listing_id = f"list_{uuid.uuid4().hex[:10]}"
        data = {
            "listing_id": listing_id,
            "seller_agent_id": req.seller_agent_id,
            "seller_name": agent.get("name", ""),
            "asset_class": req.asset_class,
            "title": req.title,
            "description": req.description,
            "quantity": req.quantity,
            "unit": req.unit,
            "price_range": {
                "min": req.min_price,
                "ideal": req.ideal_price,
                "max": req.max_price,
                "currency": req.currency,
            },
            "tags": req.tags,
        }
        orderbook.publish_listing(data)
        return {"listing_id": listing_id, **data}

    @app.get("/api/listings")
    async def get_listings(asset_class: str = "", available_only: bool = True):
        listings = orderbook.get_listings(asset_class, available_only)
        return {"total": len(listings), "listings": listings}

    # Intents
    @app.post("/api/intents")
    async def create_intent(req: CreateIntentRequest):
        agent = registry.get(req.agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        intent_id = uuid.uuid4().hex
        data = {
            "id": intent_id,
            "agent_id": req.agent_id,
            "agent_name": agent.get("name", ""),
            "intent_type": req.intent_type,
            "asset_class": req.asset_class,
            "description": req.description,
            "quantity": req.quantity,
            "min_price": req.min_price,
            "ideal_price": req.ideal_price,
            "max_price": req.max_price,
            "currency": req.currency,
            "max_rounds": req.max_rounds,
            "auto_accept_threshold_pct": req.auto_accept_pct,
            "negotiation_style": agent.get("negotiation_style", "balanced"),
            "tags": req.tags,
            "listing_id": req.listing_id,
            "ttl_seconds": 3600,
        }
        orderbook.publish(data)
        return {
            "intent_id": intent_id,
            "intent_type": req.intent_type,
            "matches_found": 0,
            "matches": [],
        }

    @app.get("/api/intents/{intent_id}/matches")
    async def get_matches(intent_id: str, top_k: int = 10):
        intent = orderbook.get(intent_id)
        if not intent:
            raise HTTPException(404, "Intent not found")
        # Find matching counterparties
        opposite = "sell" if intent["intent_type"] == "buy" else "buy"
        candidates = orderbook.find_active_intents(opposite, intent["asset_class"])
        # Simple price overlap filter
        matches = []
        for c in candidates:
            buy = intent if intent["intent_type"] == "buy" else c
            sell = c if intent["intent_type"] == "buy" else intent
            if (
                buy["max_price"] >= sell["min_price"]
                and buy["min_price"] <= sell["max_price"]
            ):
                zone_low = max(buy["min_price"], sell["min_price"])
                zone_high = min(buy["max_price"], sell["max_price"])
                matches.append(
                    {
                        "match_id": f"match_{uuid.uuid4().hex[:10]}",
                        "score": 85.0,
                        "price_zone": [zone_low, zone_high],
                        "buyer": buy.get("agent_name", ""),
                        "seller": sell.get("agent_name", ""),
                    }
                )
        return {"intent_id": intent_id, "matches": matches[:top_k]}

    # Arena (read from DB)
    @app.get("/api/arena/leaderboard")
    async def get_leaderboard(
        asset_class: str = "", min_battles: int = 0, limit: int = 50
    ):
        entries = db.leaderboard.rank(asset_class, min_battles, limit)
        return {"total": len(entries), "leaderboard": entries}

    @app.get("/api/arena/battles")
    async def get_battle_feed(limit: int = 20):
        battles = db.leaderboard.battle_feed(limit)
        return {"total": len(battles), "battles": battles}

    @app.get("/api/arena/agents/{agent_id}/history")
    async def get_agent_history(agent_id: str, limit: int = 20):
        history = db.leaderboard.agent_history(agent_id, limit)
        agent = registry.get(agent_id)
        return {
            "agent_id": agent_id,
            "agent_name": agent.get("name", "unknown") if agent else "unknown",
            "elo": agent.get("elo_rating", 0) if agent else 0,
            "history": history,
        }

    @app.get("/api/arena/stats")
    async def get_arena_stats():
        return {
            "agents": db.agents.stats,
            "orderbook": db.orderbook.stats,
            "battles": db.leaderboard.stats,
        }

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.2.0-supabase",
            "agents": db.agents.stats,
            "listings": db.orderbook.stats,
            "battles": db.leaderboard.stats,
        }

    return app


# ============================================================
# Run
# ============================================================
# Local dev (in-memory):    uvicorn web.api:create_app --factory --reload
# Production (Supabase):     uvicorn web.api:create_app_with_db --factory --reload
# ============================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api:create_app", factory=True, reload=True, port=8000)
