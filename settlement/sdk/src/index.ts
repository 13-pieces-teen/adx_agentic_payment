/**
 * @agent-arena/settlement-sdk — 统一导出
 *
 * arena 队友：
 *   import { MockSettlement, RealSettlement, type SettlementSDK } from "@agent-arena/settlement-sdk";
 *
 * TEE 队友（签名）：
 *   import { signTransferAuthorization } from "@agent-arena/settlement-sdk";
 */
export type { SettlementSDK, AttestationReport } from "./settlement.ts";
export { PLATFORM_FEE_RATE } from "./settlement.ts";
export { MockSettlement } from "./mock.ts";
export { RealSettlement, type RealSettlementConfig } from "./real.ts";

// x402 签名（SETTLE-003）
export {
  signTransferAuthorization,
  verifyAuthorizationLocally,
  checkDomainMatchesChain,
  loadDeployments,
} from "./x402.ts";
export type { PaymentAuthorization, Deployments, SettleResult } from "./types.ts";
