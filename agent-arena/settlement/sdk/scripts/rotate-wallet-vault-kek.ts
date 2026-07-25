import process from "node:process";
import { getAddress } from "viem";
import { Pool } from "pg";

import {
  loadWalletMasterKey,
  rewrapEncryptedDataKey,
} from "../src/postgres-encrypted-wallet-secret-store.ts";

const apply = process.argv.includes("--apply");
const databaseUrl = required("ADX_WALLET_VAULT_DATABASE_URL");
const oldVersion = positiveInteger(
  required("ADX_WALLET_OLD_MASTER_KEY_VERSION"),
  "ADX_WALLET_OLD_MASTER_KEY_VERSION",
);
const newVersion = positiveInteger(
  required("ADX_WALLET_NEW_MASTER_KEY_VERSION"),
  "ADX_WALLET_NEW_MASTER_KEY_VERSION",
);
if (newVersion <= oldVersion) {
  throw new Error("ADX_WALLET_NEW_MASTER_KEY_VERSION must increase");
}

const pool = new Pool({
  connectionString: databaseUrl,
  max: 1,
  connectionTimeoutMillis: 5_000,
  application_name: "arena402-wallet-vault-rotation",
});
const client = await pool.connect();
try {
  await client.query("BEGIN");
  await client.query("SET LOCAL ROLE adx_wallet_importer");
  const result = await client.query<{
    wallet_id: string;
    account_address: string;
    encrypted_data_key: Buffer;
    data_key_nonce: Buffer;
    key_version: number;
  }>(
    "SELECT * FROM wallet_secret_vault.read_wallet_data_key_for_rotation()",
  );
  const eligible = result.rows.filter(
    (row) => Number(row.key_version) === oldVersion,
  );
  if (eligible.length === 0) {
    throw new Error(`no active wallets use key version ${oldVersion}`);
  }
  const unexpected = result.rows.length - eligible.length;
  if (unexpected > 0) {
    throw new Error(
      `${unexpected} active wallets do not use expected key version ${oldVersion}`,
    );
  }
  if (!apply) {
    await client.query("ROLLBACK");
    process.stdout.write(
      `Validated ${eligible.length} wallet data keys for rotation; no database changes made.\n`,
    );
  } else {
    const oldMasterKey = await loadWalletMasterKey(
      required("ADX_WALLET_OLD_MASTER_KEY_FILE"),
    );
    let newMasterKey: Buffer | undefined;
    try {
      newMasterKey = await loadWalletMasterKey(
        required("ADX_WALLET_NEW_MASTER_KEY_FILE"),
      );
      for (const row of eligible) {
        const wrapped = rewrapEncryptedDataKey({
          walletId: row.wallet_id,
          accountAddress: getAddress(row.account_address),
          encryptedDataKey: row.encrypted_data_key,
          dataKeyNonce: row.data_key_nonce,
          keyVersion: oldVersion,
          oldMasterKey,
          newMasterKey,
          newKeyVersion: newVersion,
        });
        await client.query(
          `SELECT wallet_secret_vault.rotate_wallet_data_key(
             $1, $2, $3, $4, $5
           )`,
          [
            row.wallet_id,
            oldVersion,
            wrapped.encryptedDataKey,
            wrapped.dataKeyNonce,
            newVersion,
          ],
        );
      }
      await client.query("COMMIT");
      process.stdout.write(
        `Wallet KEK rotation complete: ${eligible.length} data keys moved to version ${newVersion}.\n`,
      );
    } finally {
      oldMasterKey.fill(0);
      newMasterKey?.fill(0);
    }
  }
} catch (error) {
  await client.query("ROLLBACK");
  throw error;
} finally {
  client.release();
  await pool.end();
}

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function positiveInteger(raw: string, name: string): number {
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}
