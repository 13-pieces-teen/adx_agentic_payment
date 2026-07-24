# Custom EIP-3009 Relay (SETTLE-004)

This Express service accepts a buyer's offline EIP-3009 authorization and uses
the relay wallet to submit `mUSDC.transferWithAuthorization` on Injective EVM
testnet. The relay pays INJ gas; buyer and payee do not submit transactions.

## Protocol boundary

This is a custom payment relay, not yet a complete x402 facilitator:

- it does not issue or consume an HTTP `402` challenge;
- it does not exchange `PaymentRequirements` or x402 payment headers;
- it does not use `@x402/*` packages;
- its request and response bodies are project-specific.

## Start

Create `settlement/.env` first; the process reads that file and
`settlement/deployments.json`.

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
| `POST` | `/verify` | Checks expiry, token nonce state, and buyer token balance |
| `POST` | `/settle` | Runs `/verify`, then submits `transferWithAuthorization` |
| `POST` | `/faucet` | Calls the public mUSDC `faucet(to)` |

`/settle` accepts the SETTLE-003 `PaymentAuthorization` JSON object.
`/faucet` accepts:

```json
{ "to": "0x..." }
```

## What `/verify` does and does not verify

It currently checks:

- `validBefore` is later than the relay's current time;
- `authorizationState[from][nonce]` is unused;
- `balanceOf(from)` is at least `value`.

It does not currently:

- recover the EIP-712 signature;
- check `validAfter`;
- constrain `token` to the configured mUSDC address;
- validate the supplied `chainId`;
- bind `to`, `value`, or an order ID to server-side requirements.

The mUSDC contract verifies the EIP-712 signature during `/settle`. An invalid
authorization may therefore pass the precheck, revert on-chain, and still cost
the relay gas.

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
