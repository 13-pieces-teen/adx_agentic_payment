"""Fail-closed HTTP boundary for the Hosted Agent control plane.

Only the capability route is safe to mount without production persistence.
Credential and Agent mutations are omitted from the router unless every
dependency is explicitly supplied and readiness is true. Secret-bearing bodies
are parsed manually so FastAPI's default validation payload cannot echo a raw
Provider key in ``detail[].input``.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
)

from connector_gateway.auth import AuthError, AuthPrincipal, ConnectorAuth
from hosted_agent_control_plane import (
    CapabilityCatalogService,
    CredentialIngressRequest,
    CredentialIngressService,
    HostedAgentCreateRequest,
    HostedAgentService,
    HostedControlPlaneError,
)


_MAX_CREDENTIAL_BODY_BYTES = 70 * 1024
_MAX_AGENT_BODY_BYTES = 16 * 1024
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _StrictApiBody(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: name.split("_")[0]
        + "".join(part.capitalize() for part in name.split("_")[1:]),
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=False,
        strict=True,
    )


class _CredentialIngressBody(_StrictApiBody):
    provider_id: str
    api_key: SecretStr


class _HostedAgentCreateBody(_StrictApiBody):
    display_name: str
    credential_id: str
    provider_id: str
    model_id: str
    thinking_enabled: bool
    strategy_instructions: str = ""


def _reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _safe_body(
    request: Request,
    model: type[_ModelT],
    *,
    max_bytes: int,
) -> _ModelT:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(
            status_code=415,
            detail={"code": "application_json_required"},
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length < 0 or declared_length > max_bytes:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_request"},
            )

    raw_buffer = bytearray()
    async for chunk in request.stream():
        if len(raw_buffer) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_request"},
            )
        raw_buffer.extend(chunk)
    raw = bytes(raw_buffer)
    if not raw or len(raw) > max_bytes:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request"},
        )
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
        )
        return model.model_validate(decoded)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
        # Never include the Pydantic error, raw body, or rejected field value.
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request"},
        ) from None


def _idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key")
    if value is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "idempotency_key_required"},
        )
    return value


def _require_owner_scope(request: Request) -> None:
    query_items = list(request.query_params.multi_items())
    if query_items not in ([], [("scope", "mine")]):
        # Do not delegate query validation to FastAPI: its default 422 payload
        # includes the rejected value and could reflect a misplaced secret.
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_request"},
        )


async def _principal(
    auth: ConnectorAuth,
    request: Request,
    *,
    csrf: bool = False,
) -> AuthPrincipal:
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


def _service_error(exc: HostedControlPlaneError) -> HTTPException:
    if exc.code in {"credential_not_found", "agent_not_found"}:
        status = 404
    elif exc.code == "idempotency_conflict":
        status = 409
    elif exc.code in {
        "credential_not_usable",
        "provider_mismatch",
    }:
        status = 409
    elif exc.code in {
        "credential_ingress_unavailable",
        "credential_write_recovery_required",
        "hosted_agents_disabled",
        "non_durable_repository_forbidden",
        "repository_unavailable",
        "secret_store_unavailable",
    }:
        status = 503
    else:
        status = 422
    return HTTPException(
        status_code=status,
        detail={"code": exc.code},
    )


def _public(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    )


def create_hosted_agent_router(
    *,
    catalog: CapabilityCatalogService,
    auth: ConnectorAuth | None = None,
    credential_service: CredentialIngressService | None = None,
    agent_service: HostedAgentService | None = None,
    enable_mutations: bool = False,
) -> APIRouter:
    """Build routes without silently falling back to test-only dependencies."""

    if not isinstance(catalog, CapabilityCatalogService):
        raise TypeError("catalog must be CapabilityCatalogService")
    readiness = catalog.readiness()
    if enable_mutations and (
        not readiness.available
        or auth is None
        or credential_service is None
        or agent_service is None
    ):
        raise RuntimeError(
            "Hosted Agent mutations require production-ready dependencies"
        )

    router = APIRouter(tags=["hosted-agents"])

    @router.get("/api/hosted-agents/capabilities")
    async def capabilities() -> dict[str, Any]:
        current = catalog.readiness()
        return {
            "creationEnabled": bool(
                enable_mutations and current.available
            ),
            "reasonCodes": list(current.reason_codes),
            "registryVersion": current.registry_version,
            "models": [
                _public(item)
                for item in catalog.list_capabilities()
            ],
            "schemaVersion": current.schema_version,
        }

    if auth is not None and credential_service is not None:

        @router.get("/api/model-credentials")
        async def list_credentials(
            request: Request,
        ) -> dict[str, Any]:
            _require_owner_scope(request)
            principal = await _principal(auth, request)
            try:
                values = await credential_service.list_credentials(
                    owner_user_id=principal.user_id,
                )
            except HostedControlPlaneError as exc:
                raise _service_error(exc) from None
            return {
                "credentials": [_public(value) for value in values],
                "total": len(values),
            }

    if auth is not None and agent_service is not None:

        @router.get("/api/hosted-agents")
        async def list_hosted_agents(
            request: Request,
        ) -> dict[str, Any]:
            _require_owner_scope(request)
            principal = await _principal(auth, request)
            try:
                values = await agent_service.list_hosted_agents(
                    owner_user_id=principal.user_id,
                )
            except HostedControlPlaneError as exc:
                raise _service_error(exc) from None
            return {
                "agents": [_public(value) for value in values],
                "total": len(values),
            }

        @router.get("/api/hosted-agents/{agent_id}")
        async def get_hosted_agent(
            agent_id: str,
            request: Request,
        ) -> dict[str, Any]:
            principal = await _principal(auth, request)
            try:
                value = await agent_service.get_hosted_agent(
                    owner_user_id=principal.user_id,
                    agent_id=agent_id,
                )
            except HostedControlPlaneError as exc:
                raise _service_error(exc) from None
            return _public(value)

    if enable_mutations:
        assert auth is not None
        assert credential_service is not None
        assert agent_service is not None

        @router.post("/api/model-credentials", status_code=201)
        async def create_credential(
            request: Request,
        ) -> dict[str, Any]:
            principal = await _principal(auth, request, csrf=True)
            body = await _safe_body(
                request,
                _CredentialIngressBody,
                max_bytes=_MAX_CREDENTIAL_BODY_BYTES,
            )
            try:
                command = CredentialIngressRequest(
                    provider_id=body.provider_id,
                    api_key=body.api_key,
                    idempotency_key=_idempotency_key(request),
                )
                value = await credential_service.create_credential(
                    owner_user_id=principal.user_id,
                    request=command,
                )
            except (ValidationError, HostedControlPlaneError) as exc:
                if isinstance(exc, HostedControlPlaneError):
                    raise _service_error(exc) from None
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_request"},
                ) from None
            return _public(value)

        @router.post("/api/hosted-agents", status_code=201)
        async def create_hosted_agent(
            request: Request,
        ) -> dict[str, Any]:
            principal = await _principal(auth, request, csrf=True)
            body = await _safe_body(
                request,
                _HostedAgentCreateBody,
                max_bytes=_MAX_AGENT_BODY_BYTES,
            )
            try:
                command = HostedAgentCreateRequest(
                    **body.model_dump(),
                    idempotency_key=_idempotency_key(request),
                )
                value = await agent_service.create_hosted_agent(
                    owner_user_id=principal.user_id,
                    request=command,
                )
            except (ValidationError, HostedControlPlaneError) as exc:
                if isinstance(exc, HostedControlPlaneError):
                    raise _service_error(exc) from None
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_request"},
                ) from None
            return _public(value)

    return router


__all__ = ["create_hosted_agent_router"]
