"""Provision Official Agents that call DeepSeek through LiteLLM.

This command accepts one LiteLLM gateway token. Upstream DeepSeek keys belong
to LiteLLM and never enter an Official Agent credential or Arena task.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
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
from hosted_agent_runtime.strategy import (
    STRATEGY_CATALOG_VERSION_V1,
    official_strategy_archetype,
    render_strategy_revision,
)
from hosted_agent_runtime.learning import default_policy_profile

_CONFIG_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]{0,5}$")
_LITELLM_HEALTH_URL = "http://official-litellm:4000/health"


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


def _load_litellm_token(path: Path) -> SecretStr:
    descriptor = -1
    try:
        path_status = path.lstat()
        if stat.S_ISLNK(path_status.st_mode):
            raise RuntimeError("LiteLLM token file must not be a symlink")
        if not stat.S_ISREG(path_status.st_mode):
            raise RuntimeError("LiteLLM token path must be a regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or not os.path.samestat(path_status, opened_status)
        ):
            raise RuntimeError(
                "LiteLLM token path changed while it was opened"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(16_385)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("LiteLLM token file is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw or len(raw) > 16_384 or b"\x00" in raw:
        raise RuntimeError("LiteLLM token file is invalid")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("LiteLLM token file must be UTF-8") from None
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("LiteLLM token file must contain one token")
    if not value.startswith("sk-"):
        raise RuntimeError("LiteLLM token must start with sk-")
    return SecretStr(value)


def _require_healthy_litellm_payload(payload: object) -> int:
    if not isinstance(payload, dict):
        raise RuntimeError("LiteLLM deployment health response is invalid")
    healthy = payload.get("healthy_endpoints")
    unhealthy = payload.get("unhealthy_endpoints")
    if not isinstance(healthy, list) or not isinstance(unhealthy, list):
        raise RuntimeError("LiteLLM deployment health response is invalid")
    if unhealthy or not healthy:
        raise RuntimeError("LiteLLM has unhealthy DeepSeek deployments")
    return len(healthy)


async def _verify_litellm_deployments(
    token: SecretStr,
    *,
    timeout_seconds: int,
) -> int:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout_seconds)),
            trust_env=False,
        ) as client:
            response = await client.get(
                _LITELLM_HEALTH_URL,
                headers={
                    "Authorization": (
                        f"Bearer {token.get_secret_value()}"
                    )
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError(
            "LiteLLM deployment health check failed"
        ) from None
    return _require_healthy_litellm_payload(payload)


def _owner_id(index: int) -> str:
    return f"official:deepseek:{index:03d}"


def _username(index: int) -> str:
    return f"official-deepseek-{index:03d}"


_OFFICIAL_STRATEGY_PROFILES = (
    (
        "Deep Value Buyer",
        "BUY_BIASED",
        1200,
        35,
        "2 units per good",
        "grain > iron > warhorse > gems",
        "buy when marketPrice <= 0.90 * fairValue and set buyer limitPrice "
        "to 0.94 * fairValue; sell only when marketPrice >= 1.12 * "
        "fairValue and set seller limitPrice to 1.08 * fairValue",
    ),
    (
        "Opportunistic Accumulator",
        "BUY_BIASED",
        800,
        20,
        "3 units in the strongest two goods",
        "iron > gems > grain > warhorse",
        "buy when marketPrice <= 0.97 * fairValue and set buyer limitPrice "
        "to 0.99 * fairValue; sell when marketPrice >= 1.08 * fairValue "
        "and set seller limitPrice to 1.04 * fairValue",
    ),
    (
        "Defensive Seller",
        "SELL_BIASED",
        -800,
        65,
        "at most 1 unit per good",
        "gems > warhorse > iron > grain",
        "sell when marketPrice >= 0.96 * fairValue and set seller "
        "limitPrice to 0.94 * fairValue; buy only when marketPrice <= "
        "0.82 * fairValue and set buyer limitPrice to 0.85 * fairValue",
    ),
    (
        "Fast Liquidator",
        "SELL_BIASED",
        -1500,
        75,
        "0 units except the strongest final-value good",
        "warhorse > gems > iron > grain",
        "sell when marketPrice >= 0.90 * fairValue and set seller "
        "limitPrice to 0.88 * fairValue; buy only when marketPrice <= "
        "0.75 * fairValue and set buyer limitPrice to 0.78 * fairValue",
    ),
    (
        "Tight Market Maker",
        "TWO_SIDED",
        200,
        40,
        "1 unit per good",
        "grain > warhorse > iron > gems",
        "buy when marketPrice <= 0.99 * fairValue and set buyer limitPrice "
        "to 1.00 * fairValue; sell when marketPrice >= 1.01 * fairValue "
        "and set seller limitPrice to 1.00 * fairValue",
    ),
    (
        "Wide Market Maker",
        "TWO_SIDED",
        -200,
        45,
        "1 unit per good",
        "gems > grain > warhorse > iron",
        "buy when marketPrice <= 0.96 * fairValue and set buyer limitPrice "
        "to 0.98 * fairValue; sell when marketPrice >= 1.04 * fairValue "
        "and set seller limitPrice to 1.02 * fairValue",
    ),
    (
        "Final-Event Momentum Trader",
        "TWO_SIDED",
        600,
        30,
        "2 units in goods with positive active final effects",
        "iron > warhorse > gems > grain",
        "buy when marketPrice <= 0.98 * fairValue and set buyer limitPrice "
        "to 1.01 * fairValue; sell when marketPrice >= 1.02 * fairValue "
        "and set seller limitPrice to 0.99 * fairValue",
    ),
    (
        "Market-Effect Contrarian",
        "TWO_SIDED",
        -600,
        50,
        "1 unit per good",
        "grain > gems > iron > warhorse",
        "buy when marketPrice <= 0.92 * fairValue and set buyer limitPrice "
        "to 0.95 * fairValue; sell when marketPrice >= 1.08 * fairValue "
        "and set seller limitPrice to 1.05 * fairValue",
    ),
    (
        "Diversified Rebalancer",
        "TWO_SIDED",
        300,
        45,
        "1 unit in every good before adding a second",
        "warhorse > iron > grain > gems",
        "buy when marketPrice <= 0.97 * fairValue and set buyer limitPrice "
        "to 0.99 * fairValue; sell when marketPrice >= 1.03 * fairValue "
        "and set seller limitPrice to 1.01 * fairValue",
    ),
    (
        "Late-Game Closer",
        "TWO_SIDED",
        -300,
        25,
        "2 units early, then 0 speculative units in the final 2 rounds",
        "gems > iron > grain > warhorse",
        "buy when marketPrice <= 1.00 * fairValue and set buyer limitPrice "
        "to 1.02 * fairValue; sell when marketPrice >= 0.99 * fairValue "
        "and set seller limitPrice to 0.98 * fairValue",
    ),
)


def _strategy(index: int) -> str:
    if index < 1:
        raise ValueError("official Agent index must be positive")
    (
        profile_name,
        side_bias,
        valuation_adjustment_bps,
        cash_reserve_percent,
        inventory_target,
        good_order,
        numeric_policy,
    ) = _OFFICIAL_STRATEGY_PROFILES[
        (index - 1) % len(_OFFICIAL_STRATEGY_PROFILES)
    ]
    numeric_variant = (
        "You are an official Arena 402 market participant. Follow this "
        f"stable profile: {profile_name}; side bias {side_bias}; cash reserve "
        f"{cash_reserve_percent}% of current marked net worth; inventory "
        f"target {inventory_target}; deterministic equal-signal good order "
        f"{good_order}. Use the frozen task eventImpliedFinal value as the "
        "public anchor for each good; it contains only already revealed "
        "public final-target effects. Set this profile's private reservation "
        f"fairValue to public anchor * {10000 + valuation_adjustment_bps} / "
        "10000. This fixed adjustment represents the profile's uncertainty, "
        "inventory utility, and liquidity preference; do not change it from "
        "other Agents' messages. The market field already includes current "
        "market-target effects, so never apply those effects twice or assume "
        "an unrevealed future event. In every numeric rule below, fairValue "
        "means this adjusted private reservation fairValue. "
        f"Numeric decision policy: {numeric_policy}. "
        "When both a valid buy and sell qualify, follow the side bias first, "
        "then choose the first qualifying good in the stated good order; use "
        "the percentage gap only to break a tie on the same priority. Submit "
        "quantity 1 unless the inventory target requires less. "
        "Before ranking triggers, build the legal candidate set: eliminate "
        "every sell whose current holding is below quantity; eliminate every "
        "buy whose quantity times limitPrice exceeds current cash or breaks "
        "the cash reserve; and eliminate unavailable or disallowed goods and "
        "actions. Never select a zero-holding good for sell. If the preferred "
        "side or good is illegal, evaluate the next legal good and then the "
        "other legal side before passing. "
        "Re-evaluate every round and never reuse an expired event price. "
        "Respect the cash reserve after a buy and the inventory target before "
        "a sell. Use the current market as the quote anchor, obey quantity "
        "and limit-price constraints, and prefer executable good-faith trades. "
        "A buyer never quotes above its limitPrice; a seller never quotes "
        "below its limitPrice. When the latest quote is outside that boundary, "
        "counter exactly at the boundary rather than widening the gap. Accept "
        "immediately when the latest quote is within the boundary. Close the "
        "final negotiation turn with accept or reject. Pass only when neither "
        "numeric trigger is satisfied or constraints make all qualifying "
        "actions illegal. Never disclose credentials or private reasoning."
    )
    return render_strategy_revision(
        archetype=official_strategy_archetype(index),
        variant_instructions=numeric_variant,
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
            archetype = official_strategy_archetype(priority).value
            await connection.execute(
                """
                INSERT INTO arena402.official_agent_pool (
                    agent_id, wallet_id, priority, strategy_archetype,
                    strategy_catalog_version, enabled, disabled_at
                )
                VALUES ($1, $2, $3, $4, $5, TRUE, NULL)
                ON CONFLICT (agent_id) DO UPDATE
                SET priority = EXCLUDED.priority,
                    strategy_archetype = EXCLUDED.strategy_archetype,
                    strategy_catalog_version =
                        EXCLUDED.strategy_catalog_version,
                    enabled = TRUE,
                    disabled_at = NULL
                """,
                agent_id,
                wallet_by_agent[agent_id],
                priority,
                archetype,
                STRATEGY_CATALOG_VERSION_V1,
            )
            await connection.execute(
                """
                UPDATE public.hosted_agent_strategy_revisions
                SET archetype = $2,
                    catalog_version = $3,
                    source = 'preset',
                    policy_profile = $4::jsonb
                WHERE agent_id = $1
                  AND status = 'active'
                """,
                agent_id,
                archetype,
                STRATEGY_CATALOG_VERSION_V1,
                json.dumps(
                    default_policy_profile(archetype).model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )


async def _provision(args: argparse.Namespace) -> dict[str, object]:
    import asyncpg

    gateway_token = _load_litellm_token(args.litellm_token_file)
    control_database_url = _required_environment("ADX_HOSTED_CONTROL_DATABASE_URL")
    operator_database_url = _required_environment("ADX_OFFICIAL_BOOTSTRAP_DATABASE_URL")

    operator_connection = await asyncpg.connect(
        operator_database_url,
        command_timeout=30,
    )
    repository = PostgresHostedAgentControlRepository(control_database_url)
    secret_writer = build_production_secret_writer(control_database_url)
    try:
        await _verify_litellm_deployments(
            gateway_token,
            timeout_seconds=args.validation_timeout_seconds,
        )
        await repository.initialize()
        await initialize_secret_port(secret_writer)
        await _ensure_official_users(
            operator_connection,
            count=args.count,
        )

        registry = build_production_capability_registry(include_official=True)
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
                    provider_id="official-deepseek",
                    api_key=gateway_token,
                    idempotency_key=(
                        "official-litellm-credential-"
                        f"{index:03d}-{args.config_version}"
                    ),
                ),
            )
            agent = await agent_service.create_hosted_agent(
                owner_user_id=owner_user_id,
                request=HostedAgentCreateRequest(
                    display_name=f"Arena Official {index:02d}",
                    credential_id=credential.credential_id,
                    provider_id="official-deepseek",
                    model_id=args.model,
                    thinking_enabled=args.thinking,
                    strategy_instructions=_strategy(index),
                    idempotency_key=(
                        "official-litellm-agent-"
                        f"{index:03d}-{args.config_version}"
                    ),
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
                    raise RuntimeError(
                        "LiteLLM gateway validation did not succeed"
                    )
            if pending:
                await asyncio.sleep(1)
        if pending:
            raise RuntimeError("timed out waiting for LiteLLM gateway validation")

        if args.activate:
            await _activate_pool(
                operator_connection,
                tuple(provisioned),
                replace_enabled_pool=args.replace_enabled_pool,
            )

        return {
            "status": "ready",
            "provider": "official-deepseek",
            "model": args.model,
            "agentCount": len(provisioned),
            "gateway": "litellm",
            "configVersion": args.config_version,
            "poolActivated": args.activate,
            "replacedEnabledPool": (args.activate and args.replace_enabled_pool),
            "agentIds": [agent.agent_id for agent in provisioned],
        }
    finally:
        gateway_token = SecretStr("")
        await close_secret_port(secret_writer)
        await repository.close()
        await operator_connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Provision validated LiteLLM-backed DeepSeek Hosted Agents into "
            "the explicit Arena 402 official pool."
        )
    )
    parser.add_argument(
        "--litellm-token-file",
        type=Path,
        required=True,
        help=(
            "UTF-8 file containing only the internal LiteLLM gateway token."
        ),
    )
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--config-version", default="v1")
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
    if not _CONFIG_VERSION_PATTERN.fullmatch(args.config_version):
        raise SystemExit("--config-version must match v<positive integer>")
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
