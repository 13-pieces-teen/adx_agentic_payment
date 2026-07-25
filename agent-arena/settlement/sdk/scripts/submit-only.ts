/**
 * item4 crash-drill helper: sign + facilitator settle + record submission,
 * then STOP. Deliberately does NOT call recover-confirmation, leaving the
 * intent at status `submitted` to simulate a process crash after broadcast.
 *
 * This reuses the exact SDK signing + facilitator relay path as
 * settle-arena-intent.ts; it only omits the recovery loop.
 */
import "dotenv/config";
import { writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import {
  authorizationNonceForIntent,
  loadDeployments,
  signTransferAuthorization,
  validateArenaSettlementIntent,
  verifyAuthorizationLocally,
  type ArenaSettlementIntent,
  type PaymentAuthorization,
} from "../src/index.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(resolve(__dirname, "../../.env"));

const arenaUrl = (process.env.ARENA_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const facilitatorUrl = (process.env.FACILITATOR_URL ?? "http://127.0.0.1:4021").replace(/\/+$/, "");
const gameId = argumentValue("--game-id") ?? process.env.ARENA_GAME_ID;
const requestedIntentId =
  argumentValue("--intent-id") ??
  process.env.ARENA_SETTLEMENT_INTENT_ID;
const devToken = process.env.ARENA_DEV_TOKEN;
const buyerPrivateKey = process.env.BUYER_PRIVATE_KEY as Hex | undefined;
const confirmed = process.argv.includes("--confirm-testnet-transfer");
const approvedIntentHash =
  argumentValue("--approved-intent-hash") ??
  process.env.ARENA_APPROVED_INTENT_HASH;
const evidenceOut = argumentValue("--evidence-out");

function argumentValue(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  if (index < 0) return undefined;
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a value`);
  }
  return value;
}

function required(value: string | undefined, name: string): string {
  if (!value) throw new Error(`${name} is required`);
  return value;
}

async function jsonRequest(url: string, init: RequestInit = {}): Promise<any> {
  const response = await fetch(url, init);
  const body = await response.json();
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${JSON.stringify(body)}`);
  return body;
}

function arenaHeaders(): Record<string, string> {
  return { "content-type": "application/json", "x-arena-dev-token": required(devToken, "ARENA_DEV_TOKEN") };
}

async function main(): Promise<void> {
  const privateKey = required(buyerPrivateKey, "BUYER_PRIVATE_KEY") as Hex;
  const buyer = privateKeyToAccount(privateKey);
  const game = required(gameId, "ARENA_GAME_ID");
  const dep = loadDeployments(resolve(__dirname, "../../deployments.json"));

  const list = await jsonRequest(`${arenaUrl}/api/v1/pawnhouse/games/${encodeURIComponent(game)}/settlement-intents`);
  const rawCandidates: unknown = list.settlementIntents;
  const candidates = Array.isArray(rawCandidates)
    ? rawCandidates.filter(
        (value: unknown): value is ArenaSettlementIntent =>
          typeof value === "object" && value !== null,
      )
    : [];
  const intent = requestedIntentId
    ? candidates.find(
        (value) => value.settlementIntentId === requestedIntentId,
      )
    : candidates.length === 1 ? candidates[0] : undefined;
  if (!intent) throw new Error("select one intent with ARENA_SETTLEMENT_INTENT_ID");
  const approvedHash = required(
    approvedIntentHash,
    "--approved-intent-hash or ARENA_APPROVED_INTENT_HASH",
  );
  validateArenaSettlementIntent({
    intent,
    buyerAddress: buyer.address,
    deployments: dep,
    approvedIntentHash: approvedHash,
  });
  const authorizationNonce = authorizationNonceForIntent(intent);

  console.log(JSON.stringify({
    step: "preflight",
    settlementIntentId: intent.settlementIntentId,
    intentHash: intent.intentHash,
    chainId: intent.chainId,
    tokenAddress: intent.tokenAddress,
    buyerAccount: intent.buyerAccount,
    sellerAccount: intent.sellerAccount,
    amountAtomic: intent.amountAtomic,
    confirmed,
  }, null, 2));
  if (!confirmed) throw new Error("refusing chain submission without --confirm-testnet-transfer");

  const authorization = await signTransferAuthorization({
    account: buyer,
    to: intent.sellerAccount,
    value: BigInt(intent.amountAtomic),
    dep,
    nonce: authorizationNonce,
    nowSeconds: Math.floor(Date.now() / 1000),
  });
  if (!(await verifyAuthorizationLocally(authorization, dep))) throw new Error("local EIP-3009 verify failed");

  await jsonRequest(
    `${arenaUrl}/api/dev/pawnhouse/settlement-intents/${encodeURIComponent(intent.settlementIntentId)}/approval`,
    {
      method: "POST",
      headers: arenaHeaders(),
      body: JSON.stringify({
        approvedIntentHash: approvedHash,
        authorizationNonce: authorization.nonce,
        approvalSource: "operator_cli",
        humanConfirmed: true,
      }),
    },
  );

  const verified = await jsonRequest(`${facilitatorUrl}/verify`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(authorization),
  });
  if (verified.ok !== true) throw new Error(`facilitator /verify rejected: ${verified.reason}`);

  const settled = await jsonRequest(`${facilitatorUrl}/settle`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(authorization as PaymentAuthorization),
  });
  if (settled.status !== "success" || typeof settled.txHash !== "string")
    throw new Error(`facilitator /settle failed: ${JSON.stringify(settled)}`);
  const txHash = settled.txHash;

  const submission = await jsonRequest(
    `${arenaUrl}/api/dev/pawnhouse/settlement-intents/${encodeURIComponent(intent.settlementIntentId)}/submission`,
    {
      method: "POST",
      headers: arenaHeaders(),
      body: JSON.stringify({
        txHash,
        authorizationNonce: authorization.nonce,
        approvedIntentHash: approvedHash,
        submissionSource: "wallet",
        humanConfirmed: true,
      }),
    },
  );

  const publicEvidence = {
    schemaVersion: "arena402.restart-drill-submission.v1",
    step: "submitted_and_stopped",
    settlementIntentId: intent.settlementIntentId,
    intentHash: intent.intentHash,
    txHash,
    arenaStatusAfterSubmission: submission.status,
    note: "recover-confirmation intentionally NOT called (simulating crash)",
    blockscout: `https://testnet.blockscout.injective.network/tx/${txHash}`,
    recordedAt: new Date().toISOString(),
  };
  if (evidenceOut !== undefined) {
    writeFileSync(
      resolve(process.cwd(), evidenceOut),
      `${JSON.stringify(publicEvidence, null, 2)}\n`,
      { encoding: "utf8", flag: "wx" },
    );
  }
  console.log(JSON.stringify(publicEvidence, null, 2));
}

main().catch((e) => { console.error(`submit-only failed: ${e instanceof Error ? e.message : e}`); process.exit(1); });
