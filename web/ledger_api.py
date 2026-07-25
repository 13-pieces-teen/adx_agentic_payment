"""Public read-only trade-ledger projection for Arena 402."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import StringConstraints


_FilterId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
_GoodId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
    ),
]


@dataclass(frozen=True, slots=True)
class LedgerMetadata:
    chain_id: int
    explorer_tx_url_template: str

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("ledger chain id must be positive")
        parsed = urlsplit(self.explorer_tx_url_template.replace("{txHash}", "0x0"))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or self.explorer_tx_url_template.count("{txHash}") != 1
        ):
            raise ValueError(
                "ledger explorer template must be an absolute HTTP URL "
                "containing one {txHash} placeholder"
            )


def load_ledger_metadata_from_env() -> LedgerMetadata:
    raw_chain_id = os.getenv("ADX_CURRENT_GAME_CHAIN_ID", "1439").strip()
    try:
        chain_id = int(raw_chain_id)
    except ValueError as exc:
        raise ValueError("ADX_CURRENT_GAME_CHAIN_ID must be an integer") from exc
    explorer_template = os.getenv(
        "ADX_ARENA_EXPLORER_TX_URL_TEMPLATE",
        "",
    ).strip()
    if not explorer_template:
        explorer_base = (
            os.getenv("ADX_WALLET_EXPLORER_URL", "").strip()
            or "https://testnet.blockscout.injective.network"
        )
        explorer_template = f"{explorer_base.rstrip('/')}/tx/{{txHash}}"
    metadata = LedgerMetadata(
        chain_id=chain_id,
        explorer_tx_url_template=explorer_template,
    )
    if os.getenv(
        "ADX_ENV", "development"
    ).strip().lower() == "production" and not metadata.explorer_tx_url_template.lower().startswith(
        "https://"
    ):
        raise ValueError(
            "ADX_ARENA_EXPLORER_TX_URL_TEMPLATE must use HTTPS in production"
        )
    return metadata


def encode_ledger_cursor(*, created_at: str, trade_id: str) -> str:
    try:
        value = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ValueError("invalid ledger cursor timestamp") from exc
    if value.tzinfo is None:
        raise ValueError("ledger cursor timestamp must include a timezone")
    normalized = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    payload = json.dumps(
        {
            "v": 1,
            "createdAt": normalized,
            "tradeId": trade_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_ledger_cursor(value: str) -> tuple[datetime, str]:
    if not value or len(value) > 1024:
        raise ValueError("invalid ledger cursor")
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
    ) as exc:
        raise ValueError("invalid ledger cursor") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "createdAt", "tradeId"}
        or payload.get("v") != 1
        or not isinstance(payload.get("createdAt"), str)
        or not isinstance(payload.get("tradeId"), str)
        or not 1 <= len(payload["tradeId"]) <= 512
    ):
        raise ValueError("invalid ledger cursor")
    try:
        created_at = datetime.fromisoformat(payload["createdAt"])
    except ValueError as exc:
        raise ValueError("invalid ledger cursor") from exc
    if created_at.tzinfo is None:
        raise ValueError("invalid ledger cursor")
    return created_at, payload["tradeId"]


def create_ledger_router(
    *,
    repository: Any,
    metadata: LedgerMetadata,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ledger", tags=["ledger"])

    @router.get("/trades")
    async def trades(
        response: Response,
        game_id: Annotated[_FilterId | None, Query(alias="gameId")] = None,
        agent_id: Annotated[_FilterId | None, Query(alias="agentId")] = None,
        good_id: Annotated[_GoodId | None, Query(alias="goodId")] = None,
        after: Annotated[
            str | None,
            Query(min_length=1, max_length=1024),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, object]:
        after_created_at = None
        after_trade_id = None
        if after is not None:
            try:
                after_created_at, after_trade_id = decode_ledger_cursor(after)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_ledger_cursor"},
                ) from None
        values = await repository.ledger_trades(
            game_id=game_id,
            agent_id=agent_id,
            good_id=good_id,
            after_created_at=after_created_at,
            after_trade_id=after_trade_id,
            limit=limit + 1,
        )
        has_more = len(values) > limit
        page = values[:limit]
        next_after = None
        if has_more and page:
            last = page[-1]
            next_after = encode_ledger_cursor(
                created_at=str(last["createdAt"]),
                trade_id=str(last["tradeId"]),
            )
        response.headers["Cache-Control"] = "public, max-age=5"
        return {
            "trades": page,
            "nextAfter": next_after,
            "chainId": metadata.chain_id,
            "explorerTxUrlTemplate": (metadata.explorer_tx_url_template),
            "schemaVersion": "arena402.trade-ledger-list.v1",
        }

    @router.get("/stats")
    async def stats(response: Response) -> dict[str, object]:
        values = await repository.ledger_stats()
        response.headers["Cache-Control"] = "public, max-age=5"
        return {
            **values,
            "chainId": metadata.chain_id,
            "explorerTxUrlTemplate": (metadata.explorer_tx_url_template),
            "schemaVersion": "arena402.trade-ledger-stats.v1",
        }

    return router


__all__ = [
    "LedgerMetadata",
    "create_ledger_router",
    "decode_ledger_cursor",
    "encode_ledger_cursor",
    "load_ledger_metadata_from_env",
]
