from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_payments.coordinator import X402ExecutionResult
from arena_payments.internal_api import create_internal_settlement_router
from tests.test_arena_payments import _mandate, _terms


TOKEN = "s" * 48


class _Source:
    async def settlement_terms(self, settlement_intent_id: str):
        assert settlement_intent_id == "intent-1"
        return _terms()

    async def active_mandate(
        self,
        settlement_intent_id: str,
        now: datetime,
    ):
        assert settlement_intent_id == "intent-1"
        assert now.utcoffset() is not None
        return _mandate()


class _Coordinator:
    async def execute(self, **kwargs):
        assert kwargs["mandate_id"] == "mandate-1"
        assert kwargs["terms"].settlement_intent_id == "intent-1"
        assert kwargs["payment_payload"] == {"signed": True}
        return X402ExecutionResult(
            success=True,
            status="submitted",
            transaction="0x" + "55" * 32,
            network="eip155:1439",
        )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(
        create_internal_settlement_router(
            source=_Source(),  # type: ignore[arg-type]
            coordinator=_Coordinator(),  # type: ignore[arg-type]
            bearer_token=TOKEN,
        )
    )
    return TestClient(app)


def test_internal_settlement_ingress_requires_service_capability() -> None:
    response = _client().post(
        "/internal/v1/x402/execute",
        json={
            "settlementIntentId": "intent-1",
            "mandateId": "mandate-1",
            "paymentPayload": {"signed": True},
        },
    )
    assert response.status_code == 401


def test_internal_settlement_ingress_revalidates_and_executes() -> None:
    response = _client().post(
        "/internal/v1/x402/execute",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "settlementIntentId": "intent-1",
            "mandateId": "mandate-1",
            "paymentPayload": {"signed": True},
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
