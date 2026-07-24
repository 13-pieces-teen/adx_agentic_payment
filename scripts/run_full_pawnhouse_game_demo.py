#!/usr/bin/env python3
"""Run a complete backend-only five-round Pawnhouse game.

The deterministic Rule Agents deliberately reject every negotiation. This
exercises events, four FCFS pools, concurrent pair creation, bounded
negotiation, round close, next-round recovery, final prices, and ranking
without fabricating a payment or moving inventory before chain confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    dev_token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    encoded = None
    headers = {"X-Arena-Dev-Token": dev_token}
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        safe = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"{method} {path} failed with HTTP {exc.code}: {safe}"
        ) from None
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned a non-object")
    return value


def _portfolio(good: str | None) -> dict[str, object]:
    prices = {"grain": 2, "iron": 5, "warhorse": 8, "gems": 3}
    holdings = {value: 0 for value in prices}
    cash = 20
    if good is not None:
        holdings[good] = 1
        cash -= prices[good]
    return {"cash": str(cash), "holdings": holdings}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "ARENA_BASE_URL",
            "http://127.0.0.1:8000",
        ),
    )
    parser.add_argument(
        "--dev-token",
        default=os.environ.get(
            "ARENA_DEV_TOKEN",
            "arena402-local-development-control-token",
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=60)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    suffix = secrets.token_hex(4)
    game_id = f"full-game-{int(time.time())}-{suffix}"
    game_path = f"/api/dev/pawnhouse/games/{game_id}"

    _request(
        base_url,
        "POST",
        "/api/dev/pawnhouse/games",
        dev_token=args.dev_token,
        body={
            "gameId": game_id,
            "eventSeed": f"full-game-seed-{secrets.token_hex(12)}",
            "actionTimeoutMs": 120_000,
        },
    )

    target_prices = {
        "grain": (2, 3),
        "iron": (5, 6),
        "warhorse": (8, 9),
        "gems": (3, 4),
    }
    for good, (buyer_price, seller_price) in target_prices.items():
        for role, intent, price in (
            ("buyer", "buy", buyer_price),
            ("seller", "sell", seller_price),
        ):
            participant_suffix = f"{good}-{role}-{suffix}"
            _request(
                base_url,
                "POST",
                f"{game_path}/rule-participants",
                dev_token=args.dev_token,
                body={
                    "userId": f"user-{participant_suffix}",
                    "agentId": f"agent-{participant_suffix}",
                    "portfolio": _portfolio(
                        good if role == "seller" else None
                    ),
                    "strategy": {
                        "intent": intent,
                        "good": good,
                        "targetPrice": str(price),
                        "publicMessage": (
                            f"{role} holds at {price} gold for {good}."
                        ),
                    },
                },
            )

    _request(
        base_url,
        "POST",
        f"{game_path}/start",
        dev_token=args.dev_token,
    )
    deadline = time.monotonic() + args.timeout_seconds
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = _request(
            base_url,
            "GET",
            f"/api/v1/pawnhouse/games/{game_id}",
            dev_token=args.dev_token,
        )
        if state.get("phase") in {"completed", "cancelled"}:
            break
        time.sleep(0.1)
    else:
        raise TimeoutError("full game did not reach a terminal state")

    timeline = _request(
        base_url,
        "GET",
        f"/api/v1/pawnhouse/games/{game_id}/timeline",
        dev_token=args.dev_token,
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
        "worldEventCount": event_types.count("world.event_revealed"),
        "decisionCount": event_types.count("decision.applied"),
        "pairingCount": event_types.count("pairing.created"),
        "negotiationMessageCount": event_types.count(
            "negotiation.message"
        ),
        "roundClosedCount": event_types.count("round.closed"),
        "finalPrices": state.get("finalPrices"),
        "rankings": rankings,
        "eventSeedRevealed": state.get("eventSeed") is not None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    expected = (
        summary["phase"] == "completed"
        and summary["completedRoundCount"] == summary["roundCount"] == 5
        and summary["worldEventCount"] == 5
        and summary["decisionCount"] == 40
        and summary["pairingCount"] == 20
        and summary["roundClosedCount"] == 5
        and len(rankings) == 8
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
