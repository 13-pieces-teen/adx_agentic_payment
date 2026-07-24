/**
 * arena402-m / arena402-g 合约单测(不上链,连本地 hardhat node)。
 *
 * 运行方式(见 package.json "test" 脚本):
 *   1. 后台起 `npx hardhat node`(内存 EVM,固定测试私钥)
 *   2. node --test test/tokens.test.mjs
 *
 * 断言重点(对应产品红线):
 *   -m ArenaMemorial: soulbound —— transfer/transferFrom/approve 全 revert;仅 owner 可 mint。
 *   -g ArenaGameCoin: 白名单 —— 双方登记才可转;EIP-3009 路径同样受白名单约束;
 *                     未登记转账 revert;facilitator(非 from/to)无需登记。
 */
import { test, before, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  createWalletClient,
  createPublicClient,
  http,
  getAddress,
  parseUnits,
  hexToSignature,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { hardhat } from "viem/chains";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RPC = "http://127.0.0.1:8545";

// hardhat node 默认账户(公开固定私钥,仅本地测试用)
const PK = {
  owner: "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
  alice: "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
  bob: "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
  facilitator: "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
};

function loadArtifact(name) {
  const p = resolve(__dirname, `../artifacts/contracts/${name}.sol/${name}.json`);
  const art = JSON.parse(readFileSync(p, "utf8"));
  return { abi: art.abi, bytecode: art.bytecode };
}

const pub = createPublicClient({ chain: hardhat, transport: http(RPC) });
const accounts = {};
for (const [k, pk] of Object.entries(PK)) {
  accounts[k] = {
    account: privateKeyToAccount(pk),
    wallet: createWalletClient({ account: privateKeyToAccount(pk), chain: hardhat, transport: http(RPC) }),
  };
}

async function deploy(name, args) {
  const { abi, bytecode } = loadArtifact(name);
  const hash = await accounts.owner.wallet.deployContract({ abi, bytecode, args });
  const receipt = await pub.waitForTransactionReceipt({ hash });
  return { address: receipt.contractAddress, abi };
}

// ------------------------------------------------------------------
describe("ArenaMemorial (-m) soulbound", () => {
  let mem;
  before(async () => {
    mem = await deploy("ArenaMemorial", ["Arena 402 Memorial", "arena402-m", 0]);
  });

  test("owner 可 mint,余额增加", async () => {
    await accounts.owner.wallet.writeContract({
      ...mem, functionName: "mint", args: [accounts.alice.account.address, 5n],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    const bal = await pub.readContract({ ...mem, functionName: "balanceOf", args: [accounts.alice.account.address] });
    assert.equal(bal, 5n);
  });

  test("mintBatch 批量铸造", async () => {
    await accounts.owner.wallet.writeContract({
      ...mem, functionName: "mintBatch", args: [[accounts.bob.account.address, accounts.facilitator.account.address], 3n],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    assert.equal(await pub.readContract({ ...mem, functionName: "balanceOf", args: [accounts.bob.account.address] }), 3n);
    assert.equal(await pub.readContract({ ...mem, functionName: "balanceOf", args: [accounts.facilitator.account.address] }), 3n);
  });

  test("非 owner mint 被拒", async () => {
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...mem, functionName: "mint", args: [accounts.bob.account.address, 1n] }),
      /NotOwner|revert/,
    );
  });

  test("持有者 transfer 一律 revert(soulbound)", async () => {
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...mem, functionName: "transfer", args: [accounts.bob.account.address, 1n] }),
      /Soulbound|revert/,
    );
  });

  test("owner 自己也不能 transfer", async () => {
    await accounts.owner.wallet.writeContract({
      ...mem, functionName: "mint", args: [accounts.owner.account.address, 10n],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    await assert.rejects(
      accounts.owner.wallet.writeContract({ ...mem, functionName: "transfer", args: [accounts.alice.account.address, 1n] }),
      /Soulbound|revert/,
    );
  });

  test("approve / transferFrom 也 revert", async () => {
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...mem, functionName: "approve", args: [accounts.bob.account.address, 1n] }),
      /Soulbound|revert/,
    );
    await assert.rejects(
      accounts.bob.wallet.writeContract({ ...mem, functionName: "transferFrom", args: [accounts.alice.account.address, accounts.bob.account.address, 1n] }),
      /Soulbound|revert/,
    );
  });

  test("allowance 恒为 0", async () => {
    const a = await pub.readContract({ ...mem, functionName: "allowance", args: [accounts.alice.account.address, accounts.bob.account.address] });
    assert.equal(a, 0n);
  });
});

// ------------------------------------------------------------------
describe("ArenaGameCoin (-g) 白名单 + EIP-3009", () => {
  let coin;
  const DECIMALS = 6;
  const amt = (n) => parseUnits(String(n), DECIMALS);

  before(async () => {
    coin = await deploy("ArenaGameCoin", ["Arena 402 Gold", "arena402-g", DECIMALS]);
    // 给 alice/bob 铸币(mint 不受白名单约束)
    await accounts.owner.wallet.writeContract({ ...coin, functionName: "mint", args: [accounts.alice.account.address, amt(100)] }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    await accounts.owner.wallet.writeContract({ ...coin, functionName: "mint", args: [accounts.bob.account.address, amt(100)] }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
  });

  test("未登记白名单时 transfer revert", async () => {
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...coin, functionName: "transfer", args: [accounts.bob.account.address, amt(1)] }),
      /NotWhitelisted|revert/,
    );
  });

  test("owner 批量登记后,白名单双方可转", async () => {
    await accounts.owner.wallet.writeContract({
      ...coin, functionName: "addToWhitelistBatch", args: [[accounts.alice.account.address, accounts.bob.account.address]],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    await accounts.alice.wallet.writeContract({
      ...coin, functionName: "transfer", args: [accounts.bob.account.address, amt(10)],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    assert.equal(await pub.readContract({ ...coin, functionName: "balanceOf", args: [accounts.bob.account.address] }), amt(110));
  });

  test("转给未登记地址(如 DEX 池)revert —— 无法建池炒作", async () => {
    // facilitator 此处扮演一个未登记的第三方地址(模拟 DEX 池合约)
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...coin, functionName: "transfer", args: [accounts.facilitator.account.address, amt(1)] }),
      /NotWhitelisted|revert/,
    );
  });

  test("EIP-3009 transferWithAuthorization:白名单内 + facilitator 代付可成功", async () => {
    // alice(付款,已登记)签名授权转给 bob(已登记);facilitator(未登记)代付 gas 提交
    const value = amt(5);
    const nowSec = BigInt(Math.floor(Date.now() / 1000));
    const validAfter = 0n;
    const validBefore = nowSec + 3600n;
    const nonce = "0x" + "11".repeat(32);
    const domain = {
      name: "Arena 402 Gold", version: "1", chainId: BigInt(hardhat.id), verifyingContract: coin.address,
    };
    const types = {
      TransferWithAuthorization: [
        { name: "from", type: "address" }, { name: "to", type: "address" }, { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" }, { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" },
      ],
    };
    const message = { from: accounts.alice.account.address, to: accounts.bob.account.address, value, validAfter, validBefore, nonce };
    const sig = await accounts.alice.account.signTypedData({ domain, types, primaryType: "TransferWithAuthorization", message });
    const { v, r, s } = hexToSignature(sig);

    const bobBefore = await pub.readContract({ ...coin, functionName: "balanceOf", args: [accounts.bob.account.address] });
    await accounts.facilitator.wallet.writeContract({
      ...coin, functionName: "transferWithAuthorization",
      args: [message.from, message.to, value, validAfter, validBefore, nonce, Number(v), r, s],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    const bobAfter = await pub.readContract({ ...coin, functionName: "balanceOf", args: [accounts.bob.account.address] });
    assert.equal(bobAfter - bobBefore, value);
  });

  test("EIP-3009:收款方未登记时 revert(白名单覆盖 EIP-3009 路径)", async () => {
    // alice 签名转给 facilitator(未登记)
    const value = amt(1);
    const validBefore = BigInt(Math.floor(Date.now() / 1000)) + 3600n;
    const nonce = "0x" + "22".repeat(32);
    const domain = { name: "Arena 402 Gold", version: "1", chainId: BigInt(hardhat.id), verifyingContract: coin.address };
    const types = {
      TransferWithAuthorization: [
        { name: "from", type: "address" }, { name: "to", type: "address" }, { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" }, { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" },
      ],
    };
    const message = { from: accounts.alice.account.address, to: accounts.facilitator.account.address, value, validAfter: 0n, validBefore, nonce };
    const sig = await accounts.alice.account.signTypedData({ domain, types, primaryType: "TransferWithAuthorization", message });
    const { v, r, s } = hexToSignature(sig);
    await assert.rejects(
      accounts.facilitator.wallet.writeContract({
        ...coin, functionName: "transferWithAuthorization",
        args: [message.from, message.to, value, 0n, validBefore, nonce, Number(v), r, s],
      }),
      /NotWhitelisted|revert/,
    );
  });

  test("removeFromWhitelist 后不能再转", async () => {
    await accounts.owner.wallet.writeContract({
      ...coin, functionName: "removeFromWhitelist", args: [accounts.bob.account.address],
    }).then((h) => pub.waitForTransactionReceipt({ hash: h }));
    await assert.rejects(
      accounts.alice.wallet.writeContract({ ...coin, functionName: "transfer", args: [accounts.bob.account.address, amt(1)] }),
      /NotWhitelisted|revert/,
    );
  });
});
