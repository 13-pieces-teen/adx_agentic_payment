"""FastAPI routes for device enrollment, control and outbound WSS transport."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .models import (
    ApprovePairingRequest,
    ConnectorEnvelope,
    CreateBindingRequest,
    CreateCommandRequest,
    CreatePairingRequest,
    ExchangePairingRequest,
    RevokeDeviceRequest,
    RuntimeInventoryItem,
)
from .service import ConnectorError, ConnectorGateway


def create_connector_router(service: ConnectorGateway) -> APIRouter:
    router = APIRouter(prefix="/api/connectors", tags=["connectors"])

    @router.post("/pairings", status_code=201)
    async def create_pairing(req: CreatePairingRequest):
        return await _call(service.create_pairing(req.owner_id, req.device_name))

    @router.post("/pairings/{user_code}/approve")
    async def approve_pairing(user_code: str, req: ApprovePairingRequest):
        return await _call(service.approve_pairing(user_code, req.owner_id))

    @router.post("/pairings/exchange")
    async def exchange_pairing(req: ExchangePairingRequest):
        return await _call(service.exchange_pairing(req.device_code))

    @router.get("/devices")
    async def list_devices(
        owner_id: Optional[str] = Query(default=None, max_length=128)
    ):
        devices = await service.list_devices(owner_id)
        return {"total": len(devices), "devices": devices}

    @router.get("/devices/{device_id}")
    async def get_device(device_id: str):
        return await _call(service.get_device(device_id))

    @router.post("/devices/{device_id}/revoke")
    async def revoke_device(device_id: str, req: RevokeDeviceRequest):
        return await _call(service.revoke_device(device_id, req.owner_id))

    @router.post("/devices/{device_id}/bindings", status_code=201)
    async def create_binding(device_id: str, req: CreateBindingRequest):
        return await _call(
            service.create_binding(
                device_id,
                req.runtime_id,
                req.agent_id,
                req.display_name,
            )
        )

    @router.get("/bindings")
    async def list_bindings(device_id: Optional[str] = None):
        bindings = await service.list_bindings(device_id)
        return {"total": len(bindings), "bindings": bindings}

    @router.post("/bindings/{binding_id}/commands", status_code=202)
    async def create_command(binding_id: str, req: CreateCommandRequest):
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
    async def list_commands(binding_id: str, limit: int = Query(100, ge=1, le=500)):
        commands = await _call(service.list_commands(binding_id, limit))
        return {"total": len(commands), "commands": commands}

    @router.get("/bindings/{binding_id}/events")
    async def list_events(binding_id: str, limit: int = Query(200, ge=1, le=1000)):
        events = await _call(service.list_events(binding_id, limit))
        return {"total": len(events), "events": events}

    @router.get("/audit")
    async def list_audit(limit: int = Query(200, ge=1, le=1000)):
        audit = await service.list_audit(limit)
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
                        ack_type = (
                            "event.ack" if envelope.type == "runtime.event" else "ack"
                        )
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
    raise ConnectorError(422, f"Unsupported message type: {envelope.type}")


async def _call(awaitable):
    try:
        return await awaitable
    except ConnectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
