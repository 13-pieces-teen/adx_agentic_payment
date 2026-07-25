import assert from "node:assert/strict";
import test from "node:test";
import { randomBytes } from "node:crypto";
import { chmod, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { privateKeyToAccount } from "viem/accounts";

import {
  decryptWalletPrivateKey,
  encryptWalletPrivateKey,
  loadWalletMasterKey,
  PostgresEncryptedWalletSecretStore,
  rewrapWalletDataKey,
} from "../src/postgres-encrypted-wallet-secret-store.ts";
import {
  signTransferAuthorizationWithWallet,
  verifyAuthorizationLocally,
  WalletSigningError,
  type Deployments,
} from "../src/index.ts";

const PRIVATE_KEY = `0x${"11".repeat(32)}` as const;
const WALLET_ID = "agent-wallet-0001";
const ADDRESS = privateKeyToAccount(PRIVATE_KEY).address;
const SELLER = `0x${"22".repeat(20)}` as const;
const TOKEN = `0x${"33".repeat(20)}` as const;
const DEPLOYMENTS: Deployments = {
  chainId: 1439,
  rpc: "https://rpc.invalid",
  usdc: {
    address: TOKEN,
    symbol: "mUSDC",
    decimals: 6,
    eip712Name: "Mock USD Coin",
    eip712Version: "1",
  },
  wallets: { buyer: ADDRESS, seller: SELLER, facilitator: SELLER },
};

test("wallet envelope encryption round-trips without persisting plaintext", () => {
  const masterKey = randomBytes(32);
  const encrypted = encryptWalletPrivateKey({
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    privateKey: PRIVATE_KEY,
    masterKey,
    keyVersion: 1,
  });

  assert.equal(
    Buffer.concat([
      encrypted.privateKeyCiphertext,
      encrypted.encryptedDataKey,
    ]).includes(Buffer.from(PRIVATE_KEY.slice(2), "hex")),
    false,
  );
  assert.equal(
    decryptWalletPrivateKey({
      ...encrypted,
      walletId: WALLET_ID,
      accountAddress: ADDRESS,
      masterKey,
    }),
    PRIVATE_KEY,
  );
});

test("wrong master keys and tampered ciphertext fail closed", () => {
  const masterKey = randomBytes(32);
  const encrypted = encryptWalletPrivateKey({
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    privateKey: PRIVATE_KEY,
    masterKey,
    keyVersion: 1,
  });

  assert.throws(() =>
    decryptWalletPrivateKey({
      ...encrypted,
      walletId: WALLET_ID,
      accountAddress: ADDRESS,
      masterKey: randomBytes(32),
    }),
  );

  const tampered = Buffer.from(encrypted.privateKeyCiphertext);
  tampered[0] ^= 1;
  assert.throws(() =>
    decryptWalletPrivateKey({
      ...encrypted,
      privateKeyCiphertext: tampered,
      walletId: WALLET_ID,
      accountAddress: ADDRESS,
      masterKey,
    }),
  );
});

test("master-key rotation only rewraps the data key", () => {
  const oldMasterKey = randomBytes(32);
  const newMasterKey = randomBytes(32);
  const encrypted = encryptWalletPrivateKey({
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    privateKey: PRIVATE_KEY,
    masterKey: oldMasterKey,
    keyVersion: 1,
  });
  const rotated = rewrapWalletDataKey({
    ...encrypted,
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    oldMasterKey,
    newMasterKey,
    newKeyVersion: 2,
  });

  assert.deepEqual(rotated.privateKeyCiphertext, encrypted.privateKeyCiphertext);
  assert.deepEqual(rotated.privateKeyNonce, encrypted.privateKeyNonce);
  assert.equal(
    decryptWalletPrivateKey({
      ...rotated,
      walletId: WALLET_ID,
      accountAddress: ADDRESS,
      masterKey: newMasterKey,
    }),
    PRIVATE_KEY,
  );
  assert.throws(() =>
    decryptWalletPrivateKey({
      ...rotated,
      walletId: WALLET_ID,
      accountAddress: ADDRESS,
      masterKey: oldMasterKey,
    }),
  );
});

test("a restarted PostgreSQL signer decrypts on demand and signs for the bound address", async () => {
  const masterKey = randomBytes(32);
  const encrypted = encryptWalletPrivateKey({
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    privateKey: PRIVATE_KEY,
    masterKey,
    keyVersion: 1,
  });
  const row = {
    wallet_id: WALLET_ID,
    account_address: ADDRESS.toLowerCase(),
    private_key_ciphertext: encrypted.privateKeyCiphertext,
    private_key_nonce: encrypted.privateKeyNonce,
    encrypted_data_key: encrypted.encryptedDataKey,
    data_key_nonce: encrypted.dataKeyNonce,
    key_version: 1,
    status: "active",
  };
  const database = {
    async query() {
      return {
        command: "SELECT",
        rowCount: 1,
        oid: 0,
        fields: [],
        rows: [row],
      };
    },
  };
  const signOnce = async () => {
    const restartedStore = new PostgresEncryptedWalletSecretStore(
      database,
      new Map([[1, masterKey]]),
    );
    return signTransferAuthorizationWithWallet(
      {
        walletId: WALLET_ID,
        expectedFrom: ADDRESS,
        to: SELLER,
        value: 7_000_000n,
        dep: DEPLOYMENTS,
        nonce: `0x${"44".repeat(32)}`,
        nowSeconds: 1_784_000_000,
      },
      restartedStore,
    );
  };

  const first = await signOnce();
  const afterRestart = await signOnce();
  assert.equal(first.from, ADDRESS);
  assert.equal(afterRestart.signature, first.signature);
  assert.equal(
    await verifyAuthorizationLocally(afterRestart, DEPLOYMENTS),
    true,
  );
});

test("the PostgreSQL signer rejects ciphertext not matching the stored address", async () => {
  const masterKey = randomBytes(32);
  const encrypted = encryptWalletPrivateKey({
    walletId: WALLET_ID,
    accountAddress: ADDRESS,
    privateKey: PRIVATE_KEY,
    masterKey,
    keyVersion: 1,
  });
  const database = {
    async query() {
      return {
        command: "SELECT",
        rowCount: 1,
        oid: 0,
        fields: [],
        rows: [
          {
            wallet_id: WALLET_ID,
            account_address: `0x${"99".repeat(20)}`,
            private_key_ciphertext: encrypted.privateKeyCiphertext,
            private_key_nonce: encrypted.privateKeyNonce,
            encrypted_data_key: encrypted.encryptedDataKey,
            data_key_nonce: encrypted.dataKeyNonce,
            key_version: 1,
            status: "active",
          },
        ],
      };
    },
  };
  const store = new PostgresEncryptedWalletSecretStore(
    database,
    new Map([[1, masterKey]]),
  );

  await assert.rejects(
    () =>
      signTransferAuthorizationWithWallet(
        {
          walletId: WALLET_ID,
          expectedFrom: `0x${"99".repeat(20)}`,
          to: SELLER,
          value: 7_000_000n,
          dep: DEPLOYMENTS,
          nonce: `0x${"55".repeat(32)}`,
          nowSeconds: 1_784_000_000,
        },
        store,
      ),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "wallet_secret_invalid",
  );
});

test("wallet KEK loading requires an absolute non-symlink 0400 file", async () => {
  const directory = await mkdtemp(join(tmpdir(), "arena402-wallet-kek-"));
  const keyPath = join(directory, "wallet-master.key");
  const linkPath = join(directory, "wallet-master-link.key");
  const expected = randomBytes(32);
  try {
    await writeFile(keyPath, expected, { mode: 0o400 });
    assert.deepEqual(await loadWalletMasterKey(keyPath), expected);

    await chmod(keyPath, 0o600);
    await assert.rejects(
      () => loadWalletMasterKey(keyPath),
      /wallet_master_key_permissions/,
    );

    await chmod(keyPath, 0o400);
    await symlink(keyPath, linkPath);
    await assert.rejects(() => loadWalletMasterKey(linkPath));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
