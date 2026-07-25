import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { privateKeyToAccount } from "viem/accounts";
import { loadFacilitatorPrivateKey } from "../src/facilitator-csv.ts";
import { validateX402FacilitatorRequest } from "../src/x402-v2.ts";

const PRIVATE_KEY = `0x${"11".repeat(32)}` as const;
const TOKEN = `0x${"33".repeat(20)}` as const;
const PAYEE = `0x${"22".repeat(20)}` as const;

test("facilitator CSV selects an explicit validated index", async () => {
  const directory = await mkdtemp(join(tmpdir(), "arena402-facilitator-"));
  const path = join(directory, "facilitators.csv");
  const account = privateKeyToAccount(PRIVATE_KEY);
  try {
    await writeFile(
      path,
      [
        "facilitator_index,role,ethereum_address,private_key,inj_funded,source",
        `1,original_facilitator,${account.address},${PRIVATE_KEY},true,test`,
      ].join("\n"),
      { mode: 0o600 },
    );
    assert.equal(await loadFacilitatorPrivateKey(path, "1"), PRIVATE_KEY);
    await chmod(path, 0o644);
    await assert.rejects(
      loadFacilitatorPrivateKey(path, "1"),
      /facilitator_csv_permissions/,
    );
  } finally {
    await rm(directory, { recursive: true });
  }
});

test("x402 adapter verifies exact requirement and EIP-3009 signature", async () => {
  const account = privateKeyToAccount(PRIVATE_KEY);
  const now = Math.floor(Date.now() / 1000);
  const intentHash = `sha256:${"ab".repeat(32)}`;
  const requirement = {
    scheme: "exact" as const,
    network: "eip155:1439" as const,
    asset: TOKEN,
    amount: "40",
    payTo: PAYEE,
    maxTimeoutSeconds: 600,
    extra: {
      name: "Mock USD Coin",
      version: "1",
      arena402IntentHash: intentHash,
      arena402SettlementIntentId: "intent-1",
    },
  };
  const authorization = {
    from: account.address,
    to: PAYEE,
    value: "40",
    validAfter: String(now - 1),
    validBefore: String(now + 599),
    nonce: `0x${"ab".repeat(32)}` as const,
  };
  const signature = await account.signTypedData({
    domain: {
      name: "Mock USD Coin",
      version: "1",
      chainId: 1439,
      verifyingContract: TOKEN,
    },
    types: {
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    primaryType: "TransferWithAuthorization",
    message: {
      from: account.address,
      to: PAYEE,
      value: 40n,
      validAfter: BigInt(now - 1),
      validBefore: BigInt(now + 599),
      nonce: authorization.nonce,
    },
  });
  const resource = {
    url: "https://api.arena402.example/api/v1/x402/settlement-intents/intent-1/execute",
    description: "Arena 402 settlement intent-1",
    mimeType: "application/json",
  };
  const result = await validateX402FacilitatorRequest(
    {
      x402Version: 2,
      paymentPayload: {
        x402Version: 2,
        resource,
        accepted: requirement,
        payload: { signature, authorization },
      },
      paymentRequirements: requirement,
    },
    {
      chainId: 1439,
      tokenAddress: TOKEN,
      allowedResourceOrigin: "https://api.arena402.example",
    },
  );

  assert.equal(result.authorization.from, account.address);
  assert.equal(result.authorization.value, "40");
  assert.ok(result.authorization.v === 27 || result.authorization.v === 28);
});
