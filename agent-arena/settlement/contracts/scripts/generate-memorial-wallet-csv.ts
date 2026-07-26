import { createHash } from "node:crypto";
import { chmod, mkdir, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { toHex } from "viem";
import {
  english,
  generateMnemonic,
  mnemonicToAccount,
  privateKeyToAccount,
} from "viem/accounts";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(__dirname, "../../../..");
const positional = process.argv.slice(2).filter((value) => !value.startsWith("--"));
const count = positiveInteger(option("--count") ?? positional[0] ?? "402", "--count");
if (count > 402) throw new Error("--count cannot exceed 402");
const outputPath = requiredAbsolutePath(option("--out") ?? positional[1], "--out");
assertOutsideRepository(outputPath);

const derivationPath = "m/44'/60'/0'/0/0" as const;
const rows = [
  [
    "index",
    "token_id",
    "wallet_id",
    "ethereum_address",
    "public_key",
    "private_key",
    "mnemonic",
    "derivation_path",
    "chain_id",
    "network",
  ],
];
const addresses = new Set<string>();

for (let index = 0; index < count; index += 1) {
  const mnemonic = generateMnemonic(english, 128);
  const account = mnemonicToAccount(mnemonic, { path: derivationPath });
  const privateKeyBytes = account.getHdKey().privateKey;
  if (!privateKeyBytes) throw new Error("generated wallet has no private key");
  const privateKey = toHex(privateKeyBytes);
  const derived = privateKeyToAccount(privateKey);
  if (derived.address !== account.address) {
    throw new Error(`wallet derivation mismatch at index ${index}`);
  }
  const normalizedAddress = account.address.toLowerCase();
  if (addresses.has(normalizedAddress)) {
    throw new Error(`duplicate wallet address at index ${index}`);
  }
  addresses.add(normalizedAddress);
  rows.push([
    String(index),
    String(index),
    `memorial-wallet-${String(index).padStart(4, "0")}`,
    account.address,
    account.publicKey,
    privateKey,
    mnemonic,
    derivationPath,
    "1439",
    "injective-evm-testnet",
  ]);
}

const csv = `${rows.map((row) => row.map(csvField).join(",")).join("\n")}\n`;
await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, csv, { flag: "wx", mode: 0o600 });
await chmod(outputPath, 0o600);
const checksum = createHash("sha256").update(csv).digest("hex");
process.stdout.write(
  `Generated ${count} unique memorial wallets.\n` +
    `CSV: ${outputPath}\n` +
    `SHA-256: ${checksum}\n`,
);

function csvField(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  const value = process.argv[index + 1]?.trim();
  if (!value || value.startsWith("--")) throw new Error(`${name} requires a value`);
  return value;
}

function positiveInteger(raw: string, name: string): number {
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

function requiredAbsolutePath(
  path: string | undefined,
  name: string,
): string {
  if (!path || !isAbsolute(path)) throw new Error(`${name} must be an absolute path`);
  return resolve(path);
}

function assertOutsideRepository(path: string): void {
  const relation = relative(repositoryRoot, path);
  if (!relation.startsWith("..") && !isAbsolute(relation)) {
    throw new Error("--out must be outside the repository");
  }
}
