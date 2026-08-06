from __future__ import annotations

from pathlib import Path

import pytest

from scripts.payment_canary_config import (
    CanaryAssetConfig,
    CanaryPlayerConfig,
    canary_summary_is_accepted,
    canary_mandate_limits,
    phase_d_portfolio_for_seat,
    resolve_canary_event_seed,
    resolve_canary_asset_config,
    resolve_canary_game_config,
    resolve_canary_player_config,
    resolve_canary_settlement_mode,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = (
    ROOT / "scripts" / "run_payment_enabled_hosted_canary.py"
).read_text(encoding="utf-8")


def test_payment_canary_accepts_phase_d_a2a_game_config(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CANARY_MARKET_PROTOCOL", "agent_a2a.v1")
    monkeypatch.setenv("CANARY_ROUND_COUNT", "8")

    assert resolve_canary_game_config() == ("agent_a2a.v1", 8)


def test_payment_canary_scales_the_player_mandate_to_eight_rounds() -> None:
    assert canary_mandate_limits(8) == (20_000_000, 160_000_000)


def test_phase_d_canary_portfolios_create_controlled_iron_liquidity() -> None:
    initial_prices = {
        "grain": 2_000_000,
        "iron": 5_000_000,
        "warhorse": 8_000_000,
        "gems": 3_000_000,
    }
    portfolios = [phase_d_portfolio_for_seat(seat) for seat in range(10)]

    assert portfolios[2] == (20_000_000, {})
    assert portfolios[3] == (0, {"grain": 10})
    assert portfolios[4] == (0, {"grain": 10})
    assert all(
        cash
        + sum(
            quantity * initial_prices[good]
            for good, quantity in holdings.items()
        )
        == 20_000_000
        for cash, holdings in portfolios
    )


def test_phase_d_canary_accepts_a_frozen_liquidity_seed(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "CANARY_EVENT_SEED",
        "phase-d-a2a-liquidity-v1",
    )

    assert (
        resolve_canary_event_seed("game-ignored")
        == "phase-d-a2a-liquidity-v1"
    )


def test_payment_canary_accepts_an_external_connector_player(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CANARY_PLAYER_RUNTIME", "connector")
    monkeypatch.setenv("CANARY_PLAYER_USER_ID", "user:codex")
    monkeypatch.setenv("CANARY_PLAYER_AGENT_ID", "agent:codex")

    assert resolve_canary_player_config() == CanaryPlayerConfig(
        runtime_kind="connector",
        user_id="user:codex",
        agent_id="agent:codex",
    )


def test_payment_canary_rejects_connector_without_frozen_identity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CANARY_PLAYER_RUNTIME", "connector")
    monkeypatch.delenv("CANARY_PLAYER_USER_ID", raising=False)
    monkeypatch.delenv("CANARY_PLAYER_AGENT_ID", raising=False)

    with pytest.raises(
        RuntimeError,
        match="CANARY_PLAYER_USER_ID.*CANARY_PLAYER_AGENT_ID",
    ):
        resolve_canary_player_config()


def test_payment_canary_rejects_unknown_player_runtime(monkeypatch) -> None:
    monkeypatch.setenv("CANARY_PLAYER_RUNTIME", "native-a2a")

    with pytest.raises(
        RuntimeError,
        match="CANARY_PLAYER_RUNTIME",
    ):
        resolve_canary_player_config()


def test_payment_canary_defaults_to_testnet_eip3009_settlement(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CANARY_SETTLEMENT_MODE", raising=False)

    assert resolve_canary_settlement_mode() == "testnet_eip3009"


def test_payment_canary_accepts_the_phase_d_game_coin(monkeypatch) -> None:
    monkeypatch.setenv("CANARY_TOKEN_PROFILE", "arena402_g")

    assert resolve_canary_asset_config() == CanaryAssetConfig(
        profile="arena402_g",
        chain_id=1439,
        token_address="0xbf7b7268ce82d92bac7a95a741f4003fe84e1884",
        token_symbol="arena402-g",
        token_decimals=6,
        token_eip712_name="Arena 402 Gold",
        token_eip712_version="1",
    )


def test_payment_canary_rejects_an_unknown_token_profile(monkeypatch) -> None:
    monkeypatch.setenv("CANARY_TOKEN_PROFILE", "mainnet-usdc")

    with pytest.raises(RuntimeError, match="CANARY_TOKEN_PROFILE"):
        resolve_canary_asset_config()


def test_payment_canary_accepts_disabled_settlement(monkeypatch) -> None:
    monkeypatch.setenv("CANARY_SETTLEMENT_MODE", "disabled")

    assert resolve_canary_settlement_mode() == "disabled"


def test_payment_canary_rejects_unknown_settlement_mode(monkeypatch) -> None:
    monkeypatch.setenv("CANARY_SETTLEMENT_MODE", "production")

    with pytest.raises(
        RuntimeError,
        match="CANARY_SETTLEMENT_MODE",
    ):
        resolve_canary_settlement_mode()


def test_disabled_mixed_smoke_does_not_require_learning_activation() -> None:
    summary = {
        "phase": "completed",
        "participantCount": 10,
        "runtimeCounts": {"hosted": 9, "connector": 1},
        "roundCount": 1,
        "marketProtocol": "agent_a2a.v1",
        "agentTaskCount": 10,
        "completedAgentTaskCount": 10,
        "settledTradeCount": 0,
        "chainSubmissionCount": 0,
        "inventoryCommitCount": 0,
        "learningJobs": [{"status": "rejected", "count": 9}],
    }

    assert canary_summary_is_accepted(
        summary=summary,
        expected_runtime_counts={"hosted": 9, "connector": 1},
        round_count=1,
        market_protocol="agent_a2a.v1",
        settlement_mode="disabled",
    )


def test_testnet_canary_requires_settlement_and_learning_activation() -> None:
    summary = {
        "phase": "completed",
        "participantCount": 10,
        "runtimeCounts": {"hosted": 9, "connector": 1},
        "roundCount": 8,
        "marketProtocol": "agent_a2a.v1",
        "agentTaskCount": 80,
        "completedAgentTaskCount": 80,
        "settledTradeCount": 1,
        "chainSubmissionCount": 1,
        "inventoryCommitCount": 1,
        "learningJobs": [{"status": "rejected", "count": 9}],
    }

    assert not canary_summary_is_accepted(
        summary=summary,
        expected_runtime_counts={"hosted": 9, "connector": 1},
        round_count=8,
        market_protocol="agent_a2a.v1",
        settlement_mode="testnet_eip3009",
    )


def test_payment_canary_rejects_unknown_market_protocol(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CANARY_MARKET_PROTOCOL", "agent_a2a.latest")

    with pytest.raises(
        RuntimeError,
        match="CANARY_MARKET_PROTOCOL",
    ):
        resolve_canary_game_config()


@pytest.mark.parametrize("round_count", ["0", "11", "not-an-integer"])
def test_payment_canary_rejects_invalid_round_count(
    monkeypatch,
    round_count: str,
) -> None:
    monkeypatch.setenv("CANARY_ROUND_COUNT", round_count)

    with pytest.raises(
        RuntimeError,
        match="CANARY_ROUND_COUNT",
    ):
        resolve_canary_game_config()


def test_payment_canary_runner_freezes_the_validated_game_config() -> None:
    assert "resolve_canary_game_config()" in RUNNER
    assert "round_count=round_count" in RUNNER
    assert "market_protocol=market_protocol" in RUNNER
    assert '"marketProtocol": str(game["market_protocol"])' in RUNNER


def test_payment_canary_runner_joins_the_external_connector_player() -> None:
    assert "resolve_canary_player_config()" in RUNNER
    assert "add_connector_participant(" in RUNNER
    assert '"connector": 1' in RUNNER
    assert '"hosted": 9' in RUNNER


def test_payment_canary_runner_supports_a_no_chain_protocol_smoke() -> None:
    assert "resolve_canary_settlement_mode()" in RUNNER
    assert 'authorization_mode="none"' in RUNNER
    assert '"settlementMode": settlement_mode' in RUNNER


def test_payment_canary_runner_waits_for_game_coin_provisioning() -> None:
    assert "resolve_canary_asset_config()" in RUNNER
    assert "_wait_for_game_coin_ready(" in RUNNER
    assert '"settlementAsset": asset.profile' in RUNNER


def test_payment_canary_runner_uses_manual_phase_d_portfolios() -> None:
    assert "phase_d_portfolio_for_seat(" in RUNNER
    assert 'portfolio_mode="manual"' in RUNNER
    assert "resolve_canary_event_seed(game_id)" in RUNNER
