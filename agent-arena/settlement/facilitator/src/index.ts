import "dotenv/config";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import express, { type Request } from "express";
import { formatUnits, type Hex } from "viem";
import { loadFacilitatorPrivateKey } from "./facilitator-csv.ts";
import { Facilitator, type PaymentAuthorization } from "./settle.ts";
import {
  X402FacilitatorRequestError,
  validateX402FacilitatorRequest,
} from "./x402-v2.ts";

const environment = process.env.ADX_ENV?.trim().toLowerCase() ?? "development";
const deploymentsPath = resolve(
  required(
    "ADX_SETTLEMENT_DEPLOYMENTS_PATH",
    environment === "production" ? undefined : "../deployments.json",
  ),
);
const dep = JSON.parse(readFileSync(deploymentsPath, "utf8"));
const port = Number(process.env.FACILITATOR_PORT ?? "4021");
if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
  throw new Error("FACILITATOR_PORT is invalid");
}
const facilitatorKey = await resolveFacilitatorKey(environment);
const bearerToken = required("ADX_X402_FACILITATOR_BEARER_TOKEN");
if (bearerToken.length < 32) {
  throw new Error("ADX_X402_FACILITATOR_BEARER_TOKEN is too short");
}
const allowedResourceOrigin = new URL(
  required("ADX_FACILITATOR_ALLOWED_RESOURCE_ORIGIN"),
).origin;
const facilitator = new Facilitator({
  rpc: process.env.ADX_ARENA_SETTLEMENT_RPC_URL?.trim() || dep.rpc,
  chainId: dep.chainId,
  facilitatorPk: facilitatorKey,
});

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "64kb" }));

app.get("/health", async (_request, response) => {
  const balance = await facilitator.gasBalance();
  response.setHeader("Cache-Control", "no-store");
  response.json({
    ok: true,
    facilitator: facilitator.address,
    injBalance: formatUnits(balance, 18),
    token: dep.usdc.address,
    chainId: dep.chainId,
    x402Version: 2,
  });
});

app.post("/verify", async (request, response) => {
  response.setHeader("Cache-Control", "no-store");
  if (!authorized(request)) {
    response.status(401).json({ error: "unauthorized" });
    return;
  }
  try {
    const value = await validateX402FacilitatorRequest(request.body, {
      chainId: dep.chainId,
      tokenAddress: dep.usdc.address,
      allowedResourceOrigin,
    });
    const result = await facilitator.verify(value.authorization);
    response.json({
      isValid: result.ok,
      payer: value.authorization.from,
      invalidReason: result.ok ? undefined : "payment_verification_failed",
    });
  } catch (error) {
    response.status(400).json({
      isValid: false,
      invalidReason: safeCode(error),
    });
  }
});

app.post("/settle", async (request, response) => {
  response.setHeader("Cache-Control", "no-store");
  if (!authorized(request)) {
    response.status(401).json({ error: "unauthorized" });
    return;
  }
  try {
    const value = await validateX402FacilitatorRequest(request.body, {
      chainId: dep.chainId,
      tokenAddress: dep.usdc.address,
      allowedResourceOrigin,
    });
    const verification = await facilitator.verify(value.authorization);
    if (!verification.ok) {
      response.status(400).json({
        success: false,
        network: value.request.paymentRequirements.network,
        errorReason: "payment_verification_failed",
      });
      return;
    }
    const result = await facilitator.settle(value.authorization);
    if (result.status === "pending") {
      response.status(503).json({
        success: false,
        network: value.request.paymentRequirements.network,
        errorReason: "payment_submission_unknown",
      });
      return;
    }
    if (result.status !== "success" || !result.txHash) {
      response.status(400).json({
        success: false,
        network: value.request.paymentRequirements.network,
        errorReason: "payment_settlement_failed",
      });
      return;
    }
    response.json({
      success: true,
      transaction: result.txHash,
      network: value.request.paymentRequirements.network,
      payer: value.authorization.from,
    });
  } catch (error) {
    const status = error instanceof X402FacilitatorRequestError ? 400 : 500;
    response.status(status).json({
      success: false,
      errorReason: safeCode(error),
    });
  }
});

if (
  environment !== "production" &&
  process.env.ADX_FACILITATOR_LEGACY_ENABLED === "true"
) {
  app.post("/legacy/verify", async (request, response) => {
    const result = await facilitator.verify(
      request.body as PaymentAuthorization,
    );
    response.json(result);
  });
}

app.listen(port, "0.0.0.0", () => {
  console.log(`arena402_x402_facilitator_ready port=${port}`);
});

async function resolveFacilitatorKey(currentEnvironment: string): Promise<Hex> {
  const csvPath = process.env.ADX_FACILITATOR_CSV_PATH?.trim();
  if (csvPath) {
    return loadFacilitatorPrivateKey(
      csvPath,
      required("ADX_FACILITATOR_WALLET_INDEX"),
    );
  }
  if (currentEnvironment === "production") {
    throw new Error("ADX_FACILITATOR_CSV_PATH is required in production");
  }
  return required("FACILITATOR_PRIVATE_KEY") as Hex;
}

function authorized(request: Request): boolean {
  const supplied = request.headers.authorization;
  if (!supplied?.startsWith("Bearer ")) return false;
  const actual = Buffer.from(supplied.slice("Bearer ".length));
  const expected = Buffer.from(bearerToken);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function required(name: string, fallback?: string): string {
  const value = process.env[name]?.trim() || fallback;
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function safeCode(error: unknown): string {
  if (error instanceof X402FacilitatorRequestError) return error.code;
  return "facilitator_internal_error";
}
