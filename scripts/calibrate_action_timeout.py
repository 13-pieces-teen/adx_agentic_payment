"""Calibrate Arena action deadlines from persisted real-Runtime task evidence.

The report is deliberately fail-closed: it emits an action timeout
recommendation only when every explicitly required Runtime/task combination
has enough terminal samples and stays within the allowed deadline-timeout
rate. HTTP endpoint latency is a separate capacity signal and is not accepted
as AgentTask latency evidence here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TaskLatencySample:
    task_id: str
    game_id: str
    runtime_label: str
    task_kind: str
    task_status: str
    runtime_status: str | None
    attempt_count: int
    created_at: datetime
    first_leased_at: datetime | None
    result_received_at: datetime | None
    arena_applied_at: datetime | None
    terminal_reason: str | None = None


def nearest_rank_percentile(
    values: Sequence[float],
    percentile: float,
) -> float | None:
    """Return an observed value using the nearest-rank definition."""

    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    value = (end - start).total_seconds() * 1000
    return max(0.0, value)


def _latency_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _rounded_percentile(values, 0.50),
        "p95": _rounded_percentile(values, 0.95),
        "p99": _rounded_percentile(values, 0.99),
        "max": round(max(values), 2) if values else None,
    }


def _rounded_percentile(
    values: Sequence[float],
    percentile: float,
) -> float | None:
    value = nearest_rank_percentile(values, percentile)
    return round(value, 2) if value is not None else None


def _is_deadline_timeout(sample: TaskLatencySample) -> bool:
    if sample.runtime_status == "timed_out":
        return True
    reason = (sample.terminal_reason or "").lower()
    return "deadline" in reason or "timeout" in reason


def _group_report(
    runtime_label: str,
    task_kind: str,
    samples: Sequence[TaskLatencySample],
) -> dict[str, object]:
    terminal = [
        sample
        for sample in samples
        if sample.task_status in {"completed", "defaulted", "cancelled"}
        and sample.result_received_at is not None
    ]
    successful = [
        sample
        for sample in terminal
        if sample.task_status == "completed" and sample.runtime_status == "succeeded"
    ]
    timeout_count = sum(_is_deadline_timeout(sample) for sample in terminal)
    retry_count = sum(sample.attempt_count > 1 for sample in terminal)
    failed_count = sum(
        sample.runtime_status in {"failed", "cancelled"} for sample in terminal
    )
    queue_age = [
        value
        for sample in successful
        if (value := _duration_ms(sample.created_at, sample.first_leased_at))
        is not None
    ]
    e2e_latency = [
        value
        for sample in successful
        if (
            value := _duration_ms(
                sample.created_at,
                sample.result_received_at,
            )
        )
        is not None
    ]
    apply_latency = [
        value
        for sample in successful
        if (
            value := _duration_ms(
                sample.result_received_at,
                sample.arena_applied_at,
            )
        )
        is not None
    ]
    terminal_count = len(terminal)
    return {
        "runtimeLabel": runtime_label,
        "taskKind": task_kind,
        "sampleCount": terminal_count,
        "observedTaskCount": len(samples),
        "nonTerminalCount": len(samples) - terminal_count,
        "successfulSampleCount": len(successful),
        "deadlineTimeoutCount": timeout_count,
        "deadlineTimeoutRate": (
            timeout_count / terminal_count if terminal_count else None
        ),
        "failedOrCancelledCount": failed_count,
        "failedOrCancelledRate": (
            failed_count / terminal_count if terminal_count else None
        ),
        "retryCount": retry_count,
        "retryRate": retry_count / terminal_count if terminal_count else None,
        "queueAgeMs": _latency_summary(queue_age),
        "e2eLatencyMs": _latency_summary(e2e_latency),
        "applyLatencyMs": _latency_summary(apply_latency),
    }


def build_calibration_report(
    samples: Iterable[TaskLatencySample],
    *,
    required_combinations: Sequence[tuple[str, str]],
    minimum_samples: int = 100,
    maximum_timeout_rate: float = 0.01,
    safety_factor: float = 1.25,
    round_up_ms: int = 5_000,
) -> dict[str, object]:
    if not required_combinations:
        raise ValueError("at least one required Runtime/task combination is required")
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be positive")
    if not 0 <= maximum_timeout_rate <= 1:
        raise ValueError("maximum_timeout_rate must be in [0, 1]")
    if safety_factor <= 0:
        raise ValueError("safety_factor must be positive")
    if round_up_ms < 1:
        raise ValueError("round_up_ms must be positive")

    sample_values = list(samples)
    grouped: dict[tuple[str, str], list[TaskLatencySample]] = {}
    for sample in sample_values:
        key = (sample.runtime_label, sample.task_kind)
        grouped.setdefault(key, []).append(sample)

    groups = [
        _group_report(runtime_label, task_kind, group_samples)
        for (runtime_label, task_kind), group_samples in sorted(grouped.items())
    ]
    groups_by_key = {
        (str(group["runtimeLabel"]), str(group["taskKind"])): group for group in groups
    }
    blockers: list[dict[str, object]] = []
    required_p99_values: list[float] = []
    for runtime_label, task_kind in required_combinations:
        group = groups_by_key.get((runtime_label, task_kind))
        sample_count = int(group["sampleCount"]) if group else 0
        if sample_count < minimum_samples:
            blockers.append(
                {
                    "runtimeLabel": runtime_label,
                    "taskKind": task_kind,
                    "reason": "sample_count_below_minimum",
                    "observed": sample_count,
                    "required": minimum_samples,
                }
            )
            continue

        non_terminal_count = int(group["nonTerminalCount"])
        if non_terminal_count:
            blockers.append(
                {
                    "runtimeLabel": runtime_label,
                    "taskKind": task_kind,
                    "reason": "non_terminal_samples_present",
                    "observed": non_terminal_count,
                    "required": 0,
                }
            )
        timeout_rate = group["deadlineTimeoutRate"]
        if timeout_rate is not None and timeout_rate > maximum_timeout_rate:
            blockers.append(
                {
                    "runtimeLabel": runtime_label,
                    "taskKind": task_kind,
                    "reason": "deadline_timeout_rate_exceeded",
                    "observed": timeout_rate,
                    "required": maximum_timeout_rate,
                }
            )
        e2e_p99 = group["e2eLatencyMs"]["p99"]
        if e2e_p99 is None:
            blockers.append(
                {
                    "runtimeLabel": runtime_label,
                    "taskKind": task_kind,
                    "reason": "no_successful_e2e_samples",
                    "observed": 0,
                    "required": 1,
                }
            )
        else:
            required_p99_values.append(float(e2e_p99))

    recommendation: int | None = None
    if not blockers:
        raw_recommendation = max(required_p99_values) * safety_factor
        recommendation = math.ceil(raw_recommendation / round_up_ms) * round_up_ms

    if not blockers:
        status = "calibrated"
    elif any(blocker["reason"] == "sample_count_below_minimum" for blocker in blockers):
        status = "insufficient_samples"
    elif any(
        blocker["reason"] == "deadline_timeout_rate_exceeded" for blocker in blockers
    ):
        status = "timeout_rate_exceeded"
    else:
        status = "incomplete_evidence"

    return {
        "schemaVersion": "arena.action-timeout-calibration.v1",
        "status": status,
        "minimumSamplesPerCombination": minimum_samples,
        "maximumDeadlineTimeoutRate": maximum_timeout_rate,
        "safetyFactor": safety_factor,
        "roundUpMs": round_up_ms,
        "requiredCombinations": [
            {"runtimeLabel": runtime, "taskKind": task}
            for runtime, task in required_combinations
        ],
        "includedGameIds": sorted({sample.game_id for sample in sample_values}),
        "groups": groups,
        "blockingReasons": blockers,
        "recommendedActionTimeoutMs": recommendation,
    }


TASK_SAMPLE_SQL = """
WITH first_lease AS (
    SELECT task_id, min(created_at) AS first_leased_at
    FROM arena_agent_task_events
    WHERE event_type = 'leased'
    GROUP BY task_id
)
SELECT
    task.task_id,
    task.game_id,
    CASE binding.runtime_kind
        WHEN 'connector' THEN coalesce(runtime.kind, 'connector:unknown')
        WHEN 'hosted' THEN concat(
            'hosted:',
            coalesce(
                task.runtime_config_snapshot->>'provider_id',
                hosted.provider,
                'unknown'
            ),
            '/',
            coalesce(
                task.runtime_config_snapshot->>'model_id',
                hosted.model,
                'unknown'
            ),
            '#thinking=',
            coalesce(
                task.runtime_config_snapshot->>'thinking_enabled',
                hosted.thinking_enabled::text,
                'unknown'
            )
        )
        ELSE binding.runtime_kind
    END AS runtime_label,
    task.task_kind,
    task.status AS task_status,
    result.runtime_status,
    task.attempt_count,
    task.created_at,
    lease.first_leased_at,
    result.result_received_at,
    result.arena_applied_at,
    task.terminal_reason
FROM arena_agent_tasks AS task
JOIN arena_runtime_bindings AS binding
  ON binding.runtime_binding_id = task.runtime_binding_id
LEFT JOIN connector_bindings AS connector_binding
  ON connector_binding.binding_id = binding.connector_binding_id
LEFT JOIN connector_runtimes AS runtime
  ON runtime.device_id = connector_binding.device_id
 AND runtime.runtime_id = connector_binding.runtime_id
LEFT JOIN arena_hosted_configs AS hosted
  ON hosted.hosted_config_id = binding.hosted_config_id
LEFT JOIN arena_agent_task_results AS result
  ON result.task_id = task.task_id
LEFT JOIN first_lease AS lease
  ON lease.task_id = task.task_id
WHERE task.game_id = ANY($1::text[])
ORDER BY task.created_at, task.task_id
"""


async def load_samples(
    database_url: str,
    game_ids: Sequence[str],
) -> list[TaskLatencySample]:
    import asyncpg

    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        rows = await connection.fetch(TASK_SAMPLE_SQL, list(game_ids))
    finally:
        await connection.close()
    return [
        TaskLatencySample(
            task_id=row["task_id"],
            game_id=row["game_id"],
            runtime_label=row["runtime_label"],
            task_kind=row["task_kind"],
            task_status=row["task_status"],
            runtime_status=row["runtime_status"],
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
            first_leased_at=row["first_leased_at"],
            result_received_at=row["result_received_at"],
            arena_applied_at=row["arena_applied_at"],
            terminal_reason=row["terminal_reason"],
        )
        for row in rows
    ]


def _required_combination(value: str) -> tuple[str, str]:
    runtime_label, separator, task_kind = value.rpartition(":")
    if not separator or not runtime_label or not task_kind:
        raise argparse.ArgumentTypeError(
            "required combination must be RUNTIME_LABEL:TASK_KIND"
        )
    return runtime_label, task_kind


def _database_url(value: str | None) -> str:
    resolved = (
        value
        or os.getenv("ARENA_TEST_DATABASE_URL")
        or os.getenv("ADX_ARENA_API_DATABASE_URL")
        or ""
    ).strip()
    if not resolved:
        raise RuntimeError(
            "--database-url, ARENA_TEST_DATABASE_URL, or "
            "ADX_ARENA_API_DATABASE_URL is required"
        )
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a fail-closed action_timeout_ms calibration report from "
            "persisted real-Runtime AgentTasks."
        )
    )
    parser.add_argument("--database-url")
    parser.add_argument("--game-id", action="append", required=True)
    parser.add_argument(
        "--require",
        action="append",
        type=_required_combination,
        required=True,
        dest="required_combinations",
        metavar="RUNTIME_LABEL:TASK_KIND",
    )
    parser.add_argument("--minimum-samples", type=int, default=100)
    parser.add_argument("--maximum-timeout-rate", type=float, default=0.01)
    parser.add_argument("--safety-factor", type=float, default=1.25)
    parser.add_argument("--round-up-ms", type=int, default=5_000)
    args = parser.parse_args()

    try:
        samples = asyncio.run(
            load_samples(_database_url(args.database_url), args.game_id)
        )
        report = build_calibration_report(
            samples,
            required_combinations=args.required_combinations,
            minimum_samples=args.minimum_samples,
            maximum_timeout_rate=args.maximum_timeout_rate,
            safety_factor=args.safety_factor,
            round_up_ms=args.round_up_ms,
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(0 if report["status"] == "calibrated" else 2)


if __name__ == "__main__":
    main()
