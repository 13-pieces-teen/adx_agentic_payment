/**
 * SETTLE-002 · Settlement SDK 接口契约（对齐 AGENT_ARENA_FULL_SPEC Part 5 Interface B）
 *
 * arena 队友按此接口集成，MockSettlement / RealSettlement 二选一注入。
 * 接口签名与全量 spec 逐字一致，勿改（变更需通知 arena）。
 */

export interface AttestationReport {
  enclaveId: string;
  measurement: string; // hex
  data: string; // hex
  signature: string; // hex
  platformInfo: string;
  timestamp: number;
}

export interface SettlementSDK {
  /** 注册 Agent 链上身份 (mint ERC-8004 NFT) */
  registerAgent(params: {
    agentId: string;
    ownerWallet: string;
    attestation: AttestationReport;
    metadata?: Record<string, string>;
  }): Promise<{ nftTokenId: string; txHash: string }>;

  /** 锁定资金到 Escrow（直接结算模式下：记录待结算意图 + 校验签名，不真锁链上） */
  lockFunds(params: {
    negotiationId: string;
    buyerWallet: string;
    sellerWallet: string;
    amount: number;
    currency: "USDC";
    x402Signature: string; // 承载 SETTLE-003 PaymentAuthorization 的 JSON
    expiry: number;
  }): Promise<{ escrowId: string; txHash: string; status: "locked" }>;

  /** 结算交易（经 facilitator 上链，mUSDC 买方→卖方） */
  settleTrade(params: {
    escrowId: string;
    deliveryProof?: string;
  }): Promise<{ txHash: string; amountToSeller: number; platformFee: number; status: "settled" }>;

  /** 退款 */
  refund(params: {
    escrowId: string;
    reason: string;
  }): Promise<{ txHash: string; status: "refunded" }>;

  /** 查询 Escrow 状态 */
  getEscrowStatus(escrowId: string): Promise<{
    status: "locked" | "settled" | "refunded" | "expired";
    amount: number;
    buyer: string;
    seller: string;
    createdAt: number;
    expiresAt: number;
  }>;

  /** 查询 Agent 链上信息 */
  getAgentInfo(agentId: string): Promise<{
    nftTokenId: string;
    owner: string;
    registeredAt: number;
    attestationHash: string;
    totalSettlements: number;
  }>;

  /** 验证 TEE attestation (链上) */
  verifyAttestation(report: AttestationReport): Promise<{ valid: boolean; txHash: string }>;
}

/** 平台手续费率（spec §5.1：demo 阶段可关闭，见 D5）。默认 0.5%。 */
export const PLATFORM_FEE_RATE = 0.005;
