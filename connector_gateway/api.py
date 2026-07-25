"""FastAPI routes for device enrollment, control and outbound WSS transport."""

from __future__ import annotations

from typing import Optional, Protocol
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from .auth import AuthError, AuthPrincipal, ConnectorAuth
from .models import (
    AcceptInviteRequest,
    ApprovePairingRequest,
    ConnectorEnvelope,
    CreateBindingRequest,
    CreateCommandRequest,
    CreatePairingRequest,
    ExchangePairingRequest,
    LoginRequest,
    RegisterRequest,
    RevokeDeviceRequest,
    RuntimeInventoryItem,
)
from .rate_limit import RateLimitExceeded, SlidingWindowRateLimiter
from .service import ConnectorError, ConnectorGateway


class ArenaConnectorRegistrar(Protocol):
    async def register_connector_binding(
        self,
        *,
        owner_user_id: str,
        connector_binding_id: str,
    ) -> dict[str, str]: ...


def create_connector_router(
    service: ConnectorGateway,
    auth: ConnectorAuth | None = None,
    pairing_limiter: SlidingWindowRateLimiter | None = None,
    arena_registrar: ArenaConnectorRegistrar | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/connectors", tags=["connectors"])

    @router.post("/pairings", status_code=201)
    async def create_pairing(req: CreatePairingRequest, request: Request):
        await _enforce_rate_limit(pairing_limiter, request, "pairing:create")
        requested_owner = req.owner_id if auth is None else None
        return await _call(service.create_pairing(requested_owner, req.device_name))

    @router.post("/pairings/{user_code}/approve")
    async def approve_pairing(
        user_code: str,
        req: ApprovePairingRequest,
        request: Request,
    ):
        await _enforce_rate_limit(pairing_limiter, request, "pairing:approve")
        owner_id = req.owner_id
        if auth is not None:
            owner_id = (await _require_principal(auth, request, csrf=True)).user_id
        return await _call(service.approve_pairing(user_code, owner_id))

    @router.post("/pairings/exchange")
    async def exchange_pairing(req: ExchangePairingRequest, request: Request):
        await _enforce_rate_limit(pairing_limiter, request, "pairing:exchange")
        return await _call(service.exchange_pairing(req.device_code))

    @router.get("/devices")
    async def list_devices(
        request: Request, owner_id: Optional[str] = Query(default=None, max_length=128)
    ):
        if auth is not None:
            owner_id = (await _require_principal(auth, request)).user_id
        devices = await service.list_devices(owner_id)
        return {"total": len(devices), "devices": devices}

    @router.get("/devices/{device_id}")
    async def get_device(device_id: str, request: Request):
        if auth is not None:
            principal = await _require_principal(auth, request)
            await _require_owned_device(service, principal, device_id)
        return await _call(service.get_device(device_id))

    @router.post("/devices/{device_id}/revoke")
    async def revoke_device(
        device_id: str,
        req: RevokeDeviceRequest,
        request: Request,
    ):
        owner_id = req.owner_id
        if auth is not None:
            principal = await _require_principal(auth, request, csrf=True)
            await _require_owned_device(service, principal, device_id)
            owner_id = principal.user_id
        return await _call(service.revoke_device(device_id, owner_id))

    @router.post("/devices/{device_id}/bindings", status_code=201)
    async def create_binding(
        device_id: str, req: CreateBindingRequest, request: Request
    ):
        principal = None
        if auth is not None:
            principal = await _require_principal(auth, request, csrf=True)
            await _require_owned_device(service, principal, device_id)
            if req.agent_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail="agent_id is assigned by the Arena",
                )
        binding = await _call(
            service.create_binding(
                device_id,
                req.runtime_id,
                req.agent_id if auth is None else None,
                req.display_name,
            )
        )
        if arena_registrar is not None and principal is not None:
            try:
                registration = (
                    await arena_registrar.register_connector_binding(
                        owner_user_id=principal.user_id,
                        connector_binding_id=str(binding["binding_id"]),
                    )
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if isinstance(code, str):
                    raise HTTPException(
                        status_code=(
                            404
                            if code == "connector_binding_not_found"
                            else 409
                        ),
                        detail={"code": code},
                    ) from None
                raise
            return {**binding, "arenaRegistration": registration}
        return binding

    @router.get("/bindings")
    async def list_bindings(request: Request, device_id: Optional[str] = None):
        principal = None
        if auth is not None:
            principal = await _require_principal(auth, request)
            if device_id is not None:
                await _require_owned_device(service, principal, device_id)
        bindings = await service.list_bindings(device_id)
        if principal is not None:
            owned_devices = {
                item["device_id"]
                for item in await service.list_devices(principal.user_id)
            }
            bindings = [item for item in bindings if item["device_id"] in owned_devices]
        return {"total": len(bindings), "bindings": bindings}

    @router.post("/bindings/{binding_id}/commands", status_code=202)
    async def create_command(
        binding_id: str, req: CreateCommandRequest, request: Request
    ):
        if auth is not None:
            principal = await _require_principal(auth, request, csrf=True)
            await _require_owned_binding(service, principal, binding_id)
        return await _call(
            service.queue_command(
                binding_id,
                req.action,
                req.payload,
                req.idempotency_key,
                req.expires_in_seconds,
            )
        )

    @router.get("/bindings/{binding_id}/commands")
    async def list_commands(
        binding_id: str,
        request: Request,
        limit: int = Query(100, ge=1, le=500),
    ):
        if auth is not None:
            principal = await _require_principal(auth, request)
            await _require_owned_binding(service, principal, binding_id)
        commands = await _call(service.list_commands(binding_id, limit))
        return {"total": len(commands), "commands": commands}

    @router.get("/bindings/{binding_id}/events")
    async def list_events(
        binding_id: str,
        request: Request,
        limit: int = Query(200, ge=1, le=1000),
    ):
        if auth is not None:
            principal = await _require_principal(auth, request)
            await _require_owned_binding(service, principal, binding_id)
        events = await _call(service.list_events(binding_id, limit))
        return {"total": len(events), "events": events}

    @router.get("/audit")
    async def list_audit(request: Request, limit: int = Query(200, ge=1, le=1000)):
        owner_id = None
        if auth is not None:
            owner_id = (await _require_principal(auth, request)).user_id
        audit = await service.list_audit(limit, owner_id)
        return {"total": len(audit), "audit": audit}

    @router.websocket("/ws")
    async def connector_socket(
        websocket: WebSocket,
        device_id: str = Query(...),
    ):
        authorization = websocket.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "device" or not token:
            await websocket.close(code=4401, reason="Missing device authorization")
            return
        try:
            await service.authenticate_device(device_id, token)
        except ConnectorError as exc:
            if exc.status_code == 410:
                await websocket.accept()
                await websocket.close(code=4403, reason="Device revoked")
            else:
                await websocket.close(code=4401, reason="Invalid device credentials")
            return

        await websocket.accept()
        try:
            connection_generation = await service.connect_device(device_id, websocket)
        except ConnectorError as exc:
            await websocket.close(
                code=4403 if exc.status_code == 410 else 1011,
                reason=(
                    exc.detail
                    if exc.status_code == 410
                    else "Connector connection setup failed"
                ),
            )
            return
        try:
            await service.send_active_message(
                device_id,
                websocket,
                connection_generation,
                {
                    "type": "welcome",
                    "protocol_version": service.protocol_version,
                    "device_id": device_id,
                    "payload": {
                        "protocol_version": service.protocol_version,
                        "heartbeat_interval_seconds": 15,
                        "heartbeat_lease_seconds": service.heartbeat_lease_seconds,
                    },
                },
            )
            await service.deliver_pending(device_id)
            while True:
                raw = await websocket.receive_json()
                try:
                    await service.assert_active_connection(
                        device_id, websocket, connection_generation
                    )
                    envelope = ConnectorEnvelope(**raw)
                    if (
                        envelope.protocol_version is not None
                        and envelope.protocol_version != service.protocol_version
                    ):
                        raise ConnectorError(
                            409, "Unsupported envelope protocol version"
                        )
                    if (
                        envelope.device_id is not None
                        and envelope.device_id != device_id
                    ):
                        raise ConnectorError(
                            403, "Envelope device_id does not match credentials"
                        )
                    await service.observe_inbound_sequence(
                        device_id,
                        envelope.sequence,
                        expected_generation=connection_generation,
                    )
                    response = await _handle_envelope(
                        service,
                        device_id,
                        envelope,
                        connection_generation,
                    )
                    if envelope.message_id:
                        ack_type = {
                            "runtime.event": "event.ack",
                            "agent_task.result": "agent_task.result.ack",
                        }.get(envelope.type, "ack")
                        ack_payload = response or {"accepted": True}
                        if envelope.type == "runtime.event":
                            ack_payload = {
                                "accepted": True,
                                "through_sequence": response.get(
                                    "ack_through_sequence", 0
                                ),
                            }
                        await service.send_active_message(
                            device_id,
                            websocket,
                            connection_generation,
                            {
                                "type": ack_type,
                                "protocol_version": service.protocol_version,
                                "device_id": device_id,
                                "message_id": envelope.message_id,
                                "payload": ack_payload,
                            },
                        )
                    await service.deliver_pending(device_id)
                except ConnectorError as exc:
                    connection_is_invalid = (
                        exc.status_code == 410
                        or exc.detail
                        == "WebSocket is no longer an active device connection"
                        or exc.detail == "Unsupported envelope protocol version"
                        or exc.detail.startswith("Unsupported protocol version ")
                    )
                    if connection_is_invalid:
                        close_code = 4409
                        if exc.status_code == 410:
                            close_code = 4403
                        elif exc.detail.startswith("Unsupported"):
                            close_code = 4406
                        await websocket.close(
                            code=close_code,
                            reason=exc.detail,
                        )
                        return
                    await service.send_active_message(
                        device_id,
                        websocket,
                        connection_generation,
                        {
                            "type": "error",
                            "protocol_version": service.protocol_version,
                            "device_id": device_id,
                            "message_id": (
                                raw.get("message_id") if isinstance(raw, dict) else None
                            ),
                            "payload": {"detail": exc.detail},
                        },
                    )
                except (ValidationError, ValueError) as exc:
                    detail = str(exc)
                    await service.send_active_message(
                        device_id,
                        websocket,
                        connection_generation,
                        {
                            "type": "error",
                            "protocol_version": service.protocol_version,
                            "device_id": device_id,
                            "message_id": (
                                raw.get("message_id") if isinstance(raw, dict) else None
                            ),
                            "payload": {"detail": detail},
                        },
                    )
        except WebSocketDisconnect:
            pass
        finally:
            await service.disconnect_device(device_id, websocket)

    return router


def create_production_connector_router(
    service: ConnectorGateway,
    auth: ConnectorAuth,
    arena_registrar: ArenaConnectorRegistrar | None = None,
) -> APIRouter:
    """Create the remotely reachable, session-authenticated Connector API."""

    router = APIRouter()
    auth_limiter = SlidingWindowRateLimiter(
        auth.config.auth_rate_limit_attempts,
        auth.config.rate_limit_window_seconds,
    )
    pairing_limiter = SlidingWindowRateLimiter(
        auth.config.pairing_rate_limit_attempts,
        auth.config.rate_limit_window_seconds,
    )

    @router.get("/api/auth/github/start")
    async def github_start(request: Request, return_to: str | None = None):
        await _enforce_rate_limit(auth_limiter, request, "auth:github")
        try:
            authorization_url, signed_state = auth.begin_github_oauth(return_to)
        except AuthError:
            query = urlencode(
                {
                    "error": "github_unavailable",
                    "return_to": auth.safe_return_to(return_to),
                }
            )
            return RedirectResponse(
                f"{auth.config.public_app_url}/signin?{query}",
                status_code=307,
                headers={"Cache-Control": "no-store"},
            )
        response = RedirectResponse(
            authorization_url,
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
        auth.set_github_oauth_state_cookie(response, signed_state)
        return response

    @router.get("/api/auth/github/callback")
    async def github_callback(
        request: Request,
        code: str | None = Query(default=None, max_length=512),
        state: str | None = Query(default=None, max_length=512),
        error: str | None = Query(default=None, max_length=128),
    ):
        await _enforce_rate_limit(auth_limiter, request, "auth:github")
        signed_state = request.cookies.get(
            auth.config.github_oauth_state_cookie_name
        )
        try:
            oauth_state = auth.validate_github_oauth_state(signed_state, state)
        except AuthError:
            return_to = "/agents"
            error_code = "invalid_state"
        else:
            return_to = oauth_state["return_to"]
            if error is not None:
                error_code = (
                    "github_denied" if error == "access_denied" else "github_failed"
                )
            elif not code:
                error_code = "github_failed"
            else:
                try:
                    issued = await auth.sign_in_with_github(
                        code=code,
                        oauth_state=oauth_state,
                    )
                except AuthError as exc:
                    error_code = (
                        exc.detail
                        if exc.detail
                        in {
                            "github_unavailable",
                            "github_failed",
                            "account_disabled",
                        }
                        else "github_failed"
                    )
                else:
                    response = RedirectResponse(
                        f"{auth.config.public_app_url}{return_to}",
                        status_code=307,
                        headers={"Cache-Control": "no-store"},
                    )
                    auth.set_session_cookies(response, issued)
                    auth.clear_github_oauth_state_cookie(response)
                    return response

        query = urlencode({"error": error_code, "return_to": return_to})
        response = RedirectResponse(
            f"{auth.config.public_app_url}/signin?{query}",
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
        auth.clear_github_oauth_state_cookie(response)
        return response

    @router.post("/api/auth/invite", status_code=201)
    async def accept_invite(
        req: AcceptInviteRequest,
        response: Response,
        request: Request,
    ):
        await _enforce_rate_limit(auth_limiter, request, "auth:credentials")
        issued = await _call_auth(
            auth.accept_invite(req.invite_code, req.username, req.password)
        )
        auth.set_session_cookies(response, issued)
        return _session_response(issued.principal, issued.csrf_token)

    @router.post("/api/auth/register", status_code=201)
    async def register(req: RegisterRequest, response: Response, request: Request):
        await _enforce_rate_limit(auth_limiter, request, "auth:credentials")
        issued = await _call_auth(
            auth.register(req.invite_code, req.username, req.password)
        )
        auth.set_session_cookies(response, issued)
        return _session_response(issued.principal, issued.csrf_token)

    @router.post("/api/auth/login")
    async def login(req: LoginRequest, response: Response, request: Request):
        await _enforce_rate_limit(auth_limiter, request, "auth:credentials")
        issued = await _call_auth(auth.login(req.username, req.password))
        auth.set_session_cookies(response, issued)
        return _session_response(issued.principal, issued.csrf_token)

    @router.get("/api/auth/session")
    async def session(request: Request, response: Response):
        principal = await _require_principal(auth, request)
        response.headers["Cache-Control"] = "no-store"
        return _session_response(
            principal,
            request.cookies.get(auth.config.csrf_cookie_name, ""),
        )

    @router.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        principal = await _require_principal(auth, request, csrf=True)
        await auth.logout(principal)
        auth.clear_session_cookies(response)
        response.status_code = 204
        return None

    router.include_router(
        create_connector_router(
            service,
            auth=auth,
            pairing_limiter=pairing_limiter,
            arena_registrar=arena_registrar,
        )
    )
    return router


def _session_response(
    principal: AuthPrincipal,
    csrf_token: str,
) -> dict:
    return {
        "user": {
            "user_id": principal.user_id,
            "username": principal.username,
            "temporary": principal.temporary,
        },
        "csrf_token": csrf_token,
    }


async def _require_principal(
    auth: ConnectorAuth,
    request: Request,
    csrf: bool = False,
) -> AuthPrincipal:
    try:
        principal = await auth.authenticate(request)
        if csrf:
            await auth.require_csrf(request, principal)
        return principal
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _require_owned_device(
    service: ConnectorGateway,
    principal: AuthPrincipal,
    device_id: str,
) -> dict:
    try:
        device = await service.get_device(device_id)
    except ConnectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if device["owner_id"] != principal.user_id:
        # Do not disclose another tenant's object identifiers.
        raise HTTPException(status_code=404, detail="Device not found")
    return device


async def _require_owned_binding(
    service: ConnectorGateway,
    principal: AuthPrincipal,
    binding_id: str,
) -> dict:
    binding = next(
        (
            value
            for value in await service.list_bindings()
            if value["binding_id"] == binding_id
        ),
        None,
    )
    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")
    await _require_owned_device(service, principal, str(binding["device_id"]))
    return binding


async def _handle_envelope(
    service: ConnectorGateway,
    device_id: str,
    envelope: ConnectorEnvelope,
    connection_generation: int,
):
    if envelope.type == "hello":
        return await service.apply_hello(
            device_id,
            envelope.payload,
            expected_generation=connection_generation,
        )
    if envelope.type == "heartbeat":
        await service.heartbeat(
            device_id,
            envelope.payload,
            expected_generation=connection_generation,
        )
        return {"accepted": True}
    if envelope.type == "inventory.snapshot":
        raw_runtimes = envelope.payload.get("runtimes", [])
        if not isinstance(raw_runtimes, list):
            raise ConnectorError(422, "inventory.snapshot runtimes must be a list")
        runtimes = [RuntimeInventoryItem(**item) for item in raw_runtimes]
        device = await service.update_inventory(
            device_id,
            runtimes,
            envelope.payload.get("host"),
            expected_generation=connection_generation,
        )
        return {"accepted": True, "runtime_count": len(device["runtimes"])}
    if envelope.type == "command.ack":
        return await service.acknowledge_command(
            device_id,
            envelope.payload,
            expected_generation=connection_generation,
        )
    if envelope.type == "runtime.event":
        return await service.append_runtime_event(
            device_id,
            envelope.payload,
            expected_generation=connection_generation,
        )
    if envelope.type == "agent_task.result":
        return await service.submit_agent_task_result(
            device_id,
            envelope.payload,
            expected_generation=connection_generation,
        )
    raise ConnectorError(422, f"Unsupported message type: {envelope.type}")


async def _call(awaitable):
    try:
        return await awaitable
    except ConnectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _call_auth(awaitable):
    try:
        return await awaitable
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _enforce_rate_limit(
    limiter: SlidingWindowRateLimiter | None,
    request: Request,
    scope: str,
) -> None:
    if limiter is None:
        return
    client_host = request.client.host if request.client is not None else "unknown"
    try:
        await limiter.check(f"{scope}:{client_host}")
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many requests; retry later",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
