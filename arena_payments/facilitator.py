"""x402 facilitator port and HTTPS adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


class FacilitatorError(RuntimeError):
    """Safe classified facilitator failure."""

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class FacilitatorSettlement:
    success: bool
    transaction: str | None
    network: str
    payer: str | None = None
    error_reason: str | None = None
    facilitator_id: str = "configured"


class FacilitatorClient(Protocol):
    async def verify(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> bool: ...

    async def settle(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> FacilitatorSettlement: ...


class ShardedFacilitatorClient:
    """Deterministically route one frozen Intent to one relay shard."""

    def __init__(self, shards: Mapping[str, FacilitatorClient]) -> None:
        if not 2 <= len(shards) <= 64:
            raise ValueError("facilitator_shard_count_must_be_between_2_and_64")
        ordered: list[tuple[str, FacilitatorClient]] = []
        for facilitator_id, client in sorted(shards.items()):
            if (
                not facilitator_id
                or len(facilitator_id) > 128
                or not re.fullmatch(r"[A-Za-z0-9._:-]+", facilitator_id)
            ):
                raise ValueError("invalid_facilitator_id")
            ordered.append((facilitator_id, client))
        self._shards = tuple(ordered)

    def facilitator_id_for(
        self,
        payment_requirements: dict[str, Any],
    ) -> str:
        route_key = self._route_key(payment_requirements)
        digest = hashlib.sha256(route_key.encode("utf-8")).digest()
        return self._shards[
            int.from_bytes(digest[:8], "big") % len(self._shards)
        ][0]

    async def verify(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> bool:
        _, client = self._shard_for(payment_requirements)
        return await client.verify(
            payment_payload=payment_payload,
            payment_requirements=payment_requirements,
        )

    async def settle(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> FacilitatorSettlement:
        facilitator_id, client = self._shard_for(payment_requirements)
        result = await client.settle(
            payment_payload=payment_payload,
            payment_requirements=payment_requirements,
        )
        return replace(result, facilitator_id=facilitator_id)

    def _shard_for(
        self,
        payment_requirements: dict[str, Any],
    ) -> tuple[str, FacilitatorClient]:
        facilitator_id = self.facilitator_id_for(payment_requirements)
        for candidate_id, client in self._shards:
            if candidate_id == facilitator_id:
                return candidate_id, client
        raise AssertionError("selected facilitator shard is missing")

    @staticmethod
    def _route_key(payment_requirements: dict[str, Any]) -> str:
        extra = payment_requirements.get("extra")
        route_key = (
            extra.get("arena402IntentHash")
            if isinstance(extra, dict)
            else None
        )
        if not isinstance(route_key, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            route_key,
        ):
            raise FacilitatorError("facilitator_route_key_invalid")
        return route_key


class DisabledFacilitatorClient:
    async def verify(self, **_: object) -> bool:
        raise FacilitatorError("facilitator_disabled")

    async def settle(self, **_: object) -> FacilitatorSettlement:
        raise FacilitatorError("facilitator_disabled")


class HttpX402FacilitatorClient:
    """Calls a V2 facilitator without logging payment signatures."""

    def __init__(
        self,
        base_url: str,
        *,
        facilitator_id: str,
        timeout_seconds: float = 20.0,
        authorization: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        internal_http_hosts = {
            "arena-facilitator",
            "localhost",
            "127.0.0.1",
        }
        is_internal_facilitator_shard = bool(
            parsed.hostname
            and re.fullmatch(
                r"arena-facilitator-[1-9][0-9]*",
                parsed.hostname,
            )
        )
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and (
                parsed.hostname in internal_http_hosts
                or is_internal_facilitator_shard
            )
        ):
            raise ValueError("facilitator_url_must_use_https_or_internal")
        if not facilitator_id or len(facilitator_id) > 128:
            raise ValueError("invalid_facilitator_id")
        self._base_url = base_url.rstrip("/")
        self._facilitator_id = facilitator_id
        self._timeout = timeout_seconds
        self._headers = {"Authorization": authorization} if authorization else {}

    def facilitator_id_for(
        self,
        _: dict[str, Any],
    ) -> str:
        return self._facilitator_id

    async def verify(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> bool:
        value = await self._post(
            "/verify",
            payment_payload=payment_payload,
            payment_requirements=payment_requirements,
        )
        return value.get("isValid") is True

    async def settle(
        self,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> FacilitatorSettlement:
        value = await self._post(
            "/settle",
            payment_payload=payment_payload,
            payment_requirements=payment_requirements,
        )
        transaction = value.get("transaction")
        return FacilitatorSettlement(
            success=value.get("success") is True,
            transaction=(str(transaction) if isinstance(transaction, str) else None),
            network=str(value.get("network") or payment_requirements["network"]),
            payer=(str(value["payer"]) if value.get("payer") is not None else None),
            error_reason=(
                str(value["errorReason"])
                if value.get("errorReason") is not None
                else None
            ),
            facilitator_id=self._facilitator_id,
        )

    async def _post(
        self,
        path: str,
        *,
        payment_payload: dict[str, Any],
        payment_requirements: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "x402Version": 2,
            "paymentPayload": payment_payload,
            "paymentRequirements": payment_requirements,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._headers,
            ) as client:
                response = await client.post(self._base_url + path, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise FacilitatorError(
                "facilitator_unreachable",
                ambiguous=path == "/settle",
            ) from exc
        if response.status_code >= 500:
            raise FacilitatorError(
                "facilitator_unavailable",
                ambiguous=path == "/settle",
            )
        if response.status_code >= 400:
            raise FacilitatorError("facilitator_rejected")
        try:
            value = response.json()
        except ValueError as exc:
            raise FacilitatorError(
                "facilitator_invalid_response",
                ambiguous=path == "/settle",
            ) from exc
        if not isinstance(value, dict):
            raise FacilitatorError(
                "facilitator_invalid_response",
                ambiguous=path == "/settle",
            )
        return value


def build_facilitator_client(
    environment: Mapping[str, str],
) -> FacilitatorClient:
    """Build the configured single relay or deterministic shard set."""

    raw_count = environment.get(
        "ADX_X402_FACILITATOR_SHARD_COUNT",
        "1",
    ).strip()
    try:
        shard_count = int(raw_count)
    except ValueError as exc:
        raise RuntimeError(
            "ADX_X402_FACILITATOR_SHARD_COUNT must be an integer"
        ) from exc
    if shard_count == 1:
        return HttpX402FacilitatorClient(
            _required_environment(
                environment,
                "ADX_X402_FACILITATOR_URL",
            ),
            facilitator_id=_required_environment(
                environment,
                "ADX_X402_FACILITATOR_ID",
            ),
            authorization=(
                environment.get(
                    "ADX_X402_FACILITATOR_AUTHORIZATION",
                    "",
                ).strip()
                or None
            ),
        )
    if not 2 <= shard_count <= 64:
        raise RuntimeError(
            "ADX_X402_FACILITATOR_SHARD_COUNT must be between 1 and 64"
        )
    shards: dict[str, FacilitatorClient] = {}
    for index in range(1, shard_count + 1):
        prefix = f"ADX_X402_FACILITATOR_{index}"
        facilitator_id = _required_environment(
            environment,
            f"{prefix}_ID",
        )
        if facilitator_id in shards:
            raise RuntimeError("facilitator shard ids must be unique")
        shards[facilitator_id] = HttpX402FacilitatorClient(
            _required_environment(
                environment,
                f"{prefix}_URL",
            ),
            facilitator_id=facilitator_id,
            authorization=(
                environment.get(f"{prefix}_AUTHORIZATION", "").strip()
                or None
            ),
        )
    return ShardedFacilitatorClient(shards)


def _required_environment(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value
