import { readFile, stat } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { getAddress, type Address } from "viem";
import { privateKeyToAccount, type PrivateKeyAccount } from "viem/accounts";
import {
  EIP3009_TRANSFER_TYPES,
  WalletSigningError,
  type SignEip3009AuthorizationRequest,
  type WalletAuthorizationSignature,
  type WalletSecretStore,
} from "./wallet-secret-store.ts";

export type LocalCsvWalletStoreErrorCode =
  | "wallet_secret_path_must_be_absolute"
  | "wallet_secret_file_not_regular"
  | "wallet_secret_file_permissions"
  | "wallet_secret_csv_invalid"
  | "wallet_secret_duplicate";

export class LocalCsvWalletStoreError extends Error {
  constructor(readonly code: LocalCsvWalletStoreErrorCode) {
    super(code);
    this.name = "LocalCsvWalletStoreError";
  }
}

/**
 * Development/testnet-only adapter for the pre-generated wallet CSV.
 *
 * The file stays outside the repository and must be owner-readable only. The
 * adapter retains LocalAccount instances internally and never returns keys.
 */
export class LocalCsvWalletSecretStore implements WalletSecretStore {
  private constructor(
    private readonly accounts: ReadonlyMap<string, PrivateKeyAccount>,
  ) {}

  static async load(path: string): Promise<LocalCsvWalletSecretStore> {
    if (!isAbsolute(path)) {
      throw new LocalCsvWalletStoreError(
        "wallet_secret_path_must_be_absolute",
      );
    }
    const metadata = await stat(path);
    if (!metadata.isFile()) {
      throw new LocalCsvWalletStoreError("wallet_secret_file_not_regular");
    }
    if ((metadata.mode & 0o077) !== 0) {
      throw new LocalCsvWalletStoreError("wallet_secret_file_permissions");
    }
    const content = await readFile(path, "utf8");
    const rows = parseCsv(content);
    const header = rows.shift();
    if (!header) {
      throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
    }
    const indexColumn = header.indexOf("index");
    const addressColumn = header.indexOf("ethereum_address");
    const keyColumn = header.indexOf("private_key");
    if (Math.min(indexColumn, addressColumn, keyColumn) < 0) {
      throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
    }
    const accounts = new Map<string, PrivateKeyAccount>();
    const addresses = new Set<string>();
    for (const row of rows) {
      if (row.length !== header.length) {
        throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
      }
      const rawIndex = row[indexColumn]?.trim();
      const rawAddress = row[addressColumn]?.trim();
      const rawKey = row[keyColumn]?.trim();
      if (
        !rawIndex ||
        !/^[0-9]+$/.test(rawIndex) ||
        !rawAddress ||
        !rawKey ||
        !/^0x[0-9a-fA-F]{64}$/.test(rawKey)
      ) {
        throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
      }
      let account: PrivateKeyAccount;
      try {
        account = privateKeyToAccount(rawKey as `0x${string}`);
        if (getAddress(account.address) !== getAddress(rawAddress)) {
          throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
        }
      } catch (error) {
        if (error instanceof LocalCsvWalletStoreError) throw error;
        throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
      }
      const walletId = `agent-wallet-${rawIndex.padStart(4, "0")}`;
      const normalizedAddress = getAddress(account.address);
      if (accounts.has(walletId) || addresses.has(normalizedAddress)) {
        throw new LocalCsvWalletStoreError("wallet_secret_duplicate");
      }
      accounts.set(walletId, account);
      addresses.add(normalizedAddress);
    }
    if (accounts.size === 0) {
      throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
    }
    return new LocalCsvWalletSecretStore(accounts);
  }

  async signEip3009Authorization(
    request: SignEip3009AuthorizationRequest,
  ): Promise<WalletAuthorizationSignature> {
    const account = this.accounts.get(request.walletId);
    if (!account) throw new WalletSigningError("wallet_not_found");
    if (getAddress(account.address) !== getAddress(request.expectedFrom)) {
      throw new WalletSigningError("wallet_address_mismatch");
    }
    const signature = await account.signTypedData({
      domain: request.domain,
      types: EIP3009_TRANSFER_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from: getAddress(account.address),
        to: request.to,
        value: request.value,
        validAfter: request.validAfter,
        validBefore: request.validBefore,
        nonce: request.nonce,
      },
    });
    return { from: account.address as Address, signature };
  }
}

function parseCsv(content: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < content.length; index += 1) {
    const character = content[index];
    if (quoted) {
      if (character === '"' && content[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        field += character;
      }
      continue;
    }
    if (character === '"') {
      if (field) {
        throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
      }
      quoted = true;
    } else if (character === ",") {
      row.push(field);
      field = "";
    } else if (character === "\n") {
      row.push(field.replace(/\r$/, ""));
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }
  if (quoted) {
    throw new LocalCsvWalletStoreError("wallet_secret_csv_invalid");
  }
  row.push(field.replace(/\r$/, ""));
  if (row.some((value) => value !== "")) rows.push(row);
  return rows;
}
