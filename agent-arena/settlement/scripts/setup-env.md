# Settlement Prototype Environment Setup

This guide prepares test-only wallets for the deployed mUSDC EIP-3009 relay on
Injective EVM testnet `1439`.

The current path does not use Circle USDC. It uses the mock token recorded in
`settlement/deployments.json`.

## 1. Install each package separately

There is no npm workspace at `agent-arena/` or `settlement/`. Install the
packages you will use:

```bash
cd agent-arena/settlement
npm install

cd contracts
npm install

cd ../facilitator
npm install

cd ../sdk
npm install
```

Compiling or deploying the contract is not required for the existing testnet
deployment.

## 2. Create three test-only wallets

Create separate EVM accounts for:

- **buyer** — holds mUSDC and signs EIP-3009 authorizations;
- **payee/seller** — receives mUSDC;
- **relay/facilitator** — holds testnet INJ and submits transactions.

Never reuse a wallet that holds real assets. Never commit, package, print in
logs, or share its private key or seed phrase.

## 3. Create `settlement/.env`

Copy `settlement/.env.example` to `settlement/.env`, then fill:

```dotenv
INJECTIVE_EVM_RPC=https://k8s.testnet.json-rpc.injective.network/
INJECTIVE_CHAIN_ID=1439
USDC_ADDRESS=0x06D223D12774386A96D33863D9106A800e52BDeD

BUYER_PRIVATE_KEY=
SELLER_PRIVATE_KEY=
FACILITATOR_PRIVATE_KEY=
```

The `USDC_ADDRESS` is the deployed mock token, not Circle USDC. Confirm the
current value in `settlement/deployments.json` before use.

## 4. Synchronize fresh wallet metadata

This step is required when you generated new wallets.

The E2E derives the buyer from `BUYER_PRIVATE_KEY`, but it sends payment to
`deployments.json`'s `wallets.seller`; it does not select the payee from
`SELLER_PRIVATE_KEY` at runtime. From `agent-arena/settlement`, run:

```bash
npm run check-env
```

The command does not submit a transaction, but it rewrites
`deployments.json` using the wallet addresses derived from the three private
keys. Before funding or running the E2E:

1. confirm the printed seller address is the intended payee;
2. confirm `deployments.wallets.seller` matches that address;
3. review the full diff and keep the existing mUSDC address, decimals, and
   EIP-712 domain metadata accurate;
4. do not commit wallet-specific metadata unintentionally.

Before the wallets are funded, AC2 or AC3 may report failure even though the
address synchronization was written. Re-run the check after funding.

## 5. Add Injective EVM testnet to a wallet UI

| Field | Value |
|---|---|
| Network name | Injective EVM Testnet |
| RPC URL | `https://k8s.testnet.json-rpc.injective.network/` |
| Chain ID | `1439` |
| Currency | INJ |
| Explorer | `https://testnet.blockscout.injective.network/` |

This step is optional for the scripts but useful for inspecting test balances.

## 6. Fund the relay wallet

Send testnet INJ from `https://testnet.faucet.injective.network/` to the relay's
`0x` address. Buyer and payee do not need INJ for the relay E2E.

## 7. Start the relay and fund the buyer with mUSDC

```bash
cd agent-arena/settlement/facilitator
npm start
```

Check `GET http://localhost:4021/health`. It should report:

- `ok: true`;
- the expected relay address;
- a positive INJ balance;
- token `0x06D2…BDeD`;
- chain ID `1439`.

Then call `POST http://localhost:4021/faucet` with:

```json
{ "to": "<buyer 0x address>" }
```

The public token faucet mints 1,000 mUSDC per call. It is unrestricted test
infrastructure and must not be exposed as a production faucet.

## 8. Verify the funded environment

From `agent-arena/settlement`:

```bash
npm run check-env
```

The script does not submit a transaction, but it does write the local
`deployments.json`. Review the diff and restore any canonical metadata fields
if the probe was run only for diagnosis.

The original SETTLE-001 Circle-faucet probe is complete. A missing Circle USDC
address is no longer a blocker and Permit2 is not the current branch.

## 9. Run local SDK checks

```bash
cd agent-arena/settlement/sdk
npx tsc --noEmit
npx tsx scripts/test-mock.ts
```

## 10. Run the state-changing relay E2E

With the relay already running:

```bash
FACILITATOR_URL=http://localhost:4021 npm run e2e
```

This submits a real Injective testnet transaction. Run it only after explicit
human confirmation.

The expected scope is:

- buyer signs offline;
- relay pays INJ gas;
- full signed mUSDC value reaches the payee;
- reusing the exact authorization nonce is rejected.

It does not test an HTTP x402 challenge, game-level idempotency, inventory commit, escrow,
refund, platform fee collection, matching, or delivery.
