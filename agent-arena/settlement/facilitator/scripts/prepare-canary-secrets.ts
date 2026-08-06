import { randomBytes } from "node:crypto";
import { chmod, mkdir, writeFile } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

const scriptDirectory = fileURLToPath(new URL(".", import.meta.url));
const repositoryRoot = resolve(scriptDirectory, "../../../..");
const outputDirectory = requiredAbsolutePath(
  option("--out-dir"),
  "--out-dir",
);
const agentCount = boundedInteger(
  option("--agents") ?? "10",
  "--agents",
  2,
  100,
);

assertOutsideRepository(outputDirectory);
await mkdir(outputDirectory, { recursive: false, mode: 0o700 });
await chmod(outputDirectory, 0o700);

const agentRows = [
  ["index", "ethereum_address", "private_key"],
];
const publicAgents: Array<{ walletId: string; address: string }> = [];
const addresses = new Set<string>();
for (let index = 0; index < agentCount; index += 1) {
  const privateKey = generatePrivateKey();
  const account = privateKeyToAccount(privateKey);
  const normalized = account.address.toLowerCase();
  if (addresses.has(normalized)) {
    throw new Error("generated duplicate Agent wallet");
  }
  addresses.add(normalized);
  agentRows.push([String(index), account.address, privateKey]);
  publicAgents.push({
    walletId: `agent-wallet-${String(index).padStart(4, "0")}`,
    address: account.address,
  });
}

const facilitatorPrivateKey = generatePrivateKey();
const facilitator = privateKeyToAccount(facilitatorPrivateKey);
if (addresses.has(facilitator.address.toLowerCase())) {
  throw new Error("generated duplicate facilitator wallet");
}

await writeSecretFile(
  resolve(outputDirectory, "agent-wallets.csv"),
  csv(agentRows),
  0o600,
);
await writeSecretFile(
  resolve(outputDirectory, "facilitators.csv"),
  csv([
    ["facilitator_index", "ethereum_address", "private_key"],
    ["1", facilitator.address, facilitatorPrivateKey],
  ]),
  0o600,
);
await writeSecretFile(
  resolve(outputDirectory, "wallet-master.key"),
  randomBytes(32),
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "hosted-master.key"),
  randomBytes(32),
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "litellm-token.key"),
  `sk-${randomBytes(48).toString("base64url")}\n`,
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "facilitator-token.key"),
  `${randomBytes(48).toString("base64url")}\n`,
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "wallet-signer-token.key"),
  `${randomBytes(48).toString("base64url")}\n`,
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "settlement-service-token.key"),
  `${randomBytes(48).toString("base64url")}\n`,
  0o400,
);
await writeSecretFile(
  resolve(outputDirectory, "public-manifest.json"),
  `${JSON.stringify(
    {
      schemaVersion: "arena.testnet-canary-wallets.v1",
      chainId: 1439,
      facilitator: {
        facilitatorId: "arena402-testnet-canary-1",
        address: facilitator.address,
      },
      agents: publicAgents,
    },
    null,
    2,
  )}\n`,
  0o600,
);

process.stdout.write(
  JSON.stringify({
    status: "ready",
    chainId: 1439,
    agentCount,
    facilitatorAddress: facilitator.address,
    publicManifest: resolve(outputDirectory, "public-manifest.json"),
  }) + "\n",
);

async function writeSecretFile(
  path: string,
  content: string | Buffer,
  mode: number,
) {
  await writeFile(path, content, { flag: "wx", mode });
  await chmod(path, mode);
}

function csv(rows: string[][]): string {
  return `${rows
    .map((row) => row.map((value) => `"${value.replaceAll('"', '""')}"`).join(","))
    .join("\n")}\n`;
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

function requiredAbsolutePath(
  value: string | undefined,
  name: string,
): string {
  if (!value || !isAbsolute(value)) {
    throw new Error(`${name} must be an absolute path`);
  }
  return resolve(value);
}

function boundedInteger(
  raw: string,
  name: string,
  minimum: number,
  maximum: number,
): number {
  const value = Number(raw);
  if (
    !Number.isSafeInteger(value)
    || value < minimum
    || value > maximum
  ) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function assertOutsideRepository(path: string): void {
  const relation = relative(repositoryRoot, path);
  if (!relation.startsWith("..") && !isAbsolute(relation)) {
    throw new Error("--out-dir must be outside the repository");
  }
}
