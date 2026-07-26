# Settlement Module — EIP-3009 Relay Prototype on Injective EVM

> **Owner:** Felix · **Network:** Injective EVM testnet (`1439`) · **Status:** settlement prototype implemented and verified on 2026-07-23

This directory contains a testnet settlement prototype built with viem, a mock
EIP-3009 stablecoin, a custom Express relay, and a TypeScript SDK. The SDK now
also defines a narrow guest-wallet signing seam, an explicitly test-only
in-memory adapter, and a testnet-only external CSV adapter for the isolated
signer service. The default composition still disables signing.

## Scope boundary

The implemented flow demonstrates a payment building block that is relevant to
x402's EVM `exact` scheme:

1. a buyer signs an EIP-712 `TransferWithAuthorization` message offline;
2. a relay submits that authorization to an EIP-3009 token contract;
3. the relay pays INJ gas;
4. the frozen token contract transfers `arena402-g` for current Games and
   rejects reuse of the same nonce. The mUSDC deployment remains a legacy
   compatibility fixture.

The guest-wallet signing seam accepts a stable wallet ID, the public address
frozen by Arena, and the exact EIP-3009 fields. It returns only a public address
and signature. The current Fake adapter generates process-local keys, keeps a
stable address per test wallet ID, and refuses unknown, disabled, or
address-mismatched wallets. The default composition disables signing.

The cross-module `arena_payments/` integration now implements the x402 V2 HTTP
envelope: a resource server returns `402` with `PAYMENT-REQUIRED`, the isolated
signer creates `PAYMENT-SIGNATURE`, and the server returns
`PAYMENT-RESPONSE` after `/verify` and `/settle`. Requirements use CAIP-2
network identifiers and are bound to one immutable Arena intent.

This has passed Fake end-to-end tests, but it has not yet completed a fresh
transaction against a standard public Facilitator. Until that acceptance run,
describe the chain execution as the existing EIP-3009 relay prototype with an
x402 V2 integration layer, not as production-certified public compatibility.

The current Arena 402 game path—accepted negotiation, frozen settlement intent,
chain confirmation, and idempotent cash/inventory update—is also outside this
module. See
[`../../docs/arena-settlement-integration.md`](../../docs/arena-settlement-integration.md).

## Relationship to the completed specs

The completed files under [`../specs/`](../specs/README.md) are frozen
development and acceptance records. They preserve the decisions and terminology
used while this prototype was built and are not retroactively rewritten when
the implementation or product framing changes.

This README is the maintained description of the module's current behavior.
Where a completed spec and the current source differ, keep the spec unchanged
and record the present behavior and limitation here. A materially new behavior
requires a new approved spec rather than an edit that rewrites the old record.

## Implemented flow

```text
Buyer
  │  viem signTypedData (EIP-3009, no on-chain transaction)
  ▼
PaymentAuthorization
  │
  ├─ RealSettlement.lockFunds()
  │    - recovers the signer locally
  │    - records an in-memory settlement intent
  │    - does not lock funds on-chain
  │
  └─ RealSettlement.settleTrade()
       │ POST /settle
       ▼
Custom Express relay
  │  /verify checks expiry, nonce state, and token balance
  │  relay sends a legacy transaction with dynamic gasPrice × 3
  ▼
MockStablecoin.transferWithAuthorization()
  │  verifies the EIP-712 signature and consumes the nonce
  ▼
Full authorized frozen-token amount reaches auth.to
```

`refund()` is currently an in-memory status change only. There is no on-chain
escrow, release, refund, or fee collection contract. It also does not reject an
already settled intent before changing its local status to `refunded`, so that
status cannot be treated as evidence that a transfer was reversed.

## Current on-chain deployment

The machine-readable authority is [`deployments.json`](deployments.json).

| Item | Value |
|---|---|
| Network | Injective EVM testnet |
| Chain ID | `1439` |
| RPC | `https://k8s.testnet.json-rpc.injective.network/` |
| Explorer | `https://testnet.blockscout.injective.network/` |
| Current Game token | `arena402-g`, 6 decimals |
| Current Game token address | `0xBF7B7268CE82d92BaC7a95a741F4003FE84e1884` |
| Current Game EIP-712 domain | name `Arena 402 Gold`, version `1` |
| Legacy test token | mUSDC at `0x06D223D12774386A96D33863D9106A800e52BDeD` |

Both tokens are test-only. `arena402-g` is whitelist-gated and has no public
faucet; a separate owner worker prepares participant wallets before Game start.
Neither token is Circle USDC or a production asset.

## Repository structure

```text
settlement/
├── .env.example
├── deployments.json
├── package.json
├── README.md
├── contracts/
│   ├── contracts/MockStablecoin.sol
│   ├── hardhat.config.cjs
│   ├── scripts/deploy.ts
│   ├── scripts/finalize-deploy.ts
│   ├── scripts/lib-tx.ts
│   └── README.md
├── facilitator/
│   ├── src/index.ts
│   ├── src/settle.ts
│   ├── src/lib-tx.ts
│   └── README.md
├── sdk/
│   ├── src/index.ts
│   ├── src/mock.ts
│   ├── src/real.ts
│   ├── src/settlement.ts
│   ├── src/testing/fake-wallet-secret-store.ts
│   ├── src/testing.ts
│   ├── src/types.ts
│   ├── src/wallet-secret-store.ts
│   ├── src/x402.ts
│   ├── scripts/test-mock.ts
│   ├── scripts/e2e.ts
│   ├── test/wallet-secret-store.test.ts
│   └── README.md
└── scripts/
    ├── check-env.ts
    └── setup-env.md
```

## Setup and verified commands

Create `settlement/.env` from `.env.example` and use test-only private keys.
See [`scripts/setup-env.md`](scripts/setup-env.md) for the current wallet and
funding procedure.

### Environment probe and wallet metadata synchronization

```bash
cd agent-arena/settlement
npm install
npm run check-env
```

The probe does not submit a transaction, but it **does rewrite the local
`deployments.json` file**. With newly generated wallets, run it before the E2E
so `deployments.wallets.seller` matches the intended payee. Review the diff,
keep the token/domain metadata accurate, and do not commit wallet-specific
changes unintentionally. Follow [`scripts/setup-env.md`](scripts/setup-env.md)
for the required order.

### Compile the mock token

```bash
cd agent-arena/settlement/contracts
npm install
npm run compile
```

`npm run deploy` performs state-changing testnet transactions and rewrites
`deployments.json`. Re-deploy only with explicit human confirmation; the
current deployment is already recorded.

### Start the relay

```bash
cd agent-arena/settlement/facilitator
npm install
npm start
```

The default port is `4021`.

### Check the SDK

```bash
cd agent-arena/settlement/sdk
npm install
npx tsc --noEmit
npx tsx scripts/test-mock.ts
```

The real E2E sends a testnet transaction. With the relay already running:

```bash
FACILITATOR_URL=http://localhost:4021 npm run e2e
```

Run it only with explicit human confirmation.

## Relay endpoints

| Method | Route | Current behavior |
|---|---|---|
| `GET` | `/health` | Relay address, INJ balance, configured token, and chain ID |
| `POST` | `/verify` | Checks `validBefore`, nonce state, and buyer token balance |
| `POST` | `/settle` | Runs `/verify`, then submits `transferWithAuthorization` |
| `POST` | `/faucet` | Calls the public mUSDC faucet for `{ "to": "0x..." }` |

`/verify` does not currently recover the EIP-712 signature. The token contract
performs signature verification during `/settle`; an invalid signature can
therefore still cause the relay to submit a reverting transaction and spend
gas.

## SDK behavior

| Method | Prototype behavior |
|---|---|
| `registerAgent` | Off-chain placeholder; no ERC-8004 transaction |
| `lockFunds` | Recovers the authorization signer and stores an in-memory intent; no funds are locked |
| `settleTrade` | Sends the stored authorization to `/settle` |
| `refund` | Marks the in-memory intent refunded; no on-chain cancellation or transfer |
| `getEscrowStatus` | Reads in-memory state |
| `getAgentInfo` | Mock or placeholder data |
| `verifyAttestation` | Non-empty-measurement mock; no on-chain TEE verification |

The interface still returns `amountToSeller` and `platformFee` for compatibility
with the earlier full specification. The current chain transaction transfers
the complete signed `auth.value` to `auth.to`; no fee is withheld. Those return
fields are reporting metadata, not evidence of fee collection.

`RealSettlement.lockFunds()` currently verifies that the signature recovers to
`auth.from`, but it does not bind every authorization field to the separate
business parameters (`buyerWallet`, `sellerWallet`, `amount`, and `expiry`).
The integrating service must enforce that binding before production use.

## Verified prototype evidence

- Direct relay E2E:
  `0x2458782ea387e981fde73b50bb00880736a7ec5953d50679096be72d0f9cef55`
  at block `134438902`.
- Re-submitting the exact same authorization was rejected because its EIP-3009
  nonce had already been consumed.

This evidence covers the payment relay only. It does not prove game-level
idempotency, inventory commit, standard x402 compatibility, or the full product
demo. The frozen SETTLE-002 record mentions a separate
`RealSettlement.settleTrade()` run using only a truncated transaction hash;
without the complete hash or a checked-in run artifact, this README does not
treat that run as independently reproducible evidence.

## Known constraints and next work

- All transactions use legacy type `0` with a dynamically queried gas price
  multiplied by `3`.
- Transaction confirmation is polled through Blockscout because the public RPC
  can lag when serving receipts.
- Relay state and SDK intents are in memory and are lost on restart.
- The relay must constrain token, chain, payee, amount, and business order ID
  before production use.
- The next product integration must connect an immutable accepted-negotiation
  snapshot to the settlement authorization, then update game cash and inventory
  only after chain confirmation.
- The frozen specs' M4 milestone proposed a TEE-produced authorization. Current
  product scope allows a test-only signer first and treats TEE custody as later
  work.
- A new approved spec or implementation plan must add the actual x402 HTTP
  challenge, requirements, headers, and compatibility tests.
