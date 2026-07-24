/**
 * SETTLE-004 · facilitator HTTP 服务（Express）
 *
 * 端点：
 *   GET  /health          存活 + facilitator INJ 余额
 *   POST /verify          预检（不上链）
 *   POST /settle          代付 gas 上链结算
 *   POST /faucet          给地址发 mUSDC
 *
 * 启动：cd facilitator && npm install && npm start
 */
import "dotenv/config";
import express from "express";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { formatUnits, type Hex } from "viem";
import { Facilitator, type PaymentAuthorization } from "./settle.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(resolve(__dirname, "../../.env"));

const dep = JSON.parse(readFileSync(resolve(__dirname, "../../deployments.json"), "utf8"));
const PORT = Number(process.env.FACILITATOR_PORT ?? 4021);

const facilitator = new Facilitator({
  rpc: dep.rpc,
  chainId: dep.chainId,
  facilitatorPk: process.env.FACILITATOR_PRIVATE_KEY as Hex,
});

const app = express();
app.use(express.json());

app.get("/health", async (_req, res) => {
  const bal = await facilitator.gasBalance();
  res.json({
    ok: true,
    facilitator: facilitator.address,
    injBalance: formatUnits(bal, 18),
    token: dep.usdc.address,
    chainId: dep.chainId,
  });
});

app.post("/verify", async (req, res) => {
  try {
    const result = await facilitator.verify(req.body as PaymentAuthorization);
    res.json(result);
  } catch (e: any) {
    res.status(400).json({ ok: false, reason: e.message });
  }
});

app.post("/settle", async (req, res) => {
  try {
    const auth = req.body as PaymentAuthorization;
    // 先预检，避免明知失败还烧 gas
    const pre = await facilitator.verify(auth);
    if (!pre.ok) return res.status(400).json({ status: "error", error: pre.reason });
    const result = await facilitator.settle(auth);
    res.json(result);
  } catch (e: any) {
    res.status(500).json({ status: "error", error: e.message });
  }
});

app.post("/faucet", async (req, res) => {
  try {
    const { to } = req.body as { to: string };
    if (!to) return res.status(400).json({ status: "error", error: "missing 'to'" });
    const result = await facilitator.faucet(dep.usdc.address, to);
    res.json(result);
  } catch (e: any) {
    res.status(500).json({ status: "error", error: e.message });
  }
});

app.listen(PORT, () => {
  console.log(`🟢 facilitator on :${PORT}`);
  console.log(`   address: ${facilitator.address}`);
  console.log(`   token:   ${dep.usdc.address} (${dep.usdc.symbol})`);
});
