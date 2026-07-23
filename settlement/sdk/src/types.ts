/**
 * Settlement 共享类型（SETTLE-003/004/005）
 */

/** 买方离线签名产出的 x402 支付授权（EIP-3009） */
export interface PaymentAuthorization {
  from: string;
  to: string;
  value: string; // atomic units (mUSDC decimals=6)
  validAfter: string; // unix 秒
  validBefore: string; // unix 秒
  nonce: `0x${string}`; // 32 字节随机
  v: number;
  r: `0x${string}`;
  s: `0x${string}`;
  token: `0x${string}`; // mUSDC 合约地址
  chainId: number;
}

/** deployments.json 结构（子集） */
export interface Deployments {
  chainId: number;
  rpc: string;
  usdc: {
    address: `0x${string}`;
    symbol: string;
    decimals: number;
    eip712Name: string;
    eip712Version: string;
  };
  wallets: { buyer: string; seller: string; facilitator: string };
}

export interface SettleResult {
  status: "success" | "error" | "pending";
  txHash?: string;
  blockNumber?: number;
  error?: string;
}
