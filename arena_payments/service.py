"""Use cases for stable wallets, bounded mandates, and x402 declarations."""

from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import PaymentMandate, SettlementTerms, UserWalletBinding
from .repository import PaymentRepository
from .x402 import X402ProtocolError


class ArenaPaymentService:
    def __init__(self, *, repository: PaymentRepository) -> None:
        self.repository = repository

    async def get_or_bind_github_wallet(
        self,
        *,
        user_id: str,
        identity_provider: str,
        provider_subject: str | None,
        now: datetime | None = None,
    ) -> UserWalletBinding:
        return await self.repository.get_or_bind_wallet(
            user_id=user_id,
            identity_provider=identity_provider,
            provider_subject=provider_subject,
            now=now or datetime.now(timezone.utc),
        )

    async def create_mandate(self, mandate: PaymentMandate) -> PaymentMandate:
        return await self.repository.create_mandate(mandate)

    @staticmethod
    def payment_required(terms: SettlementTerms) -> dict[str, Any]:
        return {
            "x402Version": 2,
            "resource": {
                "url": terms.resource_url,
                "description": (f"Arena 402 settlement {terms.settlement_intent_id}"),
                "mimeType": "application/json",
            },
            "accepts": [
                {
                    "scheme": "exact",
                    "network": f"eip155:{terms.chain_id}",
                    "asset": terms.token_address,
                    "amount": str(terms.amount_atomic),
                    "payTo": terms.payee,
                    "maxTimeoutSeconds": 600,
                    "extra": {
                        "name": terms.token_eip712_name,
                        "version": terms.token_eip712_version,
                        "arena402IntentHash": terms.intent_hash,
                        "arena402SettlementIntentId": (terms.settlement_intent_id),
                    },
                }
            ],
        }

    def validate_payment_payload(
        self,
        terms: SettlementTerms,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        required = self.payment_required(terms)
        if payload.get("x402Version") != 2:
            raise X402ProtocolError("unsupported_x402_version")
        if not self._same_json(payload.get("resource"), required["resource"]):
            raise X402ProtocolError("payment_resource_mismatch")
        if not self._same_json(
            payload.get("accepted"),
            required["accepts"][0],
        ):
            raise X402ProtocolError("payment_requirement_mismatch")
        scheme_payload = payload.get("payload")
        if not isinstance(scheme_payload, dict):
            raise X402ProtocolError("invalid_payment_payload")
        signature = scheme_payload.get("signature")
        authorization = scheme_payload.get("authorization")
        if (
            not isinstance(signature, str)
            or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature)
            or not isinstance(authorization, dict)
        ):
            raise X402ProtocolError("invalid_payment_payload")
        expected_nonce = "0x" + terms.intent_hash.removeprefix("sha256:")
        expected = {
            "from": terms.payer,
            "to": terms.payee,
            "value": str(terms.amount_atomic),
            "nonce": expected_nonce,
        }
        for key, value in expected.items():
            supplied = authorization.get(key)
            if key in {"from", "to", "nonce"} and isinstance(supplied, str):
                supplied = supplied.lower()
                value = value.lower()
            if supplied != value:
                raise X402ProtocolError("authorization_intent_mismatch")
        try:
            valid_after = int(authorization["validAfter"])
            valid_before = int(authorization["validBefore"])
        except (KeyError, TypeError, ValueError):
            raise X402ProtocolError("invalid_authorization_window") from None
        timestamp = int((now or datetime.now(timezone.utc)).timestamp())
        if valid_after > timestamp or valid_before <= timestamp:
            raise X402ProtocolError("authorization_outside_valid_window")
        if valid_before - timestamp > 600:
            raise X402ProtocolError("authorization_window_too_long")
        return scheme_payload

    @staticmethod
    def _same_json(left: object, right: object) -> bool:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        encoded_left = json.dumps(
            left, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        encoded_right = json.dumps(
            right, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hmac.compare_digest(encoded_left, encoded_right)
