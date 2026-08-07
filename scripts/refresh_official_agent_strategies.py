"""Refresh enabled official Agents without re-ingesting provider credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from hosted_agent_control_plane import HostedAgentUpdateRequest
from hosted_agent_control_plane.postgres_repository import (
    PostgresHostedAgentControlRepository,
)
from hosted_agent_control_plane.services import HostedAgentService
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
)
from hosted_agent_runtime.official_market_strategy import (
    OFFICIAL_MARKET_STRATEGY_RELEASE_V2,
    official_market_strategy_v2,
)


STRATEGY_VERSION = OFFICIAL_MARKET_STRATEGY_RELEASE_V2


def _strategy(index: int) -> str:
    return official_market_strategy_v2(index).instructions


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _update_idempotency_key(agent_id: str) -> str:
    return f"official-strategy-{STRATEGY_VERSION}-{agent_id}"


def _select_official_rows(
    rows: list[object],
    *,
    priorities: tuple[int, ...] | None,
) -> list[object]:
    if priorities is None:
        return list(rows)
    if not priorities or len(set(priorities)) != len(priorities):
        raise RuntimeError("official priorities must be unique")
    requested = set(priorities)
    selected = [
        row
        for row in rows
        if int(row["priority"]) in requested  # type: ignore[index]
    ]
    available = {
        int(row["priority"])  # type: ignore[index]
        for row in selected
    }
    if available != requested:
        raise RuntimeError("requested official priority is not enabled")
    return selected


async def _refresh(args: argparse.Namespace) -> dict[str, object]:
    import asyncpg

    control_database_url = _required_environment("ADX_HOSTED_CONTROL_DATABASE_URL")
    operator_database_url = _required_environment(
        "ADX_OFFICIAL_BOOTSTRAP_DATABASE_URL"
    )
    operator_connection = await asyncpg.connect(
        operator_database_url,
        command_timeout=30,
    )
    repository = PostgresHostedAgentControlRepository(control_database_url)
    try:
        await repository.initialize()
        rows = await operator_connection.fetch(
            """
            SELECT
                pool.agent_id,
                agents.owner_user_id,
                pool.priority
            FROM arena402.official_agent_pool AS pool
            JOIN public.arena_agents AS agents
              ON agents.agent_id = pool.agent_id
            WHERE pool.enabled = TRUE
            ORDER BY pool.priority ASC, pool.agent_id ASC
            """
        )
        if not rows:
            raise RuntimeError("official Agent pool is empty")
        rows = _select_official_rows(
            list(rows),
            priorities=(
                tuple(args.priority)
                if args.priority is not None
                else None
            ),
        )

        service = HostedAgentService(
            repository,
            capabilities=build_production_capability_registry(
                include_official=True
            ),
            hosted_agents_enabled=True,
        )
        refreshed: list[tuple[str, str]] = []
        for row in rows:
            owner_user_id = str(row["owner_user_id"])
            agent_id = str(row["agent_id"])
            current = await service.get_hosted_agent(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
            )
            await service.update_hosted_agent(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                request=HostedAgentUpdateRequest(
                    provider_id=current.provider_id,
                    model_id=current.model_id,
                    thinking_enabled=current.thinking_enabled,
                    strategy_instructions=_strategy(int(row["priority"])),
                    idempotency_key=_update_idempotency_key(agent_id),
                ),
            )
            refreshed.append((owner_user_id, agent_id))

        deadline = time.monotonic() + args.validation_timeout_seconds
        pending = {agent_id: owner_user_id for owner_user_id, agent_id in refreshed}
        while pending and time.monotonic() < deadline:
            for agent_id, owner_user_id in tuple(pending.items()):
                current = await service.get_hosted_agent(
                    owner_user_id=owner_user_id,
                    agent_id=agent_id,
                )
                if (
                    current.route_status.value == "ready"
                    and current.provisioning_status.value == "ready"
                ):
                    pending.pop(agent_id)
                elif current.provisioning_status.value in {"degraded", "disabled"}:
                    raise RuntimeError(
                        f"official Agent validation failed: {agent_id}"
                    )
            if pending:
                await asyncio.sleep(1)
        if pending:
            raise RuntimeError("timed out waiting for official Agent validation")

        return {
            "status": "ready",
            "strategyVersion": STRATEGY_VERSION,
            "agentCount": len(refreshed),
            "priorities": [int(row["priority"]) for row in rows],
            "agentIds": [agent_id for _, agent_id in refreshed],
        }
    finally:
        await repository.close()
        await operator_connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh enabled Arena 402 official Agent strategies while reusing "
            "their existing validated Hosted credentials."
        )
    )
    parser.add_argument(
        "--priority",
        action="append",
        type=int,
        choices=range(1, 101),
        help=(
            "Refresh only this enabled official pool priority. Repeat to "
            "select multiple priorities; omit to refresh the full pool."
        ),
    )
    parser.add_argument(
        "--validation-timeout-seconds",
        type=int,
        default=600,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 30 <= args.validation_timeout_seconds <= 3600:
        raise SystemExit("--validation-timeout-seconds must be between 30 and 3600")
    try:
        result = asyncio.run(_refresh(args))
    except Exception as exc:
        safe_code = getattr(exc, "code", exc.__class__.__name__)
        print(
            json.dumps(
                {"status": "failed", "error": str(safe_code)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
