import assert from "node:assert/strict";
import test from "node:test";
import type { Hex } from "viem";

import {
  authorizationNonceForIntent,
  validateArenaSettlementIntent,
  type ArenaSettlementIntent,
  type Deployments,
} from "../src/index.ts";

const BUYER = `0x${"11".repeat(20)}`;
const SELLER = `0x${"22".repeat(20)}`;
const TOKEN = `0x${"33".repeat(20)}` as Hex;
const INTENT_HASH = `sha256:${"44".repeat(32)}`;

const deployments: Deployments = {
  chainId: 1439,
  rpc: "https://rpc.invalid",
  usdc: {
    address: TOKEN,
    symbol: "mUSDC",
    decimals: 6,
    eip712Name: "Mock USD Coin",
    eip712Version: "1",
  },
  wallets: {
    buyer: BUYER,
    seller: SELLER,
    facilitator: `0x${"55".repeat(20)}`,
  },
};

function intent(
  overrides: Partial<ArenaSettlementIntent> = {},
): ArenaSettlementIntent {
  return {
    settlementIntentId: "settlement:neg-1",
    gameId: "game-1",
    status: "authorization_requested",
    buyerAccount: BUYER,
    sellerAccount: SELLER,
    amountAtomic: "7000000",
    chainId: 1439,
    tokenAddress: TOKEN,
    tokenDecimals: 6,
    authorizationMode: "single_eip3009",
    intentHash: INTENT_HASH,
    ...overrides,
  };
}

test("one frozen intent always derives the same EIP-3009 nonce", () => {
  const first = authorizationNonceForIntent(intent());
  const second = authorizationNonceForIntent(intent());

  assert.equal(first, `0x${"44".repeat(32)}`);
  assert.equal(second, first);
});

test("operator approval is bound to the exact frozen intent hash", () => {
  assert.doesNotThrow(() =>
    validateArenaSettlementIntent({
      intent: intent(),
      buyerAddress: BUYER,
      deployments,
      approvedIntentHash: INTENT_HASH,
    }),
  );

  assert.throws(
    () =>
      validateArenaSettlementIntent({
        intent: intent(),
        buyerAddress: BUYER,
        deployments,
        approvedIntentHash: `sha256:${"66".repeat(32)}`,
      }),
    /approved intent hash does not match/i,
  );
});

test("state-changing bridge rejects every mismatched frozen payment field", () => {
  const cases: Array<[Partial<ArenaSettlementIntent>, RegExp]> = [
    [{ status: "submitted" }, /not authorizable/i],
    [{ authorizationMode: "none" }, /single_eip3009/i],
    [{ chainId: 1 }, /chain does not match/i],
    [{ tokenAddress: `0x${"77".repeat(20)}` }, /token does not match/i],
    [{ tokenDecimals: 18 }, /token decimals/i],
    [{ buyerAccount: `0x${"88".repeat(20)}` }, /signer/i],
    [{ sellerAccount: BUYER }, /payer and payee must differ/i],
    [{ amountAtomic: "0" }, /amount must be positive/i],
    [{ amountAtomic: "7.5" }, /amount must be an integer/i],
    [{ intentHash: "sha256:not-a-hash" }, /valid sha256 intentHash/i],
  ];

  for (const [override, expected] of cases) {
    assert.throws(
      () =>
        validateArenaSettlementIntent({
          intent: intent(override),
          buyerAddress: BUYER,
          deployments,
          approvedIntentHash:
            override.intentHash === undefined
              ? INTENT_HASH
              : override.intentHash,
        }),
      expected,
    );
  }
});
