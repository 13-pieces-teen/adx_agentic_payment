import assert from "node:assert/strict";
import test from "node:test";
import type { Hex } from "viem";

import {
  createWalletSecretStore,
  signTransferAuthorizationWithWallet,
  verifyAuthorizationLocally,
  WalletSigningError,
  type Deployments,
  type WalletSignParams,
  type WalletSecretStore,
} from "../src/index.ts";
import { FakeWalletSecretStore } from "../src/testing.ts";

const SELLER = `0x${"22".repeat(20)}`;
const TOKEN = `0x${"33".repeat(20)}` as Hex;

const deployments: Deployments = {
  chainId: 1439,
  rpc: "https://rpc.invalid",
  usdc: {
    address: TOKEN,
    symbol: "mUSDC",
    decimals: 6,
    eip712Name: "Mock USD Coin",
    eip712Version: "1",
  },
  wallets: {
    buyer: `0x${"11".repeat(20)}`,
    seller: SELLER,
    facilitator: `0x${"44".repeat(20)}`,
  },
};

test("a fake wallet signs one verifiable EIP-3009 authorization without exposing its key", async () => {
  const secrets = new FakeWalletSecretStore();
  const buyerAddress = secrets.provisionTestWallet("wallet-1");

  const authorization = await signTransferAuthorizationWithWallet(
    {
      walletId: "wallet-1",
      expectedFrom: buyerAddress,
      to: SELLER,
      value: 7_000_000n,
      dep: deployments,
      nonce: `0x${"55".repeat(32)}`,
      nowSeconds: 1_784_000_000,
    },
    secrets,
  );

  assert.equal(authorization.from, buyerAddress);
  assert.equal(authorization.to, SELLER);
  assert.equal(authorization.value, "7000000");
  assert.equal(
    await verifyAuthorizationLocally(authorization, deployments),
    true,
  );
});

test("wallet signing is fail-closed when no secret backend is configured", async () => {
  const secrets = createWalletSecretStore();

  await assert.rejects(
    () =>
      signTransferAuthorizationWithWallet(
        {
          walletId: "wallet-1",
          expectedFrom: deployments.wallets.buyer as Hex,
          to: SELLER,
          value: 7_000_000n,
          dep: deployments,
          nonce: `0x${"66".repeat(32)}`,
          nowSeconds: 1_784_000_000,
        },
        secrets,
      ),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "wallet_signing_disabled",
  );
});

test("the wallet path rejects a missing deterministic nonce at runtime", async () => {
  const secrets = new FakeWalletSecretStore();
  const buyerAddress = secrets.provisionTestWallet("wallet-1");
  const unsafeJavascriptRequest = {
    walletId: "wallet-1",
    expectedFrom: buyerAddress,
    to: SELLER,
    value: 7_000_000n,
    dep: deployments,
    nowSeconds: 1_784_000_000,
  } as unknown as WalletSignParams;

  await assert.rejects(
    () =>
      signTransferAuthorizationWithWallet(
        unsafeJavascriptRequest,
        secrets,
      ),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "deterministic_nonce_required",
  );
});

test("the fake store keeps a stable address and rejects unsafe wallet selection", async () => {
  const secrets = new FakeWalletSecretStore();
  const buyerAddress = secrets.provisionTestWallet("wallet-1");
  assert.equal(secrets.provisionTestWallet("wallet-1"), buyerAddress);

  const sign = (walletId: string, expectedFrom: Hex) =>
    signTransferAuthorizationWithWallet(
      {
        walletId,
        expectedFrom,
        to: SELLER,
        value: 7_000_000n,
        dep: deployments,
        nonce: `0x${"77".repeat(32)}`,
        nowSeconds: 1_784_000_000,
      },
      secrets,
    );

  await assert.rejects(
    () => sign("missing-wallet", buyerAddress),
    (error: unknown) =>
      error instanceof WalletSigningError && error.code === "wallet_not_found",
  );
  await assert.rejects(
    () => sign("wallet-1", deployments.wallets.buyer as Hex),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "wallet_address_mismatch",
  );

  secrets.disableTestWallet("wallet-1");
  await assert.rejects(
    () => sign("wallet-1", buyerAddress),
    (error: unknown) =>
      error instanceof WalletSigningError && error.code === "wallet_disabled",
  );
});

test("the SDK rejects an adapter that signs with a different wallet", async () => {
  const backingStore = new FakeWalletSecretStore();
  const expectedAddress = backingStore.provisionTestWallet("expected-wallet");
  const otherAddress = backingStore.provisionTestWallet("other-wallet");
  const unsafeAdapter: WalletSecretStore = {
    signEip3009Authorization: (request) =>
      backingStore.signEip3009Authorization({
        ...request,
        walletId: "other-wallet",
        expectedFrom: otherAddress,
      }),
  };

  await assert.rejects(
    () =>
      signTransferAuthorizationWithWallet(
        {
          walletId: "expected-wallet",
          expectedFrom: expectedAddress,
          to: SELLER,
          value: 7_000_000n,
          dep: deployments,
          nonce: `0x${"88".repeat(32)}`,
          nowSeconds: 1_784_000_000,
        },
        unsafeAdapter,
      ),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "wallet_address_mismatch",
  );
});

test("the SDK rejects a malformed signature from an adapter", async () => {
  const expectedFrom = deployments.wallets.buyer as Hex;
  const unsafeAdapter: WalletSecretStore = {
    async signEip3009Authorization() {
      return {
        from: expectedFrom,
        signature: `0x${"00".repeat(65)}`,
      };
    },
  };

  await assert.rejects(
    () =>
      signTransferAuthorizationWithWallet(
        {
          walletId: "wallet-1",
          expectedFrom,
          to: SELLER,
          value: 7_000_000n,
          dep: deployments,
          nonce: `0x${"99".repeat(32)}`,
          nowSeconds: 1_784_000_000,
        },
        unsafeAdapter,
      ),
    (error: unknown) =>
      error instanceof WalletSigningError &&
      error.code === "wallet_signature_invalid",
  );
});
