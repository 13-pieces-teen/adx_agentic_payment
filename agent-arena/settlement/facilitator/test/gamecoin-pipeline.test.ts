import assert from "node:assert/strict";
import test from "node:test";

import {
  nextSafeGameCoinNonce,
  runGameCoinProvisioningPipeline,
  type GameCoinPipelineAdapter,
} from "../src/gamecoin-pipeline.ts";

type Pending = { id: string };
type Prepared = { id: string };
type Signed = { id: string; nonce: number };

test("wallet preparation fills available in-flight capacity without waiting", async () => {
  const durable: Signed[] = [];
  const broadcast: Signed[] = [];
  const adapter: GameCoinPipelineAdapter<Pending, string, Prepared, Signed> = {
    listSubmitted: async () => ["already-submitted"],
    reconcile: async () => undefined,
    countInflight: async () => 1,
    listPending: async (limit) =>
      [{ id: "wallet-a" }, { id: "wallet-b" }, { id: "wallet-c" }].slice(
        0,
        limit,
      ),
    prepare: async (pending) => pending,
    getPendingNonce: async () => 42,
    sign: async (prepared, nonce) => ({ ...prepared, nonce }),
    persist: async (signed) => {
      durable.push(signed);
      return true;
    },
    broadcast: async (signed) => {
      broadcast.push(signed);
    },
  };

  const result = await runGameCoinProvisioningPipeline(adapter, 3);

  assert.deepEqual(result, {
    reconciled: 1,
    prepared: 2,
    submitted: 2,
    inflight: 3,
  });
  assert.deepEqual(durable, [
    { id: "wallet-a", nonce: 42 },
    { id: "wallet-b", nonce: 43 },
  ]);
  assert.deepEqual(broadcast, durable);
});

test("wallet preparation never reuses a durably reserved nonce", () => {
  assert.equal(nextSafeGameCoinNonce(42, 44), 45);
  assert.equal(nextSafeGameCoinNonce(46, 44), 46);
  assert.equal(nextSafeGameCoinNonce(42, null), 42);
});

test("wallet preparation stops the nonce stream after a broadcast failure", async () => {
  const durable: Signed[] = [];
  const attempted: Signed[] = [];
  const adapter: GameCoinPipelineAdapter<Pending, string, Prepared, Signed> = {
    listSubmitted: async () => [],
    reconcile: async () => undefined,
    countInflight: async () => 0,
    listPending: async () => [
      { id: "wallet-a" },
      { id: "wallet-b" },
      { id: "wallet-c" },
    ],
    prepare: async (pending) => pending,
    getPendingNonce: async () => 7,
    sign: async (prepared, nonce) => ({ ...prepared, nonce }),
    persist: async (signed) => {
      durable.push(signed);
      return true;
    },
    broadcast: async (signed) => {
      attempted.push(signed);
      if (signed.id === "wallet-b") throw new Error("rpc_unavailable");
    },
  };

  await assert.rejects(
    runGameCoinProvisioningPipeline(adapter, 3),
    /rpc_unavailable/,
  );
  assert.deepEqual(durable, [
    { id: "wallet-a", nonce: 7 },
    { id: "wallet-b", nonce: 8 },
  ]);
  assert.deepEqual(attempted, durable);
});

test("wallet preparation cannot exceed its in-flight limit", async () => {
  const broadcast: Signed[] = [];
  const adapter: GameCoinPipelineAdapter<Pending, string, Prepared, Signed> = {
    listSubmitted: async () => [],
    reconcile: async () => undefined,
    countInflight: async () => 0,
    listPending: async () => [
      { id: "wallet-a" },
      { id: "wallet-b" },
      { id: "wallet-c" },
    ],
    prepare: async (pending) => pending,
    getPendingNonce: async () => 20,
    sign: async (prepared, nonce) => ({ ...prepared, nonce }),
    persist: async () => true,
    broadcast: async (signed) => {
      broadcast.push(signed);
    },
  };

  const result = await runGameCoinProvisioningPipeline(adapter, 2);

  assert.equal(result.inflight, 2);
  assert.deepEqual(broadcast, [
    { id: "wallet-a", nonce: 20 },
    { id: "wallet-b", nonce: 21 },
  ]);
});
