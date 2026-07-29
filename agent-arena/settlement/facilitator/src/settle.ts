/**
 * SETTLE-004 · facilitator 结算核心逻辑
 *
 * 职责：拿买方离线签名的 PaymentAuthorization，用 facilitator 私钥代付 gas，
 * 调 mUSDC.transferWithAuthorization 上链，把 mUSDC 从买方转给卖方。
 *
 * 可被 HTTP 层(index.ts)或 e2e 脚本直接调用。
 */
import {
  createPublicClient,
  createWalletClient,
  http,
  getAddress,
  type Hex,
  type Address,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { waitViaBlockscout, type TxResult } from "./lib-tx.ts";

// EIP-3009 + ERC20 只用到的 ABI 片段
const ABI = [
  {
    type: "function", name: "transferWithAuthorization", stateMutability: "nonpayable",
    inputs: [
      { name: "from", type: "address" }, { name: "to", type: "address" }, { name: "value", type: "uint256" },
      { name: "validAfter", type: "uint256" }, { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" },
      { name: "v", type: "uint8" }, { name: "r", type: "bytes32" }, { name: "s", type: "bytes32" },
    ], outputs: [],
  },
  { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
  { type: "function", name: "authorizationState", stateMutability: "view", inputs: [{ name: "a", type: "address" }, { name: "n", type: "bytes32" }], outputs: [{ type: "bool" }] },
  { type: "function", name: "faucet", stateMutability: "nonpayable", inputs: [{ name: "to", type: "address" }], outputs: [] },
] as const;

export interface PaymentAuthorization {
  from: string; to: string; value: string;
  validAfter: string; validBefore: string; nonce: Hex;
  v: number; r: Hex; s: Hex;
  token: Hex; chainId: number;
}

export interface FacilitatorConfig {
  rpc: string;
  chainId: number;
  facilitatorPk: Hex;
  gasPriceMult?: bigint; // 默认 3（D7: 须 > baseFee）
}

export class Facilitator {
  private pub;
  private wallet;
  private account;
  private cfg: FacilitatorConfig;
  // 串行队列：facilitator 单账户发交易，避免并发 nonce 冲突（spec 004 约束）
  private chain: Promise<unknown> = Promise.resolve();

  constructor(cfg: FacilitatorConfig) {
    this.cfg = cfg;
    this.account = privateKeyToAccount(cfg.facilitatorPk);
    const transport = http(cfg.rpc, { timeout: 60_000, retryCount: 8, retryDelay: 3000 });
    const chainDef = {
      id: cfg.chainId, name: "Injective EVM Testnet",
      nativeCurrency: { name: "Injective", symbol: "INJ", decimals: 18 },
      rpcUrls: { default: { http: [cfg.rpc] } },
    } as const;
    this.pub = createPublicClient({ chain: chainDef, transport });
    this.wallet = createWalletClient({ account: this.account, chain: chainDef, transport });
  }

  get address(): Address { return this.account.address; }

  async gasBalance(): Promise<bigint> {
    return this.pub.getBalance({ address: this.account.address });
  }

  async balanceOf(token: Hex, who: string): Promise<bigint> {
    return this.pub.readContract({ address: token, abi: ABI, functionName: "balanceOf", args: [getAddress(who)] }) as Promise<bigint>;
  }

  private async gasPrice(): Promise<bigint> {
    const base = await this.pub.getGasPrice();
    return base * (this.cfg.gasPriceMult ?? 3n);
  }

  /**
   * /verify：不上链的预检。校验 nonce 未用 + 买方余额足够 + 授权未过期。
   */
  async verify(auth: PaymentAuthorization): Promise<{ ok: boolean; reason?: string }> {
    const now = Math.floor(Date.now() / 1000);
    if (Number(auth.validBefore) <= now) return { ok: false, reason: "authorization expired" };
    const used = await this.pub.readContract({
      address: auth.token, abi: ABI, functionName: "authorizationState", args: [getAddress(auth.from), auth.nonce],
    }) as boolean;
    if (used) return { ok: false, reason: "nonce already used (replay)" };
    const bal = await this.balanceOf(auth.token, auth.from);
    if (bal < BigInt(auth.value)) return { ok: false, reason: `insufficient balance: has ${bal}, needs ${auth.value}` };
    return { ok: true };
  }

  /**
   * /settle：facilitator 代付 gas 上链结算。串行执行避免 nonce 冲突。
   */
  async settle(auth: PaymentAuthorization): Promise<TxResult & { txHash?: string }> {
    // 入队串行
    const run = this.chain.then(() => this._settleNow(auth));
    this.chain = run.catch(() => {}); // 保持链不因单次失败断裂
    return run;
  }

  private async _settleNow(auth: PaymentAuthorization): Promise<TxResult & { txHash?: string }> {
    const gasPrice = await this.gasPrice();
    const hash = await this.wallet.writeContract({
      address: auth.token,
      abi: ABI,
      functionName: "transferWithAuthorization",
      args: [
        getAddress(auth.from), getAddress(auth.to), BigInt(auth.value),
        BigInt(auth.validAfter), BigInt(auth.validBefore), auth.nonce,
        auth.v, auth.r, auth.s,
      ],
      type: "legacy",
      gasPrice,
      gas: 150_000n,
    });
    // Release the signer nonce queue as soon as the node accepts the
    // transaction. Arena owns confirmation/recovery and will not move
    // inventory until the configured chain confirmation threshold is met.
    return { status: "pending", txHash: hash };
  }

  /** /faucet：给地址发 mUSDC（expo 现场用） */
  async faucet(token: Hex, to: string): Promise<TxResult & { txHash?: string }> {
    const run = this.chain.then(async () => {
      const gasPrice = await this.gasPrice();
      const hash = await this.wallet.writeContract({
        address: token, abi: ABI, functionName: "faucet", args: [getAddress(to)],
        type: "legacy", gasPrice, gas: 120_000n,
      });
      const res = await waitViaBlockscout(hash);
      return { ...res, txHash: hash };
    });
    this.chain = run.catch(() => {});
    return run;
  }
}
