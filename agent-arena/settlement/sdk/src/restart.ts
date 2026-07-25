export interface SettlementProjection {
  settlementIntentId: string;
  status: string;
  txHash: string | null;
}

export interface InventoryCommitReceipt {
  settlementIntentId: string;
  status: string;
  inventoryCommitId: string;
  buyerCashBeforeAtomic: string;
  buyerCashAfterAtomic: string;
  sellerCashBeforeAtomic: string;
  sellerCashAfterAtomic: string;
  buyerHoldingBefore: number;
  buyerHoldingAfter: number;
  sellerHoldingBefore: number;
  sellerHoldingAfter: number;
}

export interface RestartEvidence {
  expectedTxHash: string;
  before: SettlementProjection;
  after: SettlementProjection;
  firstCommit: InventoryCommitReceipt;
  replayCommit: InventoryCommitReceipt;
}

export interface RestartVerificationSummary {
  settlementIntentId: string;
  txHash: string;
  inventoryCommitId: string;
  paymentReplayProtected: true;
  inventoryReplayProtected: true;
}

function commitFingerprint(receipt: InventoryCommitReceipt): string {
  return JSON.stringify([
    receipt.settlementIntentId,
    receipt.status,
    receipt.inventoryCommitId,
    receipt.buyerCashBeforeAtomic,
    receipt.buyerCashAfterAtomic,
    receipt.sellerCashBeforeAtomic,
    receipt.sellerCashAfterAtomic,
    receipt.buyerHoldingBefore,
    receipt.buyerHoldingAfter,
    receipt.sellerHoldingBefore,
    receipt.sellerHoldingAfter,
  ]);
}

export function verifyRestartEvidence({
  expectedTxHash,
  before,
  after,
  firstCommit,
  replayCommit,
}: RestartEvidence): RestartVerificationSummary {
  const normalizedExpected = expectedTxHash.toLowerCase();
  if (!/^0x[0-9a-f]{64}$/.test(normalizedExpected)) {
    throw new Error("Expected transaction hash is invalid");
  }
  if (
    before.txHash?.toLowerCase() !== normalizedExpected ||
    after.txHash?.toLowerCase() !== normalizedExpected
  ) {
    throw new Error("Settlement transaction hash changed after restart");
  }
  if (
    !["submitted", "confirmation_timeout", "chain_confirmed_uncommitted"].includes(
      before.status,
    )
  ) {
    throw new Error(`Unexpected pre-restart status: ${before.status}`);
  }
  if (after.status !== "inventory_committed") {
    throw new Error(`Settlement is not inventory_committed: ${after.status}`);
  }
  if (
    before.settlementIntentId !== after.settlementIntentId ||
    before.settlementIntentId !== firstCommit.settlementIntentId ||
    firstCommit.settlementIntentId !== replayCommit.settlementIntentId
  ) {
    throw new Error("Restart evidence refers to different settlement intents");
  }
  if (
    firstCommit.status !== "inventory_committed" ||
    firstCommit.buyerHoldingAfter !==
      firstCommit.buyerHoldingBefore + 1 ||
    firstCommit.sellerHoldingAfter !==
      firstCommit.sellerHoldingBefore - 1
  ) {
    throw new Error("Inventory commit receipt has invalid holding deltas");
  }
  const buyerCashDelta =
    BigInt(firstCommit.buyerCashBeforeAtomic) -
    BigInt(firstCommit.buyerCashAfterAtomic);
  const sellerCashDelta =
    BigInt(firstCommit.sellerCashAfterAtomic) -
    BigInt(firstCommit.sellerCashBeforeAtomic);
  if (buyerCashDelta <= 0n || buyerCashDelta !== sellerCashDelta) {
    throw new Error("Inventory commit receipt has invalid cash deltas");
  }
  if (commitFingerprint(firstCommit) !== commitFingerprint(replayCommit)) {
    throw new Error("Inventory commit changed after restart");
  }
  return {
    settlementIntentId: before.settlementIntentId,
    txHash: normalizedExpected,
    inventoryCommitId: firstCommit.inventoryCommitId,
    paymentReplayProtected: true,
    inventoryReplayProtected: true,
  };
}
