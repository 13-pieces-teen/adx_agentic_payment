from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.ledger_api import (
    LedgerMetadata,
    create_ledger_router,
    decode_ledger_cursor,
    load_ledger_metadata_from_env,
)


def _trade(
    trade_id: str,
    *,
    created_at: str,
) -> dict[str, object]:
    return {
        "tradeId": trade_id,
        "gameId": "game-1",
        "round": 2,
        "goodId": "iron",
        "quantity": 1,
        "priceAtomic": "7000000",
        "amountAtomic": "7000000",
        "buyer": {
            "agentId": "buyer-1",
            "displayName": "Buyer One",
            "accountAddress": "0x" + "11" * 20,
        },
        "seller": {
            "agentId": "seller-1",
            "displayName": "Seller One",
            "accountAddress": "0x" + "22" * 20,
        },
        "pairingId": "pairing-1",
        "chainId": 1439,
        "txHash": "0x" + "33" * 32,
        "blockNumber": "100",
        "chainConfirmedAt": "2026-07-26T01:01:00+00:00",
        "facilitatorAddress": "0x" + "44" * 20,
        "status": "inventory_committed",
        "createdAt": created_at,
        "schemaVersion": "arena402.trade-ledger-entry.v1",
    }


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.values = [
            _trade(
                "trade-2",
                created_at="2026-07-26T02:00:00.123456+00:00",
            ),
            _trade(
                "trade-1",
                created_at="2026-07-26T01:00:00.123456+00:00",
            ),
        ]

    async def ledger_trades(self, **values):
        self.calls.append(values)
        return self.values

    async def ledger_stats(self):
        return {
            "totalTrades": 7,
            "totalAmountAtomic": "49000000",
            "agentCount": 4,
        }


def _client() -> tuple[TestClient, _Repository]:
    repository = _Repository()
    app = FastAPI()
    app.include_router(
        create_ledger_router(
            repository=repository,
            metadata=LedgerMetadata(
                chain_id=1439,
                explorer_tx_url_template=("https://explorer.example/tx/{txHash}"),
            ),
        )
    )
    return TestClient(app), repository


def test_trade_ledger_filters_and_returns_opaque_cursor() -> None:
    client, repository = _client()

    response = client.get(
        "/api/v1/ledger/trades",
        params={
            "gameId": "game-1",
            "agentId": "buyer-1",
            "goodId": "iron",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["trades"] == repository.values[:1]
    assert body["chainId"] == 1439
    assert body["explorerTxUrlTemplate"] == "https://explorer.example/tx/{txHash}"
    assert body["nextAfter"]
    assert response.headers["cache-control"] == "public, max-age=5"
    cursor_time, cursor_id = decode_ledger_cursor(body["nextAfter"])
    assert cursor_time == datetime(
        2026,
        7,
        26,
        2,
        0,
        0,
        123456,
        tzinfo=timezone.utc,
    )
    assert cursor_id == "trade-2"
    assert repository.calls == [
        {
            "game_id": "game-1",
            "agent_id": "buyer-1",
            "good_id": "iron",
            "after_created_at": None,
            "after_trade_id": None,
            "limit": 2,
        }
    ]


def test_trade_ledger_decodes_after_cursor_for_repository() -> None:
    client, repository = _client()
    first = client.get(
        "/api/v1/ledger/trades",
        params={"limit": 1},
    ).json()
    repository.values = []

    response = client.get(
        "/api/v1/ledger/trades",
        params={"after": first["nextAfter"], "limit": 10},
    )

    assert response.status_code == 200
    assert response.json()["trades"] == []
    call = repository.calls[-1]
    assert call["after_trade_id"] == "trade-2"
    assert call["after_created_at"] == datetime(
        2026,
        7,
        26,
        2,
        0,
        0,
        123456,
        tzinfo=timezone.utc,
    )
    assert call["limit"] == 11


def test_trade_ledger_rejects_invalid_cursor() -> None:
    client, _ = _client()

    response = client.get(
        "/api/v1/ledger/trades",
        params={"after": "not-a-cursor"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "invalid_ledger_cursor"}}


def test_ledger_stats_are_confirmed_chain_totals() -> None:
    client, _ = _client()

    response = client.get("/api/v1/ledger/stats")

    assert response.status_code == 200
    assert response.json() == {
        "totalTrades": 7,
        "totalAmountAtomic": "49000000",
        "agentCount": 4,
        "chainId": 1439,
        "explorerTxUrlTemplate": ("https://explorer.example/tx/{txHash}"),
        "schemaVersion": "arena402.trade-ledger-stats.v1",
    }


def test_ledger_metadata_is_backend_configured(monkeypatch) -> None:
    monkeypatch.setenv("ADX_CURRENT_GAME_CHAIN_ID", "1776")
    monkeypatch.setenv(
        "ADX_ARENA_EXPLORER_TX_URL_TEMPLATE",
        "https://mainnet.example/tx/{txHash}",
    )

    metadata = load_ledger_metadata_from_env()

    assert metadata.chain_id == 1776
    assert metadata.explorer_tx_url_template == "https://mainnet.example/tx/{txHash}"
