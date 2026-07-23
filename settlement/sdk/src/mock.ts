/**
 * SETTLE-002 · MockSettlement — 内存版，零链上依赖。
 * arena 队友开发期用它，一行切换到 RealSettlement 即可联调。
 */
import { type SettlementSDK, type AttestationReport, PLATFORM_FEE_RATE } from "./settlement.ts";

interface EscrowRecord {
  negotiationId: string;
  buyer: string;
  seller: string;
  amount: number;
  status: "locked" | "settled" | "refunded" | "expired";
  createdAt: number;
  expiresAt: number;
}

export class MockSettlement implements SettlementSDK {
  private escrows = new Map<string, EscrowRecord>();
  private agents = new Map<string, { nftTokenId: string; owner: string; registeredAt: number; attestationHash: string; totalSettlements: number }>();
  private seq = 0;

  private id(prefix: string): string {
    this.seq += 1;
    return `${prefix}_${Date.now()}_${this.seq}`;
  }

  async registerAgent(params: {
    agentId: string;
    ownerWallet: string;
    attestation: AttestationReport;
    metadata?: Record<string, string>;
  }) {
    const nftTokenId = this.id("nft");
    this.agents.set(params.agentId, {
      nftTokenId,
      owner: params.ownerWallet,
      registeredAt: Date.now(),
      attestationHash: params.attestation.measurement,
      totalSettlements: 0,
    });
    return { nftTokenId, txHash: `mock_register_${nftTokenId}` };
  }

  async lockFunds(params: {
    negotiationId: string;
    buyerWallet: string;
    sellerWallet: string;
    amount: number;
    currency: "USDC";
    x402Signature: string;
    expiry: number;
  }) {
    const escrowId = this.id("escrow");
    this.escrows.set(escrowId, {
      negotiationId: params.negotiationId,
      buyer: params.buyerWallet,
      seller: params.sellerWallet,
      amount: params.amount,
      status: "locked",
      createdAt: Date.now(),
      expiresAt: params.expiry,
    });
    return { escrowId, txHash: `mock_lock_${escrowId}`, status: "locked" as const };
  }

  async settleTrade(params: { escrowId: string; deliveryProof?: string }) {
    const e = this.escrows.get(params.escrowId);
    if (!e) throw new Error(`ESCROW_NOT_FOUND: ${params.escrowId}`);
    if (e.status !== "locked") throw new Error(`SETTLEMENT_FAILED: escrow status=${e.status}`);
    const platformFee = e.amount * PLATFORM_FEE_RATE;
    const amountToSeller = e.amount - platformFee;
    e.status = "settled";
    // 更新卖方 agent 结算计数（若注册过）
    for (const a of this.agents.values()) if (a.owner === e.seller) a.totalSettlements += 1;
    return { txHash: `mock_settle_${params.escrowId}`, amountToSeller, platformFee, status: "settled" as const };
  }

  async refund(params: { escrowId: string; reason: string }) {
    const e = this.escrows.get(params.escrowId);
    if (!e) throw new Error(`ESCROW_NOT_FOUND: ${params.escrowId}`);
    e.status = "refunded";
    return { txHash: `mock_refund_${params.escrowId}`, status: "refunded" as const };
  }

  async getEscrowStatus(escrowId: string) {
    const e = this.escrows.get(escrowId);
    if (!e) throw new Error(`ESCROW_NOT_FOUND: ${escrowId}`);
    return { status: e.status, amount: e.amount, buyer: e.buyer, seller: e.seller, createdAt: e.createdAt, expiresAt: e.expiresAt };
  }

  async getAgentInfo(agentId: string) {
    const a = this.agents.get(agentId);
    if (!a) throw new Error(`AGENT_NOT_FOUND: ${agentId}`);
    return { nftTokenId: a.nftTokenId, owner: a.owner, registeredAt: a.registeredAt, attestationHash: a.attestationHash, totalSettlements: a.totalSettlements };
  }

  async verifyAttestation(report: AttestationReport) {
    // mock：非空 measurement 即视为有效
    return { valid: Boolean(report.measurement), txHash: `mock_attest_${Date.now()}` };
  }
}
