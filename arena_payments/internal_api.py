"""Authenticated, non-public execution ingress owned by Settlement."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .coordinator import X402SettlementCoordinator
from .postgres_worker import PostgresAutomaticSettlementSource


class ExecutePaymentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    settlement_intent_id: str = Field(
        alias="settlementIntentId",
        min_length=1,
        max_length=512,
    )
    mandate_id: str = Field(alias="mandateId", min_length=1, max_length=128)
    payment_payload: dict[str, Any] = Field(alias="paymentPayload")


def create_internal_settlement_router(
    *,
    source: PostgresAutomaticSettlementSource,
    coordinator: X402SettlementCoordinator,
    bearer_token: str,
) -> APIRouter:
    if len(bearer_token) < 32:
        raise ValueError("settlement_service_token_too_short")
    router = APIRouter()

    @router.post("/internal/v1/x402/execute")
    async def execute(
        body: ExecutePaymentRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        expected = f"Bearer {bearer_token}"
        if authorization is None or not hmac.compare_digest(
            authorization,
            expected,
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        now = datetime.now(timezone.utc)
        terms = await source.settlement_terms(body.settlement_intent_id)
        mandate = await source.active_mandate(
            body.settlement_intent_id,
            now,
        )
        if mandate is None or mandate.mandate_id != body.mandate_id:
            raise HTTPException(
                status_code=409,
                detail="payment_mandate_not_active",
            )
        result = await coordinator.execute(
            terms=terms,
            mandate_id=mandate.mandate_id,
            payment_payload=body.payment_payload,
            now=now,
        )
        return result.to_response()

    return router


__all__ = ["create_internal_settlement_router"]
