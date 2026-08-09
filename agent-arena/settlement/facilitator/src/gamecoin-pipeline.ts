export interface GameCoinPipelineAdapter<
  Pending,
  Submitted,
  Prepared,
  Signed,
> {
  listSubmitted(limit: number): Promise<readonly Submitted[]>;
  reconcile(submitted: Submitted): Promise<void>;
  countInflight(): Promise<number>;
  listPending(limit: number): Promise<readonly Pending[]>;
  prepare(pending: Pending): Promise<Prepared | null>;
  getPendingNonce(): Promise<number>;
  sign(prepared: Prepared, nonce: number): Promise<Signed>;
  persist(signed: Signed): Promise<boolean>;
  broadcast(signed: Signed): Promise<void>;
}

export type GameCoinPipelineResult = {
  reconciled: number;
  prepared: number;
  submitted: number;
  inflight: number;
};

export function nextSafeGameCoinNonce(
  rpcPendingNonce: number,
  durableMaxNonce: number | null,
): number {
  if (!Number.isSafeInteger(rpcPendingNonce) || rpcPendingNonce < 0) {
    throw new Error("gamecoin_rpc_nonce_invalid");
  }
  if (
    durableMaxNonce !== null &&
    (!Number.isSafeInteger(durableMaxNonce) || durableMaxNonce < 0)
  ) {
    throw new Error("gamecoin_durable_nonce_invalid");
  }
  return Math.max(rpcPendingNonce, (durableMaxNonce ?? -1) + 1);
}

export async function runGameCoinProvisioningPipeline<
  Pending,
  Submitted,
  Prepared,
  Signed,
>(
  adapter: GameCoinPipelineAdapter<Pending, Submitted, Prepared, Signed>,
  maxInflight: number,
): Promise<GameCoinPipelineResult> {
  if (!Number.isSafeInteger(maxInflight) || maxInflight < 1) {
    throw new Error("gamecoin_max_inflight_invalid");
  }

  const submitted = await adapter.listSubmitted(maxInflight);
  await Promise.all(submitted.map((attempt) => adapter.reconcile(attempt)));

  const inflight = await adapter.countInflight();
  if (!Number.isSafeInteger(inflight) || inflight < 0) {
    throw new Error("gamecoin_inflight_count_invalid");
  }
  const available = Math.max(0, maxInflight - inflight);
  if (available === 0) {
    return {
      reconciled: submitted.length,
      prepared: 0,
      submitted: 0,
      inflight,
    };
  }

  const pending = (await adapter.listPending(available)).slice(0, available);
  const preparedResults = await Promise.all(
    pending.map((item) => adapter.prepare(item)),
  );
  const prepared = preparedResults.filter(
    (item) => item !== null,
  ) as Prepared[];
  if (prepared.length === 0) {
    return {
      reconciled: submitted.length,
      prepared: 0,
      submitted: 0,
      inflight,
    };
  }

  let nonce = await adapter.getPendingNonce();
  let submittedCount = 0;
  for (const item of prepared) {
    const signed = await adapter.sign(item, nonce);
    if (!(await adapter.persist(signed))) continue;
    await adapter.broadcast(signed);
    nonce += 1;
    submittedCount += 1;
  }

  return {
    reconciled: submitted.length,
    prepared: prepared.length,
    submitted: submittedCount,
    inflight: inflight + submittedCount,
  };
}
