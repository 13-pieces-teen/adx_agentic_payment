"""Read-only EVM receipt verification for Arena settlement recovery."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .settlement import (
    ChainConfirmation,
    SettlementError,
    SettlementIntent,
    normalize_evm_address,
)


_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa"
    "952ba7f163c4a11628f55a4df523b3ef"
)


class ChainReadError(RuntimeError):
    pass


class EvmJsonRpcConfirmationReader:
    """Resolve one frozen payment from a transaction receipt.

    The reader has no wallet client and cannot submit a transaction. It checks
    the chain id, receipt status, canonical block hash, confirmation depth, and
    the exact ERC-20 Transfer event bound by the SettlementIntent.
    """

    def __init__(
        self,
        rpc_url: str,
        *,
        blockscout_base_url: str | None = None,
        rpc_call: Callable[[str, list[object]], object] | None = None,
        http_get: Callable[[str], object] | None = None,
    ) -> None:
        if not rpc_url:
            raise ValueError("Settlement RPC URL is required")
        self._rpc_url = rpc_url
        self._blockscout_base_url = (
            blockscout_base_url.rstrip("/")
            if blockscout_base_url
            else None
        )
        self._rpc_call_override = rpc_call
        self._http_get_override = http_get

    async def read(
        self,
        intent: SettlementIntent,
        tx_hash: str,
    ) -> ChainConfirmation | None:
        if self._rpc_call_override is not None:
            return self._read_sync(intent, tx_hash)
        return await asyncio.to_thread(self._read_sync, intent, tx_hash)

    def _read_sync(
        self,
        intent: SettlementIntent,
        tx_hash: str,
    ) -> ChainConfirmation | None:
        chain_id = _hex_int(self._rpc("eth_chainId", []))
        if chain_id != intent.chain_id:
            raise ChainReadError("settlement_rpc_chain_mismatch")
        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            if self._blockscout_base_url is not None:
                return self._read_blockscout(intent, tx_hash)
            return None
        if not isinstance(receipt, dict):
            raise ChainReadError("invalid_transaction_receipt")
        block_number = _hex_int(receipt.get("blockNumber"))
        block_hash = receipt.get("blockHash")
        status = _hex_int(receipt.get("status"))
        latest_block = _hex_int(self._rpc("eth_blockNumber", []))
        confirmation_count = max(0, latest_block - block_number + 1)

        if status == 0:
            return ChainConfirmation(
                tx_hash=tx_hash,
                chain_id=chain_id,
                token_address=intent.token_address,
                from_account=intent.buyer_account,
                to_account=intent.seller_account,
                amount_atomic=intent.amount_atomic,
                block_number=block_number,
                block_hash=str(block_hash),
                confirmation_count=confirmation_count,
                success=False,
            )
        if status != 1:
            raise ChainReadError("unknown_transaction_receipt_status")

        matches: list[tuple[str, str, int]] = []
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            raise ChainReadError("transaction_receipt_logs_missing")
        for log in logs:
            if not isinstance(log, dict):
                continue
            try:
                address = normalize_evm_address(str(log.get("address")))
            except SettlementError:
                continue
            topics = log.get("topics")
            if (
                address != intent.token_address
                or not isinstance(topics, list)
                or len(topics) < 3
                or str(topics[0]).lower() != _TRANSFER_TOPIC
            ):
                continue
            from_account = _topic_address(topics[1])
            to_account = _topic_address(topics[2])
            amount = _hex_int(log.get("data"))
            if (
                from_account == intent.buyer_account
                and to_account == intent.seller_account
                and amount == intent.amount_atomic
            ):
                matches.append((from_account, to_account, amount))
        if len(matches) != 1:
            raise ChainReadError("expected_transfer_event_not_found")

        return ChainConfirmation(
            tx_hash=tx_hash,
            chain_id=chain_id,
            token_address=intent.token_address,
            from_account=matches[0][0],
            to_account=matches[0][1],
            amount_atomic=matches[0][2],
            block_number=block_number,
            block_hash=str(block_hash),
            confirmation_count=confirmation_count,
            success=True,
        )

    def _read_blockscout(
        self,
        intent: SettlementIntent,
        tx_hash: str,
    ) -> ChainConfirmation | None:
        assert self._blockscout_base_url is not None
        transaction = self._get_json(
            f"{self._blockscout_base_url}/transactions/{tx_hash}"
        )
        if transaction is None:
            return None
        if not isinstance(transaction, dict):
            raise ChainReadError("blockscout_invalid_transaction")
        status = transaction.get("status")
        result = transaction.get("result")
        if status not in {"ok", "error"}:
            return None
        if status != "ok" or result != "success":
            raise ChainReadError("chain_transaction_reverted")
        try:
            block_number = int(transaction["block_number"])
            confirmation_count = int(transaction["confirmations"])
        except (KeyError, TypeError, ValueError):
            raise ChainReadError(
                "blockscout_invalid_transaction"
            ) from None
        transfers = self._get_json(
            f"{self._blockscout_base_url}/transactions/"
            f"{tx_hash}/token-transfers"
        )
        if not isinstance(transfers, dict):
            raise ChainReadError("blockscout_invalid_token_transfers")
        if transfers.get("next_page_params") is not None:
            # Do not accept a partial view: another matching Transfer on a
            # later page would make the payment evidence ambiguous.
            raise ChainReadError("blockscout_transfer_page_truncated")
        items = transfers.get("items")
        if not isinstance(items, list):
            raise ChainReadError("blockscout_invalid_token_transfers")
        matches: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            token = item.get("token")
            from_value = item.get("from")
            to_value = item.get("to")
            total = item.get("total")
            if not all(
                isinstance(value, dict)
                for value in (token, from_value, to_value, total)
            ):
                continue
            try:
                token_address = normalize_evm_address(
                    str(token.get("address_hash"))
                )
                from_account = normalize_evm_address(
                    str(from_value.get("hash"))
                )
                to_account = normalize_evm_address(
                    str(to_value.get("hash"))
                )
                amount = int(total.get("value"))
                decimals = int(total.get("decimals"))
            except (SettlementError, TypeError, ValueError):
                continue
            if (
                token_address == intent.token_address
                and from_account == intent.buyer_account
                and to_account == intent.seller_account
                and amount == intent.amount_atomic
                and decimals == intent.token_decimals
            ):
                matches.append(item)
        if len(matches) != 1:
            raise ChainReadError("expected_transfer_event_not_found")
        match = matches[0]
        try:
            transfer_block_number = int(match["block_number"])
            block_hash = str(match["block_hash"])
        except (KeyError, TypeError, ValueError):
            raise ChainReadError(
                "blockscout_invalid_token_transfers"
            ) from None
        if transfer_block_number != block_number:
            raise ChainReadError("blockscout_block_mismatch")
        return ChainConfirmation(
            tx_hash=tx_hash,
            chain_id=intent.chain_id,
            token_address=intent.token_address,
            from_account=intent.buyer_account,
            to_account=intent.seller_account,
            amount_atomic=intent.amount_atomic,
            block_number=block_number,
            block_hash=block_hash,
            confirmation_count=confirmation_count,
            success=True,
        )

    def _rpc(self, method: str, params: list[object]) -> object:
        if self._rpc_call_override is not None:
            return self._rpc_call_override(method, params)
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self._rpc_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ChainReadError("settlement_rpc_unavailable") from None
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            raise ChainReadError("settlement_rpc_invalid_response") from None
        if not isinstance(body, dict) or "error" in body:
            raise ChainReadError("settlement_rpc_error")
        return body.get("result")

    def _get_json(self, url: str) -> object:
        if self._http_get_override is not None:
            return self._http_get_override(url)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise ChainReadError("blockscout_unavailable") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ChainReadError("blockscout_unavailable") from None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            raise ChainReadError("blockscout_invalid_response") from None


def _hex_int(value: object) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ChainReadError("invalid_rpc_quantity")
    try:
        parsed = int(value, 16)
    except ValueError:
        raise ChainReadError("invalid_rpc_quantity") from None
    if parsed < 0:
        raise ChainReadError("invalid_rpc_quantity")
    return parsed


def _topic_address(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ChainReadError("invalid_transfer_topic")
    hex_value = value[2:]
    if len(hex_value) != 64:
        raise ChainReadError("invalid_transfer_topic")
    return normalize_evm_address("0x" + hex_value[-40:])


__all__ = [
    "ChainReadError",
    "EvmJsonRpcConfirmationReader",
]
