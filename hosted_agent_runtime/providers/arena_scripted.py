"""Local-development Provider for deterministic Hosted Agent demonstrations.

This adapter exercises the complete Hosted Runtime path without making a
network request. It is intentionally mounted only by the development
composition and is never part of the production Provider allowlist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from hosted_agent_runtime.secret_store import WorkerSecret

from .base import (
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
)


ARENA_SCRIPTED_PROVIDER_ID = "arena-scripted"
ARENA_SCRIPTED_ADAPTER_ID = "arena-scripted-v1"
ARENA_SCRIPTED_BUYER_MODEL = "arena-buyer-v1"
ARENA_SCRIPTED_FALLBACK_BUYER_MODEL = "arena-fallback-buyer-v1"
ARENA_SCRIPTED_SELLER_MODEL = "arena-seller-v1"
ARENA_SCRIPTED_REJECTING_BUYER_MODEL = "arena-rejecting-buyer-v1"
ARENA_SCRIPTED_REJECTING_SELLER_MODEL = "arena-rejecting-seller-v1"

_USAGE = ProviderUsage(
    input_tokens=32,
    output_tokens=8,
    cached_input_tokens=0,
    reasoning_tokens=0,
    complete=True,
)


class ArenaScriptedProvider:
    """Return deterministic iron actions from an immutable model identity."""

    @property
    def adapter_id(self) -> str:
        return ARENA_SCRIPTED_ADAPTER_ID

    async def invoke(
        self,
        request: ProviderRequest,
        credential: WorkerSecret,
    ) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise ProviderInvocationError("permanent_request")
        if not isinstance(credential, WorkerSecret):
            raise ProviderInvocationError("authentication_failed")

        if request.context_version == "arena.credential-validation.v1":
            output: dict[str, object] = {"ok": True}
        else:
            output = self._action(request)
        return ProviderResponse(
            structured_output=output,
            usage=_USAGE,
            provider_request_id=f"scripted-{request.attempt_id}",
            actual_model=request.model_id,
        )

    @staticmethod
    def _action(request: ProviderRequest) -> dict[str, object]:
        if request.model_id not in {
            ARENA_SCRIPTED_BUYER_MODEL,
            ARENA_SCRIPTED_FALLBACK_BUYER_MODEL,
            ARENA_SCRIPTED_SELLER_MODEL,
            ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
            ARENA_SCRIPTED_REJECTING_SELLER_MODEL,
        }:
            raise ProviderInvocationError("permanent_request")
        try:
            envelope = json.loads(request.input_json)
            arena_input = envelope["untrustedArenaData"]
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ProviderInvocationError("invalid_json") from None
        if not isinstance(arena_input, Mapping):
            raise ProviderInvocationError("invalid_structured_output")

        if request.task_kind == "arena.decide":
            if request.model_id in {
                ARENA_SCRIPTED_BUYER_MODEL,
                ARENA_SCRIPTED_FALLBACK_BUYER_MODEL,
                ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
            }:
                return {"action": "buy", "good": "iron"}
            return {"action": "sell", "good": "iron"}
        if request.task_kind == "arena.market.intent":
            if request.model_id in {
                ARENA_SCRIPTED_BUYER_MODEL,
                ARENA_SCRIPTED_FALLBACK_BUYER_MODEL,
                ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
            }:
                return {
                    "action": "buy",
                    "good": "iron",
                    "quantity": 1,
                    "publicPrice": "7.000000",
                    "limitPrice": "8.000000",
                    "message": "Seeking one iron lot.",
                }
            return {
                "action": "sell",
                "good": "iron",
                "quantity": 1,
                "publicPrice": "7.000000",
                "limitPrice": "6.000000",
                "message": "Offering one iron lot.",
            }
        if request.task_kind == "arena.market.rfq":
            directory = arena_input.get("directory")
            if not isinstance(directory, list) or not directory:
                return {"action": "pass"}
            target = directory[0]
            if request.model_id == ARENA_SCRIPTED_FALLBACK_BUYER_MODEL:
                target = next(
                    (
                        entry
                        for entry in directory
                        if isinstance(entry, Mapping)
                        and any(
                            marker
                            in str(
                                entry.get("displayName", "")
                            ).casefold()
                            for marker in ("rejecting", "primary")
                        )
                    ),
                    target,
                )
            if not isinstance(target, Mapping):
                raise ProviderInvocationError(
                    "invalid_structured_output"
                )
            primary_target = (
                request.model_id == ARENA_SCRIPTED_FALLBACK_BUYER_MODEL
                and "primary"
                in str(target.get("displayName", "")).casefold()
            )
            return {
                "action": "request_negotiations",
                "requests": [
                    {
                        "targetIntentId": target["intentId"],
                        "openingPrice": (
                            "1.000000"
                            if primary_target
                            else target["publicPrice"]
                        ),
                        "message": (
                            "I choose the primary seller with a low "
                            "opening bid."
                            if primary_target
                            else "I choose this seller."
                        ),
                    }
                ],
            }
        if request.task_kind == "arena.market.select":
            requests = arena_input.get("requests")
            if not isinstance(requests, list) or not requests:
                return {"action": "reject_all"}
            selected = requests[0]
            if not isinstance(selected, Mapping):
                raise ProviderInvocationError(
                    "invalid_structured_output"
                )
            return {
                "action": "engage",
                "requestId": selected["requestId"],
            }

        role = arena_input.get("role")
        if role == "buyer":
            counterparty = arena_input.get("counterparty")
            if (
                request.model_id
                == ARENA_SCRIPTED_FALLBACK_BUYER_MODEL
                and isinstance(counterparty, Mapping)
                and "primary"
                in str(
                    counterparty.get("displayName", "")
                ).casefold()
            ):
                return {
                    "action": "reject",
                    "message": "I will try another seller.",
                }
            return {
                "action": "propose",
                "price": "7.000000",
                "message": "I offer seven gold for one lot of iron.",
            }
        if role == "seller":
            if (
                request.model_id
                == ARENA_SCRIPTED_REJECTING_SELLER_MODEL
            ):
                return {
                    "action": "reject",
                    "message": "No agreement this round.",
                }
            return {"action": "accept"}
        raise ProviderInvocationError("invalid_structured_output")


__all__ = [
    "ARENA_SCRIPTED_ADAPTER_ID",
    "ARENA_SCRIPTED_BUYER_MODEL",
    "ARENA_SCRIPTED_FALLBACK_BUYER_MODEL",
    "ARENA_SCRIPTED_PROVIDER_ID",
    "ARENA_SCRIPTED_REJECTING_BUYER_MODEL",
    "ARENA_SCRIPTED_REJECTING_SELLER_MODEL",
    "ARENA_SCRIPTED_SELLER_MODEL",
    "ArenaScriptedProvider",
]
