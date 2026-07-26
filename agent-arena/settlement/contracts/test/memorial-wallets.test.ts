import { test } from "node:test";
import assert from "node:assert/strict";
import { privateKeyToAccount } from "viem/accounts";

import {
  decryptMemorialWalletRecord,
  generateMemorialWalletRecords,
  publicMemorialManifest,
  type MemorialWalletVault,
} from "../scripts/lib-memorial-wallets.ts";

const PRIVATE_KEYS = [
  `0x${"11".repeat(32)}`,
  `0x${"22".repeat(32)}`,
] as const;

test("generated memorial wallets are sequential and encrypted", () => {
  const masterKey = Buffer.alloc(32, 7);
  let index = 0;
  const records = generateMemorialWalletRecords({
    startTokenId: 7,
    count: 2,
    masterKey,
    privateKeyFactory: () => PRIVATE_KEYS[index++],
  });

  assert.deepEqual(
    records.map(({ tokenId, walletId }) => ({ tokenId, walletId })),
    [
      { tokenId: 7, walletId: "memorial-wallet-0007" },
      { tokenId: 8, walletId: "memorial-wallet-0008" },
    ],
  );
  assert.equal(records[0].address, privateKeyToAccount(PRIVATE_KEYS[0]).address);
  assert.equal(decryptMemorialWalletRecord(records[0], masterKey), PRIVATE_KEYS[0]);
  assert.equal(JSON.stringify(records).includes(PRIVATE_KEYS[0].slice(2)), false);
});

test("public manifest never includes encrypted wallet material", () => {
  const vault: MemorialWalletVault = {
    version: 1,
    chainId: 1439,
    contractAddress: "0x1111111111111111111111111111111111111111",
    createdAt: "2026-07-26T00:00:00.000Z",
    records: generateMemorialWalletRecords({
      startTokenId: 0,
      count: 1,
      masterKey: Buffer.alloc(32, 9),
      privateKeyFactory: () => PRIVATE_KEYS[0],
    }),
  };

  const manifest = JSON.stringify(publicMemorialManifest(vault));
  assert.equal(manifest.includes("privateKey"), false);
  assert.equal(manifest.includes("encryptedDataKey"), false);
  assert.match(manifest, /memorial-wallet-0000/);
});
