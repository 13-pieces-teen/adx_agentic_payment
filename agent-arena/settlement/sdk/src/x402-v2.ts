import { getAddress, type Address } from "viem";
import {
  validatePaymentPayload,
  validatePaymentRequired,
} from "@x402/core/schemas";
import type { WalletSecretStore } from "./wallet-secret-store.ts";
import { signTransferAuthorizationWithWallet } from "./x402.ts";

export interface X402ResourceInfo {
  url: string;
  description: string;
  mimeType: string;
}

export interface ArenaX402PaymentRequirement {
  scheme: "exact";
  network: `eip155:${number}`;
  asset: Address;
  amount: string;
  payTo: Address;
  maxTimeoutSeconds: number;
  extra: {
    name: string;
    version: string;
    arena402IntentHash?: string;
    arena402SettlementIntentId?: string;
  };
}

export interface ArenaX402PaymentRequired {
  x402Version: 2;
  resource: X402ResourceInfo;
  accepts: [ArenaX402PaymentRequirement];
}

export interface ArenaX402PaymentPayload {
  x402Version: 2;
  resource: X402ResourceInfo;
  accepted: ArenaX402PaymentRequirement;
  payload: {
    signature: `0x${string}`;
    authorization: {
      from: Address;
      to: Address;
      value: string;
      validAfter: string;
      validBefore: string;
      nonce: `0x${string}`;
    };
  };
}

export async function createX402PaymentPayload(input: {
  paymentRequired: ArenaX402PaymentRequired;
  walletId: string;
  expectedFrom: Address;
  nowSeconds: number;
  secrets: WalletSecretStore;
}): Promise<ArenaX402PaymentPayload> {
  const required = input.paymentRequired;
  validatePaymentRequired(required);
  if (required.x402Version !== 2 || required.accepts.length !== 1) {
    throw new Error("unsupported_x402_requirement");
  }
  const accepted = required.accepts[0];
  if (accepted.scheme !== "exact") {
    throw new Error("unsupported_x402_scheme");
  }
  const match = /^eip155:([1-9][0-9]*)$/.exec(accepted.network);
  if (!match) throw new Error("invalid_x402_network");
  const intentHash = accepted.extra.arena402IntentHash;
  if (!intentHash || !/^sha256:[0-9a-f]{64}$/.test(intentHash)) {
    throw new Error("x402_intent_hash_required");
  }
  if (
    !accepted.extra.arena402SettlementIntentId ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$/.test(
      accepted.extra.arena402SettlementIntentId,
    )
  ) {
    throw new Error("x402_settlement_intent_id_required");
  }
  if (!/^[1-9][0-9]*$/.test(accepted.amount)) {
    throw new Error("invalid_x402_amount");
  }
  const chainId = Number(match[1]);
  if (!Number.isSafeInteger(chainId)) throw new Error("invalid_x402_network");
  const nonce = `0x${intentHash.slice("sha256:".length)}` as const;
  const authorization = await signTransferAuthorizationWithWallet(
    {
      walletId: input.walletId,
      expectedFrom: getAddress(input.expectedFrom),
      to: accepted.payTo,
      value: BigInt(accepted.amount),
      dep: {
        chainId,
        rpc: "",
        usdc: {
          address: accepted.asset,
          symbol: accepted.extra.name,
          decimals: 6,
          eip712Name: accepted.extra.name,
          eip712Version: accepted.extra.version,
        },
        wallets: {
          buyer: input.expectedFrom,
          seller: accepted.payTo,
          facilitator: "0x0000000000000000000000000000000000000000",
        },
      },
      nonce,
      validForSeconds: Math.min(accepted.maxTimeoutSeconds, 600),
      nowSeconds: input.nowSeconds,
    },
    input.secrets,
  );
  const signature =
    `0x${authorization.r.slice(2)}${authorization.s.slice(2)}${authorization.v
      .toString(16)
      .padStart(2, "0")}` as `0x${string}`;
  const payload: ArenaX402PaymentPayload = {
    x402Version: 2,
    resource: required.resource,
    accepted,
    payload: {
      signature,
      authorization: {
        from: getAddress(authorization.from),
        to: getAddress(authorization.to),
        value: authorization.value,
        validAfter: authorization.validAfter,
        validBefore: authorization.validBefore,
        nonce: authorization.nonce,
      },
    },
  };
  validatePaymentPayload(payload);
  return payload;
}

export function encodeX402Header(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64");
}

export function decodeX402Header(value: string): unknown {
  return JSON.parse(Buffer.from(value, "base64").toString("utf8"));
}
