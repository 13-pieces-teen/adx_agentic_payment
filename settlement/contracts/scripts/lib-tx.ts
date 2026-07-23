/**
 * Injective EVM 交易工具（SETTLE-002.5 踩坑产物）
 *
 * 坑：公共 RPC `k8s.testnet` 是负载均衡，写交易能进块，但读节点索引延迟，
 *     viem 的 waitForTransactionReceipt 常超时/查不到，误判"失败"。
 * 解：发交易后轮询 blockscout API 确认状态（独立索引，可靠）。
 */
import type { PublicClient } from "viem";

const BLOCKSCOUT_API = "https://testnet.blockscout-api.injective.network/api/v2";

export interface TxResult {
  status: "success" | "error" | "pending";
  blockNumber?: number;
  createdContract?: string;
}

/** 轮询 blockscout 直到交易被索引，返回状态 + 生成的合约地址 */
export async function waitViaBlockscout(hash: string, timeoutMs = 90_000): Promise<TxResult> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${BLOCKSCOUT_API}/transactions/${hash}`);
      if (res.ok) {
        const d: any = await res.json();
        if (d.result === "success" || d.status === "ok") {
          return { status: "success", blockNumber: d.block_number, createdContract: d.created_contract?.hash };
        }
        if (d.result && d.result !== "pending" && d.status === "error") {
          return { status: "error", blockNumber: d.block_number };
        }
      }
    } catch {
      // 索引延迟，继续轮询
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  return { status: "pending" };
}
