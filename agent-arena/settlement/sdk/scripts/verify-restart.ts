/**
 * Verify the second half of the settlement crash drill without signing or
 * broadcasting. Run submit-only.ts with --evidence-out, restart Arena Worker,
 * then run this script against the same public SettlementIntent.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  verifyRestartEvidence,
  type InventoryCommitReceipt,
  type SettlementProjection,
} from "../src/index.ts";

interface JsonObject {
  [key: string]: unknown;
}

interface SubmissionEvidence {
  schemaVersion: "arena402.restart-drill-submission.v1";
  settlementIntentId: string;
  txHash: string;
  arenaStatusAfterSubmission: string;
}

function argumentValue(name: string, required = true): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) {
    if (required) throw new Error(`${name} is required`);
    return undefined;
  }
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a value`);
  }
  return value;
}

function positiveInteger(name: string, fallback: number): number {
  const raw = argumentValue(name, false);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

function object(value: unknown, label: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} is not a JSON object`);
  }
  return value as JsonObject;
}

async function jsonGet(url: string): Promise<JsonObject> {
  const response = await fetch(url);
  const body = object(await response.json(), `HTTP ${response.status} body`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${JSON.stringify(body)}`);
  }
  return body;
}

function loadSubmissionEvidence(path: string): SubmissionEvidence {
  const parsed = object(
    JSON.parse(readFileSync(resolve(process.cwd(), path), "utf8")),
    "submission evidence",
  );
  if (
    parsed.schemaVersion !== "arena402.restart-drill-submission.v1" ||
    typeof parsed.settlementIntentId !== "string" ||
    typeof parsed.txHash !== "string" ||
    typeof parsed.arenaStatusAfterSubmission !== "string"
  ) {
    throw new Error("submission evidence has an invalid schema");
  }
  return parsed as unknown as SubmissionEvidence;
}

function settlementProjection(
  value: unknown,
  intentId: string,
): SettlementProjection {
  const row = object(value, "settlement projection");
  if (
    row.settlementIntentId !== intentId ||
    typeof row.status !== "string" ||
    (row.txHash !== null && typeof row.txHash !== "string")
  ) {
    throw new Error("settlement projection has an invalid schema");
  }
  return {
    settlementIntentId: intentId,
    status: row.status,
    txHash: row.txHash,
  };
}

function inventoryCommit(value: JsonObject): InventoryCommitReceipt {
  return value as unknown as InventoryCommitReceipt;
}

async function loadIntent(
  arenaUrl: string,
  gameId: string,
  intentId: string,
): Promise<SettlementProjection> {
  const response = await jsonGet(
    `${arenaUrl}/api/v1/pawnhouse/games/${encodeURIComponent(gameId)}` +
      "/settlement-intents",
  );
  const values = response.settlementIntents;
  if (!Array.isArray(values)) {
    throw new Error("Arena omitted settlementIntents");
  }
  const selected = values.find(
    (value) =>
      typeof value === "object" &&
      value !== null &&
      (value as JsonObject).settlementIntentId === intentId,
  );
  if (selected === undefined) {
    throw new Error("SettlementIntent was not found");
  }
  return settlementProjection(selected, intentId);
}

async function main(): Promise<void> {
  const evidence = loadSubmissionEvidence(
    argumentValue("--before-evidence") as string,
  );
  const gameId = argumentValue("--game-id") as string;
  const arenaUrl = (
    process.env.ARENA_API_URL ?? "http://127.0.0.1:8000"
  ).replace(/\/+$/, "");
  const timeoutSeconds = positiveInteger("--timeout-seconds", 180);
  const stabilityWaitMs = positiveInteger("--stability-wait-ms", 3_000);
  const before: SettlementProjection = {
    settlementIntentId: evidence.settlementIntentId,
    status: evidence.arenaStatusAfterSubmission,
    txHash: evidence.txHash,
  };
  const deadline = Date.now() + timeoutSeconds * 1_000;
  let after = await loadIntent(
    arenaUrl,
    gameId,
    evidence.settlementIntentId,
  );
  while (after.status !== "inventory_committed" && Date.now() < deadline) {
    if (["reverted", "submission_failed", "expired"].includes(after.status)) {
      throw new Error(`Settlement reached terminal failure: ${after.status}`);
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 2_000));
    after = await loadIntent(
      arenaUrl,
      gameId,
      evidence.settlementIntentId,
    );
  }
  if (after.status !== "inventory_committed") {
    throw new Error("Timed out waiting for Arena Worker inventory commit");
  }
  const commitUrl =
    `${arenaUrl}/api/v1/pawnhouse/settlement-intents/` +
    `${encodeURIComponent(evidence.settlementIntentId)}/inventory-commit`;
  const firstCommit = inventoryCommit(await jsonGet(commitUrl));
  await new Promise((resolveWait) => setTimeout(resolveWait, stabilityWaitMs));
  const replayCommit = inventoryCommit(await jsonGet(commitUrl));
  const summary = verifyRestartEvidence({
    expectedTxHash: evidence.txHash,
    before,
    after,
    firstCommit,
    replayCommit,
  });
  console.log(
    JSON.stringify(
      {
        ...summary,
        blockscout:
          `https://testnet.blockscout.injective.network/tx/${summary.txHash}`,
        workerRecoveryObserved: true,
      },
      null,
      2,
    ),
  );
}

main().catch((error: unknown) => {
  const message =
    error instanceof Error ? error.message : "unknown_restart_verification";
  console.error(`Restart verification failed: ${message}`);
  process.exit(1);
});
