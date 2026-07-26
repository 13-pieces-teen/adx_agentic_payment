import "dotenv/config";
import {
  createPublicClient,
  createWalletClient,
  formatUnits,
  getAddress,
  http,
  type Abi,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { waitViaBlockscout } from "./lib-tx.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile?.(resolve(__dirname, "../../.env"));

const RPC =
  process.env.INJECTIVE_EVM_RPC ??
  "https://k8s.testnet.json-rpc.injective.network/";
const CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
const PRIVATE_KEY = process.env.FACILITATOR_PRIVATE_KEY as Hex | undefined;
const BASE_URI = process.env.MEMORIAL_BASE_URI ?? "";

if (!process.argv.includes("--apply")) {
  process.stdout.write(
    "Dry run only. Review FACILITATOR_PRIVATE_KEY and MEMORIAL_BASE_URI, then rerun with --apply to deploy.\n",
  );
  process.exit(0);
}
if (!PRIVATE_KEY) throw new Error("FACILITATOR_PRIVATE_KEY is required");

const chain = {
  id: CHAIN_ID,
  name: "Injective EVM Testnet",
  nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
  rpcUrls: { default: { http: [RPC] } },
} as const;
const deployer = privateKeyToAccount(PRIVATE_KEY);
const transport = http(RPC, {
  timeout: 60_000,
  retryCount: 5,
  retryDelay: 2_000,
});
const publicClient = createPublicClient({ chain, transport });
const wallet = createWalletClient({ account: deployer, chain, transport });
const artifactPath = resolve(
  __dirname,
  "../artifacts/contracts/ArenaMemorialNFT.sol/ArenaMemorialNFT.json",
);
if (!existsSync(artifactPath)) {
  throw new Error(`Run npm run compile first: ${artifactPath}`);
}
const artifact = JSON.parse(readFileSync(artifactPath, "utf8")) as {
  abi: Abi;
  bytecode: Hex;
};
const baseGasPrice = await publicClient.getGasPrice();
const gasPrice = baseGasPrice * 3n;
process.stdout.write(
  `Deploying ArenaMemorialNFT from ${deployer.address} on chain ${CHAIN_ID}; gasPrice ${formatUnits(gasPrice, 9)} gwei.\n`,
);
const hash = await wallet.deployContract({
  abi: artifact.abi,
  bytecode: artifact.bytecode as Hex,
  args: [BASE_URI],
  type: "legacy",
  gasPrice,
  gas: 3_000_000n,
});
process.stdout.write(`Deployment submitted: ${hash}\n`);
const result = await waitViaBlockscout(hash);
if (result.status !== "success" || !result.createdContract) {
  throw new Error(`Deployment not confirmed: ${result.status}`);
}
const address = getAddress(result.createdContract);
const deploymentsPath = resolve(__dirname, "../../deployments.json");
const deployments = JSON.parse(readFileSync(deploymentsPath, "utf8"));
deployments.memorial = {
  address,
  name: "Arena 402 Memorial",
  symbol: "arena402",
  standard: "ERC-721",
  maxSupply: 402,
  soulbound: true,
  soulboundStandard: "ERC-5192",
  baseURI: BASE_URI,
  image: "TODO_UPLOAD_COLLECTION_ICON_URL",
  deployTx: hash,
};
writeFileSync(deploymentsPath, `${JSON.stringify(deployments, null, 2)}\n`);
process.stdout.write(
  `Confirmed ArenaMemorialNFT at ${address}; updated deployments.json memorial only.\n`,
);
