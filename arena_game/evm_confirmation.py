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

    async def find_transaction_for_authorization(
        self,
        intent: Any,
        *,
        lookback_blocks: int = 4_096,
    ) -> str | None:
        """Read-only recovery of a relay result by its frozen authorization nonce.

        EIP-3009 binds the nonce to the Arena intent hash.  When a relay times
        out after broadcasting, scan only matching token Transfers and inspect
        their calldata for that nonce; never sign or re-submit a payment.
        """

        if not 1 <= lookback_blocks <= 20_000:
            raise ValueError("invalid_authorization_recovery_lookback")
        if self._rpc_call_override is not None:
            return self._find_transaction_for_authorization_sync(
                intent, lookback_blocks
            )
        return await asyncio.to_thread(
            self._find_transaction_for_authorization_sync,
            intent,
            lookback_blocks,
        )

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
        facilitator_address = _transaction_sender(receipt)
        if facilitator_address is None:
            transaction = self._rpc("eth_getTransactionByHash", [tx_hash])
            facilitator_address = _transaction_sender(transaction)
        if facilitator_address is None:
            raise ChainReadError("transaction_sender_missing")
        latest_block = _hex_int(self._rpc("eth_blockNumber", []))
        confirmation_count = max(0, latest_block - block_number + 1)

        if status == 0:
            return ChainConfirmation(
                tx_hash=tx_hash,
                chain_id=chain_id,
                facilitator_address=facilitator_address,
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
            facilitator_address=facilitator_address,
            token_address=intent.token_address,
            from_account=matches[0][0],
            to_account=matches[0][1],
            amount_atomic=matches[0][2],
            block_number=block_number,
            block_hash=str(block_hash),
            confirmation_count=confirmation_count,
            success=True,
        )

    def _find_transaction_for_authorization_sync(
        self,
        intent: Any,
        lookback_blocks: int,
    ) -> str | None:
        chain_id, token_address, buyer, seller, amount, intent_hash = (
            _authorization_recovery_values(intent)
        )
        observed_chain_id = _hex_int(self._rpc("eth_chainId", []))
        if observed_chain_id != chain_id:
            raise ChainReadError("settlement_rpc_chain_mismatch")
        latest = _hex_int(self._rpc("eth_blockNumber", []))
        logs = self._rpc(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(max(0, latest - lookback_blocks + 1)),
                    "toBlock": hex(latest),
                    "address": token_address,
                    "topics": [
                        _TRANSFER_TOPIC,
                        _address_topic(buyer),
                        _address_topic(seller),
                    ],
                }
            ],
        )
        if not isinstance(logs, list):
            raise ChainReadError("invalid_authorization_recovery_logs")
        expected_nonce = intent_hash.removeprefix("sha256:")
        matches: set[str] = set()
        for log in logs:
            if not isinstance(log, dict) or _hex_int(log.get("data")) != amount:
                continue
            tx_hash = log.get("transactionHash")
            if not isinstance(tx_hash, str) or len(tx_hash) != 66:
                raise ChainReadError("invalid_authorization_recovery_transaction")
            transaction = self._rpc("eth_getTransactionByHash", [tx_hash])
            if not isinstance(transaction, dict):
                continue
            input_data = transaction.get("input")
            if _authorization_nonce_from_calldata(input_data) == expected_nonce:
                matches.add(tx_hash.lower())
        if len(matches) > 1:
            raise ChainReadError("authorization_recovery_ambiguous")
        return next(iter(matches), None)

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
            facilitator_address = _transaction_sender(transaction)
            if facilitator_address is None:
                raise ValueError
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
            facilitator_address=facilitator_address,
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


def _transaction_sender(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    sender = value.get("from")
    if isinstance(sender, dict):
        sender = sender.get("hash")
    try:
        return normalize_evm_address(str(sender))
    except SettlementError:
        return None


def _topic_address(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ChainReadError("invalid_transfer_topic")
    hex_value = value[2:]
    if len(hex_value) != 64:
        raise ChainReadError("invalid_transfer_topic")
    return normalize_evm_address("0x" + hex_value[-40:])


def _address_topic(address: str) -> str:
    return "0x" + "0" * 24 + normalize_evm_address(address)[2:]


def _authorization_nonce_from_calldata(value: object) -> str | None:
    """Extract the sixth ABI word used by transferWithAuthorization.

    The exact token Transfer log and frozen amount/account tuple are checked
    before this parser is used.  This keeps the lookup bounded to evidence for
    the same EIP-3009 authorization without retaining its signature.
    """

    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    raw = value[2:]
    nonce_start = 8 + 5 * 64
    nonce_end = nonce_start + 64
    if len(raw) < nonce_end:
        return None
    nonce = raw[nonce_start:nonce_end]
    if not all(char in "0123456789abcdefABCDEF" for char in nonce):
        return None
    return nonce.lower()


def _authorization_recovery_values(
    value: Any,
) -> tuple[int, str, str, str, int, str]:
    """Accept the frozen Arena intent or the payment-safe terms projection."""

    try:
        chain_id = int(value.chain_id)
        token_address = normalize_evm_address(str(value.token_address))
        buyer_value = getattr(value, "buyer_account", None)
        seller_value = getattr(value, "seller_account", None)
        buyer = normalize_evm_address(
            str(buyer_value if buyer_value is not None else value.payer)
        )
        seller = normalize_evm_address(
            str(seller_value if seller_value is not None else value.payee)
        )
        amount = int(value.amount_atomic)
        intent_hash = str(value.intent_hash)
    except (AttributeError, TypeError, ValueError, SettlementError) as exc:
        raise ChainReadError("invalid_authorization_recovery_intent") from exc
    if chain_id <= 0 or amount <= 0 or not intent_hash.startswith("sha256:"):
        raise ChainReadError("invalid_authorization_recovery_intent")
    return chain_id, token_address, buyer, seller, amount, intent_hash


__all__ = [
    "ChainReadError",
    "EvmJsonRpcConfirmationReader",
]
