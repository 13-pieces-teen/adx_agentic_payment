import assert from "node:assert/strict";
import test from "node:test";

import {
  findMatchingMintEvidence,
  type MintEventEvidence,
} from "../src/gamecoin-recovery.ts";

test("accepts only exact mint evidence for the frozen transaction", () => {
  const expectedHash =
    "0xf46165f8cd5ed15e8a840e94d74af02b4bc4fbb3f7e2eda5ed12018d4dcfcd84";
  const target = "0x98AC9AE2B22ce472e785536ceD1563f237c08418";
  const logs: MintEventEvidence[] = [
    {
      transactionHash: expectedHash,
      blockNumber: 134844354n,
      to: target,
      value: 12_000_000n,
    },
  ];

  assert.equal(
    findMatchingMintEvidence(logs, {
      transactionHash: expectedHash,
      to: target,
      value: 12_000_000n,
    }),
    134844354n,
  );
  assert.equal(
    findMatchingMintEvidence(logs, {
      transactionHash: expectedHash,
      to: target,
      value: 12_000_001n,
    }),
    null,
  );
  assert.equal(
    findMatchingMintEvidence(logs, {
      transactionHash:
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      to: target,
      value: 12_000_000n,
    }),
    null,
  );
});
