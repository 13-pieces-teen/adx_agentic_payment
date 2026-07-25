import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { getAddress } from "viem";
import { LocalCsvWalletSecretStore } from "../src/local-csv-wallet-secret-store.ts";
import {
  createX402PaymentPayload,
  type ArenaX402PaymentRequired,
} from "../src/x402-v2.ts";

const csvPath = required("ADX_WALLET_SIGNER_CSV_PATH");
const token = required("ADX_WALLET_SIGNER_TOKEN");
if (token.length < 32) throw new Error("ADX_WALLET_SIGNER_TOKEN is too short");
const allowedChainId = Number(required("ADX_WALLET_SIGNER_ALLOWED_CHAIN_ID"));
if (!Number.isSafeInteger(allowedChainId) || allowedChainId <= 0) {
  throw new Error("ADX_WALLET_SIGNER_ALLOWED_CHAIN_ID is invalid");
}
const port = Number(process.env.ADX_WALLET_SIGNER_PORT ?? "8787");
if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
  throw new Error("ADX_WALLET_SIGNER_PORT is invalid");
}
const secrets = await LocalCsvWalletSecretStore.load(csvPath);

const server = createServer(async (request, response) => {
  response.setHeader("Cache-Control", "no-store");
  if (request.method === "GET" && request.url === "/health") {
    send(response, 200, { status: "ok" });
    return;
  }
  if (request.method !== "POST" || request.url !== "/v1/x402/sign") {
    send(response, 404, { error: "not_found" });
    return;
  }
  if (!authorized(request)) {
    send(response, 401, { error: "unauthorized" });
    return;
  }
  try {
    const body = await readJson(request);
    const paymentRequired = body.paymentRequired as ArenaX402PaymentRequired;
    const walletId = body.walletId;
    const expectedFrom = body.expectedFrom;
    if (
      !paymentRequired ||
      typeof walletId !== "string" ||
      typeof expectedFrom !== "string"
    ) {
      send(response, 400, { error: "invalid_request" });
      return;
    }
    if (
      paymentRequired.accepts?.[0]?.network !==
      `eip155:${allowedChainId}`
    ) {
      send(response, 403, { error: "chain_not_allowed" });
      return;
    }
    const paymentPayload = await createX402PaymentPayload({
      paymentRequired,
      walletId,
      expectedFrom: getAddress(expectedFrom),
      nowSeconds: Math.floor(Date.now() / 1000),
      secrets,
    });
    send(response, 200, { paymentPayload });
  } catch {
    // Never reflect parser, signing, address, or key-adapter details.
    send(response, 400, { error: "signing_rejected" });
  }
});

server.listen(port, "0.0.0.0");

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function authorized(request: IncomingMessage): boolean {
  const supplied = request.headers.authorization;
  if (!supplied?.startsWith("Bearer ")) return false;
  const actual = Buffer.from(supplied.slice("Bearer ".length));
  const expected = Buffer.from(token);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += value.length;
    if (size > 64 * 1024) throw new Error("request_too_large");
    chunks.push(value);
  }
  const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("invalid_request");
  }
  return body as Record<string, unknown>;
}

function send(
  response: ServerResponse,
  status: number,
  body: Record<string, unknown>,
): void {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json");
  response.end(JSON.stringify(body));
}
