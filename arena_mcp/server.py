"""Feature-gated MCP 2026-07-28 Streamable HTTP adapter for Arena tasks."""

from __future__ import annotations

import base64
import json
from typing import Any, Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse

from arena_agent_contracts import AgentTaskResultV1
from connector_gateway.service import ConnectorError

from .auth import ExecutionTokenCodec, ExecutionTokenError, MCP_SCOPES
from .broker import ArenaMCPBrokerError, ArenaTaskBroker


MCP_PROTOCOL_VERSION: Final = "2026-07-28"
MCP_SERVER_INFO: Final[dict[str, str]] = {
    "name": "arena402_mcp",
    "version": "0.1.0",
}


class _StrictInput(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: name.split("_")[0]
        + "".join(part.capitalize() for part in name.split("_")[1:]),
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )


class ExecutionTokenRequest(_StrictInput):
    device_id: str = Field(min_length=1, max_length=256)
    binding_id: str = Field(min_length=1, max_length=256)


class ClaimTaskInput(_StrictInput):
    task_id: str = Field(
        min_length=1,
        max_length=256,
        description="Arena AgentTask identifier from a task.available wake.",
    )


class TaskStatusInput(ClaimTaskInput):
    pass


class SubmitTaskResultInput(_StrictInput):
    result: AgentTaskResultV1 = Field(
        description="One strict arena.agent-result.v1 candidate result."
    )


class ReleaseTaskInput(ClaimTaskInput):
    pass


class SyncTasksInput(_StrictInput):
    cursor: str | None = Field(
        default=None,
        max_length=512,
        description="Opaque cursor returned by a previous sync call.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of pending task hints to return.",
    )


_OUTPUT_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "arena_claim_agent_task": {
        "type": "object",
        "properties": {
            "leaseId": {"type": "string"},
            "task": {"type": "object"},
            "execution": {"type": "object"},
        },
        "required": ["leaseId", "task", "execution"],
        "additionalProperties": False,
    },
    "arena_get_agent_task_status": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "status": {"type": "string"},
            "deadlineAt": {"type": "string"},
            "leaseExpiresAt": {"type": ["string", "null"]},
            "hasAuthoritativeResult": {"type": "boolean"},
            "resultDisposition": {"type": ["string", "null"]},
        },
        "required": [
            "taskId",
            "status",
            "deadlineAt",
            "leaseExpiresAt",
            "hasAuthoritativeResult",
            "resultDisposition",
        ],
        "additionalProperties": False,
    },
    "arena_submit_agent_task_result": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "resultId": {"type": "string"},
            "disposition": {"type": "string"},
            "taskStatus": {"type": "string"},
            "authoritativeResultId": {"type": ["string", "null"]},
            "resultReceivedAt": {"type": "string"},
        },
        "required": [
            "taskId",
            "resultId",
            "disposition",
            "taskStatus",
            "authoritativeResultId",
            "resultReceivedAt",
        ],
        "additionalProperties": False,
    },
    "arena_release_agent_task": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "released": {"type": "boolean"},
        },
        "required": ["taskId", "released"],
        "additionalProperties": False,
    },
    "arena_sync_agent_tasks": {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "object"}},
            "hasMore": {"type": "boolean"},
            "nextCursor": {"type": ["string", "null"]},
        },
        "required": ["tasks", "hasMore", "nextCursor"],
        "additionalProperties": False,
    },
}


def _tool(
    *,
    name: str,
    title: str,
    description: str,
    input_model: type[BaseModel],
    read_only: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_model.model_json_schema(
            by_alias=True,
            mode="validation",
        ),
        "outputSchema": _OUTPUT_SCHEMAS[name],
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


TOOLS: Final[tuple[dict[str, Any], ...]] = (
    _tool(
        name="arena_claim_agent_task",
        title="Claim Arena Agent Task",
        description=(
            "Atomically claim one Arena-owned task assigned to this execution "
            "binding. Repeating the same claim returns the same active lease."
        ),
        input_model=ClaimTaskInput,
        read_only=False,
    ),
    _tool(
        name="arena_get_agent_task_status",
        title="Get Arena Agent Task Status",
        description=(
            "Read the authoritative Arena task status for this execution binding. "
            "This does not claim, execute, or apply a task."
        ),
        input_model=TaskStatusInput,
        read_only=True,
    ),
    _tool(
        name="arena_submit_agent_task_result",
        title="Submit Arena Agent Task Result",
        description=(
            "Submit one terminal candidate result through the Arena Result Sink. "
            "Provider success or this tool response never bypasses Arena validation."
        ),
        input_model=SubmitTaskResultInput,
        read_only=False,
    ),
    _tool(
        name="arena_release_agent_task",
        title="Release Arena Agent Task",
        description=(
            "Release a task lease owned by this execution binding before execution "
            "starts. This does not cancel or finalize the Arena task."
        ),
        input_model=ReleaseTaskInput,
        read_only=False,
    ),
    _tool(
        name="arena_sync_agent_tasks",
        title="Sync Pending Arena Agent Tasks",
        description=(
            "List bounded pending task hints for this execution binding after a "
            "startup or WSS reconnect. Claim each task separately before execution."
        ),
        input_model=SyncTasksInput,
        read_only=True,
    ),
)


def create_arena_mcp_router(
    *,
    broker: ArenaTaskBroker,
    token_codec: ExecutionTokenCodec,
    gateway: Any,
    allowed_origins: set[str],
) -> APIRouter:
    router = APIRouter(tags=["arena-mcp"])

    @router.post("/api/connectors/mcp/token")
    async def exchange_execution_token(request: Request) -> JSONResponse:
        try:
            payload = ExecutionTokenRequest.model_validate(await request.json())
        except (ValidationError, ValueError, json.JSONDecodeError):
            return JSONResponse(
                {"detail": "Invalid execution token request"},
                status_code=422,
            )
        scheme, _, device_token = request.headers.get("authorization", "").partition(
            " "
        )
        if scheme.lower() != "device" or not device_token:
            return JSONResponse(
                {"detail": "Missing device authorization"},
                status_code=401,
                headers={"WWW-Authenticate": "Device"},
            )
        try:
            await gateway.authenticate_device(
                payload.device_id,
                device_token,
            )
            binding = next(
                (
                    item
                    for item in await gateway.list_bindings(payload.device_id)
                    if item.get("binding_id") == payload.binding_id
                ),
                None,
            )
        except ConnectorError:
            binding = None
        if binding is None:
            return JSONResponse(
                {"detail": "Invalid device or binding authority"},
                status_code=401,
                headers={"WWW-Authenticate": "Device"},
            )
        token, claims = token_codec.issue(
            device_id=payload.device_id,
            binding_id=payload.binding_id,
            binding_epoch=int(binding.get("binding_epoch", 0)),
        )
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": claims.expires_at - claims.issued_at,
                "scope": " ".join(sorted(MCP_SCOPES)),
                "binding_id": claims.binding_id,
                "binding_epoch": claims.binding_epoch,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/mcp")
    async def mcp(request: Request) -> JSONResponse:
        origin = request.headers.get("origin")
        if origin is not None and origin.rstrip("/") not in allowed_origins:
            return _jsonrpc_error(
                request_id=None,
                code=-33003,
                message="Origin is not allowed",
                status_code=403,
            )
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _jsonrpc_error(
                request_id=None,
                code=-32700,
                message="Parse error",
                status_code=400,
            )
        request_id = (
            body.get("id")
            if isinstance(body, dict)
            and isinstance(body.get("id"), (str, int))
            and not isinstance(body.get("id"), bool)
            else None
        )
        protocol_error = _validate_protocol_request(request, body, request_id)
        if protocol_error is not None:
            return protocol_error
        assert isinstance(body, dict)
        method = str(body["method"])
        params = body["params"]

        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            return _jsonrpc_error(
                request_id=request_id,
                code=-33001,
                message="Missing Arena execution authorization",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            principal = token_codec.decode(bearer)
        except ExecutionTokenError:
            return _jsonrpc_error(
                request_id=request_id,
                code=-33001,
                message="Invalid or expired Arena execution authorization",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        if method == "server/discover":
            return _jsonrpc_result(
                request_id,
                {
                    "resultType": "complete",
                    "supportedVersions": [MCP_PROTOCOL_VERSION],
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": (
                        "Use sync after startup or reconnect, claim a task before "
                        "local execution, and submit exactly one terminal candidate "
                        "result. Arena remains authoritative for deadlines and actions."
                    ),
                    "ttlMs": 300_000,
                    "cacheScope": "private",
                },
            )
        if method == "tools/list":
            cursor = params.get("cursor")
            if cursor is not None and cursor != "":
                return _jsonrpc_error(
                    request_id=request_id,
                    code=-32602,
                    message="Invalid params: tools list has no additional page",
                    status_code=400,
                )
            return _jsonrpc_result(
                request_id,
                {
                    "resultType": "complete",
                    "tools": list(TOOLS),
                    "ttlMs": 300_000,
                    "cacheScope": "private",
                },
            )
        if method != "tools/call":
            return _jsonrpc_error(
                request_id=request_id,
                code=-32601,
                message="Method not found",
                status_code=404,
            )

        name = str(params.get("name") or "")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _jsonrpc_error(
                request_id=request_id,
                code=-32602,
                message="Invalid params: tool arguments must be an object",
                status_code=400,
            )
        try:
            value = await _call_tool(
                broker=broker,
                principal=principal,
                name=name,
                arguments=arguments,
            )
        except ValidationError as exc:
            return _jsonrpc_result(
                request_id,
                _tool_error(
                    "invalid_arguments",
                    _safe_validation_message(exc),
                ),
            )
        except ArenaMCPBrokerError as exc:
            return _jsonrpc_result(
                request_id,
                _tool_error(exc.code, exc.message),
            )
        except Exception:
            return _jsonrpc_result(
                request_id,
                _tool_error(
                    "internal_error",
                    "Arena MCP could not complete the operation; retry safely",
                ),
            )
        return _jsonrpc_result(request_id, _tool_success(value))

    return router


async def _call_tool(
    *,
    broker: ArenaTaskBroker,
    principal: Any,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if name == "arena_claim_agent_task":
        value = ClaimTaskInput.model_validate(arguments)
        return await broker.claim(
            principal=principal,
            task_id=value.task_id,
        )
    if name == "arena_get_agent_task_status":
        value = TaskStatusInput.model_validate(arguments)
        return await broker.status(
            principal=principal,
            task_id=value.task_id,
        )
    if name == "arena_submit_agent_task_result":
        value = SubmitTaskResultInput.model_validate(arguments)
        return await broker.submit(
            principal=principal,
            result=value.result,
        )
    if name == "arena_release_agent_task":
        value = ReleaseTaskInput.model_validate(arguments)
        return await broker.release(
            principal=principal,
            task_id=value.task_id,
        )
    if name == "arena_sync_agent_tasks":
        value = SyncTasksInput.model_validate(arguments)
        return await broker.sync(
            principal=principal,
            cursor=value.cursor,
            limit=value.limit,
        )
    raise ArenaMCPBrokerError(
        "unknown_tool",
        "Unknown Arena MCP tool; refresh tools/list",
    )


def _validate_protocol_request(
    request: Request,
    body: Any,
    request_id: str | int | None,
) -> JSONResponse | None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type.lower() != "application/json":
        return _jsonrpc_error(
            request_id=request_id,
            code=-32600,
            message="Content-Type must be application/json",
            status_code=415,
        )
    accept = {
        value.strip().lower() for value in request.headers.get("accept", "").split(",")
    }
    if "application/json" not in accept or "text/event-stream" not in accept:
        return _jsonrpc_error(
            request_id=request_id,
            code=-32600,
            message=("Accept must include application/json and text/event-stream"),
            status_code=400,
        )
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or request_id is None
        or not isinstance(body.get("method"), str)
        or not isinstance(body.get("params"), dict)
    ):
        return _jsonrpc_error(
            request_id=request_id,
            code=-32600,
            message="Invalid Request",
            status_code=400,
        )
    params = body["params"]
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return _jsonrpc_error(
            request_id=request_id,
            code=-32602,
            message="Invalid params: request _meta is required",
            status_code=400,
        )
    body_version = meta.get("io.modelcontextprotocol/protocolVersion")
    client_info = meta.get("io.modelcontextprotocol/clientInfo")
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    if body_version != MCP_PROTOCOL_VERSION:
        return _jsonrpc_error(
            request_id=request_id,
            code=-32022,
            message="Unsupported protocol version",
            status_code=400,
            data={"supportedVersions": [MCP_PROTOCOL_VERSION]},
        )
    if not isinstance(capabilities, dict):
        return _jsonrpc_error(
            request_id=request_id,
            code=-32602,
            message="Invalid params: client capabilities are required",
            status_code=400,
        )
    if (
        not isinstance(client_info, dict)
        or not isinstance(client_info.get("name"), str)
        or not client_info["name"].strip()
        or not isinstance(client_info.get("version"), str)
        or not client_info["version"].strip()
    ):
        return _jsonrpc_error(
            request_id=request_id,
            code=-32602,
            message="Invalid params: client info is required",
            status_code=400,
        )
    header_version = request.headers.get("mcp-protocol-version")
    header_method = request.headers.get("mcp-method")
    if header_version != body_version or header_method != body["method"]:
        return _jsonrpc_error(
            request_id=request_id,
            code=-32020,
            message="Header mismatch",
            status_code=400,
        )
    name_header = request.headers.get("mcp-name")
    if body["method"] == "tools/call":
        body_name = params.get("name")
        if not isinstance(body_name, str) or not body_name:
            return _jsonrpc_error(
                request_id=request_id,
                code=-32602,
                message="Invalid params: tool name is required",
                status_code=400,
            )
        try:
            decoded_name = _decode_header_value(name_header)
        except ValueError:
            decoded_name = None
        if decoded_name != body_name:
            return _jsonrpc_error(
                request_id=request_id,
                code=-32020,
                message="Header mismatch",
                status_code=400,
            )
    elif name_header is not None:
        return _jsonrpc_error(
            request_id=request_id,
            code=-32020,
            message="Header mismatch",
            status_code=400,
        )
    return None


def _decode_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("=?base64?") and value.endswith("?="):
        encoded = value[len("=?base64?") : -2]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid encoded header") from exc
    if value != value.strip() or any(
        ord(char) < 0x20 or ord(char) > 0x7E for char in value
    ):
        raise ValueError("invalid header")
    return value


def _tool_success(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            }
        ],
        "structuredContent": value,
        "isError": False,
    }


def _tool_error(code: str, message: str) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "content": [
            {
                "type": "text",
                "text": f"{code}: {message}",
            }
        ],
        "isError": True,
    }


def _safe_validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False, include_url=False)
    if not errors:
        return "Tool arguments are invalid"
    first = errors[0]
    location = ".".join(str(item) for item in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    return f"{location}: {message}" if location else message


def _response_meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/serverInfo": dict(MCP_SERVER_INFO),
    }


def _jsonrpc_result(
    request_id: str | int,
    result: dict[str, Any],
) -> JSONResponse:
    payload = dict(result)
    payload["_meta"] = {
        **payload.get("_meta", {}),
        **_response_meta(),
    }
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": payload,
        }
    )


def _jsonrpc_error(
    *,
    request_id: str | int | None,
    code: int,
    message: str,
    status_code: int,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        },
        status_code=status_code,
        headers=headers,
    )


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "TOOLS",
    "create_arena_mcp_router",
]
