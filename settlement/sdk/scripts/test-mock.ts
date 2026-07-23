/**
 * SETTLE-002 · MockSettlement 验收（002-1, 002-3）
 */
import { MockSettlement, type SettlementSDK, type AttestationReport } from "../src/index.ts";

const s: SettlementSDK = new MockSettlement();
const attestation: AttestationReport = { enclaveId: "enc1", measurement: "0xabc", data: "0x", signature: "0x", platformInfo: "sim", timestamp: Date.now() };

let pass = true;
const check = (name: string, ok: boolean) => { console.log(`${ok ? "✅" : "❌"} ${name}`); if (!ok) pass = false; };

// registerAgent
const reg = await s.registerAgent({ agentId: "seller-1", ownerWallet: "0xSeller", attestation });
check("registerAgent 返回 nftTokenId", Boolean(reg.nftTokenId));

// lockFunds
const lock = await s.lockFunds({
  negotiationId: "neg-1", buyerWallet: "0xBuyer", sellerWallet: "0xSeller",
  amount: 100, currency: "USDC", x402Signature: "mock_sig", expiry: Date.now() + 600_000,
});
check("lockFunds status=locked", lock.status === "locked");

// getEscrowStatus
const st = await s.getEscrowStatus(lock.escrowId);
check("getEscrowStatus 返回 locked + amount", st.status === "locked" && st.amount === 100);

// settleTrade + 手续费 0.5%
const settle = await s.settleTrade({ escrowId: lock.escrowId });
check("settleTrade status=settled", settle.status === "settled");
check("手续费 0.5% (platformFee=0.5, amountToSeller=99.5)", settle.platformFee === 0.5 && settle.amountToSeller === 99.5);

// 重复结算应失败
let doubleSettleRejected = false;
try { await s.settleTrade({ escrowId: lock.escrowId }); } catch { doubleSettleRejected = true; }
check("重复结算被拒", doubleSettleRejected);

// 卖方 agent 结算计数 +1
const info = await s.getAgentInfo("seller-1");
check("卖方 totalSettlements +1", info.totalSettlements === 1);

// verifyAttestation
const va = await s.verifyAttestation(attestation);
check("verifyAttestation valid", va.valid === true);

// refund 一个新 escrow
const lock2 = await s.lockFunds({ negotiationId: "neg-2", buyerWallet: "0xB", sellerWallet: "0xS", amount: 50, currency: "USDC", x402Signature: "x", expiry: Date.now()+600_000 });
const rf = await s.refund({ escrowId: lock2.escrowId, reason: "timeout" });
check("refund status=refunded", rf.status === "refunded");

console.log(`\n${pass ? "🎉 SETTLE-002 MockSettlement 全部通过" : "❌ 有失败项"}`);
process.exit(pass ? 0 : 1);
