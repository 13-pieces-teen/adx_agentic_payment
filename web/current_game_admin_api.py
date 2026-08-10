"""Protected matchmaking controls for the product Current Game."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from arena_game import PawnhouseRepositoryError, PostgresPawnhouseRepository
from arena_core.hashing import sha256_text_identifier
from connector_gateway.auth import AuthError, AuthPrincipal, ConnectorAuth


_GameId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
_IDEMPOTENCY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$"
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConfigureCurrentGameMatchmakingBody(_Body):
    game_id: _GameId = Field(alias="gameId")
    target_agent_count: int = Field(
        ge=10,
        le=100,
        alias="targetAgentCount",
    )


async def _admin_principal(
    auth: ConnectorAuth,
    request: Request,
    *,
    github_subjects: frozenset[str],
    csrf: bool = False,
) -> AuthPrincipal:
    try:
        principal = await auth.authenticate(request)
        if csrf:
            await auth.require_csrf(request, principal)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from None
    if (
        principal.identity_provider != "github"
        or principal.provider_subject not in github_subjects
    ):
        raise HTTPException(status_code=403, detail="admin_forbidden")
    return principal


def _repository_error(exc: PawnhouseRepositoryError) -> HTTPException:
    code = str(exc)
    if code == "current_game_not_found":
        status_code = 404
    elif code == "invalid_target_agent_count":
        status_code = 422
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": code},
    )


def _idempotency_digest(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not _IDEMPOTENCY_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return sha256_text_identifier(value)


async def _snapshot(
    repository: PostgresPawnhouseRepository,
    *,
    owner_user_id: str,
) -> dict[str, object]:
    game_value = await repository.current_game(
        owner_user_id=owner_user_id,
    )
    configuration = (
        await repository.current_game_matchmaking_configuration()
    )
    game = game_value.get("game")
    if (
        not isinstance(game, dict)
        or game.get("gameId") != configuration.get("gameId")
    ):
        raise PawnhouseRepositoryError("current_game_changed")
    return {
        "game": game,
        "matchmakingConfiguration": configuration,
        "schemaVersion": "arena.current-game-admin.v1",
    }


def create_current_game_admin_router(
    *,
    auth: ConnectorAuth,
    repository: PostgresPawnhouseRepository,
    github_subjects: frozenset[str],
) -> APIRouter:
    """Create allowlisted read/update routes for exact Current Game sizing."""

    router = APIRouter(tags=["current-game-admin"])

    @router.get("/api/v1/admin/current-game")
    async def current_game_admin_snapshot(
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        principal = await _admin_principal(
            auth,
            request,
            github_subjects=github_subjects,
        )
        response.headers["Cache-Control"] = "no-store"
        try:
            return await _snapshot(
                repository,
                owner_user_id=principal.user_id,
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    @router.put("/api/v1/admin/current-game/matchmaking")
    async def configure_current_game_matchmaking(
        body: ConfigureCurrentGameMatchmakingBody,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        principal = await _admin_principal(
            auth,
            request,
            github_subjects=github_subjects,
            csrf=True,
        )
        response.headers["Cache-Control"] = "no-store"
        try:
            await repository.configure_current_game_matchmaking(
                expected_game_id=body.game_id,
                target_agent_count=body.target_agent_count,
                actor_user_id=principal.user_id,
                request_digest=_idempotency_digest(request),
            )
            return await _snapshot(
                repository,
                owner_user_id=principal.user_id,
            )
        except PawnhouseRepositoryError as exc:
            raise _repository_error(exc) from None

    return router


__all__ = ["create_current_game_admin_router"]
