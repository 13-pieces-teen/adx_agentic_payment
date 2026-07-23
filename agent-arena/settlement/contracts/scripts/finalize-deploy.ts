/**
 * SETTLE-002.5 收尾：为已部署的合约补铸买方种子币 + 回写 deployments.json。
 * （因公共 RPC 回执延迟，首次 deploy 卡在 mint 前；合约已上链，这里续上。）
 *
 * 用法: CONTRACT=0x06D2...BDeD npm run finalize
 */
import "dotenv/config";
import { createWalletClient, createPublicClient, http, parseUnits, formatUnits, getAddress, type Hex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { waitViaBlockscout } from "./lib-tx.ts";

const __dirname = dirname(fileURLToPath(import.meta.url));
process.loadEnvFile(resolve(__dirname, "../../.env"));

const RPC = process.env.INJECTIVE_EVM_RPC!;
const CHAIN_ID = Number(process.env.INJECTIVE_CHAIN_ID ?? 1439);
const CONTRACT = getAddress((process.env.CONTRACT ?? "0x06D223D12774386A96D33863D9106A800e52BDeD"));
const DECIMALS = 6;
const SEED_WHOLE = "10000";

const chain = { id: CHAIN_ID, name: "Injective EVM Testnet", nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 }, rpcUrls: { default: { http: [RPC] } } } as const;

async function main() {
  const deployer = privateKeyToAccount(process.env.FACILITATOR_PRIVATE_KEY as Hex);
  const buyer = privateKeyToAccount(process.env.BUYER_PRIVATE_KEY as Hex).address;
  const art = JSON.parse(readFileSync(resolve(__dirname, "../artifacts/contracts/MockStablecoin.sol/MockStablecoin.json"), "utf8"));
  const abi = art.abi;

  const transport = http(RPC, { timeout: 60_000, retryCount: 8, retryDelay: 3000 });
  const pub = createPublicClient({ chain, transport });
  const wallet = createWalletClient({ account: deployer, chain, transport });

  const gp = (await pub.getGasPrice()) * 3n;
  console.log(`合约: ${CONTRACT}\n买方: ${buyer}\ngasPrice: ${formatUnits(gp, 9)} gwei`);

  const cur = (await pub.readContract({ address: CONTRACT, abi, functionName: "balanceOf", args: [buyer] })) as bigint;
  if (cur === 0n) {
    console.log(`\n铸 ${SEED_WHOLE} 给买方...`);
    const hash = await wallet.writeContract({
      address: CONTRACT, abi, functionName: "mint",
      args: [buyer, parseUnits(SEED_WHOLE, DECIMALS)],
      type: "legacy", gasPrice: gp, gas: 200_000n,
    });
    console.log(`tx: ${hash}`);
    console.log(`确认: ${(await waitViaBlockscout(hash)).status}`);
  } else {
    console.log(`买方已有余额，跳过铸币`);
  }
  const bal = (await pub.readContract({ address: CONTRACT, abi, functionName: "balanceOf", args: [buyer] })) as bigint;
  console.log(`买方余额: ${formatUnits(bal, DECIMALS)} mUSDC`);

  const name = await pub.readContract({ address: CONTRACT, abi, functionName: "name" });
  const domainSep = await pub.readContract({ address: CONTRACT, abi, functionName: "DOMAIN_SEPARATOR" });

  const depPath = resolve(__dirname, "../../deployments.json");
  const dep = existsSync(depPath) ? JSON.parse(readFileSync(depPath, "utf8")) : {};
  dep.chainId = CHAIN_ID;
  dep.rpc = RPC;
  dep.usdc = {
    address: CONTRACT, symbol: "mUSDC", decimals: DECIMALS,
    eip712Name: name, eip712Version: "1", domainSeparator: domainSep,
    supportsEip3009: true, faucetAmount: "1000", _spec: "SETTLE-002.5",
  };
  dep.wallets = { buyer, seller: privateKeyToAccount(process.env.SELLER_PRIVATE_KEY as Hex).address, facilitator: deployer.address };
  writeFileSync(depPath, JSON.stringify(dep, null, 2));
  console.log(`\n✅ deployments.json 已更新`);
}
main().catch((e) => { console.error("❌", e.message ?? e); process.exit(1); });
