/**
 * item4 crash-drill helper: sign + facilitator settle + record submission,
 * then STOP. Deliberately does NOT call recover-confirmation, leaving the
 * intent at status `submitted` to simulate a process crash after broadcast.
 *
 * This reuses the exact SDK signing + facilitator relay path as
 * settle-arena-intent.ts; it only omits the recovery loop.
 */
import "dotenv/config";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { getAddress, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import {
  loadDeployments,
  signTransferAuthorization,
  verifyAuthorizationLocally,
  type PaymentAuthorization,
} from "../src/index.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(resolve(__dirname, "../../.env"));

const arenaUrl = (process.env.ARENA_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
const facilitatorUrl = (process.env.FACILITATOR_URL ?? "http://127.0.0.1:4021").replace(/\/+$/, "");
const gameId = process.env.ARENA_GAME_ID;
const requestedIntentId = process.env.ARENA_SETTLEMENT_INTENT_ID;
const devToken = process.env.ARENA_DEV_TOKEN;
const buyerPrivateKey = process.env.BUYER_PRIVATE_KEY as Hex | undefined;
const confirmed = process.argv.includes("--confirm-testnet-transfer");

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
  const candidates = (list.settlementIntents ?? []).filter((v: any) => v && typeof v === "object");
  const intent = requestedIntentId
    ? candidates.find((v: any) => v.settlementIntentId === requestedIntentId)
    : candidates.length === 1 ? candidates[0] : undefined;
  if (!intent) throw new Error("select one intent with ARENA_SETTLEMENT_INTENT_ID");
  if (intent.status !== "authorization_requested")
    throw new Error(`intent not authorizable: ${intent.status}`);
  if (getAddress(intent.buyerAccount) !== getAddress(buyer.address))
    throw new Error("local buyer signer does not own the frozen payer");

  console.log(JSON.stringify({ step: "preflight", settlementIntentId: intent.settlementIntentId, amountAtomic: intent.amountAtomic, confirmed }, null, 2));
  if (!confirmed) throw new Error("refusing chain submission without --confirm-testnet-transfer");

  const authorization = await signTransferAuthorization({
    account: buyer,
    to: intent.sellerAccount,
    value: BigInt(intent.amountAtomic),
    dep,
    nowSeconds: Math.floor(Date.now() / 1000),
  });
  if (!(await verifyAuthorizationLocally(authorization, dep))) throw new Error("local EIP-3009 verify failed");

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
    { method: "POST", headers: arenaHeaders(), body: JSON.stringify({ txHash, authorizationNonce: authorization.nonce, submissionSource: "wallet", humanConfirmed: true }) },
  );

  console.log(JSON.stringify({
    step: "submitted_and_stopped",
    settlementIntentId: intent.settlementIntentId,
    txHash,
    arenaStatusAfterSubmission: submission.status,
    nonce: authorization.nonce,
    note: "recover-confirmation intentionally NOT called (simulating crash)",
    blockscout: `https://testnet.blockscout.injective.network/tx/${txHash}`,
  }, null, 2));
}

main().catch((e) => { console.error(`submit-only failed: ${e instanceof Error ? e.message : e}`); process.exit(1); });
