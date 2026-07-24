from __future__ import annotations

import asyncio
import json

from hosted_agent_runtime.production_providers import (
    build_local_development_provider_bundle,
)
from hosted_agent_runtime.providers import (
    ARENA_SCRIPTED_BUYER_MODEL,
    ARENA_SCRIPTED_PROVIDER_ID,
    ARENA_SCRIPTED_SELLER_MODEL,
    ProviderRequest,
)
from hosted_agent_runtime.secret_store import WorkerSecret


def _request(*, model: str, kind: str, role: str | None = None) -> ProviderRequest:
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
