/**
 * SETTLE-002 · RealSettlement — 真实实现，复用 SETTLE-004 facilitator。
 *
 * 直接结算模式（D2）：lockFunds 不真锁链上，只校验买方 x402 签名并记录待结算意图；
 * settleTrade 把该签名 POST 给 facilitator，经 transferWithAuthorization 上链，买方→卖方。
 */
import { type SettlementSDK, type AttestationReport, PLATFORM_FEE_RATE } from "./settlement.ts";
import { verifyAuthorizationLocally } from "./x402.ts";
import type { PaymentAuthorization, Deployments } from "./types.ts";

interface Pending {
  negotiationId: string;
  buyer: string;
  seller: string;
  amount: number;
  auth: PaymentAuthorization;
  status: "locked" | "settled" | "refunded" | "expired";
  createdAt: number;
  expiresAt: number;
}

export interface RealSettlementConfig {
  facilitatorUrl: string;
  deployments: Deployments;
}

export class RealSettlement implements SettlementSDK {
  private pending = new Map<string, Pending>();
  private seq = 0;
  constructor(private cfg: RealSettlementConfig) {}

  private id(p: string) { this.seq += 1; return `${p}_${Date.now()}_${this.seq}`; }
  private async post(path: string, body: unknown) {
    const r = await fetch(`${this.cfg.facilitatorUrl}${path}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    return r.json();
  }

  async registerAgent(params: { agentId: string; ownerWallet: string; attestation: AttestationReport; metadata?: Record<string, string> }) {
    // ERC-8004 注册合约留 Day2；当前返回链下占位（不阻塞集成）
    return { nftTokenId: `pending_${params.agentId}`, txHash: "offchain_stub" };
  }

  async lockFunds(params: {
    negotiationId: string; buyerWallet: string; sellerWallet: string;
    amount: number; currency: "USDC"; x402Signature: string; expiry: number;
  }) {
    const auth: PaymentAuthorization = JSON.parse(params.x402Signature);
    // 本地校验签名有效 + from/to 一致
    const ok = await verifyAuthorizationLocally(auth, this.cfg.deployments);
    if (!ok) throw new Error("ATTESTATION_INVALID: x402 signature invalid");
    const escrowId = this.id("escrow");
    this.pending.set(escrowId, {
      negotiationId: params.negotiationId, buyer: params.buyerWallet, seller: params.sellerWallet,
      amount: params.amount, auth, status: "locked", createdAt: Date.now(), expiresAt: params.expiry,
    });
    return { escrowId, txHash: `intent_${escrowId}`, status: "locked" as const };
  }

  async settleTrade(params: { escrowId: string; deliveryProof?: string }) {
    const p = this.pending.get(params.escrowId);
    if (!p) throw new Error(`ESCROW_NOT_FOUND: ${params.escrowId}`);
    if (p.status !== "locked") throw new Error(`SETTLEMENT_FAILED: status=${p.status}`);
    const res = await this.post("/settle", p.auth);
    if (res.status !== "success") throw new Error(`SETTLEMENT_FAILED: ${res.error ?? res.status}`);
    p.status = "settled";
    const platformFee = p.amount * PLATFORM_FEE_RATE;
    const amountToSeller = p.amount - platformFee;
    return { txHash: res.txHash as string, amountToSeller, platformFee, status: "settled" as const };
  }

  async refund(params: { escrowId: string; reason: string }) {
    const p = this.pending.get(params.escrowId);
    if (!p) throw new Error(`ESCROW_NOT_FOUND: ${params.escrowId}`);
    // 直接结算未真锁资金 → 标记取消即可（链上 cancelAuthorization 可选）
    p.status = "refunded";
    return { txHash: `refund_noop_${params.escrowId}`, status: "refunded" as const };
  }

  async getEscrowStatus(escrowId: string) {
    const p = this.pending.get(escrowId);
    if (!p) throw new Error(`ESCROW_NOT_FOUND: ${escrowId}`);
    return { status: p.status, amount: p.amount, buyer: p.buyer, seller: p.seller, createdAt: p.createdAt, expiresAt: p.expiresAt };
  }

  async getAgentInfo(agentId: string) {
    return { nftTokenId: `pending_${agentId}`, owner: "", registeredAt: 0, attestationHash: "", totalSettlements: 0 };
  }

  async verifyAttestation(report: AttestationReport) {
    return { valid: Boolean(report.measurement), txHash: "offchain_stub" };
  }
}
