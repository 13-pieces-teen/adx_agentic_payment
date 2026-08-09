# Arena 402 wallet API

> Current status — 2026-08-09: platform-assigned `sandbox_guest` wallets and
> user-controlled Injective EVM testnet bindings are separate authorities.
> Current Games use the platform wallet for unattended, Game-scoped
> PaymentMandates; proving an external wallet does not grant transaction
> authority. GameCoin preparation is confirmation-gated and now uses a bounded
> 16-transaction owner pipeline. An isolated 100-wallet run completed 100/100
> in `162.430s`; this is a load point, not a per-user latency guarantee.

## Platform-assigned wallet API

Arena platform wallets are allocated from the imported `wallet_inventory` only
after a durable platform user exists. The allocation is lazy and atomic: the
first request claims one `available` wallet, binds it to the internal `user_id`,
and marks the inventory row `bound`. Password and GitHub sign-in identities use
the same business rule. Later requests return the same wallet. A legacy
`github_subject` may remain as compatibility metadata for existing rows, but it
is not wallet authority. The API never returns `secret_ref`, a private key, or
a seed phrase.

`GET /api/v1/me/wallet`

```json
{
  "wallet": {
    "walletId": "agent-wallet-0001",
    "chainId": 1439,
    "address": "0x...",
    "custodyMode": "sandbox_guest",
    "boundAt": "2026-07-26T12:00:00+00:00"
  }
}
```

`GET /api/v1/me/wallet/overview`

This endpoint performs the same lazy allocation if needed, then reads the
assigned address through Injective EVM JSON-RPC. It returns:

```json
{
  "walletId": "agent-wallet-0001",
  "custodyMode": "sandbox_guest",
  "boundAt": "2026-07-26T12:00:00+00:00",
  "address": "0x...",
  "chainId": 1439,
  "network": "injective-testnet",
  "native": { "symbol": "INJ", "balance": "0" },
  "tokens": [
    {
      "symbol": "arena402-g",
      "contract": "0x...",
      "balance": "0"
    }
  ],
  "checkedAt": "2026-07-26T12:00:01+00:00"
}
```

The frontend only needs to change `getWalletBinding()` to request
`/api/v1/me/wallet` and unwrap `response.wallet`, and
`getWalletOverview()` to request `/api/v1/me/wallet/overview`.

The platform-assigned wallet API is intentionally separate from the
user-controlled wallet challenge API below. The latter proves ownership of an
external wallet and must not be used for platform custody.

## User-controlled wallet binding

This API binds one user-controlled Injective EVM Testnet wallet to the
authenticated Arena user. It is separate from `arena402.user_wallets`, which
stores platform-managed settlement wallets.

## Binding flow

All endpoints require the Arena session cookie. `POST` and `DELETE` endpoints
also require the existing `X-CSRF-Token` header.

1. `POST /api/wallet/challenge` with `{ "address": "0x...", "chainId": 1439 }`.
2. Ask MetaMask/WalletConnect to sign the returned `message` with
   EIP-191 `personal_sign`. This is an ownership proof only; it is not a
   transaction authorization.
3. `POST /api/wallet/verify` with `challengeId`, the same `address` and
   `message`, and the returned `signature`.
4. Use `GET /api/wallet` to read the binding. `DELETE /api/wallet` removes the
   current binding; it does not remove the verification audit records.

The backend stores only the normalized address, chain, verification time, and
hashed challenge/audit metadata. It never accepts, stores, or logs a private
key, seed phrase, or wallet credential.

## Read APIs

`GET /api/wallet/overview` returns the bound address, chain, native INJ balance,
and configured Arena 402 token balances. Balances are read with JSON-RPC
`eth_getBalance` and ERC-20 `balanceOf`; no transaction is submitted.

`GET /api/wallet/activity?limit=50` returns Arena settlement records scoped to
the bound address. `GET /api/wallet/transactions/{txHash}` returns one record.
The activity projection joins the immutable SettlementIntent with submission
and chain-confirmation evidence, so a pending settlement can have a null
`txHash` and a confirmed record includes `confirmedAt`, `blockNumber`, and
`explorerUrl`.

## Configuration

```text
ADX_WALLET_RPC_URL=https://k8s.testnet.json-rpc.injective.network/
ADX_WALLET_EXPLORER_URL=https://testnet.blockscout.injective.network
ADX_ARENA402_G_TOKEN_ADDRESS=0x...
ADX_ARENA402_G_TOKEN_DECIMALS=6
ADX_ARENA402_M_TOKEN_ADDRESS=0x...
ADX_ARENA402_M_TOKEN_DECIMALS=6
```

The token list contains only configured contracts. Production configures the
allowlisted `arena402-g` test-game token; compatibility assets are exposed only
when explicitly configured. The current implementation is fixed to Injective
EVM Testnet chain ID `1439`.
