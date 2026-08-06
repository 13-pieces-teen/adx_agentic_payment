import { getAddress, type Hex } from "viem";

import { loadFacilitatorPrivateKey } from "../src/facilitator-csv.ts";
import { Facilitator } from "../src/settle.ts";

const chainId = positiveInteger(required("CANARY_CHAIN_ID"), "CANARY_CHAIN_ID");
if (chainId !== 1439) {
  throw new Error("CANARY_CHAIN_ID must be Injective EVM testnet");
}
const target = getAddress(required("CANARY_FAUCET_TARGET"));
const token = getAddress(required("CANARY_TOKEN_ADDRESS")) as Hex;
const minimumAtomic = positiveBigInt(
  required("CANARY_MINIMUM_ATOMIC"),
  "CANARY_MINIMUM_ATOMIC",
);
const facilitatorKey = await loadFacilitatorPrivateKey(
  required("ADX_FACILITATOR_CSV_PATH"),
  required("ADX_FACILITATOR_WALLET_INDEX"),
);
const facilitator = new Facilitator({
  rpc: required("ADX_ARENA_SETTLEMENT_RPC_URL"),
  chainId,
  facilitatorPk: facilitatorKey,
});

const beforeAtomic = await facilitator.balanceOf(token, target);
if (beforeAtomic >= minimumAtomic) {
  process.stdout.write(
    `${JSON.stringify({
      status: "already_funded",
      chainId,
      target,
      token,
      balanceAtomic: beforeAtomic.toString(),
    })}\n`,
  );
  process.exit(0);
}

const result = await facilitator.faucet(token, target);
if (result.status !== "success" || !result.txHash) {
  throw new Error("canary faucet transaction did not confirm");
}
const afterAtomic = await facilitator.balanceOf(token, target);
if (afterAtomic < minimumAtomic || afterAtomic <= beforeAtomic) {
  throw new Error("canary faucet balance verification failed");
}
process.stdout.write(
  `${JSON.stringify({
    status: "funded",
    chainId,
    target,
    token,
    txHash: result.txHash,
    blockNumber: result.blockNumber,
    beforeAtomic: beforeAtomic.toString(),
    afterAtomic: afterAtomic.toString(),
  })}\n`,
);

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function positiveInteger(raw: string, name: string): number {
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}

function positiveBigInt(raw: string, name: string): bigint {
  if (!/^[1-9][0-9]*$/.test(raw)) {
    throw new Error(`${name} is invalid`);
  }
  return BigInt(raw);
}
