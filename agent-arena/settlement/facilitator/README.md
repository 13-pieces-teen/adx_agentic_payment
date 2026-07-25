# Custom EIP-3009 Relay (SETTLE-004)

This Express service accepts a buyer's offline EIP-3009 authorization and uses
the relay wallet to submit `mUSDC.transferWithAuthorization` on Injective EVM
testnet. The relay pays INJ gas; buyer and payee do not submit transactions.

## Protocol boundary

This remains a custom Injective EIP-3009 relay, but its production HTTP surface
accepts the x402 V2 facilitator envelope:

- `/verify` validates the official x402 schemas, exact requirement, CAIP-2
  network, token, resource origin, deterministic Arena nonce, authorization
  window, and EIP-712 signature before reading chain state;
- `/settle` returns the V2 response consumed by Arena;
- the relay key can be selected by explicit index from an owner-only external
  facilitator CSV and is never returned or logged;
- the Arena API owns the `402 Payment Required` challenge and payment headers.

Public facilitator discovery and fresh testnet acceptance remain unverified.

## Start

For local legacy development, configure `settlement/.env`. Production uses
`ADX_SETTLEMENT_DEPLOYMENTS_PATH`, an owner-only CSV path, explicit wallet
index, allowed Arena API origin, and a bearer token.

```bash
cd agent-arena/settlement/facilitator
npm install
npm start
```

The default port is `4021`. Override it with `FACILITATOR_PORT`.

## Endpoints

| Method | Route | Implemented behavior |
|---|---|---|
| `GET` | `/health` | Returns relay address, INJ balance, token address, and chain ID |
| `POST` | `/verify` | Validates V2 payload and checks token nonce/balance |
| `POST` | `/settle` | Revalidates V2 payload, then submits `transferWithAuthorization` |

Both mutation endpoints require `Authorization: Bearer ...`.

## What `/verify` does and does not verify

The V2 adapter checks:

- official payload and requirement schemas;
- exact accepted requirement equality and configured chain/token;
- allowed Arena resource origin, payee, amount, and deterministic intent nonce;
- EIP-712 signature recovery and payer equality;
- `validAfter`, `validBefore`, and the requirement timeout;
- `authorizationState[from][nonce]` is unused;
- `balanceOf(from)` is at least `value`.

Arena separately validates the same facts against its immutable
SettlementIntent and PaymentMandate before calling the relay. The token
contract verifies the signature again during `/settle`.

## Transaction behavior

- Transactions use legacy type `0`.
- The relay queries the current gas price and multiplies it by `3`.
- One relay account submits writes through a serial promise queue to reduce
  transaction-nonce conflicts.
- Confirmation is polled through the Injective testnet Blockscout API.
- `/settle` transfers the complete signed `value` to the signed `to` address.
- There is no escrow, refund, release, or on-chain platform fee.

## Verification

With the relay running, the SDK E2E signs, settles, checks balance deltas, and
re-submits the same authorization to test EIP-3009 nonce rejection:

```bash
cd agent-arena/settlement/sdk
FACILITATOR_URL=http://localhost:4021 npm run e2e
```

This command performs a state-changing testnet transaction and requires
explicit human confirmation.

The recorded successful relay transaction is:
`0x2458782ea387e981fde73b50bb00880736a7ec5953d50679096be72d0f9cef55`.
