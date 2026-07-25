"""Read-only Injective EVM wallet queries."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .crypto import normalize_address
from .repository import ExternalWalletBinding


class WalletChainError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class WalletTokenConfig:
    symbol: str
    contract: str
    decimals: int = 6

    def __post_init__(self) -> None:
        if not self.symbol or not 0 <= self.decimals <= 36:
            raise ValueError("invalid_wallet_token_config")
        normalized = normalize_address(self.contract)
        object.__setattr__(self, "contract", normalized)


def _format_units(value: int, decimals: int) -> str:
    if value < 0 or decimals < 0:
        raise ValueError("invalid_units")
    if decimals == 0:
        return str(value)
    whole, fractional = divmod(value, 10**decimals)
    if fractional == 0:
        return str(whole)
    suffix = f"{fractional:0{decimals}d}".rstrip("0")
    return f"{whole}.{suffix}"


class InjectiveWalletService:
    """Resolve balances with standard EVM JSON-RPC calls.

    The service never sends transactions. Token contracts are explicitly
    configured, so a missing game-token address results in an empty token list
    instead of an invented or stale address.
    """

    def __init__(
        self,
        rpc_url: str,
        *,
        chain_id: int = 1439,
        network: str = "injective-testnet",
        explorer_url: str = "https://testnet.blockscout.injective.network",
        tokens: tuple[WalletTokenConfig, ...] = (),
        timeout_seconds: float = 10.0,
        rpc_call: Any | None = None,
    ) -> None:
        if not rpc_url:
            raise ValueError("wallet_rpc_url_required")
        if chain_id != 1439:
            raise ValueError("only_injective_testnet_is_supported")
        if not rpc_url.lower().startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("wallet_rpc_url_must_be_https")
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.network = network
        self.explorer_url = explorer_url.rstrip("/")
        self.tokens = tokens
        self.timeout_seconds = timeout_seconds
        self._rpc_call_override = rpc_call

    async def overview(self, binding: ExternalWalletBinding) -> dict[str, object]:
        address = normalize_address(binding.address)
        chain_id = int(await self._rpc("eth_chainId", []), 16)
        if chain_id != self.chain_id:
            raise WalletChainError("wallet_rpc_chain_mismatch")
        native_raw, token_raw = await asyncio.gather(
            self._rpc("eth_getBalance", [address, "latest"]),
            self._token_balances(address),
        )
        return {
            "address": address,
            "chainId": self.chain_id,
            "network": self.network,
            "native": {
                "symbol": "INJ",
                "balance": _format_units(int(native_raw, 16), 18),
            },
            "tokens": token_raw,
            "checkedAt": _utc_now_iso(),
        }

    async def _token_balances(self, address: str) -> list[dict[str, object]]:
        async def read(token: WalletTokenConfig) -> dict[str, object]:
            data = "0x70a08231" + address[2:].rjust(64, "0")
            raw = await self._rpc("eth_call", [{"to": token.contract, "data": data}, "latest"])
            return {
                "symbol": token.symbol,
                "contract": token.contract,
                "balance": _format_units(int(raw, 16), token.decimals),
            }

        return list(await asyncio.gather(*(read(token) for token in self.tokens)))

    async def _rpc(self, method: str, params: list[object]) -> str:
        if self._rpc_call_override is not None:
            result = self._rpc_call_override(method, params)
            if asyncio.iscoroutine(result):
                result = await result
            return _rpc_result(method, result)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WalletChainError("wallet_rpc_unavailable") from exc
        if not isinstance(body, dict):
            raise WalletChainError("wallet_rpc_invalid_response")
        if body.get("error") is not None:
            raise WalletChainError("wallet_rpc_call_failed")
        return _rpc_result(method, body.get("result"))


def _rpc_result(method: str, result: object) -> str:
    if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", result):
        raise WalletChainError(f"wallet_rpc_invalid_{method}_result")
    return result


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def load_wallet_service_from_env() -> InjectiveWalletService:
    rpc_url = (
        os.getenv("ADX_WALLET_RPC_URL", "").strip()
        or os.getenv("ADX_ARENA_SETTLEMENT_RPC_URL", "").strip()
        or "https://k8s.testnet.json-rpc.injective.network/"
    )
    explorer_url = (
        os.getenv("ADX_WALLET_EXPLORER_URL", "").strip()
        or "https://testnet.blockscout.injective.network"
    )
    tokens: list[WalletTokenConfig] = []
    for symbol in ("arena402-g", "arena402-m"):
        env_key = symbol.upper().replace("-", "_")
        address = os.getenv(f"ADX_{env_key}_TOKEN_ADDRESS", "").strip()
        if not address:
            continue
        raw_decimals = os.getenv(f"ADX_{env_key}_TOKEN_DECIMALS", "6").strip()
        try:
            decimals = int(raw_decimals)
        except ValueError as exc:
            raise ValueError(f"ADX_{env_key}_TOKEN_DECIMALS must be an integer") from exc
        tokens.append(WalletTokenConfig(symbol, address, decimals))
    return InjectiveWalletService(
        rpc_url,
        explorer_url=explorer_url,
        tokens=tuple(tokens),
    )


__all__ = [
    "InjectiveWalletService",
    "WalletChainError",
    "WalletTokenConfig",
    "_format_units",
    "load_wallet_service_from_env",
]
