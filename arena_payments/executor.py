"""Narrow client for the internal Settlement service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .coordinator import X402ExecutionResult
from .models import SettlementTerms


class X402SettlementExecutor(Protocol):
    async def execute(
        self,
        *,
        terms: SettlementTerms,
        mandate_id: str,
        payment_payload: dict[str, Any],
        now: datetime | None = None,
    ) -> X402ExecutionResult: ...


class HttpX402SettlementExecutor:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urlsplit(base_url)
        internal_hosts = {"settlement-worker", "localhost", "127.0.0.1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in internal_hosts
        ):
            raise ValueError("settlement_service_url_must_be_https_or_internal")
        if len(bearer_token) < 32:
            raise ValueError("settlement_service_token_too_short")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = timeout_seconds

    async def execute(
        self,
        *,
        terms: SettlementTerms,
        mandate_id: str,
        payment_payload: dict[str, Any],
        now: datetime | None = None,
    ) -> X402ExecutionResult:
        del now
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._base_url + "/internal/v1/x402/execute",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "settlementIntentId": terms.settlement_intent_id,
                        "mandateId": mandate_id,
                        "paymentPayload": payment_payload,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            return X402ExecutionResult(
                success=False,
                status="unknown",
                transaction=None,
                network=f"eip155:{terms.chain_id}",
                error_reason="settlement_service_unreachable",
            )
        try:
            value = response.json()
        except ValueError:
            value = None
        if response.status_code >= 500 or response.status_code in {
            408,
            425,
            429,
        }:
            return X402ExecutionResult(
                success=False,
                status="unknown",
                transaction=None,
                network=f"eip155:{terms.chain_id}",
                error_reason="settlement_service_response_unknown",
            )
        if response.status_code != 200:
            return X402ExecutionResult(
                success=False,
                status="failed",
                transaction=None,
                network=f"eip155:{terms.chain_id}",
                error_reason="settlement_service_rejected",
            )
        if (
            not isinstance(value, dict)
            or type(value.get("success")) is not bool
            or value.get("status") not in {"submitted", "unknown", "failed"}
            or not isinstance(value.get("transaction"), str)
            or not isinstance(value.get("network"), str)
        ):
            return X402ExecutionResult(
                success=False,
                status="unknown",
                transaction=None,
                network=f"eip155:{terms.chain_id}",
                error_reason="settlement_service_response_unknown",
            )
        return X402ExecutionResult(
            success=value["success"],
            status=value["status"],
            transaction=(
                str(value["transaction"]) if value.get("transaction") else None
            ),
            network=value["network"],
            payer=str(value["payer"]) if value.get("payer") else None,
            error_reason=(
                str(value["errorReason"]) if value.get("errorReason") else None
            ),
        )


__all__ = ["HttpX402SettlementExecutor", "X402SettlementExecutor"]
