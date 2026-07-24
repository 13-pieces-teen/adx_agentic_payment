# Settlement SDK Prototype

The TypeScript source under this directory provides:

- viem helpers for signing an EIP-3009 authorization;
- a shared `SettlementSDK` interface;
- an in-memory `MockSettlement`;
- a testnet-backed `RealSettlement` that calls the custom relay.

## Packaging status

`package.json` names the package `@agent-arena/settlement-sdk`, but the current
repository does not define an npm workspace, compiled output, `main`, or
`exports`. Importing it by package name requires the consuming project to add
local package wiring first. The authoritative source entry is `src/index.ts`.

## Protocol boundary

The SDK does not implement a complete HTTP x402 client:

- it does not receive a `402 Payment Required` challenge;
- it does not parse `PaymentRequirements`;
- it does not attach x402 request headers or process response headers;
- it has no `@x402/*` dependency.

`src/x402.ts` is currently an EIP-3009 signing helper named for the intended
future integration.

## Arena SettlementIntent bridge

`scripts/settle-arena-intent.ts` bridges one frozen Arena
`SettlementIntent` to the existing EIP-3009 custom relay. It:

- loads the intent from Arena and checks chain, token, decimals, payer, payee,
  and amount against `deployments.json`;
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
$env:ARENA_SETTLEMENT_DEV_TOKEN="<local dev token>"
$env:FACILITATOR_URL="http://127.0.0.1:4021"
npm run arena:settle -- --intent-id "<intent id>" --confirm-testnet-transfer
```

This command is state-changing on Injective testnet. Run it only after a human
has checked the exact intent and explicitly approved that transfer.

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
| `src/x402.ts` | viem EIP-3009 signing and local signature/domain checks |
| `src/types.ts` | `PaymentAuthorization`, deployment, and result types |
| `src/index.ts` | Source exports |

## Local checks

```bash
cd agent-arena/settlement/sdk
npm install
npx tsc --noEmit
npx tsx scripts/test-mock.ts
```

The mock test checks the current interface behavior, including compatibility
fee metadata. It does not prove on-chain fee collection.

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
