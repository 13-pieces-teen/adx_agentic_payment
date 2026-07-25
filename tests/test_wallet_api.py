from __future__ import annotations

from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    Prehashed,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena_wallets import (
    InjectiveWalletService,
    MemoryWalletRepository,
    WalletTokenConfig,
    keccak256,
    recover_personal_signer,
)
from arena_wallets.crypto import _SECP256K1_N
from connector_gateway.auth import AuthPrincipal
from web.wallet_api import create_wallet_router


ADDRESS = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"


class _Auth:
    async def authenticate(self, _request) -> AuthPrincipal:
        return AuthPrincipal(
            user_id="user-1",
            username="alice",
            temporary=False,
            session_token_hash="session-hash",
            csrf_hash="csrf-hash",
            identity_provider="github",
            provider_subject="123456",
        )

    async def require_csrf(self, _request, _principal) -> None:
        return None


def _personal_sign(message: str) -> tuple[str, str]:
    private_key = ec.derive_private_key(1, ec.SECP256K1())
    encoded = message.encode("utf-8")
    digest = keccak256(
        b"\x19Ethereum Signed Message:\n"
        + str(len(encoded)).encode("ascii")
        + encoded
    )
    signature = private_key.sign(
        digest,
        ec.ECDSA(Prehashed(hashes.SHA256())),
    )
    r, s = decode_dss_signature(signature)
    if s > _SECP256K1_N // 2:
        s = _SECP256K1_N - s
    public = private_key.public_key().public_numbers()
    expected = "0x" + keccak256(
        public.x.to_bytes(32, "big") + public.y.to_bytes(32, "big")
    )[-20:].hex()
    for recovery_id in (0, 1):
        candidate = "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + f"{27 + recovery_id:02x}"
        try:
            if recover_personal_signer(message, candidate) == expected:
                return expected, candidate
        except ValueError:
            pass
    raise AssertionError("could not construct a recoverable test signature")


def _client() -> tuple[TestClient, MemoryWalletRepository]:
    repository = MemoryWalletRepository()

    def rpc(method: str, _params: list[object]) -> str:
        if method == "eth_chainId":
            return "0x59f"
        if method == "eth_getBalance":
            return "0xde0b6b3a7640000"
        return "0x0"

    service = InjectiveWalletService(
        "https://rpc.example.test",
        tokens=(WalletTokenConfig("arena402-g", "0x" + "11" * 20, 6),),
        rpc_call=rpc,
    )
    app = FastAPI()
    app.include_router(
        create_wallet_router(
            auth=_Auth(),
            repository=repository,
            service=service,
        )
    )
    return TestClient(app), repository


def test_keccak_uses_ethereum_legacy_variant_and_recovers_personal_signer() -> None:
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    message = "Arena 402 wallet challenge"
    address, signature = _personal_sign(message)
    assert address == ADDRESS
    assert recover_personal_signer(message, signature) == address


def test_wallet_binding_overview_and_delete_are_session_scoped() -> None:
    client, repository = _client()
    challenge = client.post(
        "/api/wallet/challenge",
        headers={"x-csrf-token": "csrf"},
        json={"address": ADDRESS, "chainId": 1439},
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    assert "private" not in challenge.text.lower()
    address, signature = _personal_sign(body["message"])
    verified = client.post(
        "/api/wallet/verify",
        headers={"x-csrf-token": "csrf"},
        json={
            "challengeId": body["challengeId"],
            "address": address,
            "message": body["message"],
            "signature": signature,
        },
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["wallet"]["address"] == ADDRESS

    wallet = client.get("/api/wallet")
    assert wallet.status_code == 200
    assert wallet.json()["chainId"] == 1439
    overview = client.get("/api/wallet/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["native"]["balance"] == "1"
    assert overview.json()["tokens"][0]["balance"] == "0"
    assert repository.bindings["user-1"].address == ADDRESS

    deleted = client.delete("/api/wallet", headers={"x-csrf-token": "csrf"})
    assert deleted.status_code == 204
    assert client.get("/api/wallet").status_code == 404


def test_wallet_activity_does_not_return_other_users_transactions() -> None:
    client, repository = _client()
    repository.bindings["user-1"] = type(
        "Binding",
        (),
        {
            "user_id": "user-1",
            "chain_id": 1439,
            "address": ADDRESS,
            "verified_at": datetime.now(timezone.utc),
        },
    )()
    repository.activity.append(
        {
            "settlement_intent_id": "intent-1",
            "game_id": "game-1",
            "round_id": "round-1",
            "round_index": 1,
            "good_id": "grain",
            "good_name": "Grain",
            "quantity": 1,
            "unit_price_atomic": 100,
            "amount_atomic": 100,
            "chain_id": 1439,
            "token_address": "0x" + "22" * 20,
            "token_symbol": "arena402-g",
            "token_decimals": 6,
            "buyer_account": ADDRESS,
            "seller_account": "0x" + "33" * 20,
            "buyer_username": "alice",
            "seller_username": "bob",
            "status": "inventory_committed",
            "tx_hash": "0x" + "44" * 32,
            "block_number": 10,
            "chain_confirmed_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }
    )
    response = client.get("/api/wallet/activity")
    assert response.status_code == 200
    transaction = response.json()["transactions"][0]
    assert transaction["buyer"]["address"] == ADDRESS
    assert transaction["explorerUrl"].endswith("/tx/0x" + "44" * 32)
