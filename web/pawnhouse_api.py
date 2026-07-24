"""Development-first API for the clean-slate King's Pawnhouse game."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from arena_game import (
    PawnhouseRepositoryError,
    Portfolio,
    PortfolioError,
    PostgresPawnhouseRepository,
    RuleStrategy,
    demo_events,
    gold,
)


_Id = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateGameBody(_Body):
    game_id: _Id = Field(alias="gameId")
    event_seed: Annotated[str, StringConstraints(min_length=8, max_length=256)] = (
        Field(alias="eventSeed")
    )
    action_timeout_ms: int = Field(
        default=90_000,
        ge=100,
        le=900_000,
        alias="actionTimeoutMs",
    )


class PortfolioBody(_Body):
    cash: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    holdings: dict[str, int]


class RuleStrategyBody(_Body):
    intent: Literal["buy", "sell", "pass"]
    good: Literal["grain", "iron", "warhorse", "gems"]
    target_price: Annotated[str, StringConstraints(min_length=1, max_length=64)] = (
        Field(alias="targetPrice")
    )
    public_message: Annotated[str, StringConstraints(min_length=1, max_length=100)] = (
        Field(alias="publicMessage")
    )


class AddRuleParticipantBody(_Body):
    user_id: _Id = Field(alias="userId")
    agent_id: _Id = Field(alias="agentId")
    portfolio: PortfolioBody
    strategy: RuleStrategyBody


def _repository_error(exc: PawnhouseRepositoryError) -> HTTPException:
    code = str(exc)
    status = 404 if code in {"game_not_found"} else 409
    return HTTPException(status_code=status, detail={"code": code})


def create_pawnhouse_router(
    *,
    repository: PostgresPawnhouseRepository,
    dev_token: str,
) -> APIRouter:
    if len(dev_token) < 16:
        raise RuntimeError("ADX_ARENA_DEV_TOKEN must contain at least 16 characters")

    router = APIRouter(tags=["pawnhouse-dev"])

    def authorize(value: str | None) -> None:
        if value != dev_token:
            raise HTTPException(
                status_code=403,
                detail={"code": "invalid_development_token"},
            )

    @router.post("/api/dev/pawnhouse/games", status_code=201)
    async def create_game(
        body: CreateGameBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.create_game(
                game_id=body.game_id,
                events=demo_events(),
                event_seed=body.event_seed,
                action_timeout_ms=body.action_timeout_ms,
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post(
        "/api/dev/pawnhouse/games/{game_id}/rule-participants",
        status_code=201,
    )
    async def add_rule_participant(
        game_id: _Id,
        body: AddRuleParticipantBody,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            portfolio = Portfolio.initial(
                cash_atomic=gold(body.portfolio.cash),
                holdings=body.portfolio.holdings,
            )
            strategy = RuleStrategy(
                intent=body.strategy.intent,
                good=body.strategy.good,
                target_price_atomic=gold(body.strategy.target_price),
                public_message=body.strategy.public_message,
            )
            participant_id = await repository.add_rule_participant(
                game_id=game_id,
                user_id=body.user_id,
                agent_id=body.agent_id,
                portfolio=portfolio,
                strategy=strategy,
            )
        except PortfolioError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_portfolio"},
            ) from exc
        except (ValueError, PawnhouseRepositoryError) as exc:
            if isinstance(exc, PawnhouseRepositoryError):
                raise _repository_error(exc) from None
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_rule_participant"},
            ) from None
        return {
            "gameId": game_id,
            "participantId": participant_id,
            "runtimeKind": "rule",
        }

    @router.post("/api/dev/pawnhouse/games/{game_id}/start")
    async def start_game(
        game_id: _Id,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.start_game(
                game_id=game_id,
                events=demo_events(),
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.post("/api/dev/pawnhouse/games/{game_id}/run-rule-market")
    async def run_rule_market(
        game_id: _Id,
        x_arena_dev_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        authorize(x_arena_dev_token)
        try:
            return await repository.run_rule_market(game_id=game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.get("/api/v1/pawnhouse/games/{game_id}")
    async def game_state(game_id: _Id) -> dict[str, object]:
        try:
            return await repository.game_state(game_id)
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.get("/api/v1/pawnhouse/games/{game_id}/timeline")
    async def game_timeline(
        game_id: _Id,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        events = await repository.timeline(game_id, after_sequence=after)
        return {
            "gameId": game_id,
            "events": events,
            "nextAfter": events[-1]["sequence"] if events else after,
            "schemaVersion": "arena.pawnhouse-timeline.v1",
        }

    return router


__all__ = ["create_pawnhouse_router"]
