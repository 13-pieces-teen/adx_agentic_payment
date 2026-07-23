/**
 * Injective EVM 交易确认（复用 SETTLE-002.5 踩坑解法）：
 * 公共 RPC 读节点索引延迟 → viem waitForTransactionReceipt 误判失败。
 * 改用 blockscout API 轮询确认。
 */
const BLOCKSCOUT_API = "https://testnet.blockscout-api.injective.network/api/v2";

export interface TxResult {
  status: "success" | "error" | "pending";
  blockNumber?: number;
  revertReason?: string;
}

export async function waitViaBlockscout(hash: string, timeoutMs = 90_000): Promise<TxResult> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${BLOCKSCOUT_API}/transactions/${hash}`);
      if (res.ok) {
        const d: any = await res.json();
        if (d.result === "success" || d.status === "ok") {
          return { status: "success", blockNumber: d.block_number };
        }
        if (d.status === "error" || (d.result && d.result !== "pending" && d.result !== null)) {
          return { status: "error", blockNumber: d.block_number, revertReason: d.revert_reason ?? d.result };
        }
      }
    } catch {
      // 索引延迟，继续
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  return { status: "pending" };
}
