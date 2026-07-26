import { before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createPublicClient,
  createWalletClient,
  http,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { hardhat } from "viem/chains";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RPC = "http://127.0.0.1:8545";
const OWNER_KEY =
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
const RECIPIENT_KEY =
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d";
const OTHER_KEY =
  "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a";

const owner = privateKeyToAccount(OWNER_KEY);
const recipient = privateKeyToAccount(RECIPIENT_KEY);
const other = privateKeyToAccount(OTHER_KEY);
const publicClient = createPublicClient({
  chain: hardhat,
  transport: http(RPC),
});
const ownerWallet = createWalletClient({
  account: owner,
  chain: hardhat,
  transport: http(RPC),
});
const recipientWallet = createWalletClient({
  account: recipient,
  chain: hardhat,
  transport: http(RPC),
});

function loadArtifact() {
  const path = resolve(
    __dirname,
    "../artifacts/contracts/ArenaMemorialNFT.sol/ArenaMemorialNFT.json",
  );
  const artifact = JSON.parse(readFileSync(path, "utf8"));
  return { abi: artifact.abi, bytecode: artifact.bytecode };
}

async function write(contract, functionName, args, wallet = ownerWallet) {
  const hash = await wallet.writeContract({
    ...contract,
    functionName,
    args,
  });
  await publicClient.waitForTransactionReceipt({ hash });
}

describe("ArenaMemorialNFT", () => {
  let memorial;

  before(async () => {
    const artifact = loadArtifact();
    const hash = await ownerWallet.deployContract({
      ...artifact,
      args: ["https://metadata.arena402.com/memorial/"],
    });
    const receipt = await publicClient.waitForTransactionReceipt({ hash });
    memorial = { address: receipt.contractAddress, abi: artifact.abi };
  });

  test("owner mints memorial #0 to the generated recipient wallet", async () => {
    await write(memorial, "mint", [recipient.address]);

    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "ownerOf",
        args: [0n],
      }),
      recipient.address,
    );
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "tokenURI",
        args: [0n],
      }),
      "https://metadata.arena402.com/memorial/0",
    );
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "locked",
        args: [0n],
      }),
      true,
    );
  });

  test("mintBatch assigns sequential token IDs and balances", async () => {
    await write(memorial, "mintBatch", [[recipient.address, other.address]]);

    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "ownerOf",
        args: [1n],
      }),
      recipient.address,
    );
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "ownerOf",
        args: [2n],
      }),
      other.address,
    );
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "balanceOf",
        args: [recipient.address],
      }),
      2n,
    );
  });

  test("non-owner mint is rejected", async () => {
    await assert.rejects(
      recipientWallet.writeContract({
        ...memorial,
        functionName: "mint",
        args: [other.address],
      }),
      /NotOwner|revert/i,
    );
  });

  test("all transfer and approval paths are soulbound", async () => {
    const calls = [
      ["transferFrom", [recipient.address, other.address, 0n]],
      ["safeTransferFrom", [recipient.address, other.address, 0n]],
      ["safeTransferFrom", [recipient.address, other.address, 0n, "0x"]],
      ["approve", [other.address, 0n]],
      ["setApprovalForAll", [other.address, true]],
    ];
    for (const [functionName, args] of calls) {
      await assert.rejects(
        recipientWallet.writeContract({
          ...memorial,
          functionName,
          args,
        }),
        /Soulbound|revert/i,
      );
    }
  });

  test("supply stops permanently at 402", async () => {
    const remaining = Array.from({ length: 399 }, () => other.address);
    await write(memorial, "mintBatch", [remaining]);
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "nextTokenId",
      }),
      402n,
    );
    assert.equal(
      await publicClient.readContract({
        ...memorial,
        functionName: "ownerOf",
        args: [401n],
      }),
      other.address,
    );
    await assert.rejects(
      ownerWallet.writeContract({
        ...memorial,
        functionName: "mint",
        args: [recipient.address],
      }),
      /MaxSupplyExceeded|revert/i,
    );
  });

  test("advertises ERC-721 metadata and ERC-5192 interfaces", async () => {
    for (const interfaceId of [
      "0x01ffc9a7",
      "0x80ac58cd",
      "0x5b5e139f",
      "0xb45a3c0e",
    ]) {
      assert.equal(
        await publicClient.readContract({
          ...memorial,
          functionName: "supportsInterface",
          args: [interfaceId],
        }),
        true,
      );
    }
    await assert.rejects(
      publicClient.readContract({
        ...memorial,
        functionName: "balanceOf",
        args: ["0x0000000000000000000000000000000000000000"],
      }),
      /BalanceQueryForZeroAddress|revert/i,
    );
  });
});
