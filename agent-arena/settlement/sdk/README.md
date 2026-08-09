# Settlement SDK Prototype

> Current status — 2026-08-09: the SDK is used by Arena's self-hosted testnet
> settlement path for deterministic EIP-3009 authorization, x402 V2 payloads,
> encrypted platform-wallet signing and recovery. The formal mixed-Runtime Game
> and a 50-settlement/100-Hosted run have exercised the integrated path.

The TypeScript source under this directory provides:

- viem helpers for signing an EIP-3009 authorization;
- a guest-wallet signing seam that does not return private keys or a
  general-purpose account;
- an x402 V2 exact-payment payload builder and header codec;
- a one-time CSV import path plus isolated PostgreSQL ciphertext signer;
- a shared `SettlementSDK` interface;
- an in-memory `MockSettlement`;
- a testnet-backed `RealSettlement` that calls the custom relay.

## Packaging status

`package.json` names the package `@agent-arena/settlement-sdk`, but the current
repository does not define an npm workspace, compiled output, `main`, or
`exports`. Importing it by package name requires the consuming project to add
local package wiring first. The authoritative source entry is `src/index.ts`.

## Protocol boundary

`src/x402-v2.ts` accepts one Arena-bound x402 V2 `PaymentRequired`, copies the
exact accepted requirement, derives the EIP-3009 nonce from the immutable
SettlementIntent hash, signs through `WalletSecretStore`, and produces a
`PaymentPayload` suitable for `PAYMENT-SIGNATURE`. The Python resource server
owns the HTTP 402/retry/response exchange and Facilitator calls.

The shape follows x402 V2 (`x402Version: 2`, CAIP-2 `eip155:<chainId>`,
`resource`, `accepted`, `payload`). The project self-hosted Facilitator path is
live on testnet; standard public Facilitator interoperability is still pending.

## Guest-wallet secret-store foundation

`src/wallet-secret-store.ts` defines the narrow signing seam for future
platform-managed `sandbox_guest` wallets. The caller supplies:

- a stable wallet ID;
- the public buyer address frozen into Arena state;
- the exact token domain, payee, atomic amount, validity window, and
  deterministic nonce.

The store returns only the matching public address and EIP-3009 signature. It
does not return a private key or a general-purpose `LocalAccount`.

`FakeWalletSecretStore` is an explicit test adapter exported only from
`src/testing.ts`, not the production `src/index.ts` entrypoint. It generates
process-local keys, keeps one stable address per test wallet ID, and rejects
unknown, disabled, or address-mismatched wallets.
`createWalletSecretStore()` returns a disabled adapter by default.

`LocalCsvWalletSecretStore` remains an explicit local-development adapter. It
requires an absolute owner-only file, maps the CSV `index` to stable
`agent-wallet-0001` identifiers, derives each address from its key, and rejects
every mismatch or duplicate.

Production uses `PostgresEncryptedWalletSecretStore`. The one-time importer
validates each CSV key/address pair, generates a random per-wallet DEK, encrypts
the raw key with AES-256-GCM, wraps the DEK with a 32-byte KEK, and sends only
ciphertext/nonces to PostgreSQL. The KEK is a separate raw 32-byte host file
with mode `0400`; it is mounted only into the signer and manual admin profile.
The long-running signer has a dedicated database login that can execute only
the one-wallet ciphertext read function. It has no CSV mount and never returns
a key or general-purpose account.

`npm run wallet:vault-import` is dry-run by default and needs `--apply` to
write. `npm run wallet:vault-rotate` likewise validates by default; `--apply`
unwraps and rewraps only DEKs, leaving wallet addresses and private-key
ciphertext unchanged. Both tools log counts only.

The wallet-backed signing function requires a caller-supplied nonce; it never
generates a fresh authorization on retry. The SDK also checks the adapter's
returned address against the frozen buyer and recovers the final signature
before returning a payment authorization.

Permanent user bindings, PaymentMandates, reservations, x402 attempts, leases,
and unattended orchestration live in `arena_payments/`. The signer receives
only its role-specific ciphertext function, not Game tables, Mandate mutation
authority, a general-purpose signing method, or transaction broadcast access.

## Arena SettlementIntent bridge

`scripts/settle-arena-intent.ts` bridges one frozen Arena
`SettlementIntent` to the existing EIP-3009 custom relay. It:

- loads the intent from Arena and checks chain, token, decimals, payer, payee,
  and amount against `deployments.json`;
- requires the operator to pass the exact frozen `intentHash`, then persists
  that approval and a nonce digest in Arena before any broadcast;
- derives the EIP-3009 nonce deterministically from `intentHash`, so restarting
  the bridge cannot create a second authorization for the same intent;
- signs only inside the local settlement process;
- verifies the authorization locally and through Facilitator `/verify`;
- requires `--confirm-testnet-transfer` before calling Facilitator `/settle`;
- sends only the transaction hash and raw nonce to Arena, where Arena stores a
  nonce digest rather than the nonce;
- asks Arena's read-only recovery endpoint to verify the exact on-chain
  transfer and commit inventory.

The buyer private key is read by this local process only. It must not be sent
to Arena, embedded in command arguments, printed, or committed.

Example, after an accepted Arena deal has frozen an intent:

```powershell
$env:ARENA_API_URL="http://127.0.0.1:8000"
$env:ARENA_DEV_TOKEN="<local dev token>"
$env:FACILITATOR_URL="http://127.0.0.1:4021"
npm run arena:settle -- `
  --game-id "<game id>" `
  --intent-id "<intent id>" `
  --approved-intent-hash "sha256:<reviewed hash>" `
  --confirm-testnet-transfer
```

This command is state-changing on Injective testnet. Run it only after a human
has checked every public field of the exact intent and copied its `intentHash`
into `--approved-intent-hash`.

If the bridge exits after Facilitator broadcast but before Arena records the
transaction hash, do not create a new authorization. The deterministic nonce
prevents a second payment. Recover the public hash from Facilitator output or
Blockscout and resume the same approved intent without broadcasting:

```powershell
npm run arena:settle -- `
  --game-id "<game id>" `
  --intent-id "<intent id>" `
  --approved-intent-hash "sha256:<same reviewed hash>" `
  --record-existing-tx-hash "0x<public transaction hash>" `
  --confirm-testnet-transfer
```

## Restart crash drill

The local Compose file provides an opt-in, non-signing Arena Worker. Produce a
submitted state and sanitized evidence, restart that Worker, and verify that
the same transaction produces one stable inventory commit:

```powershell
docker compose -f docker-compose.local.yml --profile arena up -d arena-worker

npm run arena:submit-only -- `
  --game-id "<game id>" `
  --intent-id "<intent id>" `
  --approved-intent-hash "sha256:<reviewed hash>" `
  --confirm-testnet-transfer `
  --evidence-out ".\restart-submission.json"

docker compose -f docker-compose.local.yml --profile arena restart arena-worker

npm run arena:verify-restart -- `
  --game-id "<game id>" `
  --before-evidence ".\restart-submission.json"
```

The evidence file contains only the Intent/hash/status, public transaction
hash, Blockscout URL, and timestamp. It never contains the raw nonce,
authorization signature, or wallet private key.

## Produce an authorization

```typescript
import {
  loadDeployments,
  signTransferAuthorization,
} from "@agent-arena/settlement-sdk";
import { privateKeyToAccount } from "viem/accounts";

const dep = loadDeployments("agent-arena/settlement/deployments.json");
const buyer = privateKeyToAccount(BUYER_PRIVATE_KEY);

const auth = await signTransferAuthorization({
  account: buyer,
  to: sellerAddress,
  value: 5_000_000n,
  dep,
  nowSeconds: Math.floor(Date.now() / 1000),
});
```

The package-name import above assumes local package wiring. Without that
wiring, import the same exports from the repository's `src/index.ts`.

`auth` is a project `PaymentAuthorization`, not a complete x402 payment
request.

## Settlement interface behavior

```typescript
import {
  MockSettlement,
  RealSettlement,
  type SettlementSDK,
} from "@agent-arena/settlement-sdk";

const settlement: SettlementSDK = new MockSettlement();
// const settlement = new RealSettlement({ facilitatorUrl, deployments });

const { escrowId } = await settlement.lockFunds({
  negotiationId,
  buyerWallet,
  sellerWallet,
  amount,
  currency: "USDC",
  x402Signature: JSON.stringify(auth),
  expiry,
});

const result = await settlement.settleTrade({ escrowId });
```

Despite the compatibility names:

- `lockFunds` stores an in-memory intent and does not lock funds;
- `settleTrade` submits a direct buyer-to-payee authorization;
- `refund` is an in-memory status change and does not cancel on-chain;
- registration and attestation methods are mocks or off-chain placeholders.

Both current `refund` implementations can overwrite the local status even after
an intent was marked settled. A `refunded` status is therefore neither a valid
state-transition guarantee nor evidence that funds moved back.

`RealSettlement.lockFunds()` recovers the signer, but it does not yet bind every
authorization field to `buyerWallet`, `sellerWallet`, `amount`, `expiry`,
token, chain, and the game `negotiationId`. The integrating service must
enforce those checks.

## Fee fields

The interface retains `amountToSeller` and `platformFee`. The current code
calculates 0.5% reporting values from the SDK `amount`, but the chain transfer
sends the complete signed `auth.value` to `auth.to`.

No platform fee is collected on-chain. Consumers must not treat
`platformFee` as a payment receipt or accounting fact.

## Source modules

| File | Purpose |
|---|---|
| `src/settlement.ts` | `SettlementSDK`, `AttestationReport`, and compatibility fee constant |
| `src/mock.ts` | In-memory implementation |
| `src/real.ts` | Custom-relay implementation |
| `src/wallet-secret-store.ts` | Fail-closed guest-wallet signing seam and safe error codes |
| `src/testing.ts` | Explicit test-only adapter entrypoint |
| `src/x402.ts` | viem EIP-3009 signing and local signature/domain checks |
| `src/types.ts` | `PaymentAuthorization`, deployment, and result types |
| `src/index.ts` | Source exports |

## Local checks

```bash
cd agent-arena/settlement/sdk
npm install
npm test
npx tsc --noEmit
npx tsx scripts/test-mock.ts
```

The tests cover deterministic wallet authorization, default-disabled signing,
stable fake wallet addresses, and rejection of unknown, disabled, mismatched,
or malformed signer responses. The mock script checks compatibility fee
metadata. Neither proves on-chain fee collection.

For the state-changing relay E2E, first start the relay and then run, with
explicit human confirmation:

```bash
FACILITATOR_URL=http://localhost:4021 npm run e2e
```

The E2E derives the buyer from `BUYER_PRIVATE_KEY` but sends payment to
`deployments.json`'s `wallets.seller`. When using fresh wallets, synchronize
that metadata first by following
[`../scripts/setup-env.md`](../scripts/setup-env.md); setting
`SELLER_PRIVATE_KEY` alone does not change the E2E payee.

The E2E verifies the direct mUSDC balance change and rejection of the exact same
authorization nonce. It does not verify the complete game flow, negotiation-
level idempotency, or inventory commit.
