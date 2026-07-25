import assert from "node:assert/strict";
import { chmod, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { privateKeyToAccount } from "viem/accounts";
import {
  LocalCsvWalletSecretStore,
  LocalCsvWalletStoreError,
} from "../src/local-csv-wallet-secret-store.ts";
import {
  createX402PaymentPayload,
  decodeX402Header,
  encodeX402Header,
  type ArenaX402PaymentRequired,
} from "../src/x402-v2.ts";

const privateKey =
  "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" as const;
const account = privateKeyToAccount(privateKey);

function requirement(): ArenaX402PaymentRequired {
  return {
    x402Version: 2,
    resource: {
      url: "https://api.arena402.example/api/v1/x402/settlement-intents/intent-1/execute",
      description: "Arena 402 settlement intent-1",
      mimeType: "application/json",
    },
    accepts: [
      {
        scheme: "exact",
        network: "eip155:1439",
        asset: "0x3333333333333333333333333333333333333333",
        amount: "40",
        payTo: "0x2222222222222222222222222222222222222222",
        maxTimeoutSeconds: 600,
        extra: {
          name: "mUSDC",
          version: "2",
          arena402IntentHash:
            "sha256:abababababababababababababababababababababababababababababababab",
          arena402SettlementIntentId: "intent-1",
        },
      },
    ],
  };
}

async function csvStore(mode = 0o600) {
  const directory = await mkdtemp(join(tmpdir(), "arena402-wallets-"));
  const path = join(directory, "wallets.csv");
  await writeFile(
    path,
    [
      "index,ethereum_address,private_key",
      `1,${account.address},${privateKey}`,
    ].join("\n"),
    { mode },
  );
  await chmod(path, mode);
  return {
    store: await LocalCsvWalletSecretStore.load(path),
    path,
  };
}

test("local CSV store signs only the wallet matching the stable row id", async () => {
  const { store } = await csvStore();
  const required = requirement();
  const payload = await createX402PaymentPayload({
    paymentRequired: required,
    walletId: "agent-wallet-0001",
    expectedFrom: account.address,
    nowSeconds: 1_800_000_000,
    secrets: store,
  });

  assert.equal(payload.x402Version, 2);
  assert.deepEqual(payload.accepted, required.accepts[0]);
  assert.equal(payload.payload.authorization.from, account.address);
  assert.equal(payload.payload.authorization.value, "40");
  assert.equal(
    payload.payload.authorization.nonce,
    "0xabababababababababababababababababababababababababababababababab",
  );
  assert.deepEqual(decodeX402Header(encodeX402Header(payload)), payload);
});

test("local CSV store refuses group/world-readable secret files", async () => {
  const { path } = await csvStore();
  await chmod(path, 0o644);
  await assert.rejects(
    LocalCsvWalletSecretStore.load(path),
    (error: unknown) =>
      error instanceof LocalCsvWalletStoreError &&
      error.code === "wallet_secret_file_permissions",
  );
});

test("x402 payload rejects a challenge not bound to one Arena intent", async () => {
  const { store } = await csvStore();
  const required = requirement();
  delete required.accepts[0].extra.arena402IntentHash;

  await assert.rejects(
    createX402PaymentPayload({
      paymentRequired: required,
      walletId: "agent-wallet-0001",
      expectedFrom: account.address,
      nowSeconds: 1_800_000_000,
      secrets: store,
    }),
    /x402_intent_hash_required/,
  );
});
