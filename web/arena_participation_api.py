"""Authenticated owner-scoped Agent participation routes."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from arena_core import (
    ArenaParticipationError,
    GameParticipation,
    LocalAgentRegistration,
    PostgresArenaParticipationRepository,
)
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from connector_gateway.auth import AuthError, ConnectorAuth
from arena_payments.repository import PaymentRepository, WalletUnavailable
from arena_payments.service import ArenaPaymentService


_Identifier = Annotated[
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


class _JoinBody(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: name.split("_")[0]
        + "".join(part.capitalize() for part in name.split("_")[1:]),
        populate_by_name=False,
        extra="forbid",
        strict=True,
        hide_input_in_errors=True,
    )

    agent_id: _Identifier


class _LocalAgentBody(BaseModel):
    model_config = _JoinBody.model_config

    connector_binding_id: _Identifier
    display_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=120),
    ]


async def _principal(
    auth: ConnectorAuth,
    request: Request,
    *,
    csrf: bool = False,
):
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


def _idempotency_digest(request: Request) -> str:
    value = request.headers.get("idempotency-key", "")
    if not _IDEMPOTENCY_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return sha256_text_identifier(value)


def _error(exc: ArenaParticipationError) -> HTTPException:
    if exc.code in {
        "game_not_found",
        "agent_not_found",
        "connector_binding_not_found",
    }:
        status_code = 404
    elif exc.code in {
        "game_not_open",
        "runtime_not_ready",
        "user_already_joined",
        "idempotency_conflict",
        "invalid_game_config",
        "connector_binding_already_registered",
        "wallet_not_bound",
        "wallet_chain_mismatch",
    }:
        status_code = 409
    else:
        status_code = 422
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code},
    )


def _public(value: GameParticipation) -> dict[str, str]:
    return {
        "gameAgentId": value.game_agent_id,
        "gameId": value.game_id,
        "agentId": value.agent_id,
        "runtimeBindingId": value.runtime_binding_id,
        "runtimeKind": value.runtime_kind,
        "status": value.status,
        "configHash": value.config_hash,
        "schemaVersion": "arena.game-participation.v1",
    }


def _public_local_agent(
    value: LocalAgentRegistration,
) -> dict[str, str | int]:
    return {
        "agentId": value.agent_id,
        "displayName": value.display_name,
        "runtimeBindingId": value.runtime_binding_id,
        "runtimeKind": "connector",
        "connectorBindingId": value.connector_binding_id,
        "connectorBindingEpoch": value.connector_binding_epoch,
        "routeStatus": value.route_status,
        "schemaVersion": "arena.local-agent.v1",
    }


def create_arena_participation_router(
    *,
    auth: ConnectorAuth,
    repository: PostgresArenaParticipationRepository,
    payment_repository: PaymentRepository | None = None,
) -> APIRouter:
    router = APIRouter(tags=["arena-participation"])
    payment_service = (
        ArenaPaymentService(repository=payment_repository)
        if payment_repository is not None
        else None
    )

    @router.post("/api/local-agents", status_code=201)
    async def register_local_agent(
        body: _LocalAgentBody,
        request: Request,
    ) -> dict[str, str | int]:
        principal = await _principal(auth, request, csrf=True)
        key_digest = _idempotency_digest(request)
        request_digest = sha256_identifier(
            {
                "connectorBindingId": body.connector_binding_id,
                "displayName": body.display_name,
            }
        )
        try:
            value = await repository.register_local_agent(
                owner_user_id=principal.user_id,
                connector_binding_id=body.connector_binding_id,
                display_name=body.display_name,
                key_digest=key_digest,
                request_digest=request_digest,
            )
        except ArenaParticipationError as exc:
            raise _error(exc) from None
        return _public_local_agent(value)

    @router.get("/api/game-participations")
    async def list_participations(request: Request) -> dict[str, object]:
        if list(request.query_params.multi_items()) not in (
            [],
            [("scope", "mine")],
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_request"},
            )
        principal = await _principal(auth, request)
        values = await repository.list_for_owner(principal.user_id)
        return {
            "participations": [_public(value) for value in values],
            "total": len(values),
        }

    @router.post("/api/games/{game_id}/participants", status_code=201)
    async def join_game(
        game_id: _Identifier,
        body: _JoinBody,
        request: Request,
    ) -> dict[str, str]:
        principal = await _principal(auth, request, csrf=True)
        if payment_service is not None:
            try:
                await payment_service.get_or_bind_platform_wallet(
                    user_id=principal.user_id,
                    identity_provider=principal.identity_provider,
                    provider_subject=principal.provider_subject,
                )
            except WalletUnavailable as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": str(exc)},
                ) from None
        key_digest = _idempotency_digest(request)
        request_digest = sha256_identifier(
            {"agentId": body.agent_id, "gameId": game_id}
        )
        try:
            value = await repository.join(
                owner_user_id=principal.user_id,
                game_id=game_id,
                agent_id=body.agent_id,
                key_digest=key_digest,
                request_digest=request_digest,
            )
        except ArenaParticipationError as exc:
            raise _error(exc) from None
        return _public(value)

    return router


__all__ = ["create_arena_participation_router"]
