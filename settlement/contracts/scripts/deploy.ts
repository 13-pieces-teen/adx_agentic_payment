/**
 * SETTLE-002.5 · 部署 MockStablecoin 到 Injective EVM testnet
 *
 * 关键：viem 默认发 EIP-1559，Injective 需 legacy(type 0)+gasPrice≈0.16gwei (D7)。
 * 本脚本用 facilitator 私钥付 gas 部署，部署后给买方铸初始 USDC，回写 deployments.json。
 *
 * 用法：
 *   cd settlement/contracts
 *   npm install && npm run compile
 *   npm run deploy                     # 默认部署 mUSDC
 *   TOKEN=USDT npm run deploy          # 部署 mUSDT（同合约，改 name/symbol）
 */
import "dotenv/config";
import {
  createWalletClient,
  createPublicClient,
  http,
  parseUnits,
  formatUnits,
  getAddress,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { waitViaBlockscout } from "./lib-tx.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
// settlement/.env（与 check-env 同一份）
process.loadEnvFile?.(resolve(__dirname, "../../.env"));

const RPC = process.env.INJECTIVE_EVM_RPC ?? "https://k8s.testnet.json-rpc.injective.network/";
const CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
// 链上 baseFee=0.16gwei，legacy gasPrice 必须 ≥ baseFee。取动态 gasPrice ×3 留足余量。
const GAS_PRICE_MULT = 3n;

// 部署者 = facilitator（唯一有 INJ 付 gas 的钱包）
const DEPLOYER_PK = process.env.FACILITATOR_PRIVATE_KEY as Hex;
const BUYER_PK = process.env.BUYER_PRIVATE_KEY as Hex;

// 选币种：默认 USDC，可 TOKEN=USDT
const kind = (process.env.TOKEN ?? "USDC").toUpperCase();
const TOKEN_META =
  kind === "USDT"
    ? { name: "Mock Tether USD", symbol: "mUSDT" }
    : { name: "Mock USD Coin", symbol: "mUSDC" };
const DECIMALS = 6;
const FAUCET_WHOLE = 1000n; // faucet 单次领 1000
const BUYER_SEED_WHOLE = "10000"; // 部署后给买方铸 10000，够 demo 反复跑

const injectiveTestnet = {
  id: CHAIN_ID,
  name: "Injective EVM Testnet",
  nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
  rpcUrls: { default: { http: [RPC] } },
} as const;

function loadArtifact() {
  const p = resolve(__dirname, "../artifacts/contracts/MockStablecoin.sol/MockStablecoin.json");
  if (!existsSync(p)) {
    throw new Error(`未找到编译产物，请先运行 npm run compile\n路径: ${p}`);
  }
  const art = JSON.parse(readFileSync(p, "utf8"));
  return { abi: art.abi, bytecode: art.bytecode as Hex };
}

async function main() {
  if (!DEPLOYER_PK) throw new Error("缺 FACILITATOR_PRIVATE_KEY（部署者/付gas）");
  const deployer = privateKeyToAccount(DEPLOYER_PK);
  const buyer = BUYER_PK ? privateKeyToAccount(BUYER_PK).address : null;

  console.log(`\n🚀 部署 ${TOKEN_META.symbol}（${TOKEN_META.name}）到 chainId=${CHAIN_ID}`);
  console.log(`   部署者(facilitator): ${deployer.address}`);
  console.log(`   买方(收种子币):      ${buyer ?? "(未配置，跳过铸币)"}\n`);

  const transport = http(RPC, { timeout: 60_000, retryCount: 5, retryDelay: 2000 });
  const publicClient = createPublicClient({ chain: injectiveTestnet, transport });
  const wallet = createWalletClient({ account: deployer, chain: injectiveTestnet, transport });

  const gasBal = await publicClient.getBalance({ address: deployer.address });
  console.log(`   部署者 INJ 余额: ${formatUnits(gasBal, 18)} INJ`);
  if (gasBal === 0n) throw new Error("部署者无 INJ，无法付 gas，先去 faucet 领");

  // 动态读 gasPrice，×倍数确保 ≥ baseFee（D7）
  const baseGasPrice = await publicClient.getGasPrice();
  const GAS_PRICE = baseGasPrice * GAS_PRICE_MULT;
  console.log(`   链 gasPrice=${formatUnits(baseGasPrice, 9)} gwei → 用 ${formatUnits(GAS_PRICE, 9)} gwei`);

  const { abi, bytecode } = loadArtifact();

  // --- 部署（legacy tx, D7）---
  console.log(`\n① 部署合约（legacy tx）...`);
  const deployHash = await wallet.deployContract({
    abi,
    bytecode,
    args: [TOKEN_META.name, TOKEN_META.symbol, DECIMALS, FAUCET_WHOLE],
    type: "legacy",
    gasPrice: GAS_PRICE,
    gas: 3_000_000n, // 显式给足，避免自动估算失败
  });
  console.log(`   tx: ${deployHash}`);
  // 不用 viem waitForTransactionReceipt（公共 RPC 读节点索引延迟会误判失败）→ 走 blockscout
  const deployRes = await waitViaBlockscout(deployHash);
  if (deployRes.status !== "success" || !deployRes.createdContract) {
    throw new Error(`部署未确认成功: status=${deployRes.status}（可稍后用 check-tx 复查 ${deployHash}）`);
  }
  const contractAddress = getAddress(deployRes.createdContract);
  console.log(`   ✅ 部署成功: ${contractAddress}  (block ${deployRes.blockNumber})`);

  // --- 给买方铸种子币 ---
  if (buyer) {
    console.log(`\n② 给买方铸 ${BUYER_SEED_WHOLE} ${TOKEN_META.symbol}...`);
    const mintHash = await wallet.writeContract({
      address: contractAddress,
      abi,
      functionName: "mint",
      args: [buyer, parseUnits(BUYER_SEED_WHOLE, DECIMALS)],
      type: "legacy",
      gasPrice: GAS_PRICE,
      gas: 200_000n,
    });
    console.log(`   tx: ${mintHash}`);
    const mintRes = await waitViaBlockscout(mintHash);
    console.log(`   铸币确认: ${mintRes.status}`);
    const bal = await publicClient.readContract({ address: contractAddress, abi, functionName: "balanceOf", args: [buyer] });
    console.log(`   ✅ 买方余额: ${formatUnits(bal as bigint, DECIMALS)} ${TOKEN_META.symbol}`);
  }

  // --- 读回 EIP-712 domain 参数 ---
  const domainSeparator = await publicClient.readContract({ address: contractAddress, abi, functionName: "DOMAIN_SEPARATOR" });
  console.log(`\n③ DOMAIN_SEPARATOR: ${domainSeparator}`);

  // --- 回写 deployments.json ---
  const depPath = resolve(__dirname, "../../deployments.json");
  const dep = existsSync(depPath) ? JSON.parse(readFileSync(depPath, "utf8")) : {};
  dep.chainId = CHAIN_ID;
  dep.rpc = RPC;
  dep.usdc = {
    address: getAddress(contractAddress),
    symbol: TOKEN_META.symbol,
    decimals: DECIMALS,
    eip712Name: TOKEN_META.name,
    eip712Version: "1",
    supportsEip3009: true,
    faucetAmount: FAUCET_WHOLE.toString(),
    deployTx: deployHash,
    _spec: "SETTLE-002.5",
  };
  writeFileSync(depPath, JSON.stringify(dep, null, 2));
  console.log(`   ✅ 已回写 ${depPath}`);

  console.log(`\n🎉 完成。Explorer: https://testnet.blockscout.injective.network/address/${contractAddress}\n`);
}

main().catch((e) => {
  console.error("\n❌ 部署失败:", e.message ?? e);
  process.exit(1);
});
