import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { getAddress, type Address, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import type { Pool, QueryResult, QueryResultRow } from "pg";

import {
  EIP3009_TRANSFER_TYPES,
  WalletSigningError,
  type SignEip3009AuthorizationRequest,
  type WalletAuthorizationSignature,
  type WalletSecretStore,
} from "./wallet-secret-store.ts";

const AES_GCM_NONCE_BYTES = 12;
const AES_GCM_TAG_BYTES = 16;
const KEY_BYTES = 32;
const PRIVATE_AAD_PREFIX = "arena402:wallet-private-key:aesgcm:v1";
const DATA_KEY_AAD_PREFIX = "arena402:wallet-dek:aesgcm:v1";

export interface EncryptedWalletMaterial {
  privateKeyCiphertext: Buffer;
  privateKeyNonce: Buffer;
  encryptedDataKey: Buffer;
  dataKeyNonce: Buffer;
  keyVersion: number;
}

export interface WrappedWalletDataKey {
  encryptedDataKey: Buffer;
  dataKeyNonce: Buffer;
  keyVersion: number;
}

interface EncryptWalletPrivateKeyParams {
  walletId: string;
  accountAddress: Address;
  privateKey: Hex;
  masterKey: Buffer;
  keyVersion: number;
}

interface DecryptWalletPrivateKeyParams extends EncryptedWalletMaterial {
  walletId: string;
  accountAddress: Address;
  masterKey: Buffer;
}

interface RewrapWalletDataKeyParams extends EncryptedWalletMaterial {
  walletId: string;
  accountAddress: Address;
  oldMasterKey: Buffer;
  newMasterKey: Buffer;
  newKeyVersion: number;
}

interface StoredWalletRow extends QueryResultRow {
  wallet_id: string;
  account_address: string;
  private_key_ciphertext: Buffer;
  private_key_nonce: Buffer;
  encrypted_data_key: Buffer;
  data_key_nonce: Buffer;
  key_version: number;
  status: string;
}

export interface WalletVaultQuery {
  query<R extends QueryResultRow = QueryResultRow>(
    text: string,
    values?: unknown[],
  ): Promise<QueryResult<R>>;
}

export function encryptWalletPrivateKey(
  params: EncryptWalletPrivateKeyParams,
): EncryptedWalletMaterial {
  validateIdentity(params.walletId, params.accountAddress);
  validateMasterKey(params.masterKey);
  validateKeyVersion(params.keyVersion);
  if (!/^0x[0-9a-fA-F]{64}$/.test(params.privateKey)) {
    throw new Error("wallet_private_key_invalid");
  }

  const rawPrivateKey = Buffer.from(params.privateKey.slice(2), "hex");
  const dataKey = randomBytes(KEY_BYTES);
  try {
    const privateKeyNonce = randomBytes(AES_GCM_NONCE_BYTES);
    const privateKeyCiphertext = seal(
      rawPrivateKey,
      dataKey,
      privateKeyNonce,
      privateAad(params.walletId, params.accountAddress),
    );
    const dataKeyNonce = randomBytes(AES_GCM_NONCE_BYTES);
    const encryptedDataKey = seal(
      dataKey,
      params.masterKey,
      dataKeyNonce,
      dataKeyAad(
        params.walletId,
        params.accountAddress,
        params.keyVersion,
      ),
    );
    return {
      privateKeyCiphertext,
      privateKeyNonce,
      encryptedDataKey,
      dataKeyNonce,
      keyVersion: params.keyVersion,
    };
  } finally {
    rawPrivateKey.fill(0);
    dataKey.fill(0);
  }
}

export function decryptWalletPrivateKey(
  params: DecryptWalletPrivateKeyParams,
): Hex {
  validateEncryptedMaterial(params);
  validateMasterKey(params.masterKey);
  const dataKey = unseal(
    params.encryptedDataKey,
    params.masterKey,
    params.dataKeyNonce,
    dataKeyAad(params.walletId, params.accountAddress, params.keyVersion),
  );
  try {
    const rawPrivateKey = unseal(
      params.privateKeyCiphertext,
      dataKey,
      params.privateKeyNonce,
      privateAad(params.walletId, params.accountAddress),
    );
    try {
      if (rawPrivateKey.length !== KEY_BYTES) {
        throw new Error("wallet_private_key_invalid");
      }
      return `0x${rawPrivateKey.toString("hex")}`;
    } finally {
      rawPrivateKey.fill(0);
    }
  } finally {
    dataKey.fill(0);
  }
}

export function rewrapWalletDataKey(
  params: RewrapWalletDataKeyParams,
): EncryptedWalletMaterial {
  validateEncryptedMaterial(params);
  const wrapped = rewrapEncryptedDataKey(params);
  return {
    privateKeyCiphertext: Buffer.from(params.privateKeyCiphertext),
    privateKeyNonce: Buffer.from(params.privateKeyNonce),
    ...wrapped,
  };
}

export function rewrapEncryptedDataKey(params: {
  walletId: string;
  accountAddress: Address;
  encryptedDataKey: Buffer;
  dataKeyNonce: Buffer;
  keyVersion: number;
  oldMasterKey: Buffer;
  newMasterKey: Buffer;
  newKeyVersion: number;
}): WrappedWalletDataKey {
  validateIdentity(params.walletId, params.accountAddress);
  validateKeyVersion(params.keyVersion);
  validateMasterKey(params.oldMasterKey);
  validateMasterKey(params.newMasterKey);
  validateKeyVersion(params.newKeyVersion);
  if (
    params.dataKeyNonce.length !== AES_GCM_NONCE_BYTES ||
    params.encryptedDataKey.length !== KEY_BYTES + AES_GCM_TAG_BYTES
  ) {
    throw new Error("wallet_ciphertext_invalid");
  }
  const dataKey = unseal(
    params.encryptedDataKey,
    params.oldMasterKey,
    params.dataKeyNonce,
    dataKeyAad(params.walletId, params.accountAddress, params.keyVersion),
  );
  try {
    const dataKeyNonce = randomBytes(AES_GCM_NONCE_BYTES);
    return {
      encryptedDataKey: seal(
        dataKey,
        params.newMasterKey,
        dataKeyNonce,
        dataKeyAad(
          params.walletId,
          params.accountAddress,
          params.newKeyVersion,
        ),
      ),
      dataKeyNonce,
      keyVersion: params.newKeyVersion,
    };
  } finally {
    dataKey.fill(0);
  }
}

export async function loadWalletMasterKey(path: string): Promise<Buffer> {
  if (!isAbsolute(path)) throw new Error("wallet_master_key_path_invalid");
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const metadata = await handle.stat();
    if (!metadata.isFile()) throw new Error("wallet_master_key_not_regular");
    if ((metadata.mode & 0o077) !== 0 || (metadata.mode & 0o200) !== 0) {
      throw new Error("wallet_master_key_permissions");
    }
    const key = await handle.readFile();
    validateMasterKey(key);
    return key;
  } finally {
    await handle.close();
  }
}

export class PostgresEncryptedWalletSecretStore
  implements WalletSecretStore
{
  constructor(
    private readonly database: WalletVaultQuery,
    private readonly masterKeys: ReadonlyMap<number, Buffer>,
  ) {}

  async checkHealth(): Promise<void> {
    await this.database.query("SELECT 1");
  }

  static async create(params: {
    connectionString: string;
    masterKeyFile: string;
    keyVersion: number;
  }): Promise<PostgresEncryptedWalletSecretStore & { close(): Promise<void> }> {
    const { Pool } = await import("pg");
    const pool = new Pool({
      connectionString: params.connectionString,
      max: 5,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 5_000,
      application_name: "arena402-wallet-signer",
    });
    const masterKey = await loadWalletMasterKey(params.masterKeyFile);
    const store = new PostgresEncryptedWalletSecretStore(
      pool,
      new Map([[params.keyVersion, masterKey]]),
    );
    return Object.assign(store, {
      close: async () => {
        masterKey.fill(0);
        await pool.end();
      },
    });
  }

  async signEip3009Authorization(
    request: SignEip3009AuthorizationRequest,
  ): Promise<WalletAuthorizationSignature> {
    let result: QueryResult<StoredWalletRow>;
    try {
      result = await this.database.query<StoredWalletRow>(
        "SELECT * FROM wallet_secret_vault.read_wallet_encrypted_secret($1)",
        [request.walletId],
      );
    } catch {
      throw new WalletSigningError("wallet_backend_unavailable");
    }
    if (result.rowCount !== 1) {
      throw new WalletSigningError("wallet_not_found");
    }
    const row = result.rows[0];
    if (row.status !== "active") {
      throw new WalletSigningError("wallet_disabled");
    }
    let address: Address;
    try {
      address = getAddress(row.account_address);
    } catch {
      throw new WalletSigningError("wallet_secret_invalid");
    }
    if (address !== getAddress(request.expectedFrom)) {
      throw new WalletSigningError("wallet_address_mismatch");
    }
    const masterKey = this.masterKeys.get(Number(row.key_version));
    if (!masterKey) {
      throw new WalletSigningError("wallet_secret_invalid");
    }

    try {
      const privateKey = decryptWalletPrivateKey({
        walletId: row.wallet_id,
        accountAddress: address,
        privateKeyCiphertext: row.private_key_ciphertext,
        privateKeyNonce: row.private_key_nonce,
        encryptedDataKey: row.encrypted_data_key,
        dataKeyNonce: row.data_key_nonce,
        keyVersion: Number(row.key_version),
        masterKey,
      });
      const account = privateKeyToAccount(privateKey);
      if (getAddress(account.address) !== address) {
        throw new Error("wallet_address_mismatch");
      }
      const signature = await account.signTypedData({
        domain: request.domain,
        types: EIP3009_TRANSFER_TYPES,
        primaryType: "TransferWithAuthorization",
        message: {
          from: address,
          to: request.to,
          value: request.value,
          validAfter: request.validAfter,
          validBefore: request.validBefore,
          nonce: request.nonce,
        },
      });
      return { from: address, signature };
    } catch (error) {
      if (error instanceof WalletSigningError) throw error;
      throw new WalletSigningError("wallet_secret_invalid");
    }
  }
}

function seal(
  plaintext: Buffer,
  key: Buffer,
  nonce: Buffer,
  aad: Buffer,
): Buffer {
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(aad);
  return Buffer.concat([cipher.update(plaintext), cipher.final(), cipher.getAuthTag()]);
}

function unseal(
  ciphertextAndTag: Buffer,
  key: Buffer,
  nonce: Buffer,
  aad: Buffer,
): Buffer {
  if (ciphertextAndTag.length <= AES_GCM_TAG_BYTES) {
    throw new Error("wallet_ciphertext_invalid");
  }
  const ciphertext = ciphertextAndTag.subarray(0, -AES_GCM_TAG_BYTES);
  const tag = ciphertextAndTag.subarray(-AES_GCM_TAG_BYTES);
  const decipher = createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAAD(aad);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

function privateAad(walletId: string, accountAddress: Address): Buffer {
  return Buffer.from(
    `${PRIVATE_AAD_PREFIX}\0${walletId}\0${accountAddress.toLowerCase()}`,
    "utf8",
  );
}

function dataKeyAad(
  walletId: string,
  accountAddress: Address,
  keyVersion: number,
): Buffer {
  return Buffer.from(
    `${DATA_KEY_AAD_PREFIX}\0${keyVersion}\0${walletId}\0${accountAddress.toLowerCase()}`,
    "utf8",
  );
}

function validateIdentity(walletId: string, accountAddress: Address): void {
  if (!walletId || walletId.length > 128) throw new Error("wallet_id_invalid");
  getAddress(accountAddress);
}

function validateMasterKey(masterKey: Buffer): void {
  if (masterKey.length !== KEY_BYTES) throw new Error("wallet_master_key_invalid");
}

function validateKeyVersion(keyVersion: number): void {
  if (!Number.isSafeInteger(keyVersion) || keyVersion <= 0) {
    throw new Error("wallet_key_version_invalid");
  }
}

function validateEncryptedMaterial(
  params: DecryptWalletPrivateKeyParams | RewrapWalletDataKeyParams,
): void {
  validateIdentity(params.walletId, params.accountAddress);
  validateKeyVersion(params.keyVersion);
  if (
    params.privateKeyNonce.length !== AES_GCM_NONCE_BYTES ||
    params.dataKeyNonce.length !== AES_GCM_NONCE_BYTES ||
    params.privateKeyCiphertext.length !== KEY_BYTES + AES_GCM_TAG_BYTES ||
    params.encryptedDataKey.length !== KEY_BYTES + AES_GCM_TAG_BYTES
  ) {
    throw new Error("wallet_ciphertext_invalid");
  }
}
