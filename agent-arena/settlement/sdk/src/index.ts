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
  signTransferAuthorizationWithWallet,
  verifyAuthorizationLocally,
  checkDomainMatchesChain,
  loadDeployments,
} from "./x402.ts";
export type { SignParams, WalletSignParams } from "./x402.ts";
export {
  createWalletSecretStore,
  DisabledWalletSecretStore,
  WalletSigningError,
} from "./wallet-secret-store.ts";
export type {
  SignEip3009AuthorizationRequest,
  WalletAuthorizationSignature,
  WalletSecretStore,
  WalletSigningErrorCode,
} from "./wallet-secret-store.ts";
export {
  authorizationNonceForIntent,
  validateArenaSettlementIntent,
} from "./arena.ts";
export type {
  ArenaSettlementIntent,
  ValidateArenaSettlementIntentParams,
} from "./arena.ts";
export { verifyRestartEvidence } from "./restart.ts";
export type {
  InventoryCommitReceipt,
  RestartEvidence,
  RestartVerificationSummary,
  SettlementProjection,
} from "./restart.ts";
export type { PaymentAuthorization, Deployments, SettleResult } from "./types.ts";
