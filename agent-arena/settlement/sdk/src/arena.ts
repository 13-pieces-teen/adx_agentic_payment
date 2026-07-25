import { getAddress, type Hex } from "viem";

import type { Deployments } from "./types.ts";

const INTENT_HASH = /^sha256:([0-9a-f]{64})$/;

export interface ArenaSettlementIntent {
  settlementIntentId: string;
  gameId: string;
  status: string;
  buyerAccount: string;
  sellerAccount: string;
  amountAtomic: string;
  chainId: number;
  tokenAddress: string;
  tokenDecimals: number;
  authorizationMode: string;
  intentHash: string;
}

export interface ValidateArenaSettlementIntentParams {
  intent: ArenaSettlementIntent;
  buyerAddress: string;
  deployments: Deployments;
  approvedIntentHash: string;
}

export function authorizationNonceForIntent(
  intent: Pick<ArenaSettlementIntent, "intentHash">,
): Hex {
  const match = INTENT_HASH.exec(intent.intentHash);
  if (match === null) {
    throw new Error("Intent omitted a valid sha256 intentHash");
  }
  return `0x${match[1]}`;
}

export function validateArenaSettlementIntent({
  intent,
  buyerAddress,
  deployments,
  approvedIntentHash,
}: ValidateArenaSettlementIntentParams): void {
  if (intent.status !== "authorization_requested") {
    throw new Error(`Intent is not authorizable: ${intent.status}`);
  }
  if (intent.authorizationMode !== "single_eip3009") {
    throw new Error("Intent does not use single_eip3009");
  }
  if (intent.chainId !== deployments.chainId) {
    throw new Error("Intent chain does not match deployments.json");
  }
  if (
    getAddress(intent.tokenAddress) !==
    getAddress(deployments.usdc.address)
  ) {
    throw new Error("Intent token does not match deployments.json");
  }
  if (intent.tokenDecimals !== deployments.usdc.decimals) {
    throw new Error("Intent token decimals do not match deployments.json");
  }
  if (getAddress(intent.buyerAccount) !== getAddress(buyerAddress)) {
    throw new Error("Local buyer signer does not own the frozen payer");
  }
  if (getAddress(intent.buyerAccount) === getAddress(intent.sellerAccount)) {
    throw new Error("Frozen payer and payee must differ");
  }
  let amount: bigint;
  try {
    amount = BigInt(intent.amountAtomic);
  } catch {
    throw new Error("Intent amount must be an integer");
  }
  if (amount <= 0n) {
    throw new Error("Intent amount must be positive");
  }
  authorizationNonceForIntent(intent);
  if (approvedIntentHash !== intent.intentHash) {
    throw new Error("Approved intent hash does not match frozen intent");
  }
}
