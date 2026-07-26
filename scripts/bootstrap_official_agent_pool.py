"""Provision an explicit pool of production DeepSeek Hosted Agents.

The model key is accepted only from a file and is passed through the existing
write-only Hosted credential ingress. The raw key is never written to Arena
business tables, command output, or logs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from hosted_agent_control_plane import (
    CredentialIngressRequest,
    HostedAgentCreateRequest,
)
from hosted_agent_control_plane.postgres_repository import (
    PostgresHostedAgentControlRepository,
)
from hosted_agent_control_plane.services import (
    CredentialIngressService,
    HostedAgentService,
)
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
)
from hosted_agent_runtime.production_secrets import (
    build_production_secret_writer,
    close_secret_port,
    initialize_secret_port,
)


@dataclass(frozen=True, slots=True)
class ProvisionedOfficialAgent:
    owner_user_id: str
    agent_id: str


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _fingerprint_pepper() -> bytes:
    try:
        value = base64.b64decode(
            _required_environment("ADX_HOSTED_FINGERPRINT_PEPPER_B64"),
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise RuntimeError(
            "ADX_HOSTED_FINGERPRINT_PEPPER_B64 must be valid base64"
        ) from None
    if len(value) < 32:
        raise RuntimeError("ADX_HOSTED_FINGERPRINT_PEPPER_B64 must decode to 32+ bytes")
    return value


def _load_api_key(path: Path) -> SecretStr:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError("DeepSeek API key path must be a regular file")
    raw = resolved.read_bytes()
    if not raw or len(raw) > 16_384 or b"\x00" in raw:
        raise RuntimeError("DeepSeek API key file is invalid")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("DeepSeek API key file must be UTF-8") from None
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("DeepSeek API key file must contain one key")
    return SecretStr(value)


def _owner_id(index: int) -> str:
    return f"official:deepseek:{index:03d}"


def _username(index: int) -> str:
    return f"official-deepseek-{index:03d}"


def _strategy(index: int) -> str:
    if index % 2:
        preference = (
            "Prefer buying when the public market and your portfolio imply "
            "positive expected value."
        )
    else:
        preference = (
            "Prefer selling inventory when the public market offers a "
            "profitable price."
        )
    return (
        "You are an official Arena 402 market participant. "
        f"{preference} "
        "Use only the supplied game state, obey quantity and limit-price "
        "constraints, negotiate in good faith, and pass when no safe trade "
        "exists. Never disclose credentials or private reasoning."
    )


async def _ensure_official_users(
    connection: object,
    *,
    count: int,
) -> None:
    for index in range(1, count + 1):
        await connection.execute(
            """
            INSERT INTO public.connector_users (
                user_id, username, password_hash, temporary,
                identity_provider, provider_subject
            )
            VALUES ($1, $2, NULL, TRUE, 'password', NULL)
            ON CONFLICT (user_id) DO NOTHING
            """,
            _owner_id(index),
            _username(index),
        )
        row = await connection.fetchrow(
            """
            SELECT username, temporary, disabled_at
            FROM public.connector_users
            WHERE user_id = $1
            """,
            _owner_id(index),
        )
        if (
            row is None
            or str(row["username"]) != _username(index)
            or row["temporary"] is not True
            or row["disabled_at"] is not None
        ):
            raise RuntimeError("official user identity conflict")


async def _activate_pool(
    connection: object,
    agents: tuple[ProvisionedOfficialAgent, ...],
    *,
    replace_enabled_pool: bool,
) -> None:
    agent_ids = [agent.agent_id for agent in agents]
    async with connection.transaction():
        ready_count = int(
            await connection.fetchval(
                """
                SELECT count(*)
                FROM public.arena_agents AS agent
                JOIN public.arena_runtime_bindings AS binding
                  ON binding.agent_id = agent.agent_id
                 AND binding.runtime_kind = 'hosted'
                 AND binding.route_status = 'ready'
                 AND binding.disabled_at IS NULL
                JOIN public.arena_hosted_configs AS hosted
                  ON hosted.hosted_config_id = binding.hosted_config_id
                 AND hosted.agent_id = agent.agent_id
                 AND hosted.status = 'ready'
                JOIN public.arena_model_credentials AS credential
                  ON credential.credential_id = hosted.credential_id
                 AND credential.status = 'valid'
                WHERE agent.agent_id = ANY($1::text[])
                  AND agent.status = 'active'
                """,
                agent_ids,
            )
        )
        if ready_count != len(agent_ids):
            raise RuntimeError("official Hosted Agents are not all ready")

        existing_rows = await connection.fetch(
            """
            SELECT agent_id, wallet_id
            FROM arena402.official_agent_pool
            WHERE agent_id = ANY($1::text[])
            FOR UPDATE
            """,
            agent_ids,
        )
        wallet_by_agent = {
            str(row["agent_id"]): str(row["wallet_id"]) for row in existing_rows
        }
        missing_agents = [
            agent_id for agent_id in agent_ids if agent_id not in wallet_by_agent
        ]
        available_wallets = await connection.fetch(
            """
            SELECT wallet_id
            FROM arena402.wallet_inventory
            WHERE status = 'available'
            ORDER BY wallet_id
            FOR UPDATE SKIP LOCKED
            LIMIT $1
            """,
            len(missing_agents),
        )
        if len(available_wallets) != len(missing_agents):
            raise RuntimeError("official wallet pool is exhausted")
        for agent_id, wallet in zip(
            missing_agents,
            available_wallets,
            strict=True,
        ):
            wallet_id = str(wallet["wallet_id"])
            wallet_by_agent[agent_id] = wallet_id
            await connection.execute(
                """
                UPDATE arena402.wallet_inventory
                SET status = 'bound'
                WHERE wallet_id = $1 AND status = 'available'
                """,
                wallet_id,
            )

        if replace_enabled_pool:
            await connection.execute(
                """
                UPDATE arena402.official_agent_pool
                SET enabled = FALSE, disabled_at = clock_timestamp()
                WHERE enabled
                  AND NOT (agent_id = ANY($1::text[]))
                """,
                agent_ids,
            )

        for priority, agent_id in enumerate(agent_ids, start=1):
            await connection.execute(
                """
                INSERT INTO arena402.official_agent_pool (
                    agent_id, wallet_id, priority, enabled, disabled_at
                )
                VALUES ($1, $2, $3, TRUE, NULL)
                ON CONFLICT (agent_id) DO UPDATE
                SET priority = EXCLUDED.priority,
                    enabled = TRUE,
                    disabled_at = NULL
                """,
                agent_id,
                wallet_by_agent[agent_id],
                priority,
            )


async def _provision(args: argparse.Namespace) -> dict[str, object]:
    import asyncpg

    api_key = _load_api_key(args.api_key_file)
    control_database_url = _required_environment("ADX_HOSTED_CONTROL_DATABASE_URL")
    operator_database_url = _required_environment("ADX_OFFICIAL_BOOTSTRAP_DATABASE_URL")

    operator_connection = await asyncpg.connect(
        operator_database_url,
        command_timeout=30,
    )
    repository = PostgresHostedAgentControlRepository(control_database_url)
    secret_writer = build_production_secret_writer(control_database_url)
    try:
        await repository.initialize()
        await initialize_secret_port(secret_writer)
        await _ensure_official_users(
            operator_connection,
            count=args.count,
        )

        registry = build_production_capability_registry()
        credential_service = CredentialIngressService(
            repository,
            secret_writer=secret_writer,
            fingerprint_pepper=_fingerprint_pepper(),
            fingerprint_pepper_version=int(
                os.getenv("ADX_HOSTED_FINGERPRINT_PEPPER_VERSION", "1")
            ),
        )
        agent_service = HostedAgentService(
            repository,
            capabilities=registry,
            hosted_agents_enabled=True,
        )

        provisioned: list[ProvisionedOfficialAgent] = []
        for index in range(1, args.count + 1):
            owner_user_id = _owner_id(index)
            credential = await credential_service.create_credential(
                owner_user_id=owner_user_id,
                request=CredentialIngressRequest(
                    provider_id="deepseek",
                    api_key=api_key,
                    idempotency_key=(f"official-deepseek-credential-{index:03d}-v1"),
                ),
            )
            agent = await agent_service.create_hosted_agent(
                owner_user_id=owner_user_id,
                request=HostedAgentCreateRequest(
                    display_name=f"Arena Official {index:02d}",
                    credential_id=credential.credential_id,
                    provider_id="deepseek",
                    model_id=args.model,
                    thinking_enabled=args.thinking,
                    strategy_instructions=_strategy(index),
                    idempotency_key=(f"official-deepseek-agent-{index:03d}-v1"),
                ),
            )
            provisioned.append(
                ProvisionedOfficialAgent(
                    owner_user_id=owner_user_id,
                    agent_id=agent.agent_id,
                )
            )

        deadline = time.monotonic() + args.validation_timeout_seconds
        pending = {agent.agent_id: agent for agent in provisioned}
        while pending and time.monotonic() < deadline:
            for agent_id, item in tuple(pending.items()):
                current = await agent_service.get_hosted_agent(
                    owner_user_id=item.owner_user_id,
                    agent_id=agent_id,
                )
                if (
                    current.route_status.value == "ready"
                    and current.provisioning_status.value == "ready"
                ):
                    pending.pop(agent_id)
                elif current.provisioning_status.value in {
                    "degraded",
                    "disabled",
                }:
                    raise RuntimeError("DeepSeek credential validation did not succeed")
            if pending:
                await asyncio.sleep(1)
        if pending:
            raise RuntimeError("timed out waiting for DeepSeek credential validation")

        if args.activate:
            await _activate_pool(
                operator_connection,
                tuple(provisioned),
                replace_enabled_pool=args.replace_enabled_pool,
            )

        return {
            "status": "ready",
            "provider": "deepseek",
            "model": args.model,
            "agentCount": len(provisioned),
            "poolActivated": args.activate,
            "replacedEnabledPool": (args.activate and args.replace_enabled_pool),
            "agentIds": [agent.agent_id for agent in provisioned],
        }
    finally:
        api_key = SecretStr("")
        await close_secret_port(secret_writer)
        await repository.close()
        await operator_connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Provision validated DeepSeek Hosted Agents into the explicit "
            "Arena 402 official pool."
        )
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        required=True,
        help="UTF-8 file containing only the DeepSeek API key.",
    )
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--replace-enabled-pool", action="store_true")
    parser.add_argument(
        "--validation-timeout-seconds",
        type=int,
        default=600,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.count <= 100:
        raise SystemExit("--count must be between 1 and 100")
    if not 30 <= args.validation_timeout_seconds <= 3600:
        raise SystemExit("--validation-timeout-seconds must be between 30 and 3600")
    try:
        result = asyncio.run(_provision(args))
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
