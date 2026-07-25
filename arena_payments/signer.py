"""Narrow client for an isolated wallet signer process."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


class WalletSignerError(RuntimeError):
    pass


class WalletSignerClient(Protocol):
    async def create_payment_payload(
        self,
        *,
        payment_required: dict[str, Any],
        wallet_id: str,
        expected_from: str,
    ) -> dict[str, Any]: ...


class DisabledWalletSignerClient:
    async def create_payment_payload(self, **_: object) -> dict[str, Any]:
        raise WalletSignerError("wallet_signer_disabled")


class HttpWalletSignerClient:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(base_url)
        private_http_hosts = {"wallet-signer", "localhost", "127.0.0.1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in private_http_hosts
        ):
            raise ValueError("wallet_signer_url_must_be_https_or_internal")
        if len(bearer_token) < 32:
            raise ValueError("wallet_signer_token_too_short")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = timeout_seconds

    async def create_payment_payload(
        self,
        *,
        payment_required: dict[str, Any],
        wallet_id: str,
        expected_from: str,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    self._base_url + "/v1/x402/sign",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={
                        "paymentRequired": payment_required,
                        "walletId": wallet_id,
                        "expectedFrom": expected_from,
                    },
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WalletSignerError("wallet_signer_unreachable") from exc
        if response.status_code != 200:
            raise WalletSignerError("wallet_signer_rejected")
        try:
            value = response.json()
        except ValueError as exc:
            raise WalletSignerError("wallet_signer_invalid_response") from exc
        payload = value.get("paymentPayload") if isinstance(value, dict) else None
        if not isinstance(payload, dict):
            raise WalletSignerError("wallet_signer_invalid_response")
        return payload
