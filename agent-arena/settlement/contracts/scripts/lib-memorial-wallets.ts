import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import type { Address, Hex } from "viem";

import {
  decryptWalletPrivateKey,
  encryptWalletPrivateKey,
} from "../../sdk/src/postgres-encrypted-wallet-secret-store.ts";

export type MemorialMintStatus = "prepared" | "confirmed";

export interface MemorialWalletRecord {
  tokenId: number;
  walletId: string;
  address: Address;
  privateKeyCiphertext: string;
  privateKeyNonce: string;
  encryptedDataKey: string;
  dataKeyNonce: string;
  keyVersion: number;
  status: MemorialMintStatus;
  txHash?: Hex;
  blockNumber?: number;
}

export interface MemorialWalletVault {
  version: 1;
  chainId: number;
  contractAddress: Address;
  createdAt: string;
  records: MemorialWalletRecord[];
}

export function generateMemorialWalletRecords(params: {
  startTokenId: number;
  count: number;
  masterKey: Buffer;
  keyVersion?: number;
  privateKeyFactory?: () => Hex;
}): MemorialWalletRecord[] {
  const {
    startTokenId,
    count,
    masterKey,
    keyVersion = 1,
    privateKeyFactory = generatePrivateKey,
  } = params;
  if (!Number.isSafeInteger(startTokenId) || startTokenId < 0) {
    throw new Error("memorial_start_token_id_invalid");
  }
  if (!Number.isSafeInteger(count) || count <= 0 || startTokenId + count > 402) {
    throw new Error("memorial_wallet_count_invalid");
  }

  const addresses = new Set<string>();
  const records: MemorialWalletRecord[] = [];
  for (let offset = 0; offset < count; offset += 1) {
    const tokenId = startTokenId + offset;
    const walletId = `memorial-wallet-${String(tokenId).padStart(4, "0")}`;
    const privateKey = privateKeyFactory();
    const account = privateKeyToAccount(privateKey);
    const normalizedAddress = account.address.toLowerCase();
    if (addresses.has(normalizedAddress)) {
      throw new Error("memorial_wallet_duplicate");
    }
    addresses.add(normalizedAddress);
    const encrypted = encryptWalletPrivateKey({
      walletId,
      accountAddress: account.address,
      privateKey,
      masterKey,
      keyVersion,
    });
    records.push({
      tokenId,
      walletId,
      address: account.address,
      privateKeyCiphertext: encrypted.privateKeyCiphertext.toString("base64"),
      privateKeyNonce: encrypted.privateKeyNonce.toString("base64"),
      encryptedDataKey: encrypted.encryptedDataKey.toString("base64"),
      dataKeyNonce: encrypted.dataKeyNonce.toString("base64"),
      keyVersion,
      status: "prepared",
    });
  }
  return records;
}

export function decryptMemorialWalletRecord(
  record: MemorialWalletRecord,
  masterKey: Buffer,
): Hex {
  return decryptWalletPrivateKey({
    walletId: record.walletId,
    accountAddress: record.address,
    masterKey,
    privateKeyCiphertext: Buffer.from(record.privateKeyCiphertext, "base64"),
    privateKeyNonce: Buffer.from(record.privateKeyNonce, "base64"),
    encryptedDataKey: Buffer.from(record.encryptedDataKey, "base64"),
    dataKeyNonce: Buffer.from(record.dataKeyNonce, "base64"),
    keyVersion: record.keyVersion,
  });
}

export function publicMemorialManifest(vault: MemorialWalletVault) {
  return {
    version: vault.version,
    chainId: vault.chainId,
    contractAddress: vault.contractAddress,
    createdAt: vault.createdAt,
    records: vault.records.map((record) => ({
      tokenId: record.tokenId,
      walletId: record.walletId,
      address: record.address,
      status: record.status,
      ...(record.txHash ? { txHash: record.txHash } : {}),
      ...(record.blockNumber === undefined
        ? {}
        : { blockNumber: record.blockNumber }),
    })),
  };
}
