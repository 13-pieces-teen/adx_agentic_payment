import asyncio

import pytest

from arena_payments.facilitator import (
    FacilitatorSettlement,
    HttpX402FacilitatorClient,
    ShardedFacilitatorClient,
    build_facilitator_client,
)
from arena_payments.signer import HttpWalletSignerClient


def test_cloud_payment_clients_allow_only_https_or_named_internal_services() -> None:
    HttpX402FacilitatorClient(
        "http://arena-facilitator:4021",
        facilitator_id="internal-testnet",
    )
    HttpWalletSignerClient(
        "http://wallet-signer:8787",
        bearer_token="x" * 32,
    )

    with pytest.raises(ValueError, match="https_or_internal"):
        HttpX402FacilitatorClient(
            "http://example.com",
            facilitator_id="unsafe",
        )
    with pytest.raises(ValueError, match="https_or_internal"):
        HttpWalletSignerClient(
            "http://example.com",
            bearer_token="x" * 32,
        )


class _RecordingFacilitator:
    def __init__(self) -> None:
        self.verified: list[str] = []
        self.settled: list[str] = []

    async def verify(self, *, payment_requirements, **_: object) -> bool:
        self.verified.append(
            payment_requirements["extra"]["arena402IntentHash"]
        )
        return True

    async def settle(
        self, *, payment_requirements, **_: object
    ) -> FacilitatorSettlement:
        self.settled.append(
            payment_requirements["extra"]["arena402IntentHash"]
        )
        return FacilitatorSettlement(
            success=True,
            transaction="0x" + "55" * 32,
            network=payment_requirements["network"],
        )


def _requirement(index: int) -> dict[str, object]:
    return {
        "network": "eip155:1439",
        "extra": {
            "arena402IntentHash": f"sha256:{index:064x}",
        },
    }


def test_four_facilitator_shards_route_each_intent_stably() -> None:
    shards = {
        f"shard-{index}": _RecordingFacilitator()
        for index in range(1, 5)
    }
    client = ShardedFacilitatorClient(shards)

    selected = {
        client.facilitator_id_for(_requirement(index))
        for index in range(256)
    }
    requirement = _requirement(42)
    expected = client.facilitator_id_for(requirement)

    assert selected == set(shards)
    assert client.facilitator_id_for(requirement) == expected
    assert asyncio.run(
        client.verify(payment_payload={}, payment_requirements=requirement)
    )
    settled = asyncio.run(
        client.settle(payment_payload={}, payment_requirements=requirement)
    )
    assert settled.facilitator_id == expected
    assert shards[expected].verified == [
        requirement["extra"]["arena402IntentHash"]
    ]
    assert shards[expected].settled == [
        requirement["extra"]["arena402IntentHash"]
    ]


def test_production_facilitator_builder_requires_all_four_shards() -> None:
    environment = {
        "ADX_X402_FACILITATOR_SHARD_COUNT": "4",
        **{
            f"ADX_X402_FACILITATOR_{index}_ID": f"shard-{index}"
            for index in range(1, 5)
        },
        **{
            f"ADX_X402_FACILITATOR_{index}_URL": (
                f"http://arena-facilitator-{index}:4021"
            )
            for index in range(1, 5)
        },
        **{
            f"ADX_X402_FACILITATOR_{index}_AUTHORIZATION": (
                f"Bearer {'x' * 31}{index}"
            )
            for index in range(1, 5)
        },
        **{
            f"ADX_X402_FACILITATOR_{index}_EOA": (
                "0x" + f"{index:040x}"
            )
            for index in range(1, 5)
        },
    }

    client = build_facilitator_client(environment)

    assert isinstance(client, ShardedFacilitatorClient)
    assert {
        client.facilitator_id_for(_requirement(index))
        for index in range(256)
    } == {
        "0x" + f"{index:040x}"
        for index in range(1, 5)
    }
    del environment["ADX_X402_FACILITATOR_4_URL"]
    with pytest.raises(
        RuntimeError,
        match="ADX_X402_FACILITATOR_4_URL is required",
    ):
        build_facilitator_client(environment)


def test_production_facilitator_builder_rejects_missing_or_duplicate_eoa() -> None:
    environment = {
        "ADX_X402_FACILITATOR_SHARD_COUNT": "2",
        "ADX_X402_FACILITATOR_1_ID": "shard-1",
        "ADX_X402_FACILITATOR_1_URL": "http://arena-facilitator-1:4021",
        "ADX_X402_FACILITATOR_1_EOA": "0x" + "11" * 20,
        "ADX_X402_FACILITATOR_2_ID": "shard-2",
        "ADX_X402_FACILITATOR_2_URL": "http://arena-facilitator-2:4021",
    }

    with pytest.raises(
        RuntimeError,
        match="ADX_X402_FACILITATOR_2_EOA is required",
    ):
        build_facilitator_client(environment)

    environment["ADX_X402_FACILITATOR_2_EOA"] = "0x" + "11" * 20
    with pytest.raises(RuntimeError, match="facilitator shard EOAs must be unique"):
        build_facilitator_client(environment)
