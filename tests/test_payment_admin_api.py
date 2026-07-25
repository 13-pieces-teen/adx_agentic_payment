from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_payments.admin_api import create_payment_admin_router
from connector_gateway.auth import AuthPrincipal


class _Auth:
    def __init__(self, subject: str) -> None:
        self.subject = subject

    async def authenticate(self, _request) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-admin",
            username="admin",
            temporary=False,
            session_token_hash="session-hash",
            csrf_hash="csrf-hash",
            identity_provider="github",
            provider_subject=self.subject,
        )


class _Repository:
    async def admin_snapshot(self, *, limit: int) -> dict[str, object]:
        assert limit == 25
        return {
            "counts": {"wallets": 1},
            "wallets": [
                {
                    "walletId": "wallet-1",
                    "address": "0x" + "11" * 20,
                    "status": "bound",
                }
            ],
            "mandates": [],
            "settlements": [],
        }


def _client(subject: str) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_payment_admin_router(
            auth=_Auth(subject),  # type: ignore[arg-type]
            repository=_Repository(),  # type: ignore[arg-type]
            github_subjects=frozenset({"123456"}),
            facilitator_id="arena402-testnet",
            signer_mode="external",
        )
    )
    return TestClient(app)


def test_admin_snapshot_requires_allowlisted_github_subject() -> None:
    denied = _client("999999").get("/api/v1/admin/payments?limit=25")
    assert denied.status_code == 403

    allowed = _client("123456").get("/api/v1/admin/payments?limit=25")
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["schemaVersion"] == "arena402.payment-admin.v1"
    assert body["facilitator"]["configured"] is True
    assert body["signer"]["mode"] == "external"
    assert "secret" not in str(body).lower()
