import {
  validatePaymentPayload,
  validatePaymentRequirements,
} from "@x402/core/schemas";
import {
  getAddress,
  recoverTypedDataAddress,
  type Address,
  type Hex,
} from "viem";
import type { PaymentAuthorization } from "./settle.ts";

const TRANSFER_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

export interface X402FacilitatorRequest {
  x402Version: 2;
  paymentPayload: {
    x402Version: 2;
    resource: Record<string, unknown>;
    accepted: Record<string, unknown>;
    payload: {
      signature: Hex;
      authorization: {
        from: Address;
        to: Address;
        value: string;
        validAfter: string;
        validBefore: string;
        nonce: Hex;
      };
    };
  };
  paymentRequirements: {
    scheme: "exact";
    network: `eip155:${number}`;
    asset: Address;
    amount: string;
    payTo: Address;
    maxTimeoutSeconds: number;
    extra: {
      name: string;
      version: string;
      arena402IntentHash: string;
      arena402SettlementIntentId: string;
    };
  };
}

export class X402FacilitatorRequestError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "X402FacilitatorRequestError";
  }
}

export async function validateX402FacilitatorRequest(
  value: unknown,
  options: {
    chainId: number;
    tokenAddress: Address;
    allowedResourceOrigin: string;
  },
): Promise<{
  request: X402FacilitatorRequest;
  authorization: PaymentAuthorization;
}> {
  if (!value || typeof value !== "object") {
    throw new X402FacilitatorRequestError("invalid_x402_request");
  }
  const request = value as X402FacilitatorRequest;
  try {
    validatePaymentPayload(request.paymentPayload);
    validatePaymentRequirements(request.paymentRequirements);
  } catch {
    throw new X402FacilitatorRequestError("invalid_x402_schema");
  }
  const payload = request.paymentPayload;
  const requirement = request.paymentRequirements;
  if (
    request.x402Version !== 2 ||
    payload.x402Version !== 2 ||
    requirement.scheme !== "exact" ||
    canonical(payload.accepted) !== canonical(requirement)
  ) {
    throw new X402FacilitatorRequestError("x402_requirement_mismatch");
  }
  const network = /^eip155:([1-9][0-9]*)$/.exec(requirement.network);
  if (
    !network ||
    Number(network[1]) !== options.chainId ||
    getAddress(requirement.asset) !== getAddress(options.tokenAddress)
  ) {
    throw new X402FacilitatorRequestError("x402_network_or_asset_rejected");
  }
  const resourceUrl = payload.resource.url;
  if (
    typeof resourceUrl !== "string" ||
    !resourceUrl.startsWith(`${options.allowedResourceOrigin}/`)
  ) {
    throw new X402FacilitatorRequestError("x402_resource_rejected");
  }
  const auth = payload.payload.authorization;
  if (
    auth.to.toLowerCase() !== requirement.payTo.toLowerCase() ||
    auth.value !== requirement.amount ||
    !/^[1-9][0-9]*$/.test(auth.value) ||
    !/^0x[0-9a-f]{64}$/.test(auth.nonce) ||
    !/^sha256:[0-9a-f]{64}$/.test(
      requirement.extra.arena402IntentHash,
    ) ||
    auth.nonce !==
      `0x${requirement.extra.arena402IntentHash.slice("sha256:".length)}`
  ) {
    throw new X402FacilitatorRequestError("x402_authorization_mismatch");
  }
  const validAfter = parseInteger(auth.validAfter);
  const validBefore = parseInteger(auth.validBefore);
  const now = BigInt(Math.floor(Date.now() / 1000));
  if (
    validAfter > now ||
    validBefore <= now ||
    validBefore - now > BigInt(requirement.maxTimeoutSeconds) ||
    requirement.maxTimeoutSeconds > 600
  ) {
    throw new X402FacilitatorRequestError("x402_authorization_window");
  }
  const signature = payload.payload.signature;
  if (!/^0x[0-9a-fA-F]{130}$/.test(signature)) {
    throw new X402FacilitatorRequestError("x402_signature_invalid");
  }
  let recovered: Address;
  try {
    recovered = await recoverTypedDataAddress({
      domain: {
        name: requirement.extra.name,
        version: requirement.extra.version,
        chainId: options.chainId,
        verifyingContract: getAddress(requirement.asset),
      },
      types: TRANSFER_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from: getAddress(auth.from),
        to: getAddress(auth.to),
        value: BigInt(auth.value),
        validAfter,
        validBefore,
        nonce: auth.nonce,
      },
      signature,
    });
  } catch {
    throw new X402FacilitatorRequestError("x402_signature_invalid");
  }
  if (getAddress(recovered) !== getAddress(auth.from)) {
    throw new X402FacilitatorRequestError("x402_signature_invalid");
  }
  const r = `0x${signature.slice(2, 66)}` as Hex;
  const s = `0x${signature.slice(66, 130)}` as Hex;
  const v = Number.parseInt(signature.slice(130, 132), 16);
  if (v !== 27 && v !== 28) {
    throw new X402FacilitatorRequestError("x402_signature_invalid");
  }
  return {
    request,
    authorization: {
      from: auth.from,
      to: auth.to,
      value: auth.value,
      validAfter: auth.validAfter,
      validBefore: auth.validBefore,
      nonce: auth.nonce,
      v,
      r,
      s,
      token: requirement.asset,
      chainId: options.chainId,
    },
  };
}

function parseInteger(value: string): bigint {
  if (!/^(0|[1-9][0-9]*)$/.test(value)) {
    throw new X402FacilitatorRequestError("x402_authorization_window");
  }
  return BigInt(value);
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right),
    );
    return `{${entries
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}
