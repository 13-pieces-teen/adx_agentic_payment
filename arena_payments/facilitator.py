"""x402 facilitator port and HTTPS adapter."""

from __future__ import annotations

from dataclasses import dataclass
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
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in internal_http_hosts
        ):
            raise ValueError("facilitator_url_must_use_https_or_internal")
        if not facilitator_id or len(facilitator_id) > 128:
            raise ValueError("invalid_facilitator_id")
        self._base_url = base_url.rstrip("/")
        self._facilitator_id = facilitator_id
        self._timeout = timeout_seconds
        self._headers = {"Authorization": authorization} if authorization else {}

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
