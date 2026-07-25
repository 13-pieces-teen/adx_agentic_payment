import process from "node:process";
import { Pool, type PoolClient } from "pg";

import { loadWalletCsvRecords } from "../src/local-csv-wallet-secret-store.ts";
import {
  encryptWalletPrivateKey,
  loadWalletMasterKey,
} from "../src/postgres-encrypted-wallet-secret-store.ts";

const apply = process.argv.includes("--apply");
const csvPath = option("--csv") ?? process.env.ADX_WALLET_VAULT_IMPORT_CSV_PATH;
if (!csvPath) throw new Error("--csv or ADX_WALLET_VAULT_IMPORT_CSV_PATH is required");
const records = await loadWalletCsvRecords(csvPath);
const chainId = positiveInteger(
  process.env.ADX_WALLET_SIGNER_ALLOWED_CHAIN_ID ?? "1439",
  "ADX_WALLET_SIGNER_ALLOWED_CHAIN_ID",
);
const keyVersion = positiveInteger(
  process.env.ADX_WALLET_MASTER_KEY_VERSION ?? "1",
  "ADX_WALLET_MASTER_KEY_VERSION",
);

if (!apply) {
  process.stdout.write(
    `Validated ${records.length} wallet records for chain ${chainId}; no database changes made.\n`,
  );
  process.exit(0);
}

const databaseUrl = required("ADX_WALLET_VAULT_DATABASE_URL");
const masterKey = await loadWalletMasterKey(
  required("ADX_WALLET_MASTER_KEY_FILE"),
);
const pool = new Pool({
  connectionString: databaseUrl,
  max: 1,
  connectionTimeoutMillis: 5_000,
  application_name: "arena402-wallet-vault-import",
});
let inserted = 0;
let existing = 0;
let client: PoolClient | undefined;
try {
  client = await pool.connect();
  await client.query("BEGIN");
  await client.query("SET LOCAL ROLE adx_wallet_importer");
  for (const record of records) {
    const encrypted = encryptWalletPrivateKey({
      walletId: record.walletId,
      accountAddress: record.accountAddress,
      privateKey: record.privateKey,
      masterKey,
      keyVersion,
    });
    const result = await client.query<{ imported: boolean }>(
      `SELECT wallet_secret_vault.import_wallet_encrypted_secret(
         $1, $2, $3, $4, $5, $6, $7, $8
       ) AS imported`,
      [
        record.walletId,
        chainId,
        record.accountAddress.toLowerCase(),
        encrypted.privateKeyCiphertext,
        encrypted.privateKeyNonce,
        encrypted.encryptedDataKey,
        encrypted.dataKeyNonce,
        encrypted.keyVersion,
      ],
    );
    if (result.rows[0]?.imported) inserted += 1;
    else existing += 1;
  }
  await client.query("COMMIT");
  process.stdout.write(
    `Wallet vault import complete: ${inserted} inserted, ${existing} already present.\n`,
  );
} catch (error) {
  if (client) await client.query("ROLLBACK");
  throw error;
} finally {
  masterKey.fill(0);
  client?.release();
  await pool.end();
}

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  const value = process.argv[index + 1]?.trim();
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a value`);
  }
  return value;
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
