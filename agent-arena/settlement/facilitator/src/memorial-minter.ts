import { createHash } from "node:crypto";
import { writeFile } from "node:fs/promises";
import {
  createPublicClient,
  encodeFunctionData,
  getAddress,
  http,
  keccak256,
  type Hex,
  type TransactionSerialized,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import pg from "pg";

import { loadFacilitatorPrivateKey } from "./facilitator-csv.ts";

const { Pool } = pg;

const CAMPAIGN = "arena402-genesis";
const MEMORIAL_ABI = [
  {
    type: "function",
    name: "nextTokenId",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "owner",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "address" }],
  },
  {
    type: "function",
    name: "mintBatch",
    stateMutability: "nonpayable",
    inputs: [{ name: "recipients", type: "address[]" }],
    outputs: [{ name: "firstTokenId", type: "uint256" }],
  },
] as const;

type AwardRow = {
  token_id: number;
  wallet_id: string;
  wallet_address: string;
};

type BatchRow = {
  batch_id: string;
  first_token_id: number;
  last_token_id: number;
  address_digest: string;
  status: "prepared" | "submitted";
  tx_hash: Hex | null;
  tx_nonce: string | null;
  gas_limit: string | null;
  gas_price_wei: string | null;
};

const databaseUrl = required("ADX_MEMORIAL_MINTER_DATABASE_URL");
const rpcUrl = required("ADX_ARENA_SETTLEMENT_RPC_URL");
const contract = getAddress(required("ADX_MEMORIAL_CONTRACT_ADDRESS"));
const pollMs = positiveNumber("ADX_MEMORIAL_MINTER_POLL_SECONDS", 2) * 1_000;
const batchSize = Math.min(40, positiveNumber("ADX_MEMORIAL_MINTER_BATCH_SIZE", 1));
const chainId = positiveNumber("ADX_CURRENT_GAME_CHAIN_ID", 1439);
const key = await loadFacilitatorPrivateKey(
  required("ADX_FACILITATOR_CSV_PATH"),
  required("ADX_FACILITATOR_WALLET_INDEX", "1"),
);
const account = privateKeyToAccount(key);
const chain = {
  id: chainId,
  name: "Injective EVM Testnet",
  nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
  rpcUrls: { default: { http: [rpcUrl] } },
} as const;
const publicClient = createPublicClient({
  chain,
  transport: http(rpcUrl, { timeout: 30_000, retryCount: 3 }),
});
const readContract = publicClient.readContract as unknown as (
  parameters: Record<string, unknown>,
) => Promise<unknown>;
const pool = new Pool({ connectionString: databaseUrl, max: 2 });
const lockClient = await pool.connect();

if (
  getAddress(await readContract({
    address: contract,
    abi: MEMORIAL_ABI,
    functionName: "owner",
  }) as string) !== getAddress(account.address)
) {
  throw new Error("memorial_minter_not_contract_owner");
}
const locked = await lockClient.query<{ locked: boolean }>(
  "SELECT pg_try_advisory_lock(hashtext($1)) AS locked",
  ["arena402-memorial-minter"],
);
if (!locked.rows[0]?.locked) throw new Error("memorial_minter_lock_unavailable");
await writeFile("/tmp/memorial-minter-ready", "ready\n");
console.log(
  `arena402_memorial_minter_ready account=${account.address} batch_size=${batchSize}`,
);

while (true) {
  try {
    await tick();
  } catch (error) {
    console.error(`arena402_memorial_minter_error code=${safeCode(error)}`);
  }
  await delay(pollMs);
}

async function tick(): Promise<void> {
  const active = await pool.query<{ status: string }>(
    "SELECT status FROM arena402.memorial_campaigns WHERE campaign_id = $1",
    [CAMPAIGN],
  );
  if (!["active", "minting"].includes(active.rows[0]?.status ?? "")) return;

  let batch = await currentBatch();
  if (!batch) batch = await prepareBatch();
  if (!batch) return;

  const awards = await awardsFor(batch);
  if (awards.length !== batch.last_token_id - batch.first_token_id + 1) {
    throw new Error("memorial_batch_award_count_mismatch");
  }
  if (batch.status === "prepared") batch = await submitBatch(batch, awards);
  await reconcileSubmitted(batch, awards);
}

async function currentBatch(): Promise<BatchRow | null> {
  const result = await pool.query<BatchRow>(
    `SELECT batch_id, first_token_id, last_token_id, address_digest, status,
            tx_hash, tx_nonce, gas_limit, gas_price_wei
       FROM arena402.memorial_mint_batches
      WHERE campaign_id = $1 AND status IN ('prepared', 'submitted')
      ORDER BY first_token_id
      LIMIT 1`,
    [CAMPAIGN],
  );
  return result.rows[0] ?? null;
}

async function prepareBatch(): Promise<BatchRow | null> {
  const nextTokenId = Number(await readContract({
    address: contract,
    abi: MEMORIAL_ABI,
    functionName: "nextTokenId",
  }));
  const result = await pool.query<AwardRow>(
    `SELECT token_id, wallet_id, wallet_address
       FROM arena402.memorial_awards
      WHERE campaign_id = $1
        AND eligibility_status = 'reserved'
        AND mint_status = 'reserved'
        AND token_id >= $2
      ORDER BY token_id
      LIMIT $3`,
    [CAMPAIGN, nextTokenId, batchSize],
  );
  if (!result.rows.length) return null;
  result.rows.forEach((row, index) => {
    if (row.token_id !== nextTokenId + index) {
      throw new Error("memorial_awards_not_contiguous");
    }
  });
  const last = result.rows.at(-1)!.token_id;
  const digest = addressDigest(result.rows);
  const batchId = `live-${nextTokenId.toString().padStart(3, "0")}-${last
    .toString()
    .padStart(3, "0")}`;
  await pool.query(
    `INSERT INTO arena402.memorial_mint_batches (
       batch_id, campaign_id, first_token_id, last_token_id, address_digest
     ) VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (campaign_id, first_token_id, last_token_id) DO NOTHING`,
    [batchId, CAMPAIGN, nextTokenId, last, digest],
  );
  return currentBatch();
}

async function submitBatch(
  batch: BatchRow,
  awards: AwardRow[],
): Promise<BatchRow> {
  if (addressDigest(awards) !== batch.address_digest) {
    throw new Error("memorial_batch_address_digest_mismatch");
  }
  const recipients = awards.map((row) => getAddress(row.wallet_address));
  const data = encodeFunctionData({
    abi: MEMORIAL_ABI,
    functionName: "mintBatch",
    args: [recipients],
  });
  const nonce = await publicClient.getTransactionCount({
    address: account.address,
    blockTag: "pending",
  });
  const gasPrice = (await publicClient.getGasPrice()) * 3n;
  const estimate = await publicClient.estimateGas({
    account: account.address,
    to: contract,
    data,
  });
  const gas = (estimate * 120n) / 100n;
  const serialized = await account.signTransaction({
    chainId,
    to: contract,
    data,
    gas,
    gasPrice,
    nonce,
    type: "legacy",
  });
  const hash = keccak256(serialized);

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      `UPDATE arena402.memorial_mint_batches
          SET status = 'submitted', tx_hash = $2, tx_nonce = $3,
              gas_limit = $4, gas_price_wei = $5,
              submitted_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE batch_id = $1 AND status = 'prepared'`,
      [batch.batch_id, hash, nonce, gas.toString(), gasPrice.toString()],
    );
    await client.query(
      `UPDATE arena402.memorial_awards
          SET mint_status = 'submitted', mint_tx_hash = $4,
              submitted_at = clock_timestamp(), last_error = NULL
        WHERE campaign_id = $1 AND token_id BETWEEN $2 AND $3
          AND mint_status = 'reserved'`,
      [CAMPAIGN, batch.first_token_id, batch.last_token_id, hash],
    );
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
  await broadcast(serialized, hash);
  console.log(
    `arena402_memorial_mint_submitted batch=${batch.batch_id} tx=${hash}`,
  );
  return {
    ...batch,
    status: "submitted",
    tx_hash: hash,
    tx_nonce: String(nonce),
    gas_limit: gas.toString(),
    gas_price_wei: gasPrice.toString(),
  };
}

async function reconcileSubmitted(
  batch: BatchRow,
  awards: AwardRow[],
): Promise<void> {
  if (
    !batch.tx_hash ||
    batch.tx_nonce === null ||
    batch.gas_limit === null ||
    batch.gas_price_wei === null
  ) {
    throw new Error("memorial_submitted_transaction_incomplete");
  }
  let receipt;
  try {
    receipt = await publicClient.getTransactionReceipt({ hash: batch.tx_hash });
  } catch {
    const data = encodeFunctionData({
      abi: MEMORIAL_ABI,
      functionName: "mintBatch",
      args: [awards.map((row) => getAddress(row.wallet_address))],
    });
    const serialized = await account.signTransaction({
      chainId,
      to: contract,
      data,
      gas: BigInt(batch.gas_limit),
      gasPrice: BigInt(batch.gas_price_wei),
      nonce: Number(batch.tx_nonce),
      type: "legacy",
    });
    if (keccak256(serialized) !== batch.tx_hash) {
      throw new Error("memorial_transaction_hash_mismatch");
    }
    await broadcast(serialized, batch.tx_hash);
    return;
  }
  if (receipt.status !== "success") {
    await resetFailedBatch(batch, "transaction_reverted");
    return;
  }
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const awardUpdate = await client.query(
      `UPDATE arena402.memorial_awards
          SET mint_status = 'minted', mint_block_number = $4,
              minted_at = clock_timestamp(), last_error = NULL
        WHERE campaign_id = $1 AND token_id BETWEEN $2 AND $3
          AND mint_status = 'submitted'`,
      [CAMPAIGN, batch.first_token_id, batch.last_token_id, Number(receipt.blockNumber)],
    );
    if (awardUpdate.rowCount !== awards.length) {
      throw new Error("memorial_award_confirmation_conflict");
    }
    await client.query(
      `UPDATE arena402.memorial_wallet_inventory
          SET status = 'minted', updated_at = clock_timestamp()
        WHERE campaign_id = $1 AND token_id BETWEEN $2 AND $3`,
      [CAMPAIGN, batch.first_token_id, batch.last_token_id],
    );
    await client.query(
      `UPDATE arena402.memorial_mint_batches
          SET status = 'confirmed', block_number = $2,
              confirmed_at = clock_timestamp(), updated_at = clock_timestamp(),
              last_error = NULL
        WHERE batch_id = $1 AND status = 'submitted'`,
      [batch.batch_id, Number(receipt.blockNumber)],
    );
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
  console.log(
    `arena402_memorial_mint_confirmed batch=${batch.batch_id} block=${receipt.blockNumber}`,
  );
}

async function resetFailedBatch(batch: BatchRow, reason: string): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      `UPDATE arena402.memorial_awards
          SET mint_status = 'reserved', mint_tx_hash = NULL,
              submitted_at = NULL, last_error = $4
        WHERE campaign_id = $1 AND token_id BETWEEN $2 AND $3`,
      [CAMPAIGN, batch.first_token_id, batch.last_token_id, reason],
    );
    await client.query(
      `UPDATE arena402.memorial_mint_batches
          SET status = 'prepared', tx_hash = NULL, tx_nonce = NULL,
              gas_limit = NULL, gas_price_wei = NULL, submitted_at = NULL,
              updated_at = clock_timestamp(), last_error = $2
        WHERE batch_id = $1`,
      [batch.batch_id, reason],
    );
    await client.query("COMMIT");
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

async function awardsFor(batch: BatchRow): Promise<AwardRow[]> {
  const result = await pool.query<AwardRow>(
    `SELECT token_id, wallet_id, wallet_address
       FROM arena402.memorial_awards
      WHERE campaign_id = $1 AND token_id BETWEEN $2 AND $3
      ORDER BY token_id`,
    [CAMPAIGN, batch.first_token_id, batch.last_token_id],
  );
  return result.rows;
}

async function broadcast(
  serialized: Hex,
  expectedHash: Hex,
): Promise<void> {
  try {
    const actual = await publicClient.sendRawTransaction({
      serializedTransaction: serialized as TransactionSerialized,
    });
    if (actual !== expectedHash) throw new Error("memorial_broadcast_hash_mismatch");
  } catch (error) {
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    if (!message.includes("already known") && !message.includes("nonce too low")) {
      throw error;
    }
  }
}

function addressDigest(rows: AwardRow[]): string {
  const addresses = rows.map((row) => row.wallet_address.toLowerCase());
  return `sha256:${createHash("sha256").update(JSON.stringify(addresses)).digest("hex")}`;
}

function required(name: string, fallback?: string): string {
  const value = process.env[name]?.trim() || fallback;
  if (!value) throw new Error(`${name}_required`);
  return value;
}

function positiveNumber(name: string, fallback: number): number {
  const value = Number(process.env[name] || fallback);
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${name}_invalid`);
  return value;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function safeCode(error: unknown): string {
  if (!(error instanceof Error)) return "unknown";
  return error.message.replace(/[^a-zA-Z0-9_.:-]/g, "_").slice(0, 160);
}
