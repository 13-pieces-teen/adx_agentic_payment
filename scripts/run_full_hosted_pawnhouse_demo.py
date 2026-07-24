#!/usr/bin/env python3
"""Run two Hosted Agents through all five Pawnhouse rounds.

The development-only scripted models propose and reject rather than accept, so
the game can prove durable multi-round Hosted execution without signing,
broadcasting, or pretending that an unpaid trade settled.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from typing import Any

from run_dual_hosted_pawnhouse_demo import (
    _create_hosted_agent,
    _public_request,
    _register,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ARENA_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--dev-token",
        default=os.environ.get(
            "ARENA_DEV_TOKEN",
            "arena402-local-development-control-token",
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=120)
    args = parser.parse_args()
    buyer_invite = os.environ.get("ARENA_BUYER_INVITE")
    seller_invite = os.environ.get("ARENA_SELLER_INVITE")
    if not buyer_invite or not seller_invite:
        parser.error(
            "set ARENA_BUYER_INVITE and ARENA_SELLER_INVITE to fresh "
            "one-time local invites"
        )

    base_url = args.base_url.rstrip("/")
    buyer_session = _register(base_url, buyer_invite, "full-game-buyer")
    seller_session = _register(base_url, seller_invite, "full-game-seller")
    buyer_agent_id = _create_hosted_agent(
        buyer_session,
        role="full-game-buyer",
        model_id="arena-rejecting-buyer-v1",
    )
    seller_agent_id = _create_hosted_agent(
        seller_session,
        role="full-game-seller",
        model_id="arena-rejecting-seller-v1",
    )

    game_id = (
        f"full-hosted-{int(time.time())}-{secrets.token_hex(4)}"
    )
    game_path = f"/api/dev/pawnhouse/games/{game_id}"
    _public_request(
        base_url,
        "POST",
        "/api/dev/pawnhouse/games",
        dev_token=args.dev_token,
        body={
            "gameId": game_id,
            "eventSeed": f"full-hosted-{secrets.token_hex(12)}",
            "actionTimeoutMs": 120_000,
        },
    )
    buyer_session.request(
        "POST",
        f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
        body={
            "agentId": buyer_agent_id,
            "portfolio": {
                "cash": "20",
                "holdings": {
                    "grain": 0,
                    "iron": 0,
                    "warhorse": 0,
                    "gems": 0,
                },
            },
        },
    )
    seller_session.request(
        "POST",
        f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
        body={
            "agentId": seller_agent_id,
            "portfolio": {
                "cash": "15",
                "holdings": {
                    "grain": 0,
                    "iron": 1,
                    "warhorse": 0,
                    "gems": 0,
                },
            },
        },
    )
    _public_request(
        base_url,
        "POST",
        f"{game_path}/start",
        dev_token=args.dev_token,
    )

    deadline = time.monotonic() + args.timeout_seconds
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _public_request(
            base_url,
            "GET",
            f"/api/v1/pawnhouse/games/{game_id}",
        )
        if state.get("phase") in {"completed", "cancelled"}:
            break
        time.sleep(0.1)
    else:
        raise TimeoutError("full Hosted game did not become terminal")

    timeline = _public_request(
        base_url,
        "GET",
        f"/api/v1/pawnhouse/games/{game_id}/timeline",
    )
    settlement_projection = _public_request(
        base_url,
        "GET",
        f"/api/v1/pawnhouse/games/{game_id}/settlement-intents",
    )
    events = [
        value
        for value in timeline.get("events", [])
        if isinstance(value, dict)
    ]
    event_types = [str(value.get("type")) for value in events]
    rounds = [
        value for value in state.get("rounds", []) if isinstance(value, dict)
    ]
    rankings = [
        value
        for value in state.get("rankings", [])
        if isinstance(value, dict)
    ]
    summary = {
        "gameId": game_id,
        "phase": state.get("phase"),
        "roundCount": state.get("roundCount"),
        "currentRound": state.get("currentRound"),
        "completedRoundCount": sum(
            1 for value in rounds if value.get("phase") == "completed"
        ),
        "hostedAgentCount": 2,
        "worldEventCount": event_types.count("world.event_revealed"),
        "runtimeRunCompletedCount": event_types.count(
            "runtime.run_completed"
        ),
        "decisionCount": event_types.count("decision.applied"),
        "pairingCount": event_types.count("pairing.created"),
        "negotiationMessageCount": event_types.count(
            "negotiation.message"
        ),
        "roundClosedCount": event_types.count("round.closed"),
        "settlementIntentCount": len(
            [
                value
                for value in settlement_projection.get(
                    "settlementIntents",
                    [],
                )
                if isinstance(value, dict)
            ]
        ),
        "finalPrices": state.get("finalPrices"),
        "rankings": rankings,
        "eventSeedRevealed": state.get("eventSeed") is not None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    expected = (
        summary["phase"] == "completed"
        and summary["completedRoundCount"] == summary["roundCount"] == 5
        and summary["worldEventCount"] == 5
        and summary["runtimeRunCompletedCount"] == 5
        and summary["decisionCount"] == 10
        and summary["pairingCount"] == 5
        and summary["negotiationMessageCount"] == 10
        and summary["roundClosedCount"] == 5
        and summary["settlementIntentCount"] == 0
        and len(rankings) == 2
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
