#!/usr/bin/env python3
"""Run 2-16 Hosted Agents through a persisted multi-round game.

The development-only scripted models reject every proposal. This exercises
Hosted concurrency, FCFS pairing, round recovery, and terminal ranking without
inventing a successful payment.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from typing import Any

from run_dual_hosted_pawnhouse_demo import (
    BrowserSession,
    _create_hosted_agent,
    _public_request,
    _register,
)


def _invites_from_environment(agent_count: int) -> list[str]:
    raw = os.environ.get("ARENA_HOSTED_INVITES", "").strip()
    if not raw:
        raise ValueError(
            "ARENA_HOSTED_INVITES must contain the JSON emitted by "
            "invite_cli --count N --json"
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("ARENA_HOSTED_INVITES must be valid JSON") from None
    if isinstance(decoded, dict):
        decoded = decoded.get("invites")
    if (
        not isinstance(decoded, list)
        or len(decoded) < agent_count
        or any(not isinstance(value, str) or not value for value in decoded)
    ):
        raise ValueError(
            f"ARENA_HOSTED_INVITES must contain at least {agent_count} invites"
        )
    return decoded[:agent_count]


def _join_body(*, seller: bool) -> dict[str, object]:
    return {
        "cash": "15" if seller else "20",
        "holdings": {
            "grain": 0,
            "iron": 1 if seller else 0,
            "warhorse": 0,
            "gems": 0,
        },
    }


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
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--rounds", type=int, choices=(5, 8, 10), default=5)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.agents < 2 or args.agents > 16 or args.agents % 2:
        parser.error("--agents must be an even number between 2 and 16")

    invites = _invites_from_environment(args.agents)
    base_url = args.base_url.rstrip("/")
    sessions: list[tuple[BrowserSession, str, bool]] = []
    split = args.agents // 2
    for index, invite in enumerate(invites):
        seller = index >= split
        role = (
            f"scale-seller-{index - split + 1:02d}"
            if seller
            else f"scale-buyer-{index + 1:02d}"
        )
        session = _register(base_url, invite, role)
        agent_id = _create_hosted_agent(
            session,
            role=role,
            model_id=(
                "arena-rejecting-seller-v1"
                if seller
                else "arena-rejecting-buyer-v1"
            ),
        )
        sessions.append((session, agent_id, seller))

    game_id = (
        f"many-hosted-{args.agents}x{args.rounds}-"
        f"{int(time.time())}-{secrets.token_hex(4)}"
    )
    _public_request(
        base_url,
        "POST",
        "/api/dev/pawnhouse/games",
        dev_token=args.dev_token,
        body={
            "gameId": game_id,
            "eventSeed": f"many-hosted-{secrets.token_hex(16)}",
            "roundCount": args.rounds,
            "eventDeckId": "pawnhouse-standard-v1",
            "eventMode": "seeded_shuffle",
            "maxParticipants": args.agents,
            "actionTimeoutMs": 120_000,
        },
    )
    for session, agent_id, seller in sessions:
        session.request(
            "POST",
            f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
            body={
                "agentId": agent_id,
                "portfolio": _join_body(seller=seller),
            },
        )

    _public_request(
        base_url,
        "POST",
        f"/api/dev/pawnhouse/games/{game_id}/start",
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
        time.sleep(0.2)
    else:
        automation = _public_request(
            base_url,
            "GET",
            f"/api/v1/pawnhouse/games/{game_id}/automation",
        )
        raise TimeoutError(
            "many Hosted game did not become terminal; "
            f"automation={json.dumps(automation, separators=(',', ':'))}"
        )

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
    rankings = [
        value
        for value in state.get("rankings", [])
        if isinstance(value, dict)
    ]
    pairings_per_round = args.agents // 2
    summary = {
        "gameId": game_id,
        "phase": state.get("phase"),
        "hostedAgentCount": args.agents,
        "roundCount": state.get("roundCount"),
        "currentRound": state.get("currentRound"),
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
        "rankingCount": len(rankings),
        "eventSeedRevealed": state.get("eventSeed") is not None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    expected = (
        summary["phase"] == "completed"
        and summary["roundCount"] == args.rounds
        and summary["worldEventCount"] == args.rounds
        and summary["runtimeRunCompletedCount"] == args.rounds
        and summary["decisionCount"] == args.agents * args.rounds
        and summary["pairingCount"] == pairings_per_round * args.rounds
        and summary["negotiationMessageCount"]
        == pairings_per_round * 2 * args.rounds
        and summary["roundClosedCount"] == args.rounds
        and summary["settlementIntentCount"] == 0
        and summary["rankingCount"] == args.agents
        and summary["eventSeedRevealed"] is True
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
