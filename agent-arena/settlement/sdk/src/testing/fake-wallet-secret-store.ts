import { getAddress, type Address } from "viem";
import {
  generatePrivateKey,
  privateKeyToAccount,
  type PrivateKeyAccount,
} from "viem/accounts";

import {
  EIP3009_TRANSFER_TYPES,
  WalletSigningError,
  type SignEip3009AuthorizationRequest,
  type WalletAuthorizationSignature,
  type WalletSecretStore,
} from "../wallet-secret-store.ts";

/**
 * Explicit test adapter. It generates process-local keys and never exposes
 * them. It is intentionally absent from the SDK production entrypoint.
 */
export class FakeWalletSecretStore implements WalletSecretStore {
  private readonly accounts = new Map<string, PrivateKeyAccount>();
  private readonly disabledWallets = new Set<string>();

  provisionTestWallet(walletId: string): Address {
    const normalizedWalletId = normalizeWalletId(walletId);
    const existing = this.accounts.get(normalizedWalletId);
    if (existing) return existing.address;

    const account = privateKeyToAccount(generatePrivateKey());
    this.accounts.set(normalizedWalletId, account);
    return account.address;
  }

  disableTestWallet(walletId: string): void {
    this.disabledWallets.add(normalizeWalletId(walletId));
  }

  async signEip3009Authorization(
    request: SignEip3009AuthorizationRequest,
  ): Promise<WalletAuthorizationSignature> {
    const walletId = normalizeWalletId(request.walletId);
    const account = this.accounts.get(walletId);
    if (!account) throw new WalletSigningError("wallet_not_found");
    if (this.disabledWallets.has(walletId)) {
      throw new WalletSigningError("wallet_disabled");
    }
    if (getAddress(account.address) !== getAddress(request.expectedFrom)) {
      throw new WalletSigningError("wallet_address_mismatch");
    }

    const from = getAddress(account.address);
    const signature = await account.signTypedData({
      domain: request.domain,
      types: EIP3009_TRANSFER_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from,
        to: getAddress(request.to),
        value: request.value,
        validAfter: request.validAfter,
        validBefore: request.validBefore,
        nonce: request.nonce,
      },
    });
    return { from, signature };
  }
}

function normalizeWalletId(walletId: string): string {
  const normalized = walletId.trim();
  if (!normalized) throw new WalletSigningError("invalid_wallet_id");
  return normalized;
}
