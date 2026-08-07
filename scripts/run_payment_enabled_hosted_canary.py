#!/usr/bin/env python3
"""Run an isolated 1-player + 9-official Hosted Agent payment canary.

The runner creates no signer and never reads an Agent wallet private key. It
freezes real mUSDC EIP-3009 SettlementIntents, keeps orchestration alive while
the separately pinned Settlement Worker submits them, and applies inventory
only after the Arena confirmation reader validates the Injective EVM receipt.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
from pydantic import SecretStr

from arena_core import PostgresArenaCoreRepository
from arena_core.hashing import sha256_identifier, sha256_text_identifier
from arena_game import (
    EvmJsonRpcConfirmationReader,
    Portfolio,
    PostgresPawnhouseRepository,
    SettlementConfig,
)
from arena_game.hosted_coordinator import PawnhouseAgentRuntimeCoordinator
from arena_game.event_deck import (
    build_event_schedule,
)
from arena_game.orchestrator import PawnhouseGameOrchestrator
from arena_game.settlement_worker import SettlementRecoveryWorker
from hosted_agent_control_plane import (
    CredentialIngressRequest,
    CredentialIngressService,
    HostedAgentCreateRequest,
    HostedAgentService,
    HostedAgentUpdateRequest,
    PostgresHostedAgentControlRepository,
)
from hosted_agent_runtime.official_market_strategy import (
    EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2,
    official_market_strategy_v2,
)
from hosted_agent_runtime.production_providers import (
    build_production_capability_registry,
)
from hosted_agent_runtime.production_secrets import (
    build_production_secret_writer,
    close_secret_port,
    initialize_secret_port,
)
from scripts.payment_canary_config import (
    CanaryAssetConfig,
    canary_mandate_limits,
    canary_summary_is_accepted,
    phase_d_portfolio_for_seat,
    resolve_canary_asset_config,
    resolve_canary_event_deck_id,
    resolve_canary_event_seed,
    resolve_canary_game_config,
    resolve_canary_official_strategy_profile,
    resolve_canary_player_config,
    resolve_canary_settlement_mode,
)


PLAYER_USER_ID = "canary:deepseek:player"
PLAYER_GITHUB_SUBJECT = "990000000000009"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_provider_key(path: Path) -> SecretStr:
    raw = path.read_bytes()
    if not raw or len(raw) > 16_384 or b"\x00" in raw:
        raise RuntimeError("DeepSeek key file is invalid")
    value = raw.decode("utf-8").strip()
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("DeepSeek key file must contain exactly one key")
    return SecretStr(value)


def _phase_d_portfolio(seat: int) -> Portfolio:
    cash_atomic, holdings = phase_d_portfolio_for_seat(seat)
    return Portfolio.initial(
        cash_atomic=cash_atomic,
        holdings=holdings,
    )


async def _create_player_agent(
    *,
    admin: asyncpg.Connection,
    control_database_url: str,
    key_file: Path,
) -> str:
    await admin.execute(
        """
        INSERT INTO public.connector_users (
            user_id, username, password_hash, temporary,
            identity_provider, provider_subject
        )
        VALUES (
            $1, 'canary-deepseek-player', NULL, TRUE, 'password', NULL
        )
        ON CONFLICT (user_id) DO NOTHING
        """,
        PLAYER_USER_ID,
    )
    repository = PostgresHostedAgentControlRepository(control_database_url)
    writer = build_production_secret_writer(control_database_url)
    key = _read_provider_key(key_file)
    try:
        await repository.initialize()
        await initialize_secret_port(writer)
        credential_service = CredentialIngressService(
            repository,
            secret_writer=writer,
            fingerprint_pepper=b"r" * 32,
            fingerprint_pepper_version=1,
        )
        agent_service = HostedAgentService(
            repository,
            capabilities=build_production_capability_registry(
                include_official=True
            ),
            hosted_agents_enabled=True,
        )
        credential = await credential_service.create_credential(
            owner_user_id=PLAYER_USER_ID,
            request=CredentialIngressRequest(
                provider_id="deepseek",
                api_key=key,
                idempotency_key="payment-canary-player-credential-v1",
            ),
        )
        agent = await agent_service.create_hosted_agent(
            owner_user_id=PLAYER_USER_ID,
            request=HostedAgentCreateRequest(
                display_name="Payment Canary DeepSeek Player",
                credential_id=credential.credential_id,
                provider_id="deepseek",
                model_id="deepseek-v4-flash",
                thinking_enabled=False,
                strategy_instructions=(
                    "Act as an active balanced trader. Compare each legal "
                    "price with the frozen event-implied final value, prefer "
                    "positive expected value, negotiate within the hard "
                    "limits, preserve useful cash, and pass only when no "
                    "legal positive-edge action exists."
                ),
                idempotency_key="payment-canary-player-agent-v1",
            ),
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = await agent_service.get_hosted_agent(
                owner_user_id=PLAYER_USER_ID,
                agent_id=agent.agent_id,
            )
            if (
                current.route_status.value == "ready"
                and current.provisioning_status.value == "ready"
            ):
                return agent.agent_id
            if current.provisioning_status.value in {"degraded", "disabled"}:
                raise RuntimeError("direct player Agent validation failed")
            await asyncio.sleep(0.5)
        raise TimeoutError("direct player Agent validation timed out")
    finally:
        key = SecretStr("")
        await close_secret_port(writer)
        await repository.close()


async def _prepare_official_strategy_treatment(
    *,
    control_database_url: str,
    officials: list[asyncpg.Record],
    profile: str,
) -> None:
    if profile == "existing":
        return
    if profile not in {"baseline_v4", "liquidity_v2"}:
        raise RuntimeError("unsupported official strategy treatment")

    repository = PostgresHostedAgentControlRepository(control_database_url)
    try:
        await repository.initialize()
        service = HostedAgentService(
            repository,
            capabilities=build_production_capability_registry(
                include_official=True
            ),
            hosted_agents_enabled=True,
        )
        pending: dict[str, str] = {}
        for row in officials:
            owner_user_id = str(row["owner_user_id"])
            agent_id = str(row["agent_id"])
            current = await service.get_hosted_agent(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
            )
            if profile == "liquidity_v2":
                strategy_instructions = official_market_strategy_v2(
                    int(row["priority"])
                ).instructions
                idempotency_prefix = "market-quality-liquidity-v2"
            else:
                from scripts.bootstrap_official_agent_pool import (
                    _strategy as baseline_strategy_v4,
                )

                strategy_instructions = baseline_strategy_v4(
                    int(row["priority"])
                )
                idempotency_prefix = (
                    "market-quality-baseline-v4-restore"
                )
            await service.update_hosted_agent(
                owner_user_id=owner_user_id,
                agent_id=agent_id,
                request=HostedAgentUpdateRequest(
                    provider_id=current.provider_id,
                    model_id=current.model_id,
                    thinking_enabled=current.thinking_enabled,
                    strategy_instructions=strategy_instructions,
                    idempotency_key=(
                        f"{idempotency_prefix}-{agent_id}"
                    ),
                ),
            )
            pending[agent_id] = owner_user_id

        deadline = time.monotonic() + 300
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
                elif current.provisioning_status.value in {
                    "degraded",
                    "disabled",
                }:
                    raise RuntimeError(
                        "official strategy treatment validation failed"
                    )
            if pending:
                await asyncio.sleep(0.5)
        if pending:
            raise TimeoutError(
                "official strategy treatment validation timed out"
            )
    finally:
        await repository.close()


async def _ensure_player_payment_authority(
    admin: asyncpg.Connection,
    *,
    game_id: str,
    user_id: str,
    agent_id: str,
    github_subject: str | None,
    chain_id: int,
    token_address: str,
    round_count: int,
) -> tuple[str, str, str]:
    mandate_id = f"canary-mandate:{game_id}"
    join_authorization_id = f"canary-ja:{game_id}"
    async with admin.transaction():
        wallet = await admin.fetchrow(
            """
            SELECT wallet.wallet_id, wallet.chain_id, wallet.account_address
            FROM arena402.user_wallets AS bound
            JOIN arena402.wallet_inventory AS wallet
              ON wallet.wallet_id = bound.wallet_id
            WHERE bound.user_id = $1
            FOR SHARE OF bound, wallet
            """,
            user_id,
        )
        if wallet is None:
            wallet = await admin.fetchrow(
                """
                SELECT wallet_id, chain_id, account_address
                FROM arena402.wallet_inventory
                WHERE status = 'available'
                ORDER BY wallet_id DESC
                LIMIT 1
                FOR UPDATE
                """
            )
            if wallet is None:
                raise RuntimeError("no wallet remains for the direct player")
            await admin.execute(
                """
                INSERT INTO arena402.user_wallets (
                    user_id, github_subject, wallet_id, chain_id,
                    account_address
                )
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_id,
                github_subject,
                wallet["wallet_id"],
                wallet["chain_id"],
                wallet["account_address"],
            )
            updated = await admin.execute(
                """
                UPDATE arena402.wallet_inventory
                SET status = 'bound'
                WHERE wallet_id = $1 AND status = 'available'
                """,
                wallet["wallet_id"],
            )
            if updated != "UPDATE 1":
                raise RuntimeError("direct player wallet binding raced")

        existing = await admin.fetchrow(
            """
            SELECT mandate_id, join_authorization_id
            FROM arena402.payment_mandates
            WHERE user_id = $1 AND game_id = $2 AND revoked_at IS NULL
            """,
            user_id,
            game_id,
        )
        if existing is not None:
            return (
                str(existing["mandate_id"]),
                str(existing["join_authorization_id"]),
                str(wallet["account_address"]),
            )

        expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
        (
            max_per_payment_atomic,
            max_cumulative_atomic,
        ) = canary_mandate_limits(round_count)
        await admin.execute(
            """
            INSERT INTO arena402.join_authorizations (
                join_authorization_id, user_id, game_id, agent_id,
                status, key_digest, request_digest, expires_at
            )
            VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7)
            """,
            join_authorization_id,
            user_id,
            game_id,
            agent_id,
            sha256_text_identifier(f"payment-canary:{game_id}:{agent_id}"),
            sha256_identifier(
                {
                    "gameId": game_id,
                    "agentId": agent_id,
                    "walletId": str(wallet["wallet_id"]),
                    "chainId": chain_id,
                    "tokenAddress": token_address,
                }
            ),
            expires_at,
        )
        await admin.execute(
            """
            INSERT INTO arena402.payment_mandates (
                mandate_id, user_id, wallet_id, game_id, chain_id,
                token_address, max_per_payment_atomic,
                max_cumulative_atomic, allowed_payees, valid_from,
                expires_at, join_authorization_id, allowed_payee_rule
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                ARRAY[]::text[], clock_timestamp() - interval '5 seconds',
                $9, $10, 'same_game_settlement_account'
            )
            """,
            mandate_id,
            user_id,
            wallet["wallet_id"],
            game_id,
            chain_id,
            token_address,
            max_per_payment_atomic,
            max_cumulative_atomic,
            expires_at,
            join_authorization_id,
        )
    return mandate_id, join_authorization_id, str(wallet["account_address"])


async def _require_ready_connector_player(
    admin: asyncpg.Connection,
    *,
    user_id: str,
    agent_id: str,
) -> None:
    ready = await admin.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM public.arena_agents AS agent
            JOIN public.arena_runtime_bindings AS binding
              ON binding.agent_id = agent.agent_id
            WHERE agent.agent_id = $1
              AND agent.owner_user_id = $2
              AND agent.status = 'active'
              AND binding.runtime_kind = 'connector'
              AND binding.route_status = 'ready'
              AND binding.disabled_at IS NULL
        )
        """,
        agent_id,
        user_id,
    )
    if not ready:
        raise RuntimeError("external Connector player is not ready")


async def _safe_progress(
    admin: asyncpg.Connection,
    *,
    game_id: str,
) -> dict[str, object]:
    row = await admin.fetchrow(
        """
        SELECT phase, current_round
        FROM arena402.games
        WHERE game_id = $1
        """,
        game_id,
    )
    statuses = await admin.fetch(
        """
        SELECT status, count(*)::integer AS count
        FROM arena402.settlement_intents
        WHERE game_id = $1
        GROUP BY status
        ORDER BY status
        """,
        game_id,
    )
    return {
        "phase": str(row["phase"]) if row else None,
        "currentRound": int(row["current_round"]) if row else None,
        "settlementIntents": [dict(value) for value in statuses],
    }


async def _wait_for_game_coin_ready(
    *,
    pawnhouse: PostgresPawnhouseRepository,
    admin: asyncpg.Connection,
    game_id: str,
    timeout_seconds: int = 600,
) -> None:
    """Wait for chain-confirmed whitelist/mint, then activate Current Game seats."""

    deadline = time.monotonic() + timeout_seconds
    next_progress = 0.0
    while time.monotonic() < deadline:
        await pawnhouse.activate_confirmed_game_coin_provisions()
        row = await admin.fetchrow(
            """
            SELECT
                count(*) FILTER (
                    WHERE participant.readiness = 'ready'
                )::integer AS ready_count,
                count(*)::integer AS participant_count,
                count(*) FILTER (
                    WHERE provision.status = 'failed'
                )::integer AS failed_count
            FROM arena402.game_participants AS participant
            JOIN arena402.game_coin_provisions AS provision
              ON provision.game_participant_id =
                 participant.game_participant_id
            WHERE participant.game_id = $1
            """,
            game_id,
        )
        if int(row["failed_count"]) > 0:
            raise RuntimeError("game coin provisioning failed")
        if (
            int(row["participant_count"]) == 10
            and int(row["ready_count"]) == 10
        ):
            return
        now = time.monotonic()
        if now >= next_progress:
            statuses = await admin.fetch(
                """
                SELECT status, count(*)::integer AS count
                FROM arena402.game_coin_provisions
                WHERE game_id = $1
                GROUP BY status
                ORDER BY status
                """,
                game_id,
            )
            print(
                json.dumps(
                    {
                        "phase": "game_coin_provisioning",
                        "readyCount": int(row["ready_count"]),
                        "participantCount": int(row["participant_count"]),
                        "provisions": [dict(value) for value in statuses],
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            next_progress = now + 10
        await asyncio.sleep(0.5)
    raise TimeoutError("game coin provisioning did not become ready")


async def _summary(
    admin: asyncpg.Connection,
    *,
    game_id: str,
    player_agent_id: str,
    settlement_mode: str,
    asset: CanaryAssetConfig,
    official_strategy_profile: str,
) -> dict[str, object]:
    game = await admin.fetchrow(
        """
        SELECT
            phase,
            round_count,
            current_round,
            market_protocol,
            config_snapshot ->> 'eventDeckId' AS event_deck_id
        FROM arena402.games
        WHERE game_id = $1
        """,
        game_id,
    )
    settlement_rows = await admin.fetch(
        """
        SELECT
            intent.settlement_intent_id, intent.status, submission.tx_hash,
            intent.buyer_account, intent.seller_account,
            intent.amount_atomic::text, confirmation.confirmation_count
        FROM arena402.settlement_intents AS intent
        LEFT JOIN arena402.settlement_submissions AS submission
          ON submission.settlement_intent_id =
             intent.settlement_intent_id
        LEFT JOIN arena402.settlement_confirmations AS confirmation
          ON confirmation.settlement_intent_id =
             intent.settlement_intent_id
        WHERE intent.game_id = $1
        ORDER BY intent.created_at, intent.settlement_intent_id
        """,
        game_id,
    )
    learning_rows = await admin.fetch(
        """
        SELECT status, count(*)::integer AS count
        FROM public.hosted_agent_learning_jobs
        WHERE game_id = $1
        GROUP BY status
        ORDER BY status
        """,
        game_id,
    )
    rankings = await admin.fetch(
        """
        SELECT
            ranking.rank, ranking.net_worth_atomic::text,
            participant.agent_id, participant.runtime_kind,
            frozen.config_snapshot ->> 'strategy_archetype'
                AS strategy_archetype
        FROM arena402.rankings AS ranking
        JOIN arena402.game_participants AS participant
          ON participant.game_participant_id =
             ranking.game_participant_id
        JOIN public.game_agents AS frozen
          ON frozen.game_agent_id = participant.game_participant_id
        WHERE ranking.game_id = $1
        ORDER BY ranking.rank
        """,
        game_id,
    )
    player_revision = await admin.fetchrow(
        """
        SELECT revision_no, archetype, source
        FROM public.hosted_agent_strategy_revisions
        WHERE agent_id = $1 AND status = 'active'
        """,
        player_agent_id,
    )
    activity_counts = await admin.fetchrow(
        """
        SELECT
            (
                SELECT count(*)
                FROM public.arena_agent_tasks
                WHERE game_id = $1
            )::integer AS agent_tasks,
            (
                SELECT count(*)
                FROM public.arena_agent_tasks
                WHERE game_id = $1
                  AND status = 'completed'
            )::integer AS completed_agent_tasks,
            (
                SELECT count(*)
                FROM arena402.market_deals
                WHERE game_id = $1
            )::integer AS market_deals,
            (
                SELECT count(*)
                FROM arena402.settlement_submissions AS submission
                JOIN arena402.settlement_intents AS intent
                  ON intent.settlement_intent_id =
                     submission.settlement_intent_id
                WHERE intent.game_id = $1
            )::integer AS chain_submissions,
            (
                SELECT count(*)
                FROM arena402.inventory_commits AS inventory_commit
                JOIN arena402.settlement_intents AS intent
                  ON intent.settlement_intent_id =
                     inventory_commit.settlement_intent_id
                WHERE intent.game_id = $1
            )::integer AS inventory_commits
        """,
        game_id,
    )
    runtime_counts: dict[str, int] = {}
    for row in rankings:
        runtime_kind = str(row["runtime_kind"])
        runtime_counts[runtime_kind] = runtime_counts.get(runtime_kind, 0) + 1
    return {
        "evidenceClass": (
            "isolated_real_model_payment_enabled_testnet"
            if settlement_mode == "testnet_eip3009"
            else "isolated_real_model_mixed_a2a_no_chain"
        ),
        "gameId": game_id,
        "settlementMode": settlement_mode,
        "settlementAsset": asset.profile,
        "settlementToken": {
            "chainId": asset.chain_id,
            "address": asset.token_address,
            "symbol": asset.token_symbol,
            "decimals": asset.token_decimals,
        },
        "phase": str(game["phase"]) if game else None,
        "roundCount": int(game["round_count"]) if game else None,
        "currentRound": int(game["current_round"]) if game else None,
        "marketProtocol": str(game["market_protocol"]) if game else None,
        "eventDeckId": str(game["event_deck_id"]) if game else None,
        "officialStrategyProfile": official_strategy_profile,
        "officialStrategyRelease": (
            EXPERIMENTAL_OFFICIAL_STRATEGY_RELEASE_V2
            if official_strategy_profile == "liquidity_v2"
            else (
                "pydantic-agent-v4"
                if official_strategy_profile == "baseline_v4"
                else None
            )
        ),
        "participantCount": len(rankings),
        "runtimeCounts": runtime_counts,
        "agentTaskCount": int(activity_counts["agent_tasks"]),
        "completedAgentTaskCount": int(
            activity_counts["completed_agent_tasks"]
        ),
        "marketDealCount": int(activity_counts["market_deals"]),
        "chainSubmissionCount": int(
            activity_counts["chain_submissions"]
        ),
        "inventoryCommitCount": int(
            activity_counts["inventory_commits"]
        ),
        "settlementIntents": [dict(row) for row in settlement_rows],
        "settledTradeCount": sum(
            1
            for row in settlement_rows
            if row["status"] == "inventory_committed"
        ),
        "learningJobs": [dict(row) for row in learning_rows],
        "playerActiveStrategy": (
            dict(player_revision) if player_revision else None
        ),
        "rankings": [dict(row) for row in rankings],
    }


async def main() -> int:
    admin_url = _required("CANARY_ADMIN_DATABASE_URL")
    control_url = _required("CANARY_CONTROL_DATABASE_URL")
    arena_url = _required("CANARY_ARENA_DATABASE_URL")
    game_id = _required("CANARY_GAME_ID")
    timeout_seconds = int(os.getenv("CANARY_TIMEOUT_SECONDS", "1800"))
    market_protocol, round_count = resolve_canary_game_config()
    event_deck_id = resolve_canary_event_deck_id()
    official_strategy_profile = (
        resolve_canary_official_strategy_profile()
    )
    player_config = resolve_canary_player_config()
    settlement_mode = resolve_canary_settlement_mode()
    asset = resolve_canary_asset_config()
    rpc_url = (
        _required("CANARY_RPC_URL")
        if settlement_mode == "testnet_eip3009"
        else None
    )
    blockscout_url = (
        _required("CANARY_BLOCKSCOUT_URL")
        if settlement_mode == "testnet_eip3009"
        else None
    )
    key_file = (
        Path(_required("CANARY_DEEPSEEK_KEY_FILE"))
        if player_config.runtime_kind == "hosted"
        else None
    )

    admin = await asyncpg.connect(admin_url, command_timeout=120)
    pawnhouse = PostgresPawnhouseRepository(arena_url)
    arena_core = PostgresArenaCoreRepository(arena_url)
    coordinator = PawnhouseAgentRuntimeCoordinator(
        pawnhouse=pawnhouse,
        arena_core=arena_core,
        worker_id="payment-canary-coordinator",
        lease_seconds=600,
    )
    recovery = (
        SettlementRecoveryWorker(
            repository=pawnhouse,
            confirmation_reader=EvmJsonRpcConfirmationReader(
                rpc_url,
                blockscout_base_url=blockscout_url,
            ),
        )
        if rpc_url is not None and blockscout_url is not None
        else None
    )
    try:
        if player_config.runtime_kind == "hosted":
            assert key_file is not None
            player_user_id = PLAYER_USER_ID
            player_agent_id = await _create_player_agent(
                admin=admin,
                control_database_url=control_url,
                key_file=key_file,
            )
            player_github_subject: str | None = PLAYER_GITHUB_SUBJECT
            expected_runtime_counts = {"hosted": 10}
        else:
            assert player_config.user_id is not None
            assert player_config.agent_id is not None
            player_user_id = player_config.user_id
            player_agent_id = player_config.agent_id
            player_github_subject = None
            expected_runtime_counts = {"connector": 1, "hosted": 9}
            await _require_ready_connector_player(
                admin,
                user_id=player_user_id,
                agent_id=player_agent_id,
            )
        officials = await admin.fetch(
            """
            SELECT agent.owner_user_id, pool.agent_id, pool.priority
            FROM arena402.official_agent_pool AS pool
            JOIN public.arena_agents AS agent
              ON agent.agent_id = pool.agent_id
            WHERE pool.enabled
            ORDER BY pool.priority, pool.agent_id
            LIMIT 9
            """
        )
        if len(officials) != 9:
            raise RuntimeError("nine ready official Agents are required")
        await _prepare_official_strategy_treatment(
            control_database_url=control_url,
            officials=list(officials),
            profile=official_strategy_profile,
        )

        await pawnhouse.initialize()
        await coordinator.initialize()
        phase = await admin.fetchval(
            "SELECT phase FROM arena402.games WHERE game_id = $1",
            game_id,
        )
        if phase is None:
            event_seed = resolve_canary_event_seed(game_id)
            events = build_event_schedule(
                    round_count=round_count,
                    seed=event_seed,
                    deck_id=event_deck_id,
                    mode="seeded_shuffle",
                )
            settlement_config = (
                SettlementConfig(authorization_mode="none")
                if settlement_mode == "disabled"
                else SettlementConfig(
                        authorization_mode="single_eip3009",
                        chain_id=asset.chain_id,
                        token_address=asset.token_address,
                        token_symbol=asset.token_symbol,
                        token_decimals=asset.token_decimals,
                        token_eip712_name=asset.token_eip712_name,
                        token_eip712_version=asset.token_eip712_version,
                        required_confirmations=2,
                    )
            )
            if (
                settlement_mode == "testnet_eip3009"
                and asset.profile == "arena402_g"
            ):
                current = await pawnhouse.ensure_current_game(
                    game_id=game_id,
                    events=events,
                    event_seed=event_seed,
                    event_deck_id=event_deck_id,
                    event_mode="seeded_shuffle",
                    action_timeout_ms=180_000,
                    max_negotiation_turns=3,
                    start_threshold=10,
                    max_participants=10,
                    official_fill_after_seconds=300,
                    market_protocol=market_protocol,
                    settlement_config=settlement_config,
                )
                if current["gameId"] != game_id:
                    raise RuntimeError(
                        "another nonterminal Current Game blocks the canary"
                    )
            else:
                await pawnhouse.create_game(
                    game_id=game_id,
                    events=events,
                    event_seed=event_seed,
                    event_deck_id=event_deck_id,
                    event_mode="seeded_shuffle",
                    action_timeout_ms=180_000,
                    max_negotiation_turns=3,
                    min_participants=10,
                    max_participants=10,
                    portfolio_mode="manual",
                    market_protocol=market_protocol,
                    settlement_config=settlement_config,
                )
            mandate_id: str | None = None
            join_authorization_id: str | None = None
            if settlement_mode == "testnet_eip3009":
                (
                    mandate_id,
                    join_authorization_id,
                    _player_account,
                ) = await _ensure_player_payment_authority(
                    admin,
                    game_id=game_id,
                    user_id=player_user_id,
                    agent_id=player_agent_id,
                    github_subject=player_github_subject,
                    chain_id=asset.chain_id,
                    token_address=asset.token_address,
                    round_count=round_count,
                )
            if player_config.runtime_kind == "hosted":
                await pawnhouse.add_hosted_participant(
                    game_id=game_id,
                    user_id=player_user_id,
                    agent_id=player_agent_id,
                    portfolio=_phase_d_portfolio(0),
                    payment_mandate_id=mandate_id,
                    join_authorization_id=join_authorization_id,
                )
            else:
                await pawnhouse.add_connector_participant(
                    game_id=game_id,
                    user_id=player_user_id,
                    agent_id=player_agent_id,
                    portfolio=_phase_d_portfolio(0),
                    payment_mandate_id=mandate_id,
                    join_authorization_id=join_authorization_id,
                )
            for seat, official in enumerate(officials, start=1):
                await pawnhouse.add_hosted_participant(
                    game_id=game_id,
                    user_id=str(official["owner_user_id"]),
                    agent_id=str(official["agent_id"]),
                    portfolio=_phase_d_portfolio(seat),
                    official_pool_join=True,
                )
            phase = await admin.fetchval(
                "SELECT phase FROM arena402.games WHERE game_id = $1",
                game_id,
            )
        if (
            settlement_mode == "testnet_eip3009"
            and asset.profile == "arena402_g"
            and phase == "portfolio_setup"
        ):
            await _wait_for_game_coin_ready(
                pawnhouse=pawnhouse,
                admin=admin,
                game_id=game_id,
            )
            phase = await admin.fetchval(
                "SELECT phase FROM arena402.games WHERE game_id = $1",
                game_id,
            )
        if phase == "portfolio_setup":
            await pawnhouse.start_game(game_id=game_id)
        elif phase not in {"running", "completed"}:
            raise RuntimeError(f"payment canary game is not runnable: {phase}")

        orchestrator = PawnhouseGameOrchestrator(repository=pawnhouse)
        deadline = time.monotonic() + timeout_seconds
        next_progress = 0.0
        while time.monotonic() < deadline:
            phase = await admin.fetchval(
                "SELECT phase FROM arena402.games WHERE game_id = $1",
                game_id,
            )
            if phase in {"completed", "cancelled"}:
                break
            recovered = (
                await recovery.run_once()
                if recovery is not None
                else False
            )
            orchestrated = await orchestrator.run_once()
            coordinated = await coordinator.run_once()
            now = time.monotonic()
            if now >= next_progress:
                print(
                    json.dumps(
                        await _safe_progress(admin, game_id=game_id),
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                next_progress = now + 15
            if not recovered and not orchestrated and not coordinated:
                await arena_core.finalize_expired(
                    server_clock=lambda: datetime.now(timezone.utc),
                    limit=100,
                )
                await asyncio.sleep(0.2)
        else:
            raise TimeoutError("payment canary did not become terminal")

        learning_deadline = time.monotonic() + 360
        while time.monotonic() < learning_deadline:
            pending_learning = int(
                await admin.fetchval(
                    """
                    SELECT count(*)
                    FROM public.hosted_agent_learning_jobs
                    WHERE game_id = $1 AND status IN ('pending', 'leased')
                    """,
                    game_id,
                )
            )
            if pending_learning == 0:
                break
            await asyncio.sleep(0.5)

        summary = await _summary(
            admin,
            game_id=game_id,
            player_agent_id=player_agent_id,
            settlement_mode=settlement_mode,
            asset=asset,
            official_strategy_profile=official_strategy_profile,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        accepted = canary_summary_is_accepted(
            summary=summary,
            expected_runtime_counts=expected_runtime_counts,
            round_count=round_count,
            market_protocol=market_protocol,
            settlement_mode=settlement_mode,
        )
        return 0 if accepted else 2
    finally:
        await coordinator.close()
        await pawnhouse.close()
        await admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
