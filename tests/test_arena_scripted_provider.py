from __future__ import annotations

import asyncio
import json

from hosted_agent_runtime.production_providers import (
    build_local_development_provider_bundle,
)
from hosted_agent_runtime.providers import (
    ARENA_SCRIPTED_BUYER_MODEL,
    ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
    ARENA_SCRIPTED_REJECTING_SELLER_MODEL,
    ARENA_SCRIPTED_PROVIDER_ID,
    ARENA_SCRIPTED_SELLER_MODEL,
    ProviderRequest,
)
from hosted_agent_runtime.secret_store import WorkerSecret


def _request(
    *,
    model: str,
    kind: str,
    role: str | None = None,
    arena_input: dict[str, object] | None = None,
) -> ProviderRequest:
    if arena_input is None:
        arena_input = {"phase": "decide"}
        if role is not None:
            arena_input = {"phase": "negotiate", "role": role}
    return ProviderRequest(
        attempt_id=f"attempt-{model}-{kind}-{role or 'none'}",
        task_id=f"task-{model}-{kind}-{role or 'none'}",
        task_kind=kind,
        idempotency_key=f"idempotency-{model}-{kind}-{role or 'none'}",
        model_id=model,
        prompt_version="arena.hosted-direct.v1",
        context_version="arena.agent-task.v1",
        output_version="arena.agent-action.v1",
        system_instructions="Return one structured action.",
        input_json=json.dumps({"untrustedArenaData": arena_input}),
        output_schema_json='{"type":"object"}',
        thinking_enabled=False,
        thinking_parameter_name=None,
        max_output_tokens=64,
        request_timeout_ms=1_000,
    )


def test_local_bundle_exposes_two_scripted_hosted_models_only_in_dev_bundle() -> None:
    bundle = build_local_development_provider_bundle()
    models = {
        (item.provider_id, item.model_id)
        for item in bundle.registry.list_public()
    }
    assert (ARENA_SCRIPTED_PROVIDER_ID, ARENA_SCRIPTED_BUYER_MODEL) in models
    assert (ARENA_SCRIPTED_PROVIDER_ID, ARENA_SCRIPTED_SELLER_MODEL) in models


def test_local_bundle_exposes_rejecting_models_for_full_game_safety_demo() -> None:
    bundle = build_local_development_provider_bundle()
    models = {
        (item.provider_id, item.model_id)
        for item in bundle.registry.list_public()
    }
    assert (
        ARENA_SCRIPTED_PROVIDER_ID,
        ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
    ) in models
    assert (
        ARENA_SCRIPTED_PROVIDER_ID,
        ARENA_SCRIPTED_REJECTING_SELLER_MODEL,
    ) in models


def test_local_bundle_exposes_fallback_buyer_that_publishes_buy_intent() -> None:
    async def run() -> dict[str, object]:
        bundle = build_local_development_provider_bundle()
        models = {
            (item.provider_id, item.model_id)
            for item in bundle.registry.list_public()
        }
        assert (
            ARENA_SCRIPTED_PROVIDER_ID,
            "arena-fallback-buyer-v1",
        ) in models
        provider = bundle.adapters[ARENA_SCRIPTED_PROVIDER_ID]
        secret = WorkerSecret(b"development-only-placeholder")
        try:
            response = await provider.invoke(
                _request(
                    model="arena-fallback-buyer-v1",
                    kind="arena.market.intent",
                ),
                secret,
            )
            return dict(response.structured_output)
        finally:
            secret.close()
            await bundle.close()

    assert asyncio.run(run())["action"] == "buy"


def test_scripted_provider_runs_buyer_and_seller_decide_and_negotiate() -> None:
    async def run() -> list[dict[str, object]]:
        bundle = build_local_development_provider_bundle()
        provider = bundle.adapters[ARENA_SCRIPTED_PROVIDER_ID]
        requests = [
            _request(
                model=ARENA_SCRIPTED_BUYER_MODEL,
                kind="arena.decide",
            ),
            _request(
                model=ARENA_SCRIPTED_SELLER_MODEL,
                kind="arena.decide",
            ),
            _request(
                model=ARENA_SCRIPTED_BUYER_MODEL,
                kind="arena.negotiate",
                role="buyer",
            ),
            _request(
                model=ARENA_SCRIPTED_SELLER_MODEL,
                kind="arena.negotiate",
                role="seller",
            ),
        ]
        outputs: list[dict[str, object]] = []
        for request in requests:
            secret = WorkerSecret(b"development-only-placeholder")
            try:
                response = await provider.invoke(request, secret)
                outputs.append(dict(response.structured_output))
            finally:
                secret.close()
        await bundle.close()
        return outputs

    assert asyncio.run(run()) == [
        {"action": "buy", "good": "iron"},
        {"action": "sell", "good": "iron"},
        {
            "action": "propose",
            "price": "7.000000",
            "message": "I offer seven gold for one lot of iron.",
        },
        {"action": "accept"},
    ]


def test_scripted_provider_exercises_the_agent_market_task_sequence() -> None:
    async def run() -> list[dict[str, object]]:
        bundle = build_local_development_provider_bundle()
        provider = bundle.adapters[ARENA_SCRIPTED_PROVIDER_ID]
        requests = [
            _request(
                model=ARENA_SCRIPTED_BUYER_MODEL,
                kind="arena.market.intent",
            ),
            _request(
                model=ARENA_SCRIPTED_SELLER_MODEL,
                kind="arena.market.intent",
            ),
            _request(
                model=ARENA_SCRIPTED_BUYER_MODEL,
                kind="arena.market.rfq",
                arena_input={
                    "directory": [
                        {
                            "intentId": "intent:round-1:seller-1",
                            "publicPrice": "7.000000",
                        }
                    ]
                },
            ),
            _request(
                model=ARENA_SCRIPTED_SELLER_MODEL,
                kind="arena.market.select",
                arena_input={
                    "requests": [
                        {"requestId": "request:task-rfq:1"}
                    ]
                },
            ),
        ]
        outputs: list[dict[str, object]] = []
        for request in requests:
            secret = WorkerSecret(b"development-only-placeholder")
            try:
                response = await provider.invoke(request, secret)
                outputs.append(dict(response.structured_output))
            finally:
                secret.close()
        await bundle.close()
        return outputs

    outputs = asyncio.run(run())
    assert outputs[0]["action"] == "buy"
    assert outputs[1]["action"] == "sell"
    assert outputs[2] == {
        "action": "request_negotiations",
        "requests": [
            {
                "targetIntentId": "intent:round-1:seller-1",
                "openingPrice": "7.000000",
                "message": "I choose this seller.",
            }
        ],
    }
    assert outputs[3] == {
        "action": "engage",
        "requestId": "request:task-rfq:1",
    }


def test_scripted_fallback_buyer_chooses_rejecting_seller_then_remaining_seller() -> None:
    async def run() -> list[dict[str, object]]:
        bundle = build_local_development_provider_bundle()
        provider = bundle.adapters[ARENA_SCRIPTED_PROVIDER_ID]
        requests = [
            _request(
                model="arena-fallback-buyer-v1",
                kind="arena.market.rfq",
                arena_input={
                    "attemptSequence": 1,
                    "directory": [
                        {
                            "intentId": "intent:accepting",
                            "displayName": "Accepting Seller",
                            "publicPrice": "7.000000",
                        },
                        {
                            "intentId": "intent:rejecting",
                            "displayName": "Rejecting Seller",
                            "publicPrice": "7.000000",
                        },
                    ],
                },
            ),
            _request(
                model="arena-fallback-buyer-v1",
                kind="arena.market.rfq",
                arena_input={
                    "attemptSequence": 2,
                    "directory": [
                        {
                            "intentId": "intent:accepting",
                            "displayName": "Accepting Seller",
                            "publicPrice": "7.000000",
                        }
                    ],
                },
            ),
        ]
        outputs: list[dict[str, object]] = []
        for request in requests:
            secret = WorkerSecret(b"development-only-placeholder")
            try:
                response = await provider.invoke(request, secret)
                outputs.append(dict(response.structured_output))
            finally:
                secret.close()
        await bundle.close()
        return outputs

    outputs = asyncio.run(run())
    assert outputs[0]["requests"][0]["targetIntentId"] == "intent:rejecting"
    assert outputs[1]["requests"][0]["targetIntentId"] == "intent:accepting"
