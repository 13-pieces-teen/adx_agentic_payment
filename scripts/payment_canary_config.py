"""Validated public configuration for the payment-enabled Hosted canary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Mapping


@dataclass(frozen=True, slots=True)
class CanaryPlayerConfig:
    runtime_kind: Literal["hosted", "connector"]
    user_id: str | None
    agent_id: str | None


@dataclass(frozen=True, slots=True)
class CanaryAssetConfig:
    profile: Literal["musdc", "arena402_g"]
    chain_id: int
    token_address: str
    token_symbol: str
    token_decimals: int
    token_eip712_name: str
    token_eip712_version: str


_CANARY_ASSETS = {
    "musdc": CanaryAssetConfig(
        profile="musdc",
        chain_id=1439,
        token_address="0x06d223d12774386a96d33863d9106a800e52bded",
        token_symbol="mUSDC",
        token_decimals=6,
        token_eip712_name="Mock USD Coin",
        token_eip712_version="1",
    ),
    "arena402_g": CanaryAssetConfig(
        profile="arena402_g",
        chain_id=1439,
        token_address="0xbf7b7268ce82d92bac7a95a741f4003fe84e1884",
        token_symbol="arena402-g",
        token_decimals=6,
        token_eip712_name="Arena 402 Gold",
        token_eip712_version="1",
    ),
}


def canary_mandate_limits(round_count: int) -> tuple[int, int]:
    """Return the per-trade and whole-Game player payment bounds."""

    if not 1 <= round_count <= 10:
        raise ValueError("round_count must be between 1 and 10")
    max_per_payment_atomic = 20_000_000
    return (
        max_per_payment_atomic,
        round_count * max_per_payment_atomic,
    )


_PHASE_D_PORTFOLIOS: tuple[tuple[int, dict[str, int]], ...] = (
    (20_000_000, {}),
    (20_000_000, {}),
    (20_000_000, {}),
    (0, {"grain": 10}),
    (0, {"grain": 10}),
    (15_000_000, {"iron": 1}),
    (15_000_000, {"iron": 1}),
    (12_000_000, {"warhorse": 1}),
    (17_000_000, {"gems": 1}),
    (18_000_000, {"grain": 1}),
)


def resolve_canary_event_seed(game_id: str) -> str:
    """Return a bounded explicit seed or the legacy per-Game seed."""

    configured = os.getenv("CANARY_EVENT_SEED")
    seed = (
        configured.strip()
        if configured is not None
        else f"{game_id}:phase-d-seed"
    )
    if not seed or len(seed) > 128:
        raise RuntimeError(
            "CANARY_EVENT_SEED must contain between 1 and 128 characters"
        )
    return seed


def phase_d_portfolio_for_seat(
    seat: int,
) -> tuple[int, dict[str, int]]:
    """Return a fair 20-gold portfolio with controlled iron liquidity."""

    if isinstance(seat, bool) or not 0 <= seat < len(_PHASE_D_PORTFOLIOS):
        raise ValueError("Phase D canary seat must be between 0 and 9")
    cash_atomic, holdings = _PHASE_D_PORTFOLIOS[seat]
    return cash_atomic, dict(holdings)


def resolve_canary_asset_config() -> CanaryAssetConfig:
    """Return one repository-approved Injective testnet settlement asset."""

    profile = os.getenv("CANARY_TOKEN_PROFILE", "musdc").strip()
    try:
        return _CANARY_ASSETS[profile]
    except KeyError as exc:
        raise RuntimeError(
            "CANARY_TOKEN_PROFILE must be musdc or arena402_g"
        ) from exc


def resolve_canary_game_config() -> tuple[str, int]:
    """Return the frozen market protocol and round count for a canary Game."""

    market_protocol = os.getenv("CANARY_MARKET_PROTOCOL", "fcfs.v1").strip()
    if market_protocol not in {"fcfs.v1", "agent_a2a.v1"}:
        raise RuntimeError(
            "CANARY_MARKET_PROTOCOL must be fcfs.v1 or agent_a2a.v1"
        )
    try:
        round_count = int(os.getenv("CANARY_ROUND_COUNT", "3"))
    except ValueError as exc:
        raise RuntimeError(
            "CANARY_ROUND_COUNT must be an integer between 1 and 10"
        ) from exc
    if not 1 <= round_count <= 10:
        raise RuntimeError(
            "CANARY_ROUND_COUNT must be an integer between 1 and 10"
        )
    return market_protocol, round_count


def resolve_canary_player_config() -> CanaryPlayerConfig:
    """Return the selected canary player Runtime and external identity."""

    runtime_kind = os.getenv("CANARY_PLAYER_RUNTIME", "hosted").strip()
    if runtime_kind == "connector":
        user_id = os.getenv("CANARY_PLAYER_USER_ID", "").strip()
        agent_id = os.getenv("CANARY_PLAYER_AGENT_ID", "").strip()
        if not user_id or not agent_id:
            raise RuntimeError(
                "CANARY_PLAYER_USER_ID and CANARY_PLAYER_AGENT_ID are "
                "required for connector canaries"
            )
        return CanaryPlayerConfig(
            runtime_kind="connector",
            user_id=user_id,
            agent_id=agent_id,
        )
    if runtime_kind != "hosted":
        raise RuntimeError(
            "CANARY_PLAYER_RUNTIME must be hosted or connector"
        )
    return CanaryPlayerConfig(
        runtime_kind="hosted",
        user_id=None,
        agent_id=None,
    )


def resolve_canary_settlement_mode() -> Literal[
    "disabled",
    "testnet_eip3009",
]:
    """Return the explicit no-chain smoke or Injective testnet mode."""

    mode = os.getenv(
        "CANARY_SETTLEMENT_MODE",
        "testnet_eip3009",
    ).strip()
    if mode not in {"disabled", "testnet_eip3009"}:
        raise RuntimeError(
            "CANARY_SETTLEMENT_MODE must be disabled or testnet_eip3009"
        )
    return mode


def canary_summary_is_accepted(
    *,
    summary: Mapping[str, object],
    expected_runtime_counts: Mapping[str, int],
    round_count: int,
    market_protocol: str,
    settlement_mode: Literal["disabled", "testnet_eip3009"],
) -> bool:
    """Apply mode-specific acceptance without conflating smoke and payment."""

    base_accepted = (
        summary.get("phase") == "completed"
        and summary.get("participantCount") == 10
        and summary.get("runtimeCounts") == dict(expected_runtime_counts)
        and summary.get("roundCount") == round_count
        and summary.get("marketProtocol") == market_protocol
        and isinstance(summary.get("agentTaskCount"), int)
        and summary["agentTaskCount"] >= 10
        and summary.get("completedAgentTaskCount")
        == summary.get("agentTaskCount")
    )
    if not base_accepted:
        return False
    if settlement_mode == "disabled":
        return (
            summary.get("settledTradeCount") == 0
            and summary.get("chainSubmissionCount") == 0
            and summary.get("inventoryCommitCount") == 0
        )

    learning_jobs = summary.get("learningJobs")
    activated_learning = 0
    if isinstance(learning_jobs, list):
        activated_learning = sum(
            int(row.get("count", 0))
            for row in learning_jobs
            if isinstance(row, Mapping)
            and row.get("status") == "activated"
        )
    return (
        isinstance(summary.get("settledTradeCount"), int)
        and summary["settledTradeCount"] >= 1
        and isinstance(summary.get("chainSubmissionCount"), int)
        and summary["chainSubmissionCount"] >= 1
        and isinstance(summary.get("inventoryCommitCount"), int)
        and summary["inventoryCommitCount"] >= 1
        and activated_learning >= 1
    )


__all__ = [
    "CanaryAssetConfig",
    "CanaryPlayerConfig",
    "canary_mandate_limits",
    "canary_summary_is_accepted",
    "phase_d_portfolio_for_seat",
    "resolve_canary_event_seed",
    "resolve_canary_asset_config",
    "resolve_canary_game_config",
    "resolve_canary_player_config",
    "resolve_canary_settlement_mode",
]
