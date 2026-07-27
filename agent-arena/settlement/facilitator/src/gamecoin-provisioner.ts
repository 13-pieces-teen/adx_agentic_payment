import { writeFile } from "node:fs/promises";
import {
  createPublicClient,
  encodeFunctionData,
  getAddress,
  http,
  keccak256,
  type Hex,
  type TransactionSerialized,
  zeroAddress,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import pg from "pg";

import { loadFacilitatorPrivateKey } from "./facilitator-csv.ts";
import {
  findMatchingMintEvidence,
  type MintEventEvidence,
} from "./gamecoin-recovery.ts";
import { waitViaBlockscout } from "./lib-tx.ts";

const { Pool } = pg;

const GAME_COIN_ABI = [
  {
    type: "function",
    name: "owner",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "address" }],
  },
  {
    type: "function",
    name: "whitelisted",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ type: "bool" }],
  },
  {
    type: "function",
    name: "balanceOf",
    stateMutability: "view",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "addToWhitelist",
    stateMutability: "nonpayable",
    inputs: [{ name: "account", type: "address" }],
    outputs: [],
  },
  {
    type: "function",
    name: "mint",
    stateMutability: "nonpayable",
    inputs: [
      { name: "to", type: "address" },
      { name: "amount", type: "uint256" },
    ],
    outputs: [],
  },
  {
    type: "event",
    name: "Transfer",
    inputs: [
      { name: "from", type: "address", indexed: true },
      { name: "to", type: "address", indexed: true },
      { name: "value", type: "uint256", indexed: false },
    ],
  },
] as const;

type ProvisionStatus =
  | "pending"
  | "whitelist_submitted"
  | "mint_submitted";

type ProvisionRow = {
  provision_id: string;
  chain_id: string;
  token_address: string;
  account_address: string;
  amount_atomic: string;
  balance_before_atomic: string | null;
  status: ProvisionStatus;
  whitelist_tx_hash: Hex | null;
  whitelist_tx_nonce: string | null;
  whitelist_gas_limit: string | null;
  whitelist_gas_price_wei: string | null;
  mint_tx_hash: Hex | null;
  mint_tx_nonce: string | null;
  mint_gas_limit: string | null;
  mint_gas_price_wei: string | null;
};

type SubmittedKind = "whitelist" | "mint";

const databaseUrl = required("ADX_GAMECOIN_PROVISIONER_DATABASE_URL");
const rpcUrl = required("ADX_ARENA_SETTLEMENT_RPC_URL");
const token = getAddress(required("ADX_CURRENT_GAME_TOKEN_ADDRESS"));
const chainId = positiveNumber("ADX_CURRENT_GAME_CHAIN_ID", 1439);
const pollMs =
  positiveNumber("ADX_GAMECOIN_PROVISIONER_POLL_SECONDS", 2) * 1_000;
const requiredConfirmations = positiveNumber(
  "ADX_GAMECOIN_REQUIRED_CONFIRMATIONS",
  2,
);
const key = await loadFacilitatorPrivateKey(
  required("ADX_FACILITATOR_CSV_PATH"),
  required("ADX_GAMECOIN_OWNER_WALLET_INDEX", "1"),
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
  getAddress(
    (await readContract({
      address: token,
      abi: GAME_COIN_ABI,
      functionName: "owner",
    })) as string,
  ) !== getAddress(account.address)
) {
  throw new Error("gamecoin_provisioner_not_contract_owner");
}
const locked = await lockClient.query<{ locked: boolean }>(
  "SELECT pg_try_advisory_lock(hashtext($1)) AS locked",
  ["arena402-gamecoin-provisioner"],
);
if (!locked.rows[0]?.locked) {
  throw new Error("gamecoin_provisioner_lock_unavailable");
}
await writeFile("/tmp/gamecoin-provisioner-ready", "ready\n");
console.log(
  `arena402_gamecoin_provisioner_ready account=${account.address} token=${token}`,
);

while (true) {
  try {
    await tick();
  } catch (error) {
    console.error(`arena402_gamecoin_provisioner_error code=${safeCode(error)}`);
  }
  await delay(pollMs);
}

async function tick(): Promise<void> {
  const provision = await currentProvision();
  if (!provision) return;
  validateFrozenScope(provision);

  if (provision.status === "whitelist_submitted") {
    await reconcileSubmitted(provision, "whitelist");
    return;
  }
  if (provision.status === "mint_submitted") {
    await reconcileSubmitted(provision, "mint");
    return;
  }

  const target = getAddress(provision.account_address);
  const whitelisted = Boolean(
    await readContract({
      address: token,
      abi: GAME_COIN_ABI,
      functionName: "whitelisted",
      args: [target],
    }),
  );
  if (!whitelisted) {
    await submit(provision, "whitelist");
    return;
  }

  let balanceBefore = provision.balance_before_atomic;
  if (balanceBefore === null) {
    balanceBefore = String(await balanceOf(target));
    await pool.query(
      `UPDATE arena402.game_coin_provisions
          SET balance_before_atomic = $2, updated_at = clock_timestamp()
        WHERE provision_id = $1
          AND status = 'pending'
          AND balance_before_atomic IS NULL`,
      [provision.provision_id, balanceBefore],
    );
  }
  if (BigInt(provision.amount_atomic) === 0n) {
    await confirmProvision(provision.provision_id, null);
    return;
  }
  await submit(
    { ...provision, balance_before_atomic: balanceBefore },
    "mint",
  );
}

async function currentProvision(): Promise<ProvisionRow | null> {
  const result = await pool.query<ProvisionRow>(
    `SELECT provision.provision_id, provision.chain_id,
            provision.token_address, provision.account_address,
            provision.amount_atomic, provision.balance_before_atomic,
            provision.status, provision.whitelist_tx_hash,
            provision.whitelist_tx_nonce, provision.whitelist_gas_limit,
            provision.whitelist_gas_price_wei, provision.mint_tx_hash,
            provision.mint_tx_nonce, provision.mint_gas_limit,
            provision.mint_gas_price_wei
       FROM arena402.game_coin_provisions AS provision
       JOIN arena402.current_game AS pointer
         ON pointer.singleton AND pointer.game_id = provision.game_id
       JOIN arena402.games AS game ON game.game_id = provision.game_id
      WHERE provision.status IN (
                'pending', 'whitelist_submitted', 'mint_submitted'
            )
        AND game.phase IN ('registration', 'portfolio_setup')
      ORDER BY provision.created_at, provision.provision_id
      LIMIT 1`,
  );
  return result.rows[0] ?? null;
}

async function submit(
  provision: ProvisionRow,
  kind: SubmittedKind,
): Promise<void> {
  const target = getAddress(provision.account_address);
  if (kind === "mint" && provision.balance_before_atomic === null) {
    throw new Error("gamecoin_balance_baseline_missing");
  }
  const data =
    kind === "whitelist"
      ? encodeFunctionData({
          abi: GAME_COIN_ABI,
          functionName: "addToWhitelist",
          args: [target],
        })
      : encodeFunctionData({
          abi: GAME_COIN_ABI,
          functionName: "mint",
          args: [target, BigInt(provision.amount_atomic)],
        });
  const nonce = await publicClient.getTransactionCount({
    address: account.address,
    blockTag: "pending",
  });
  const gasPrice = (await publicClient.getGasPrice()) * 3n;
  const estimate = await publicClient.estimateGas({
    account: account.address,
    to: token,
    data,
  });
  const gas = (estimate * 120n) / 100n;
  const serialized = await account.signTransaction({
    chainId,
    to: token,
    data,
    gas,
    gasPrice,
    nonce,
    type: "legacy",
  });
  const hash = keccak256(serialized);
  const prefix = kind === "whitelist" ? "whitelist" : "mint";
  const nextStatus =
    kind === "whitelist" ? "whitelist_submitted" : "mint_submitted";
  const result = await pool.query(
    `UPDATE arena402.game_coin_provisions
        SET status = $2,
            ${prefix}_tx_hash = $3,
            ${prefix}_tx_nonce = $4,
            ${prefix}_gas_limit = $5,
            ${prefix}_gas_price_wei = $6,
            submitted_at = clock_timestamp(),
            updated_at = clock_timestamp(),
            last_error = NULL
      WHERE provision_id = $1 AND status = 'pending'`,
    [
      provision.provision_id,
      nextStatus,
      hash,
      nonce,
      gas.toString(),
      gasPrice.toString(),
    ],
  );
  if (result.rowCount !== 1) return;
  await broadcast(serialized, hash);
  console.log(
    `arena402_gamecoin_${kind}_submitted provision=${provision.provision_id} tx=${hash}`,
  );
}

async function reconcileSubmitted(
  provision: ProvisionRow,
  kind: SubmittedKind,
): Promise<void> {
  const submitted = submittedFields(provision, kind);
  let receipt: { status: "success" | "reverted"; blockNumber: bigint };
  try {
    const rpcReceipt = await publicClient.getTransactionReceipt({
      hash: submitted.hash,
    });
    receipt = {
      status: rpcReceipt.status,
      blockNumber: rpcReceipt.blockNumber,
    };
  } catch {
    const recoveredMintBlock =
      kind === "mint"
        ? await recentMintEventBlock(provision, submitted.hash)
        : null;
    if (recoveredMintBlock !== null) {
      receipt = {
        status: "success",
        blockNumber: recoveredMintBlock,
      };
    } else {
      const indexed = await waitViaBlockscout(submitted.hash, 15_000);
      if (indexed.status === "pending") {
        const serialized = await recreateTransaction(
          provision,
          kind,
          submitted,
        );
        await broadcast(serialized, submitted.hash);
        return;
      }
      if (indexed.blockNumber === undefined) {
        throw new Error(`gamecoin_${kind}_block_number_missing`);
      }
      receipt = {
        status: indexed.status === "success" ? "success" : "reverted",
        blockNumber: BigInt(indexed.blockNumber),
      };
    }
  }
  if (receipt.status !== "success") {
    await failProvision(provision.provision_id, `${kind}_transaction_reverted`);
    return;
  }
  const latestBlock = await publicClient.getBlockNumber();
  if (
    latestBlock + 1n <
    receipt.blockNumber + BigInt(requiredConfirmations)
  ) {
    return;
  }
  if (kind === "whitelist") {
    const whitelisted = Boolean(
      await readContract({
        address: token,
        abi: GAME_COIN_ABI,
        functionName: "whitelisted",
        args: [getAddress(provision.account_address)],
      }),
    );
    if (!whitelisted) {
      await failProvision(provision.provision_id, "whitelist_state_mismatch");
      return;
    }
    await pool.query(
      `UPDATE arena402.game_coin_provisions
          SET status = 'pending', whitelist_block_number = $2,
              updated_at = clock_timestamp(), last_error = NULL
        WHERE provision_id = $1 AND status = 'whitelist_submitted'`,
      [provision.provision_id, Number(receipt.blockNumber)],
    );
    return;
  }

  const baseline = BigInt(
    requiredValue(
      provision.balance_before_atomic,
      "gamecoin_balance_baseline_missing",
    ),
  );
  const expected = baseline + BigInt(provision.amount_atomic);
  if (await balanceOf(getAddress(provision.account_address)) < expected) {
    await failProvision(provision.provision_id, "mint_balance_mismatch");
    return;
  }
  await confirmProvision(provision.provision_id, Number(receipt.blockNumber));
  console.log(
    `arena402_gamecoin_provision_confirmed provision=${provision.provision_id} block=${receipt.blockNumber}`,
  );
}

async function recentMintEventBlock(
  provision: ProvisionRow,
  transactionHash: Hex,
): Promise<bigint | null> {
  const latestBlock = await publicClient.getBlockNumber();
  const fromBlock = latestBlock > 4_096n ? latestBlock - 4_096n : 0n;
  const target = getAddress(provision.account_address);
  const logs = await publicClient.getContractEvents({
    address: token,
    abi: GAME_COIN_ABI,
    eventName: "Transfer",
    args: { from: zeroAddress, to: target },
    fromBlock,
    toBlock: "latest",
  });
  const evidence: MintEventEvidence[] = logs.map((item) => ({
    transactionHash: item.transactionHash,
    blockNumber: item.blockNumber,
    to: getAddress(item.args.to),
    value: item.args.value,
  }));
  return findMatchingMintEvidence(evidence, {
    transactionHash,
    to: target,
    value: BigInt(provision.amount_atomic),
  });
}

async function recreateTransaction(
  provision: ProvisionRow,
  kind: SubmittedKind,
  submitted: {
    hash: Hex;
    nonce: number;
    gas: bigint;
    gasPrice: bigint;
  },
): Promise<Hex> {
  const target = getAddress(provision.account_address);
  const data =
    kind === "whitelist"
      ? encodeFunctionData({
          abi: GAME_COIN_ABI,
          functionName: "addToWhitelist",
          args: [target],
        })
      : encodeFunctionData({
          abi: GAME_COIN_ABI,
          functionName: "mint",
          args: [target, BigInt(provision.amount_atomic)],
        });
  const serialized = await account.signTransaction({
    chainId,
    to: token,
    data,
    gas: submitted.gas,
    gasPrice: submitted.gasPrice,
    nonce: submitted.nonce,
    type: "legacy",
  });
  if (keccak256(serialized) !== submitted.hash) {
    throw new Error(`gamecoin_${kind}_transaction_hash_mismatch`);
  }
  return serialized;
}

function submittedFields(
  provision: ProvisionRow,
  kind: SubmittedKind,
): { hash: Hex; nonce: number; gas: bigint; gasPrice: bigint } {
  const hash =
    kind === "whitelist"
      ? provision.whitelist_tx_hash
      : provision.mint_tx_hash;
  const nonce =
    kind === "whitelist"
      ? provision.whitelist_tx_nonce
      : provision.mint_tx_nonce;
  const gas =
    kind === "whitelist"
      ? provision.whitelist_gas_limit
      : provision.mint_gas_limit;
  const gasPrice =
    kind === "whitelist"
      ? provision.whitelist_gas_price_wei
      : provision.mint_gas_price_wei;
  return {
    hash: requiredValue(hash, `gamecoin_${kind}_hash_missing`),
    nonce: Number(requiredValue(nonce, `gamecoin_${kind}_nonce_missing`)),
    gas: BigInt(requiredValue(gas, `gamecoin_${kind}_gas_missing`)),
    gasPrice: BigInt(
      requiredValue(gasPrice, `gamecoin_${kind}_gas_price_missing`),
    ),
  };
}

async function confirmProvision(
  provisionId: string,
  blockNumber: number | null,
): Promise<void> {
  await pool.query(
    `UPDATE arena402.game_coin_provisions
        SET status = 'confirmed', mint_block_number = COALESCE($2, mint_block_number),
            confirmed_at = clock_timestamp(), updated_at = clock_timestamp(),
            last_error = NULL
      WHERE provision_id = $1
        AND status IN ('pending', 'mint_submitted')`,
    [provisionId, blockNumber],
  );
}

async function failProvision(
  provisionId: string,
  safeError: string,
): Promise<void> {
  await pool.query(
    `UPDATE arena402.game_coin_provisions
        SET status = 'failed', last_error = $2,
            updated_at = clock_timestamp()
      WHERE provision_id = $1`,
    [provisionId, safeError],
  );
}

async function balanceOf(address: `0x${string}`): Promise<bigint> {
  return (await readContract({
    address: token,
    abi: GAME_COIN_ABI,
    functionName: "balanceOf",
    args: [address],
  })) as bigint;
}

function validateFrozenScope(provision: ProvisionRow): void {
  if (Number(provision.chain_id) !== chainId) {
    throw new Error("gamecoin_provision_chain_mismatch");
  }
  if (getAddress(provision.token_address) !== token) {
    throw new Error("gamecoin_provision_token_mismatch");
  }
}

async function broadcast(serialized: Hex, expectedHash: Hex): Promise<void> {
  try {
    const actual = await publicClient.sendRawTransaction({
      serializedTransaction: serialized as TransactionSerialized,
    });
    if (actual !== expectedHash) {
      throw new Error("gamecoin_broadcast_hash_mismatch");
    }
  } catch (error) {
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    if (
      !message.includes("already known") &&
      !message.includes("nonce too low")
    ) {
      throw error;
    }
  }
}

function required(name: string, fallback?: string): string {
  const value = process.env[name]?.trim() || fallback;
  if (!value) throw new Error(`${name}_required`);
  return value;
}

function requiredValue<T>(value: T | null, error: string): T {
  if (value === null) throw new Error(error);
  return value;
}

function positiveNumber(name: string, fallback: number): number {
  const value = Number(process.env[name] || fallback);
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name}_invalid`);
  }
  return value;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function safeCode(error: unknown): string {
  if (!(error instanceof Error)) return "unknown";
  return error.message.replace(/[^a-zA-Z0-9_.:-]/g, "_").slice(0, 160);
}
