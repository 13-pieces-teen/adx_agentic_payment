/**
 * Explicitly confirmed Arena SettlementIntent -> EIP-3009 direct relay bridge.
 *
 * The buyer private key stays in this local process. Arena receives only the
 * public transaction hash and a digest of the authorization nonce. This script
 * refuses to submit unless --confirm-testnet-transfer is present.
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

interface SettlementIntent {
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
}

interface JsonObject {
  [key: string]: unknown;
}

const arenaUrl = (process.env.ARENA_API_URL ?? "http://127.0.0.1:8000")
  .replace(/\/+$/, "");
const facilitatorUrl = (
  process.env.FACILITATOR_URL ?? "http://127.0.0.1:4021"
).replace(/\/+$/, "");
const gameId = process.env.ARENA_GAME_ID;
const requestedIntentId = process.env.ARENA_SETTLEMENT_INTENT_ID;
const devToken = process.env.ARENA_DEV_TOKEN;
const buyerPrivateKey = process.env.BUYER_PRIVATE_KEY as Hex | undefined;
const confirmed = process.argv.includes("--confirm-testnet-transfer");

function required(value: string | undefined, name: string): string {
  if (!value) throw new Error(`${name} is required`);
  return value;
}

async function jsonRequest(
  url: string,
  init: RequestInit = {},
): Promise<{ status: number; body: JsonObject }> {
  const response = await fetch(url, init);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new Error(`HTTP ${response.status} returned non-JSON`);
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new Error(`HTTP ${response.status} returned a non-object`);
  }
  if (!response.ok) {
    const detail = (body as JsonObject).detail;
    const safeCode =
      typeof detail === "object" && detail !== null
        ? String((detail as JsonObject).code ?? "request_failed")
        : "request_failed";
    throw new Error(`HTTP ${response.status}: ${safeCode}`);
  }
  return { status: response.status, body: body as JsonObject };
}

function arenaHeaders(): Record<string, string> {
  return {
    "content-type": "application/json",
    "x-arena-dev-token": required(devToken, "ARENA_DEV_TOKEN"),
  };
}

async function loadIntent(): Promise<SettlementIntent> {
  const game = required(gameId, "ARENA_GAME_ID");
  const response = await jsonRequest(
    `${arenaUrl}/api/v1/pawnhouse/games/${encodeURIComponent(game)}` +
      "/settlement-intents",
  );
  const values = response.body.settlementIntents;
  if (!Array.isArray(values)) {
    throw new Error("Arena response omitted settlementIntents");
  }
  const candidates = values.filter(
    (value): value is SettlementIntent =>
      typeof value === "object" && value !== null,
  );
  const intent = requestedIntentId
    ? candidates.find(
        (value) => value.settlementIntentId === requestedIntentId,
      )
    : candidates.length === 1
      ? candidates[0]
      : undefined;
  if (!intent) {
    throw new Error(
      "Select one intent with ARENA_SETTLEMENT_INTENT_ID",
    );
  }
  return intent;
}

function validateIntent(
  intent: SettlementIntent,
  buyerAddress: string,
): void {
  const deployments = loadDeployments(
    resolve(__dirname, "../../deployments.json"),
  );
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
  if (BigInt(intent.amountAtomic) <= 0n) {
    throw new Error("Intent amount must be positive");
  }
}

async function facilitatorPost(
  path: string,
  authorization: PaymentAuthorization,
): Promise<JsonObject> {
  return (
    await jsonRequest(`${facilitatorUrl}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(authorization),
    })
  ).body;
}

async function recoverUntilTerminal(
  intentId: string,
): Promise<JsonObject> {
  const deadline = Date.now() + 120_000;
  let latest: JsonObject = {};
  while (Date.now() < deadline) {
    const response = await jsonRequest(
      `${arenaUrl}/api/dev/pawnhouse/settlement-intents/` +
        `${encodeURIComponent(intentId)}/recover-confirmation`,
      { method: "POST", headers: arenaHeaders() },
    );
    latest = response.body;
    if (
      latest.status === "inventory_committed" ||
      latest.status === "reverted"
    ) {
      return latest;
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 3_000));
  }
  return latest;
}

async function main(): Promise<void> {
  const privateKey = required(
    buyerPrivateKey,
    "BUYER_PRIVATE_KEY in settlement/.env",
  ) as Hex;
  const buyer = privateKeyToAccount(privateKey);
  const intent = await loadIntent();
  validateIntent(intent, buyer.address);
  const deployments = loadDeployments(
    resolve(__dirname, "../../deployments.json"),
  );

  console.log(
    JSON.stringify(
      {
        action: "testnet_settlement_preflight",
        settlementIntentId: intent.settlementIntentId,
        chainId: intent.chainId,
        tokenAddress: intent.tokenAddress,
        buyerAccount: intent.buyerAccount,
        sellerAccount: intent.sellerAccount,
        amountAtomic: intent.amountAtomic,
        facilitatorUrl,
        confirmed,
      },
      null,
      2,
    ),
  );
  if (!confirmed) {
    throw new Error(
      "Refusing chain submission without --confirm-testnet-transfer",
    );
  }

  const authorization = await signTransferAuthorization({
    account: buyer,
    to: intent.sellerAccount,
    value: BigInt(intent.amountAtomic),
    dep: deployments,
    nowSeconds: Math.floor(Date.now() / 1000),
  });
  if (!(await verifyAuthorizationLocally(authorization, deployments))) {
    throw new Error("Local EIP-3009 signature verification failed");
  }
  const verified = await facilitatorPost("/verify", authorization);
  if (verified.ok !== true) {
    throw new Error("Facilitator preflight rejected the authorization");
  }

  const settled = await facilitatorPost("/settle", authorization);
  if (
    settled.status !== "success" ||
    typeof settled.txHash !== "string"
  ) {
    throw new Error("Facilitator did not return a successful tx hash");
  }
  const txHash = settled.txHash;
  await jsonRequest(
    `${arenaUrl}/api/dev/pawnhouse/settlement-intents/` +
      `${encodeURIComponent(intent.settlementIntentId)}/submission`,
    {
      method: "POST",
      headers: arenaHeaders(),
      body: JSON.stringify({
        txHash,
        authorizationNonce: authorization.nonce,
        submissionSource: "wallet",
        humanConfirmed: true,
      }),
    },
  );

  const finalState = await recoverUntilTerminal(
    intent.settlementIntentId,
  );
  console.log(
    JSON.stringify(
      {
        settlementIntentId: intent.settlementIntentId,
        txHash,
        arenaStatus: finalState.status ?? "confirmation_timeout",
        blockscout:
          `https://testnet.blockscout.injective.network/tx/${txHash}`,
      },
      null,
      2,
    ),
  );
  if (finalState.status !== "inventory_committed") {
    throw new Error(
      "Transaction was submitted but Arena has not committed inventory; " +
        "rerun recovery, never create a second authorization blindly",
    );
  }
}

main().catch((error: unknown) => {
  const message =
    error instanceof Error ? error.message : "unknown_settlement_error";
  console.error(`Arena settlement failed: ${message}`);
  process.exit(1);
});
