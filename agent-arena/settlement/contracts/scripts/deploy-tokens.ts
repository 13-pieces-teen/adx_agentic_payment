/**
 * 部署 arena402-m(ArenaMemorial)与 arena402-g(ArenaGameCoin)到 Injective EVM testnet。
 *
 * 复用 deploy.ts 的 Injective 坑解法:legacy tx(type 0)+ 动态 gasPrice×3(D7),
 * 用 blockscout 轮询确认(公共 RPC 回执延迟)。部署者 = facilitator(唯一有 INJ 付 gas)。
 *
 * 用法(先编译):
 *   npm run compile
 *   npm run deploy:tokens                 # 部署 -m 和 -g 两个
 *   TOKEN=M npm run deploy:tokens         # 只部署 -m
 *   TOKEN=G npm run deploy:tokens         # 只部署 -g
 *
 * 部署后回写 deployments.json 的 memorial / gameCoin 段(含 image 占位,待前端填 URL)。
 * 本脚本不 mint、不登记白名单 —— 那是分发阶段的独立操作,避免部署脚本承担业务决策。
 */
import "dotenv/config";
import {
  createWalletClient,
  createPublicClient,
  http,
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
process.loadEnvFile?.(resolve(__dirname, "../../.env"));

const RPC = process.env.INJECTIVE_EVM_RPC ?? "https://k8s.testnet.json-rpc.injective.network/";
const CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
const GAS_PRICE_MULT = 3n;
const DEPLOYER_PK = process.env.FACILITATOR_PRIVATE_KEY as Hex;

const injectiveTestnet = {
  id: CHAIN_ID,
  name: "Injective EVM Testnet",
  nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
  rpcUrls: { default: { http: [RPC] } },
} as const;

// 冻结的命名/decimals 约定(与 assets/token-icons/README.md 一致)
const TOKENS = {
  M: { artifact: "ArenaMemorial", name: "Arena 402 Memorial", symbol: "arena402-m", decimals: 0, depKey: "memorial" },
  G: { artifact: "ArenaGameCoin", name: "Arena 402 Gold", symbol: "arena402-g", decimals: 6, depKey: "gameCoin" },
} as const;

function loadArtifact(name: string) {
  const p = resolve(__dirname, `../artifacts/contracts/${name}.sol/${name}.json`);
  if (!existsSync(p)) throw new Error(`未找到编译产物,请先 npm run compile\n${p}`);
  const art = JSON.parse(readFileSync(p, "utf8"));
  return { abi: art.abi, bytecode: art.bytecode as Hex };
}

async function deployOne(
  spec: (typeof TOKENS)[keyof typeof TOKENS],
  wallet: ReturnType<typeof createWalletClient>,
  publicClient: ReturnType<typeof createPublicClient>,
  gasPrice: bigint,
) {
  console.log(`\n🚀 部署 ${spec.symbol}(${spec.name})...`);
  const { abi, bytecode } = loadArtifact(spec.artifact);
  const hash = await wallet.deployContract({
    abi, bytecode,
    args: [spec.name, spec.symbol, spec.decimals],
    type: "legacy", gasPrice, gas: 3_000_000n,
  });
  console.log(`   tx: ${hash}`);
  const res = await waitViaBlockscout(hash);
  if (res.status !== "success" || !res.createdContract) {
    throw new Error(`部署未确认: status=${res.status}(稍后复查 ${hash})`);
  }
  const address = getAddress(res.createdContract);
  console.log(`   ✅ ${spec.symbol} = ${address}(block ${res.blockNumber})`);

  // 读回 DOMAIN_SEPARATOR(-g 有 EIP-3009,-m 无则跳过)
  let domainSeparator: string | undefined;
  if (spec.artifact === "ArenaGameCoin") {
    domainSeparator = (await publicClient.readContract({
      address, abi, functionName: "DOMAIN_SEPARATOR",
    })) as string;
  }
  return { address, hash, domainSeparator };
}

async function main() {
  if (!DEPLOYER_PK) throw new Error("缺 FACILITATOR_PRIVATE_KEY(部署者/付gas)");
  const deployer = privateKeyToAccount(DEPLOYER_PK);
  const which = (process.env.TOKEN ?? "BOTH").toUpperCase();
  const selected =
    which === "M" ? [TOKENS.M] : which === "G" ? [TOKENS.G] : [TOKENS.M, TOKENS.G];

  console.log(`部署者(facilitator): ${deployer.address} · chainId=${CHAIN_ID}`);
  const transport = http(RPC, { timeout: 60_000, retryCount: 5, retryDelay: 2000 });
  const publicClient = createPublicClient({ chain: injectiveTestnet, transport });
  const wallet = createWalletClient({ account: deployer, chain: injectiveTestnet, transport });

  const gasBal = await publicClient.getBalance({ address: deployer.address });
  console.log(`部署者 INJ 余额: ${formatUnits(gasBal, 18)} INJ`);
  if (gasBal === 0n) throw new Error("部署者无 INJ,先领 gas");
  const baseGasPrice = await publicClient.getGasPrice();
  const gasPrice = baseGasPrice * GAS_PRICE_MULT;
  console.log(`gasPrice=${formatUnits(baseGasPrice, 9)} → 用 ${formatUnits(gasPrice, 9)} gwei`);

  const depPath = resolve(__dirname, "../../deployments.json");
  const dep = existsSync(depPath) ? JSON.parse(readFileSync(depPath, "utf8")) : {};
  dep.chainId = CHAIN_ID;
  dep.rpc = RPC;

  for (const spec of selected) {
    const r = await deployOne(spec, wallet, publicClient, gasPrice);
    dep[spec.depKey] = {
      address: r.address,
      symbol: spec.symbol,
      decimals: spec.decimals,
      eip712Name: spec.name,
      eip712Version: "1",
      soulbound: spec.artifact === "ArenaMemorial",
      whitelistGated: spec.artifact === "ArenaGameCoin",
      supportsEip3009: spec.artifact === "ArenaGameCoin",
      ...(r.domainSeparator ? { domainSeparator: r.domainSeparator } : {}),
      // 前端把设计好的图标上传后,把公开 https URL 填到这里(占位见 assets/token-icons/)
      image: `TODO_UPLOAD_ICON_URL(assets/token-icons/${spec.symbol}.png)`,
      deployTx: r.hash,
    };
  }
  writeFileSync(depPath, JSON.stringify(dep, null, 2));
  console.log(`\n✅ 已回写 ${depPath}`);
  console.log(`\n后续分发(独立操作,非本脚本):`);
  console.log(`  -m: mint / mintBatch 给参赛钱包`);
  console.log(`  -g: addToWhitelistBatch 登记参赛钱包后再 mint`);
}

main().catch((e) => {
  console.error("\n❌ 部署失败:", e.message ?? e);
  process.exit(1);
});
