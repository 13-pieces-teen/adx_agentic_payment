/**
 * SETTLE-005 · 端到端直接结算闭环
 *
 * 买方离线签名 → facilitator 代付 gas 上链 → mUSDC 从买方流到卖方。
 * 前置：facilitator 服务已启动（cd facilitator && npm start）。
 *
 * 运行：cd sdk && FACILITATOR_URL=http://localhost:4021 npm run e2e
 */
import "dotenv/config";
import { privateKeyToAccount } from "viem/accounts";
import { createPublicClient, http, formatUnits, getAddress, type Hex } from "viem";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { signTransferAuthorization, loadDeployments } from "../src/x402.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(resolve(__dirname, "../../.env"));

const FACILITATOR_URL = process.env.FACILITATOR_URL ?? "http://localhost:4021";
const dep = loadDeployments(resolve(__dirname, "../../deployments.json"));
const DEC = dep.usdc.decimals;

const buyer = privateKeyToAccount(process.env.BUYER_PRIVATE_KEY as Hex);
const sellerAddr = getAddress(dep.wallets.seller);
const TRANSFER = 5_000_000n; // 5 mUSDC

const pub = createPublicClient({ transport: http(dep.rpc, { timeout: 30_000, retryCount: 6, retryDelay: 2500 }) });
const balABI = [{ type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] }] as const;
const injBal = (a: string) => pub.getBalance({ address: getAddress(a) });
const usdcBal = (a: string) => pub.readContract({ address: dep.usdc.address, abi: balABI, functionName: "balanceOf", args: [getAddress(a)] }) as Promise<bigint>;
const fmt = (b: bigint) => formatUnits(b, DEC);

async function post(path: string, body: unknown) {
  const r = await fetch(`${FACILITATOR_URL}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return { status: r.status, json: await r.json() };
}

async function main() {
  console.log(`\n═══ SETTLE-005 端到端结算闭环 ═══`);
  console.log(`facilitator: ${FACILITATOR_URL}`);
  console.log(`买方 ${buyer.address}\n卖方 ${sellerAddr}\n转账 ${fmt(TRANSFER)} mUSDC\n`);

  // --- 初始快照 ---
  const buyer0 = await usdcBal(buyer.address);
  const seller0 = await usdcBal(sellerAddr);
  const buyerInj0 = await injBal(buyer.address);
  const sellerInj0 = await injBal(sellerAddr);
  console.log(`① 初始余额:`);
  console.log(`   买方 mUSDC=${fmt(buyer0)}  INJ=${formatUnits(buyerInj0,18)}`);
  console.log(`   卖方 mUSDC=${fmt(seller0)}  INJ=${formatUnits(sellerInj0,18)}\n`);

  // --- 买方签名 (SETTLE-003) ---
  const now = Math.floor(Date.now() / 1000);
  const auth = await signTransferAuthorization({ account: buyer, to: sellerAddr, value: TRANSFER, dep, nowSeconds: now });
  console.log(`② 买方离线签名完成 (nonce=${auth.nonce.slice(0,18)}…，不上链/不花gas)\n`);

  // --- facilitator 结算 (SETTLE-004) ---
  console.log(`③ POST /settle …`);
  const settle = await post("/settle", auth);
  console.log(`   → ${JSON.stringify(settle.json)}\n`);
  if (settle.json.status !== "success") throw new Error("结算未成功");

  // --- 最终快照 ---
  const buyer1 = await usdcBal(buyer.address);
  const seller1 = await usdcBal(sellerAddr);
  const buyerInj1 = await injBal(buyer.address);
  const sellerInj1 = await injBal(sellerAddr);
  console.log(`④ 结算后余额:`);
  console.log(`   买方 mUSDC=${fmt(buyer1)}  INJ=${formatUnits(buyerInj1,18)}`);
  console.log(`   卖方 mUSDC=${fmt(seller1)}  INJ=${formatUnits(sellerInj1,18)}\n`);

  // --- 断言 ---
  const ac1 = (seller1 - seller0 === TRANSFER) && (buyer0 - buyer1 === TRANSFER);
  const ac3 = (buyerInj0 === buyerInj1) && (sellerInj0 === sellerInj1);
  console.log(`005-1 卖方+${fmt(TRANSFER)} 买方-${fmt(TRANSFER)}: ${ac1 ? "✅" : "❌"}`);
  console.log(`005-3 买卖方零 INJ 消耗(仅facilitator付gas): ${ac3 ? "✅" : "❌"}`);

  // --- 防重放 (SETTLE-005 AC2) ---
  console.log(`\n⑤ 防重放测试：重复提交同一签名 …`);
  const replay = await post("/settle", auth);
  const ac2 = replay.json.status === "error";
  console.log(`   → ${JSON.stringify(replay.json)}`);
  console.log(`005-2 重复提交被拒(nonce防重放): ${ac2 ? "✅" : "❌"}`);

  const allPass = ac1 && ac2 && ac3;
  console.log(`\n${allPass ? "🎉 M3 达成：x402 agentic payment 点到点跑通！" : "❌ 有未通过项"}`);
  console.log(`   结算 tx: https://testnet.blockscout.injective.network/tx/${settle.json.txHash}\n`);
  process.exit(allPass ? 0 : 1);
}
main().catch((e) => { console.error("❌ e2e 失败:", e.message ?? e); process.exit(1); });
