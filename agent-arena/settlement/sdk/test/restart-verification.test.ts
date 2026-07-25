import assert from "node:assert/strict";
import test from "node:test";

import {
  verifyRestartEvidence,
  type InventoryCommitReceipt,
} from "../src/index.ts";

const TX_HASH = `0x${"11".repeat(32)}`;

function receipt(
  overrides: Partial<InventoryCommitReceipt> = {},
): InventoryCommitReceipt {
  return {
    settlementIntentId: "settlement:neg-1",
    status: "inventory_committed",
    inventoryCommitId: "inventory-commit:settlement:neg-1",
    buyerCashBeforeAtomic: "10000000",
    buyerCashAfterAtomic: "3000000",
    sellerCashBeforeAtomic: "2000000",
    sellerCashAfterAtomic: "9000000",
    buyerHoldingBefore: 0,
    buyerHoldingAfter: 1,
    sellerHoldingBefore: 1,
    sellerHoldingAfter: 0,
    ...overrides,
  };
}

test("restart evidence keeps one tx hash and one stable inventory commit", () => {
  const summary = verifyRestartEvidence({
    expectedTxHash: TX_HASH,
    before: {
      settlementIntentId: "settlement:neg-1",
      status: "submitted",
      txHash: TX_HASH,
    },
    after: {
      settlementIntentId: "settlement:neg-1",
      status: "inventory_committed",
      txHash: TX_HASH,
    },
    firstCommit: receipt(),
    replayCommit: receipt(),
  });

  assert.equal(summary.txHash, TX_HASH);
  assert.equal(summary.inventoryCommitId, receipt().inventoryCommitId);
  assert.equal(summary.paymentReplayProtected, true);
  assert.equal(summary.inventoryReplayProtected, true);
});

test("restart evidence rejects a second or changed inventory commit", () => {
  assert.throws(
    () =>
      verifyRestartEvidence({
        expectedTxHash: TX_HASH,
        before: {
          settlementIntentId: "settlement:neg-1",
          status: "submitted",
          txHash: TX_HASH,
        },
        after: {
          settlementIntentId: "settlement:neg-1",
          status: "inventory_committed",
          txHash: TX_HASH,
        },
        firstCommit: receipt(),
        replayCommit: receipt({ buyerHoldingAfter: 2 }),
      }),
    /inventory commit changed after restart/i,
  );
});
