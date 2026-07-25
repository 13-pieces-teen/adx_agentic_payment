import { getAddress, type Address, type Hex } from "viem";

export const EIP3009_TRANSFER_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

export interface Eip3009Domain {
  name: string;
  version: string;
  chainId: number;
  verifyingContract: Address;
}

export interface SignEip3009AuthorizationRequest {
  walletId: string;
  expectedFrom: Address;
  domain: Eip3009Domain;
  to: Address;
  value: bigint;
  validAfter: bigint;
  validBefore: bigint;
  nonce: Hex;
}

export interface WalletAuthorizationSignature {
  from: Address;
  signature: Hex;
}

/**
 * Signing seam for platform-managed testnet guest wallets.
 *
 * The interface deliberately exposes only the one signature the settlement
 * protocol needs. Callers never receive raw private keys or a general-purpose
 * LocalAccount.
 */
export interface WalletSecretStore {
  signEip3009Authorization(
    request: SignEip3009AuthorizationRequest,
  ): Promise<WalletAuthorizationSignature>;
}

export type WalletSigningErrorCode =
  | "wallet_signing_disabled"
  | "wallet_not_found"
  | "wallet_disabled"
  | "wallet_address_mismatch"
  | "wallet_signature_invalid"
  | "wallet_secret_invalid"
  | "wallet_backend_unavailable"
  | "deterministic_nonce_required"
  | "invalid_wallet_id";

export class WalletSigningError extends Error {
  constructor(readonly code: WalletSigningErrorCode) {
    super(code);
    this.name = "WalletSigningError";
  }
}

/** Production-safe default until a real secret backend is configured. */
export class DisabledWalletSecretStore implements WalletSecretStore {
  async signEip3009Authorization(): Promise<never> {
    throw new WalletSigningError("wallet_signing_disabled");
  }
}

export function createWalletSecretStore(): WalletSecretStore {
  return new DisabledWalletSecretStore();
}
