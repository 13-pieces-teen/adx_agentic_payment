# @agent-arena/settlement-sdk

Settlement 模块的 TypeScript SDK：x402 签名 + Settlement 接口契约（对齐全量 spec Interface B）。

## arena 队友：怎么用

```typescript
import { MockSettlement, RealSettlement, type SettlementSDK } from "@agent-arena/settlement-sdk";

// 开发期：内存 mock，零链上依赖
const settlement: SettlementSDK = new MockSettlement();

// 联调期：真实上链（需 facilitator 在跑 + deployments.json）
// const settlement = new RealSettlement({ facilitatorUrl: "http://localhost:4021", deployments });

const { escrowId } = await settlement.lockFunds({
  negotiationId, buyerWallet, sellerWallet, amount, currency: "USDC",
  x402Signature,  // = JSON.stringify(PaymentAuthorization)，见下
  expiry,
});
const { txHash, amountToSeller, platformFee } = await settlement.settleTrade({ escrowId });
```

两种实现类型完全一致（都 `implements SettlementSDK`），一行切换。

## TEE 队友：怎么产出 x402 签名

```typescript
import { signTransferAuthorization, loadDeployments } from "@agent-arena/settlement-sdk";
import { privateKeyToAccount } from "viem/accounts";

const dep = loadDeployments("settlement/deployments.json");
const buyer = privateKeyToAccount(BUYER_PK);  // 将来在 enclave 内
const auth = await signTransferAuthorization({
  account: buyer, to: sellerAddr, value: 5_000_000n, dep, nowSeconds: Math.floor(Date.now()/1000),
});
// auth 即 PaymentAuthorization，JSON.stringify 后作为 lockFunds 的 x402Signature
```

## 模块

| 文件 | 内容 |
|------|------|
| `src/settlement.ts` | `SettlementSDK` 接口 + `AttestationReport`（Interface B 契约）|
| `src/mock.ts` | `MockSettlement`（内存版）|
| `src/real.ts` | `RealSettlement`（调 facilitator 真实上链）|
| `src/x402.ts` | EIP-3009 买方签名（SETTLE-003）|
| `src/types.ts` | `PaymentAuthorization` / `Deployments` |

## 自检

```bash
npm install
npx tsc --noEmit             # 类型检查
npx tsx scripts/test-mock.ts # MockSettlement 验收
npx tsx scripts/e2e.ts       # 端到端真实结算（需先起 facilitator）
```
