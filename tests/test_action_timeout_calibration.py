from datetime import datetime, timedelta, timezone

from scripts.calibrate_action_timeout import (
    TASK_SAMPLE_SQL,
    TaskLatencySample,
    _required_combination,
    build_calibration_report,
    nearest_rank_percentile,
)


BASE = datetime(2026, 8, 5, tzinfo=timezone.utc)


def _sample(
    index: int,
    *,
    runtime_label: str = "codex",
    task_kind: str = "arena.market.intent",
    latency_ms: int = 1_000,
    task_status: str = "completed",
    runtime_status: str = "succeeded",
    attempt_count: int = 1,
) -> TaskLatencySample:
    created_at = BASE + timedelta(seconds=index)
    leased_at = created_at + timedelta(milliseconds=100)
    received_at = created_at + timedelta(milliseconds=latency_ms)
    return TaskLatencySample(
        task_id=f"task-{index}",
        game_id="game-calibration",
        runtime_label=runtime_label,
        task_kind=task_kind,
        task_status=task_status,
        runtime_status=runtime_status,
        attempt_count=attempt_count,
        created_at=created_at,
        first_leased_at=leased_at,
        result_received_at=received_at,
        arena_applied_at=received_at + timedelta(milliseconds=20),
    )


def test_nearest_rank_percentile_uses_observed_tail_value() -> None:
    values = [float(value) for value in range(1, 101)]

    assert nearest_rank_percentile(values, 0.50) == 50.0
    assert nearest_rank_percentile(values, 0.95) == 95.0
    assert nearest_rank_percentile(values, 0.99) == 99.0


def test_required_combination_preserves_colon_in_hosted_runtime_label() -> None:
    assert _required_combination(
        "hosted:deepseek/deepseek-chat:arena.market.intent"
    ) == (
        "hosted:deepseek/deepseek-chat",
        "arena.market.intent",
    )


def test_sample_query_uses_frozen_hosted_config_and_connector_runtime_identity() -> (
    None
):
    assert "task.runtime_config_snapshot->>'provider_id'" in TASK_SAMPLE_SQL
    assert "task.runtime_config_snapshot->>'model_id'" in TASK_SAMPLE_SQL
    assert "task.runtime_config_snapshot->>'thinking_enabled'" in TASK_SAMPLE_SQL
    assert "connector_runtimes AS runtime" in TASK_SAMPLE_SQL


def test_report_refuses_to_recommend_when_required_sample_is_missing() -> None:
    report = build_calibration_report(
        [_sample(1), _sample(2)],
        required_combinations=[("codex", "arena.market.intent")],
        minimum_samples=3,
    )

    assert report["status"] == "insufficient_samples"
    assert report["recommendedActionTimeoutMs"] is None
    assert report["includedGameIds"] == ["game-calibration"]
    assert report["blockingReasons"] == [
        {
            "runtimeLabel": "codex",
            "taskKind": "arena.market.intent",
            "reason": "sample_count_below_minimum",
            "observed": 2,
            "required": 3,
        }
    ]


def test_report_recommends_maximum_successful_e2e_p99_with_safety_margin() -> None:
    samples = [
        _sample(1, latency_ms=1_000),
        _sample(2, latency_ms=2_000),
        _sample(3, latency_ms=3_100),
        _sample(
            4,
            runtime_label="codex",
            task_kind="arena.market.rfq",
            latency_ms=4_100,
        ),
        _sample(
            5,
            runtime_label="codex",
            task_kind="arena.market.rfq",
            latency_ms=5_100,
        ),
        _sample(
            6,
            runtime_label="codex",
            task_kind="arena.market.rfq",
            latency_ms=6_100,
            attempt_count=2,
        ),
    ]

    report = build_calibration_report(
        samples,
        required_combinations=[
            ("codex", "arena.market.intent"),
            ("codex", "arena.market.rfq"),
        ],
        minimum_samples=3,
        safety_factor=1.25,
        round_up_ms=5_000,
    )

    assert report["status"] == "calibrated"
    assert report["recommendedActionTimeoutMs"] == 10_000
    assert report["blockingReasons"] == []
    rfq = next(
        group for group in report["groups"] if group["taskKind"] == "arena.market.rfq"
    )
    assert rfq["sampleCount"] == 3
    assert rfq["successfulSampleCount"] == 3
    assert rfq["retryRate"] == 1 / 3
    assert rfq["e2eLatencyMs"]["p99"] == 6_100.0
    assert rfq["queueAgeMs"]["p50"] == 100.0
    assert rfq["applyLatencyMs"]["p95"] == 20.0


def test_report_refuses_to_freeze_when_deadline_timeout_rate_exceeds_one_percent() -> (
    None
):
    samples = [_sample(index) for index in range(99)]
    samples.append(
        _sample(
            100,
            latency_ms=30_000,
            task_status="defaulted",
            runtime_status="timed_out",
        )
    )
    accepted = build_calibration_report(
        samples,
        required_combinations=[("codex", "arena.market.intent")],
        minimum_samples=100,
        maximum_timeout_rate=0.01,
    )
    assert accepted["status"] == "calibrated"

    samples.pop()
    samples[-1] = _sample(
        99,
        latency_ms=30_000,
        task_status="defaulted",
        runtime_status="timed_out",
    )
    rejected = build_calibration_report(
        samples,
        required_combinations=[("codex", "arena.market.intent")],
        minimum_samples=99,
        maximum_timeout_rate=0.01,
    )

    assert rejected["status"] == "timeout_rate_exceeded"
    assert rejected["recommendedActionTimeoutMs"] is None
    assert rejected["blockingReasons"][0]["observed"] == 1 / 99
