/**
 * SETTLE-001 · Injective EVM 环境验证 + USDC EIP-3009 探测
 *
 * 只读脚本：连 RPC、查余额、探测 USDC 是否支持 EIP-3009。
 * 不花 gas、不上链。逐条对照 spec 的 AC1–AC5 打钩。
 *
 * 用法：cp .env.example .env && 填好 → npx tsx scripts/check-env.ts
 */
import "dotenv/config";
import {
  createPublicClient,
  http,
  getAddress,
  toFunctionSelector,
  formatUnits,
  type Address,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { writeFileSync } from "node:fs";

const RPC = process.env.INJECTIVE_EVM_RPC ?? "https://k8s.testnet.json-rpc.injective.network/";
const EXPECTED_CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
const USDC = process.env.USDC_ADDRESS?.trim();

// ---- 小工具：打钩输出 ----
const results: Record<string, boolean> = {};
function check(id: string, ok: boolean, detail: string) {
  results[id] = ok;
  console.log(`${ok ? "✅" : "❌"} ${id}  ${detail}`);
}
function pkToAddr(pk?: string): Address | null {
  if (!pk || !pk.startsWith("0x") || pk.length < 66) return null;
  try {
    return privateKeyToAccount(pk as `0x${string}`).address;
  } catch {
    return null;
  }
}

// ERC-20 / EIP-712 只读 ABI
const ERC20_ABI = [
  { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
  { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
  { type: "function", name: "name", stateMutability: "view", inputs: [], outputs: [{ type: "string" }] },
  { type: "function", name: "version", stateMutability: "view", inputs: [], outputs: [{ type: "string" }] },
] as const;

// EIP-3009 的两个规范签名（v,r,s 版 + bytes 版），运行时算 selector，避免手写错 hex
const EIP3009_SIGS = [
  "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
  "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)",
  "receiveWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
];

async function main() {
  console.log(`\n🔍 SETTLE-001 环境探测 — RPC=${RPC}\n`);

  // 公共 RPC 偶发慢/超时 → 拉长超时 + 多重试
  const client = createPublicClient({
    transport: http(RPC, { timeout: 30_000, retryCount: 5, retryDelay: 1500 }),
  });

  // --- AC1: RPC 可连 + chainId ---
  let chainId = 0;
  let blockNumber = 0n;
  try {
    chainId = await client.getChainId();
    blockNumber = await client.getBlockNumber();
  } catch (e) {
    check("AC1", false, `RPC 连接失败: ${(e as Error).message}`);
  }
  if (chainId) {
    check(
      "AC1",
      chainId === EXPECTED_CHAIN_ID,
      `chainId=${chainId} (期望 ${EXPECTED_CHAIN_ID}), 最新区块=${blockNumber}`
    );
  }

  // --- 三个钱包地址 ---
  const buyer = pkToAddr(process.env.BUYER_PRIVATE_KEY);
  const seller = pkToAddr(process.env.SELLER_PRIVATE_KEY);
  const facilitator = pkToAddr(process.env.FACILITATOR_PRIVATE_KEY);
  console.log("");
  console.log(`   买方   : ${buyer ?? "(未配置私钥)"}`);
  console.log(`   卖方   : ${seller ?? "(未配置私钥)"}`);
  console.log(`   facilitator: ${facilitator ?? "(未配置私钥)"}\n`);

  // --- AC2: facilitator 有 INJ ---
  if (facilitator) {
    try {
      const bal = await client.getBalance({ address: facilitator });
      check("AC2", bal > 0n, `facilitator INJ 余额 = ${formatUnits(bal, 18)} INJ`);
    } catch (e) {
      check("AC2", false, `查 INJ 余额失败: ${(e as Error).message.split("\n")[0]}`);
    }
  } else {
    check("AC2", false, "未配置 FACILITATOR_PRIVATE_KEY");
  }

  // --- USDC 相关：AC3 + AC4 ---
  if (!USDC) {
    check("AC3", false, "USDC_ADDRESS 未填 — 领 Circle faucet 后把 USDC 合约地址填入 .env 再跑");
    check("AC4", false, "USDC_ADDRESS 未填，无法探测 EIP-3009");
    check("AC5", false, "缺 USDC 参数，deployments.json 未生成");
    summarize();
    return;
  }

  const usdcAddr = getAddress(USDC);
  let decimals = 6;
  let name = "";
  let version = "";
  try {
    decimals = await client.readContract({ address: usdcAddr, abi: ERC20_ABI, functionName: "decimals" });
    name = await client.readContract({ address: usdcAddr, abi: ERC20_ABI, functionName: "name" });
  } catch (e) {
    console.log(`   ⚠️ 读 USDC decimals/name 失败: ${(e as Error).message}`);
  }
  try {
    version = await client.readContract({ address: usdcAddr, abi: ERC20_ABI, functionName: "version" });
  } catch {
    version = "1"; // 多数 USDC EIP-712 version="1"，无 version() 时默认，部署时人工确认
    console.log(`   ⚠️ USDC 无 version() 方法，EIP-712 version 暂按 "1" 记录（需人工确认）`);
  }

  // AC3: 买方 token 余额（也查 facilitator，因为 faucet 把 USDT 发到了它）
  for (const [label, addr] of [["买方", buyer], ["facilitator", facilitator]] as const) {
    if (!addr) continue;
    try {
      const bal = await client.readContract({ address: usdcAddr, abi: ERC20_ABI, functionName: "balanceOf", args: [addr] });
      const isBuyer = label === "买方";
      const msg = `${label} token 余额 = ${formatUnits(bal, decimals)} (decimals=${decimals})`;
      if (isBuyer) check("AC3", bal > 0n, msg);
      else console.log(`   ℹ️ ${msg}`);
    } catch (e) {
      if (label === "买方") check("AC3", false, `查 token 余额失败: ${(e as Error).message.split("\n")[0]}`);
    }
  }

  // AC4: 探测 EIP-3009 —— 拉合约 bytecode，查是否含 transferWithAuthorization 的 selector
  const code = await client.getCode({ address: usdcAddr });
  let eip3009 = false;
  if (code && code !== "0x") {
    const found: string[] = [];
    for (const sig of EIP3009_SIGS) {
      const sel = toFunctionSelector(sig).slice(2); // 去掉 0x
      if (code.toLowerCase().includes(sel.toLowerCase())) found.push(sig.split("(")[0] + `(0x${sel})`);
    }
    eip3009 = found.some((f) => f.startsWith("transferWithAuthorization"));
    check(
      "AC4",
      eip3009,
      eip3009
        ? `USDC 支持 EIP-3009 ✓ 命中: ${found.join(", ")}`
        : `未在 bytecode 找到 transferWithAuthorization selector → 需走 Permit2（见 SETTLE-000 A2 / SETTLE-003）`
    );
  } else {
    check("AC4", false, `地址 ${usdcAddr} 无合约 bytecode（EOA 或错地址）`);
  }

  // AC5: 产出 deployments.json
  const deployments = {
    _spec: "SETTLE-001",
    chainId,
    rpc: RPC,
    usdc: { address: usdcAddr, decimals, eip712Name: name, eip712Version: version, supportsEip3009: eip3009 },
    wallets: { buyer, seller, facilitator },
    note: "eip712Version 若来自默认值需人工核对；supportsEip3009=false 时切 Permit2",
  };
  writeFileSync(new URL("../deployments.json", import.meta.url), JSON.stringify(deployments, null, 2));
  check("AC5", true, "已写出 deployments.json");

  summarize();
}

function summarize() {
  const passed = Object.values(results).filter(Boolean).length;
  const total = Object.keys(results).length;
  console.log(`\n──────────────\n结果: ${passed}/${total} 通过`);
  if (!results["AC4"]) {
    console.log("⚠️ AC4（EIP-3009）未过是最高风险点 — 决定走 EIP-3009 还是 Permit2。");
  }
  console.log("");
}

main().catch((e) => {
  console.error("脚本异常:", e);
  process.exit(1);
});
