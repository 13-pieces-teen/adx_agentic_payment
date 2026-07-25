#!/usr/bin/env python3
"""Run a local two-user, two-Hosted-Agent King's Pawnhouse demo.

The script deliberately accepts one-time invites through environment variables
so they do not have to be placed in command history. It never prints model
credentials, passwords, session cookies, CSRF tokens, or secret references.
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(slots=True)
class BrowserSession:
    base_url: str
    opener: urllib.request.OpenerDirector
    csrf_token: str
    user_id: str

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        if method not in {"GET", "HEAD"}:
            request_headers["X-CSRF-Token"] = self.csrf_token
        return _request(
            self.opener,
            method,
            f"{self.base_url}{path}",
            body=body,
            headers=request_headers,
        )


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    encoded = None
    request_headers = dict(headers or {})
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=encoded,
        headers=request_headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        safe_response = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"{method} {url} failed with HTTP {exc.code}: {safe_response}"
        ) from None
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {url} returned a non-object response")
    return value


def _register(base_url: str, invite: str, role: str) -> BrowserSession:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    suffix = secrets.token_hex(5)
    response = _request(
        opener,
        "POST",
        f"{base_url}/api/auth/register",
        body={
            "invite_code": invite,
            "username": f"hosted-{role}-{suffix}",
            "password": f"Local-{secrets.token_urlsafe(24)}",
        },
    )
    csrf = response.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise RuntimeError("registration response omitted the CSRF token")
    user = response.get("user")
    user_id = user.get("user_id") if isinstance(user, dict) else None
    if not isinstance(user_id, str) or not user_id:
        raise RuntimeError("registration response omitted the user id")
    return BrowserSession(
        base_url=base_url,
        opener=opener,
        csrf_token=csrf,
        user_id=user_id,
    )


async def _bind_local_test_wallets(
    database_url: str,
    bindings: tuple[tuple[str, str], ...],
) -> None:
    """Bind imported public wallet inventory for an explicit local test run."""

    import asyncpg

    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            for user_id, wallet_id in bindings:
                wallet = await connection.fetchrow(
                    """
                    SELECT chain_id, account_address, status
                    FROM arena402.wallet_inventory
                    WHERE wallet_id = $1
                    FOR UPDATE
                    """,
                    wallet_id,
                )
                if wallet is None or wallet["status"] != "available":
                    raise RuntimeError("local test wallet is not available")
                suffix = int(wallet_id.rsplit("-", 1)[-1])
                await connection.execute(
                    """
                    INSERT INTO arena402.user_wallets (
                        user_id, github_subject, wallet_id, chain_id,
                        account_address
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_id,
                    str(900_000_000_000_000 + suffix),
                    wallet_id,
                    wallet["chain_id"],
                    wallet["account_address"],
                )
                await connection.execute(
                    """
                    UPDATE arena402.wallet_inventory
                    SET status = 'bound'
                    WHERE wallet_id = $1 AND status = 'available'
                    """,
                    wallet_id,
                )
    finally:
        await connection.close()


async def _activate_local_test_participants(
    database_url: str,
    values: tuple[tuple[str, str, str], ...],
) -> None:
    """Attach reviewed mandates to pending local test participants."""

    import asyncpg

    connection = await asyncpg.connect(database_url, command_timeout=30)
    try:
        async with connection.transaction():
            for participant_id, user_id, mandate_id in values:
                status = await connection.execute(
                    """
                    UPDATE arena402.game_participants AS participant
                    SET payment_mandate_id = mandate.mandate_id,
                        readiness = 'ready',
                        ready_at = clock_timestamp()
                    FROM arena402.payment_mandates AS mandate
                    WHERE participant.game_participant_id = $1
                      AND participant.user_id = $2
                      AND participant.readiness = 'pending'
                      AND mandate.mandate_id = $3
                      AND mandate.user_id = participant.user_id
                      AND mandate.game_id = participant.game_id
                    """,
                    participant_id,
                    user_id,
                    mandate_id,
                )
                if status != "UPDATE 1":
                    raise RuntimeError(
                        "local test participant mandate attach failed"
                    )
    finally:
        await connection.close()


def _create_payment_mandate(
    session: BrowserSession,
    *,
    game_id: str,
    mandate_id: str,
    payee: str,
) -> None:
    now = datetime.now(timezone.utc)
    session.request(
        "POST",
        "/api/v1/me/payment-mandates",
        body={
            "mandateId": mandate_id,
            "gameId": game_id,
            "chainId": 1439,
            "tokenAddress": (
                "0x06D223D12774386A96D33863D9106A800e52BDeD"
            ),
            "maxPerPaymentAtomic": 10_000_000,
            "maxCumulativeAtomic": 50_000_000,
            "allowedPayees": [payee],
            "validFrom": (now - timedelta(seconds=5)).isoformat(),
            "expiresAt": (now + timedelta(hours=1)).isoformat(),
        },
    )


def _create_hosted_agent(
    session: BrowserSession,
    *,
    role: str,
    model_id: str,
) -> str:
    nonce = secrets.token_hex(8)
    credential = session.request(
        "POST",
        "/api/model-credentials",
        body={
            "providerId": "arena-scripted",
            "apiKey": f"local-development-placeholder-{nonce}",
        },
        headers={"Idempotency-Key": f"demo-credential-{nonce}"},
    )
    credential_id = credential.get("credentialId")
    if not isinstance(credential_id, str):
        raise RuntimeError("credential response omitted credentialId")

    agent = session.request(
        "POST",
        "/api/hosted-agents",
        body={
            "displayName": f"Demo {role.title()}",
            "credentialId": credential_id,
            "providerId": "arena-scripted",
            "modelId": model_id,
            "thinkingEnabled": False,
            "strategyInstructions": (
                f"Act as the {role} in the local deterministic Arena demo."
            ),
        },
        headers={"Idempotency-Key": f"demo-agent-{nonce}"},
    )
    agent_id = agent.get("agentId")
    if not isinstance(agent_id, str):
        raise RuntimeError("Hosted Agent response omitted agentId")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = session.request("GET", f"/api/hosted-agents/{agent_id}")
        if current.get("routeStatus") == "ready":
            return agent_id
        if current.get("provisioningStatus") == "failed":
            raise RuntimeError(f"{role} Hosted Agent validation failed")
        time.sleep(0.2)
    raise TimeoutError(f"{role} Hosted Agent did not become ready")


def _public_request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    dev_token: str | None = None,
) -> dict[str, Any]:
    headers = {}
    if dev_token is not None:
        headers["X-Arena-Dev-Token"] = dev_token
    return _request(
        urllib.request.build_opener(),
        method,
        f"{base_url}{path}",
        body=body,
        headers=headers,
    )


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
    parser.add_argument(
        "--with-settlement-intent",
        action="store_true",
        help=(
            "freeze an Injective testnet EIP-3009 SettlementIntent after "
            "acceptance; this never signs or submits a transaction"
        ),
    )
    parser.add_argument(
        "--with-local-csv-wallet-mandates",
        action="store_true",
        help=(
            "bind two already imported local test wallet IDs and create "
            "bounded PaymentMandates; requires --with-settlement-intent"
        ),
    )
    args = parser.parse_args()
    if (
        args.with_local_csv_wallet_mandates
        and not args.with_settlement_intent
    ):
        parser.error(
            "--with-local-csv-wallet-mandates requires "
            "--with-settlement-intent"
        )
    buyer_invite = os.environ.get("ARENA_BUYER_INVITE")
    seller_invite = os.environ.get("ARENA_SELLER_INVITE")
    if not buyer_invite or not seller_invite:
        parser.error(
            "set ARENA_BUYER_INVITE and ARENA_SELLER_INVITE to fresh "
            "one-time local invites"
        )

    base_url = args.base_url.rstrip("/")
    buyer_session = _register(base_url, buyer_invite, "buyer")
    seller_session = _register(base_url, seller_invite, "seller")
    buyer_agent_id = _create_hosted_agent(
        buyer_session,
        role="buyer",
        model_id="arena-buyer-v1",
    )
    seller_agent_id = _create_hosted_agent(
        seller_session,
        role="seller",
        model_id="arena-seller-v1",
    )

    game_id = f"hosted-duel-{int(time.time())}-{secrets.token_hex(3)}"
    game_path = f"/api/dev/pawnhouse/games/{game_id}"
    create_body: dict[str, Any] = {
        "gameId": game_id,
        "eventSeed": f"local-dual-hosted-{secrets.token_hex(8)}",
        "actionTimeoutMs": 120_000,
    }
    if args.with_settlement_intent:
        create_body["settlement"] = {
            "authorizationMode": "single_eip3009",
            "chainId": 1439,
            "tokenAddress": os.environ.get(
                "ARENA_SETTLEMENT_TOKEN",
                "0x06D223D12774386A96D33863D9106A800e52BDeD",
            ),
            "tokenSymbol": "mUSDC",
            "tokenDecimals": 6,
            "tokenEip712Name": "Mock USD Coin",
            "tokenEip712Version": "1",
            "requiredConfirmations": 2,
        }
    _public_request(
        base_url,
        "POST",
        "/api/dev/pawnhouse/games",
        dev_token=args.dev_token,
        body=create_body,
    )
    buyer_mandate_id: str | None = None
    seller_mandate_id: str | None = None
    database_url = ""
    if args.with_local_csv_wallet_mandates:
        database_url = os.environ.get("ARENA_TEST_DATABASE_URL", "").strip()
        buyer_wallet_id = os.environ.get(
            "ARENA_BUYER_WALLET_ID", ""
        ).strip()
        seller_wallet_id = os.environ.get(
            "ARENA_SELLER_WALLET_ID", ""
        ).strip()
        if not database_url or not buyer_wallet_id or not seller_wallet_id:
            parser.error(
                "set ARENA_TEST_DATABASE_URL, ARENA_BUYER_WALLET_ID, "
                "and ARENA_SELLER_WALLET_ID"
            )
        asyncio.run(
            _bind_local_test_wallets(
                database_url,
                (
                    (buyer_session.user_id, buyer_wallet_id),
                    (seller_session.user_id, seller_wallet_id),
                ),
            )
        )
        buyer_mandate_id = f"mandate:{game_id}:buyer"
        seller_mandate_id = f"mandate:{game_id}:seller"
    buyer_join: dict[str, Any] = {
        "agentId": buyer_agent_id,
        "portfolio": {
            "cash": "20",
            "holdings": {
                "grain": 0,
                "iron": 0,
                "warhorse": 0,
                "gems": 0,
            },
        },
    }
    seller_join: dict[str, Any] = {
        "agentId": seller_agent_id,
        "portfolio": {
            "cash": "15",
            "holdings": {
                "grain": 0,
                "iron": 1,
                "warhorse": 0,
                "gems": 0,
            },
        },
    }
    if args.with_settlement_intent:
        buyer_join["settlementAccount"] = {
            "chainId": 1439,
            "address": os.environ.get(
                "ARENA_BUYER_ACCOUNT",
                "0x07900F7f7C5E3d92BB0Eeea43981050605e1aB25",
            ),
            "custodyMode": "wallet",
        }
        seller_join["settlementAccount"] = {
            "chainId": 1439,
            "address": os.environ.get(
                "ARENA_SELLER_ACCOUNT",
                "0x93Dafa6bFa2428CA033e8d6Fe9C94e40d1AC8754",
            ),
            "custodyMode": "wallet",
        }
    buyer_joined = buyer_session.request(
        "POST",
        f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
        body=buyer_join,
    )
    seller_joined = seller_session.request(
        "POST",
        f"/api/v1/pawnhouse/games/{game_id}/hosted-participants",
        body=seller_join,
    )
    if buyer_mandate_id is not None and seller_mandate_id is not None:
        _create_payment_mandate(
            buyer_session,
            game_id=game_id,
            mandate_id=buyer_mandate_id,
            payee=os.environ["ARENA_SELLER_ACCOUNT"],
        )
        _create_payment_mandate(
            seller_session,
            game_id=game_id,
            mandate_id=seller_mandate_id,
            payee=os.environ["ARENA_BUYER_ACCOUNT"],
        )
        buyer_participant_id = buyer_joined.get("participantId")
        seller_participant_id = seller_joined.get("participantId")
        if (
            not isinstance(buyer_participant_id, str)
            or not isinstance(seller_participant_id, str)
        ):
            raise RuntimeError("participant response omitted participantId")
        asyncio.run(
            _activate_local_test_participants(
                database_url,
                (
                    (
                        buyer_participant_id,
                        buyer_session.user_id,
                        buyer_mandate_id,
                    ),
                    (
                        seller_participant_id,
                        seller_session.user_id,
                        seller_mandate_id,
                    ),
                ),
            )
        )
    _public_request(
        base_url,
        "POST",
        f"{game_path}/start",
        dev_token=args.dev_token,
    )
    queued = _public_request(
        base_url,
        "POST",
        f"{game_path}/run-hosted-market",
        dev_token=args.dev_token,
    )

    deadline = time.monotonic() + 90
    runtime: dict[str, Any] = {}
    while time.monotonic() < deadline:
        runtime = _public_request(
            base_url,
            "GET",
            f"/api/v1/pawnhouse/games/{game_id}/runtime-run",
        )
        if runtime.get("status") in {"completed", "failed"}:
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("Hosted Arena run did not reach a terminal state")

    timeline = _public_request(
        base_url,
        "GET",
        f"/api/v1/pawnhouse/games/{game_id}/timeline",
    )
    events = timeline.get("events", [])
    event_types = [
        item.get("type") for item in events if isinstance(item, dict)
    ]
    settlement_intents: list[dict[str, Any]] = []
    if args.with_settlement_intent:
        settlement_projection = _public_request(
            base_url,
            "GET",
            f"/api/v1/pawnhouse/games/{game_id}/settlement-intents",
        )
        raw_intents = settlement_projection.get("settlementIntents", [])
        settlement_intents = [
            item for item in raw_intents if isinstance(item, dict)
        ]
    summary = {
        "gameId": game_id,
        "runtimeRunId": queued.get("runtimeRunId"),
        "runtimeStatus": runtime.get("status"),
        "runtimeStage": runtime.get("stage"),
        "safeErrorCode": runtime.get("errorCode"),
        "hostedAgentCount": 2,
        "decisionCount": event_types.count("decision.applied"),
        "pairingCount": event_types.count("pairing.created"),
        "negotiationMessageCount": event_types.count(
            "negotiation.message"
        ),
        "settlementIntentCount": len(settlement_intents),
        "settlementStatus": (
            settlement_intents[0].get("status")
            if len(settlement_intents) == 1
            else None
        ),
        "settlementAmountAtomic": (
            settlement_intents[0].get("amountAtomic")
            if len(settlement_intents) == 1
            else None
        ),
        "eventTypes": event_types,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if runtime.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
