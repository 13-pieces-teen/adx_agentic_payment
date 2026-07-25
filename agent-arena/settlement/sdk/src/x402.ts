/**
 * SETTLE-003 · 买方 EIP-3009 签名封装
 *
 * 买方用私钥离线签名 TransferWithAuthorization（EIP-712）。不上链、不花 gas。
 * 产出的 PaymentAuthorization 交给 facilitator 代付 gas 上链结算（x402 核心）。
 *
 * 将来 TEE 的 GeneratePayment 会在 enclave 内做等价签名；此模块也可直接被 TEE 复用。
 */
import {
  createPublicClient,
  http,
  hexToSignature,
  recoverTypedDataAddress,
  getAddress,
  type Hex,
  type LocalAccount,
} from "viem";
import { readFileSync } from "node:fs";
import type { PaymentAuthorization, Deployments } from "./types.ts";
import {
  EIP3009_TRANSFER_TYPES,
  WalletSigningError,
  type WalletSecretStore,
} from "./wallet-secret-store.ts";

export function loadDeployments(path: string): Deployments {
  return JSON.parse(readFileSync(path, "utf8"));
}

function eip712Domain(dep: Deployments) {
  return {
    name: dep.usdc.eip712Name,
    version: dep.usdc.eip712Version,
    chainId: dep.chainId,
    verifyingContract: dep.usdc.address,
  } as const;
}

/** 生成 32 字节随机 nonce（禁止写死，D4 防重放） */
function randomNonce(): Hex {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return ("0x" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")) as Hex;
}

export interface SignParams {
  account: LocalAccount; // 买方账户（privateKeyToAccount）
  to: string; // 卖方地址
  value: bigint; // atomic units
  dep: Deployments;
  nonce?: Hex; // Arena bridge supplies the deterministic frozen-intent nonce
  validForSeconds?: number; // 默认 600s
  nowSeconds: number; // 显式传入当前时间（避免脚本环境时钟问题；调用方用 Date.now()）
}

export interface WalletSignParams extends Omit<SignParams, "account" | "nonce"> {
  walletId: string;
  expectedFrom: `0x${string}`;
  nonce: Hex;
}

interface PreparedAuthorization {
  domain: ReturnType<typeof eip712Domain>;
  to: `0x${string}`;
  value: bigint;
  validAfter: bigint;
  validBefore: bigint;
  nonce: Hex;
}

function prepareAuthorization(
  p: Omit<SignParams, "account">,
): PreparedAuthorization {
  const validAfter = 0n;
  const validBefore = BigInt(p.nowSeconds + (p.validForSeconds ?? 600));
  const nonce = p.nonce ?? randomNonce();
  if (!/^0x[0-9a-fA-F]{64}$/.test(nonce)) {
    throw new Error("EIP-3009 nonce must be exactly 32 bytes");
  }
  return {
    domain: eip712Domain(p.dep),
    to: getAddress(p.to),
    value: p.value,
    validAfter,
    validBefore,
    nonce,
  };
}

function paymentAuthorization(
  p: Omit<SignParams, "account">,
  prepared: PreparedAuthorization,
  from: `0x${string}`,
  signature: Hex,
): PaymentAuthorization {
  const { v, r, s } = hexToSignature(signature);
  return {
    from,
    to: prepared.to,
    value: prepared.value.toString(),
    validAfter: prepared.validAfter.toString(),
    validBefore: prepared.validBefore.toString(),
    nonce: prepared.nonce,
    v: Number(v),
    r,
    s,
    token: p.dep.usdc.address,
    chainId: p.dep.chainId,
  };
}

/**
 * 买方签名一笔转账授权。返回可直接交给 facilitator 的 PaymentAuthorization。
 */
export async function signTransferAuthorization(p: SignParams): Promise<PaymentAuthorization> {
  const prepared = prepareAuthorization(p);
  const message = {
    from: getAddress(p.account.address),
    to: prepared.to,
    value: prepared.value,
    validAfter: prepared.validAfter,
    validBefore: prepared.validBefore,
    nonce: prepared.nonce,
  } as const;

  const signature = await p.account.signTypedData({
    domain: prepared.domain,
    types: EIP3009_TRANSFER_TYPES,
    primaryType: "TransferWithAuthorization",
    message,
  });
  return paymentAuthorization(p, prepared, message.from, signature);
}

/**
 * Signs through an isolated store. The caller supplies only a stable wallet ID
 * and the public address frozen into the Arena participant/Intent snapshot.
 */
export async function signTransferAuthorizationWithWallet(
  p: WalletSignParams,
  secrets: WalletSecretStore,
): Promise<PaymentAuthorization> {
  if (p.nonce === undefined) {
    throw new WalletSigningError("deterministic_nonce_required");
  }
  const prepared = prepareAuthorization(p);
  const signed = await secrets.signEip3009Authorization({
    walletId: p.walletId,
    expectedFrom: getAddress(p.expectedFrom),
    domain: prepared.domain,
    to: prepared.to,
    value: prepared.value,
    validAfter: prepared.validAfter,
    validBefore: prepared.validBefore,
    nonce: prepared.nonce,
  });
  if (getAddress(signed.from) !== getAddress(p.expectedFrom)) {
    throw new WalletSigningError("wallet_address_mismatch");
  }
  try {
    const authorization = paymentAuthorization(
      p,
      prepared,
      getAddress(signed.from),
      signed.signature,
    );
    if (!(await verifyAuthorizationLocally(authorization, p.dep))) {
      throw new WalletSigningError("wallet_signature_invalid");
    }
    return authorization;
  } catch (error) {
    if (error instanceof WalletSigningError) throw error;
    throw new WalletSigningError("wallet_signature_invalid");
  }
}

/**
 * 本地自校验（SETTLE-003 AC1）：从签名恢复地址，应等于 from。
 * 不联网，纯密码学验证，供签名后即时确认。
 */
export async function verifyAuthorizationLocally(auth: PaymentAuthorization, dep: Deployments): Promise<boolean> {
  const domain = eip712Domain(dep);
  const recovered = await recoverTypedDataAddress({
    domain,
    types: EIP3009_TRANSFER_TYPES,
    primaryType: "TransferWithAuthorization",
    message: {
      from: auth.from as Hex,
      to: auth.to as Hex,
      value: BigInt(auth.value),
      validAfter: BigInt(auth.validAfter),
      validBefore: BigInt(auth.validBefore),
      nonce: auth.nonce,
    },
    signature: { v: BigInt(auth.v), r: auth.r, s: auth.s },
  });
  return getAddress(recovered) === getAddress(auth.from);
}

/** 校验 SDK 计算的 domain 与链上 DOMAIN_SEPARATOR 一致（SETTLE-003 AC2） */
export async function checkDomainMatchesChain(dep: Deployments): Promise<boolean> {
  const client = createPublicClient({ transport: http(dep.rpc, { timeout: 30_000, retryCount: 5 }) });
  const onChain = await client.readContract({
    address: dep.usdc.address,
    abi: [{ type: "function", name: "DOMAIN_SEPARATOR", stateMutability: "view", inputs: [], outputs: [{ type: "bytes32" }] }],
    functionName: "DOMAIN_SEPARATOR",
  });
  // viem 内部会用同一 domain 生成 separator；这里比对信任 signTypedData 的 domain 编码。
  // 直接用 hashDomain 计算并对比：
  const { hashDomain } = await import("viem");
  const d = eip712Domain(dep);
  const computed = hashDomain({
    domain: { name: d.name, version: d.version, chainId: BigInt(d.chainId), verifyingContract: d.verifyingContract },
    types: { EIP712Domain: [
      { name: "name", type: "string" },
      { name: "version", type: "string" },
      { name: "chainId", type: "uint256" },
      { name: "verifyingContract", type: "address" },
    ]},
  });
  return computed.toLowerCase() === (onChain as string).toLowerCase();
}
