# Arena 402 wallet API

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

The token list contains only configured contracts. Do not fill these values
until the Arena 402 token deployments are finalized. The current implementation
is fixed to Injective EVM Testnet chain ID `1439`.
