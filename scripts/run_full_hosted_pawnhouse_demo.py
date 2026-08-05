#!/usr/bin/env python3
"""Run development Hosted actors through complete Pawnhouse rounds.

The development-only scripted models publish compatible intents and accept an
in-bound negotiation.  The game always uses ``authorizationMode=none``, so an
accepted negotiation can prove the immutable deal boundary without signing,
broadcasting, or pretending that an unpaid trade settled.

With ``--market-protocol agent_a2a.v1`` this is a Fake E2E baseline for the
real task/result/orchestration path. Scripted actors remain test fixtures and
must not be reported as real-Agent evidence.

The ``fallback`` scenario adds a rejecting seller. The buyer selects that
seller first, then uses a second AgentTask to select the remaining seller.
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
    parser.add_argument("--round-count", type=int, default=1)
    parser.add_argument("--action-timeout-ms", type=int, default=15_000)
    parser.add_argument(
        "--market-protocol",
        choices=("fcfs.v1", "agent_a2a.v1"),
        default="agent_a2a.v1",
    )
    parser.add_argument(
        "--scenario",
        choices=("accepted", "fallback"),
        default="accepted",
    )
    args = parser.parse_args()
    buyer_invite = os.environ.get("ARENA_BUYER_INVITE")
    seller_invite = os.environ.get("ARENA_SELLER_INVITE")
    rejecting_seller_invite = os.environ.get(
        "ARENA_REJECTING_SELLER_INVITE"
    )
    if not buyer_invite or not seller_invite:
        parser.error(
            "set ARENA_BUYER_INVITE and ARENA_SELLER_INVITE to fresh "
            "one-time local invites"
        )
    if args.scenario == "fallback" and not rejecting_seller_invite:
        parser.error(
            "set ARENA_REJECTING_SELLER_INVITE to a third fresh one-time "
            "local invite for the fallback scenario"
        )
    if args.scenario == "fallback" and args.market_protocol != "agent_a2a.v1":
        parser.error("the fallback scenario requires agent_a2a.v1")

    base_url = args.base_url.rstrip("/")
    buyer_session = _register(base_url, buyer_invite, "full-game-buyer")
    seller_session = _register(base_url, seller_invite, "full-game-seller")
    rejecting_seller_session = (
        _register(
            base_url,
            str(rejecting_seller_invite),
            "rejecting-seller",
        )
        if args.scenario == "fallback"
        else None
    )
    buyer_agent_id = _create_hosted_agent(
        buyer_session,
        role="full-game-buyer",
        model_id=(
            "arena-fallback-buyer-v1"
            if args.scenario == "fallback"
            else "arena-buyer-v1"
        ),
    )
    seller_agent_id = _create_hosted_agent(
        seller_session,
        role="accepting-seller",
        model_id="arena-seller-v1",
    )
    rejecting_seller_agent_id = (
        _create_hosted_agent(
            rejecting_seller_session,
            role="rejecting-seller",
            model_id="arena-rejecting-seller-v1",
        )
        if rejecting_seller_session is not None
        else None
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
            "actionTimeoutMs": args.action_timeout_ms,
            "roundCount": args.round_count,
            "eventMode": (
                "fixed_demo"
                if args.round_count == 5
                else "seeded_shuffle"
            ),
            "marketProtocol": args.market_protocol,
            "settlement": {"authorizationMode": "none"},
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
    if (
        rejecting_seller_session is not None
        and rejecting_seller_agent_id is not None
    ):
        rejecting_seller_session.request(
            "POST",
            f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
            body={
                "agentId": rejecting_seller_agent_id,
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
        "marketProtocol": args.market_protocol,
        "scenario": args.scenario,
        "evidenceClass": "fake_scripted_runtime",
        "roundCount": state.get("roundCount"),
        "currentRound": state.get("currentRound"),
        "completedRoundCount": sum(
            1 for value in rounds if value.get("phase") == "completed"
        ),
        "hostedAgentCount": (
            3 if args.scenario == "fallback" else 2
        ),
        "worldEventCount": event_types.count("world.event_revealed"),
        "runtimeRunCompletedCount": event_types.count(
            "runtime.run_completed"
        ),
        "decisionCount": event_types.count("decision.applied"),
        "marketIntentCount": event_types.count(
            "market.intent_published"
        ),
        "marketRfqBatchCount": event_types.count("market.rfq_sent"),
        "marketEngagementCount": event_types.count(
            "market.engagement_created"
        ),
        "marketNegotiationCount": event_types.count(
            "market.negotiation_created"
        ),
        "marketDealCount": event_types.count("market.deal_frozen"),
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
    common_expected = (
        summary["phase"] == "completed"
        and summary["completedRoundCount"]
        == summary["roundCount"]
        == args.round_count
        and summary["worldEventCount"] == args.round_count
        and summary["runtimeRunCompletedCount"] == args.round_count
        and summary["negotiationMessageCount"]
        == (
            4 * args.round_count
            if args.scenario == "fallback"
            else 2 * args.round_count
        )
        and summary["roundClosedCount"] == args.round_count
        and summary["settlementIntentCount"] == 0
        and len(rankings) == summary["hostedAgentCount"]
    )
    if args.market_protocol == "agent_a2a.v1":
        attempt_count = (
            2 if args.scenario == "fallback" else 1
        ) * args.round_count
        expected = (
            common_expected
            and summary["decisionCount"] == 0
            and summary["marketIntentCount"]
            == summary["hostedAgentCount"] * args.round_count
            and summary["marketRfqBatchCount"] == attempt_count
            and summary["marketEngagementCount"] == attempt_count
            and summary["marketNegotiationCount"] == attempt_count
            and summary["marketDealCount"] == args.round_count
        )
    else:
        expected = (
            common_expected
            and summary["decisionCount"] == 2 * args.round_count
            and summary["pairingCount"] == args.round_count
        )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
