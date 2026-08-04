"""Authenticated production Game Operator routes for King's Pawnhouse."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import StringConstraints

from arena_game import (
    EventDeckError,
    PawnhouseRepositoryError,
    PostgresPawnhouseRepository,
    SettlementConfig,
    SettlementError,
    build_event_schedule,
)
from connector_gateway.auth import AuthError, ConnectorAuth

from .pawnhouse_api import CreateGameBody


_GameId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


async def _principal(auth: ConnectorAuth, request: Request, *, csrf: bool = False):
    try:
        principal = await auth.authenticate(request)
        if csrf:
            await auth.require_csrf(request, principal)
        return principal
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from None


def _repository_error(exc: PawnhouseRepositoryError) -> HTTPException:
    code = str(exc)
    if code == "game_operator_forbidden":
        status_code = 403
    elif code == "game_not_found":
        status_code = 404
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": code},
    )


def create_game_operator_router(
    *,
    auth: ConnectorAuth,
    repository: PostgresPawnhouseRepository,
) -> APIRouter:
    """Create owner-authenticated production list/create/start routes."""

    router = APIRouter(tags=["pawnhouse-operator"])

    @router.get("/api/v1/pawnhouse/games")
    async def list_games(request: Request) -> dict[str, object]:
        await _principal(auth, request)
        games = await repository.list_games(limit=50)
        return {
            "games": games,
            "total": len(games),
            "schemaVersion": "arena.pawnhouse-game-list.v1",
        }

    @router.post("/api/v1/pawnhouse/games", status_code=201)
    async def create_game(
        body: CreateGameBody,
        request: Request,
    ) -> dict[str, object]:
        principal = await _principal(auth, request, csrf=True)
        try:
            settlement_config = (
                SettlementConfig()
                if body.settlement is None
                else SettlementConfig(
                    authorization_mode=body.settlement.authorization_mode,
                    chain_id=body.settlement.chain_id,
                    token_address=body.settlement.token_address,
                    token_symbol=body.settlement.token_symbol,
                    token_decimals=body.settlement.token_decimals,
                    token_eip712_name=body.settlement.token_eip712_name,
                    token_eip712_version=(
                        body.settlement.token_eip712_version
                    ),
                    required_confirmations=(
                        body.settlement.required_confirmations
                    ),
                )
            )
            events = build_event_schedule(
                round_count=body.round_count,
                seed=body.event_seed,
                deck_id=body.event_deck_id,
                mode=body.event_mode,
            )
            return await repository.create_game(
                game_id=body.game_id,
                events=events,
                event_seed=body.event_seed,
                event_deck_id=body.event_deck_id,
                event_mode=body.event_mode,
                action_timeout_ms=body.action_timeout_ms,
                max_participants=body.max_participants,
                portfolio_mode=body.portfolio_mode,
                market_protocol=body.market_protocol,
                settlement_config=settlement_config,
                operator_user_id=principal.user_id,
            )
        except (EventDeckError, SettlementError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": str(exc)},
            ) from None
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post("/api/v1/pawnhouse/games/{game_id}/start")
    async def start_game(
        game_id: _GameId,
        request: Request,
    ) -> dict[str, object]:
        principal = await _principal(auth, request, csrf=True)
        try:
            return await repository.start_game(
                game_id=game_id,
                operator_user_id=principal.user_id,
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    return router


__all__ = ["create_game_operator_router"]
