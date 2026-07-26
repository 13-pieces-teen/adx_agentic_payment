import "dotenv/config";
import { createHash } from "node:crypto";
import { constants, readFileSync } from "node:fs";
import { open, rename, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createPublicClient,
  createWalletClient,
  getAddress,
  http,
  type Abi,
  type Address,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { waitViaBlockscout } from "./lib-tx.ts";

interface MintRecord {
  tokenId: number;
  walletId: string;
  address: Address;
  status: "prepared" | "confirmed";
  txHash?: Hex;
  blockNumber?: number;
}

interface MintManifest {
  version: 1;
  campaign: "arena402-genesis";
  batchId: string;
  chainId: number;
  contractAddress: Address;
  createdAt: string;
  addressDigest: string;
  records: MintRecord[];
}

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile?.(resolve(__dirname, "../../.env"));

const manifestPath = requiredAbsolutePath(option("--manifest"), "--manifest");
const manifest = await loadManifest(manifestPath);
const RPC =
  process.env.INJECTIVE_EVM_RPC ??
  "https://k8s.testnet.json-rpc.injective.network/";
const CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
if (manifest.chainId !== CHAIN_ID) {
  throw new Error("Mint manifest chainId does not match configured chain");
}

const artifact = JSON.parse(
  readFileSync(
    resolve(
      __dirname,
      "../artifacts/contracts/ArenaMemorialNFT.sol/ArenaMemorialNFT.json",
    ),
    "utf8",
  ),
) as { abi: Abi };
const chain = {
  id: CHAIN_ID,
  name: "Injective EVM Testnet",
  nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
  rpcUrls: { default: { http: [RPC] } },
} as const;
const transport = http(RPC, {
  timeout: 60_000,
  retryCount: 5,
  retryDelay: 2_000,
});
const publicClient = createPublicClient({ chain, transport });
const contractAddress = getAddress(manifest.contractAddress);

if (manifest.records.every((record) => record.status === "confirmed")) {
  process.stdout.write(`Batch ${manifest.batchId} is already confirmed.\n`);
  process.exit(0);
}
if (manifest.records.some((record) => record.status !== "prepared")) {
  throw new Error("Mint manifest contains an unsupported mixed state");
}

const expectedNextTokenId = manifest.records[0].tokenId;
await requireNextTokenId(expectedNextTokenId);
if (!process.argv.includes("--apply")) {
  process.stdout.write(
    `Validated batch ${manifest.batchId}, tokens ` +
      `${expectedNextTokenId}-${manifest.records.at(-1)?.tokenId}. ` +
      "No transaction sent; rerun with --apply after human review.\n",
  );
  process.exit(0);
}

const ownerKey = process.env.FACILITATOR_PRIVATE_KEY as Hex | undefined;
if (!ownerKey) throw new Error("FACILITATOR_PRIVATE_KEY is required with --apply");
const owner = privateKeyToAccount(ownerKey);
const contractOwner = getAddress(
  (await publicClient.readContract({
    address: contractAddress,
    abi: artifact.abi,
    functionName: "owner",
  })) as Address,
);
if (contractOwner !== getAddress(owner.address)) {
  throw new Error("FACILITATOR_PRIVATE_KEY is not the memorial contract owner");
}

const wallet = createWalletClient({ account: owner, chain, transport });
const baseGasPrice = await publicClient.getGasPrice();
const recipients = manifest.records.map((record) => record.address);
const gas = await publicClient.estimateContractGas({
  address: contractAddress,
  abi: artifact.abi,
  functionName: "mintBatch",
  args: [recipients],
  account: owner,
});
const hash = await wallet.writeContract({
  address: contractAddress,
  abi: artifact.abi,
  functionName: "mintBatch",
  args: [recipients],
  type: "legacy",
  gasPrice: baseGasPrice * 3n,
  gas: (gas * 120n) / 100n,
});
process.stdout.write(
  `Submitted batch ${manifest.batchId}, tokens ` +
    `${expectedNextTokenId}-${manifest.records.at(-1)?.tokenId}: ${hash}\n`,
);
const result = await waitViaBlockscout(hash);
if (result.status !== "success") {
  throw new Error(
    `Mint transaction is ${result.status}; reconcile ${hash} before retrying`,
  );
}
for (const record of manifest.records) {
  record.status = "confirmed";
  record.txHash = hash;
  record.blockNumber = result.blockNumber;
}
await atomicJsonWrite(manifestPath, manifest);
process.stdout.write(
  `Confirmed ${manifest.records.length} memorial NFTs in batch ` +
    `${manifest.batchId}; run record_memorial_mint_batch.py next.\n`,
);

async function requireNextTokenId(expected: number): Promise<void> {
  const actual = Number(
    await publicClient.readContract({
      address: contractAddress,
      abi: artifact.abi,
      functionName: "nextTokenId",
    }),
  );
  if (actual !== expected) {
    throw new Error(
      `Contract nextTokenId ${actual} does not match batch start ${expected}`,
    );
  }
}

async function loadManifest(path: string): Promise<MintManifest> {
  const handle = await open(
    path,
    constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0),
  );
  try {
    const metadata = await handle.stat();
    if (!metadata.isFile()) throw new Error("Mint manifest must be a regular file");
    const value = JSON.parse(await handle.readFile({ encoding: "utf8" })) as MintManifest;
    if (
      value.version !== 1 ||
      value.campaign !== "arena402-genesis" ||
      !value.batchId ||
      !Number.isSafeInteger(value.chainId) ||
      !Array.isArray(value.records) ||
      value.records.length < 1 ||
      value.records.length > 40
    ) {
      throw new Error("Mint manifest schema is invalid");
    }
    getAddress(value.contractAddress);
    const addresses = value.records.map((record, offset) => {
      if (
        record.tokenId !== value.records[0].tokenId + offset ||
        record.walletId !==
          `memorial-wallet-${String(record.tokenId).padStart(4, "0")}`
      ) {
        throw new Error("Mint manifest token sequence is invalid");
      }
      record.address = getAddress(record.address);
      return record.address.toLowerCase();
    });
    const digest =
      "sha256:" +
      createHash("sha256")
        .update(JSON.stringify(addresses))
        .digest("hex");
    if (digest !== value.addressDigest) {
      throw new Error("Mint manifest address digest is invalid");
    }
    return value;
  } finally {
    await handle.close();
  }
}

async function atomicJsonWrite(
  path: string,
  value: unknown,
): Promise<void> {
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    flag: "wx",
    mode: 0o644,
  });
  await rename(temporary, path);
}

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  const value = process.argv[index + 1]?.trim();
  if (!value || value.startsWith("--")) throw new Error(`${name} requires a value`);
  return value;
}

function requiredAbsolutePath(
  path: string | undefined,
  name: string,
): string {
  if (!path || !isAbsolute(path)) throw new Error(`${name} must be an absolute path`);
  return resolve(path);
}
