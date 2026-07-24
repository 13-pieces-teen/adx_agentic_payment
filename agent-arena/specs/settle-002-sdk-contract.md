# SETTLE-002 · Settlement SDK 接口契约 + MockSettlement

- **状态**: ✔️ Done
- **负责人**: Felix
- **依赖**: 无（MockSettlement）；SETTLE-005（RealSettlement 复用 facilitator）
- **解锁**: arena 队友并行集成（Interface B）

## 目的

把已跑通的 x402 结算能力（SETTLE-003/004/005）包装成 **spec Part 5 Interface B** 约定的
`SettlementSDK` 接口，让 arena 队友零改动 `import` 集成。提供两种实现：
- **MockSettlement**：内存版，零链上依赖，队友开发期一行切换即用。
- **RealSettlement**：真实实现，`lockFunds` 收买方 x402 签名，`settleTrade` 经 facilitator 上链。

## 接口对齐（逐字匹配 spec Interface B）

`SettlementSDK`: `registerAgent / lockFunds / settleTrade / refund / getEscrowStatus /
getAgentInfo / verifyAttestation` + `AttestationReport`。签名与全量 spec 一致，不改。

## 直接结算模式下的语义映射（D2）

我们选了直接结算（无链上 escrow 合约）。接口保留 escrow 措辞以兼容 spec，语义如下：

| 接口方法 | 直接结算下的真实行为 |
|---------|---------------------|
| `lockFunds` | **不真锁链上**：校验买方 x402 签名 + 记录一笔待结算意图，返回 escrowId。资金仍在买方处。|
| `settleTrade` | 用 lockFunds 记录的签名，经 facilitator 调 `transferWithAuthorization`，mUSDC 买方→卖方。|
| `refund` | 直接结算未真锁资金 → 标记该 escrow 取消（可选链上 cancelAuthorization）。|
| `registerAgent` / `verifyAttestation` | demo 阶段链下 mock（ERC-8004 注册合约留 Day2）。|

> 注：`x402Signature` 参数承载 SETTLE-003 的 `PaymentAuthorization`（JSON 序列化）。

## 验收结果 ✔️ Done

| # | 标准 | 结果 |
|---|------|------|
| 002-1 | MockSettlement 全部 7 方法内存跑通 | ✅ 9 项断言全过 |
| 002-2 | 两种实现类型签名一致（`implements SettlementSDK`）| ✅ tsc --noEmit 通过 |
| 002-3 | 手续费按 0.5% 计 | ✅ 100→seller 99.5/fee 0.5 |
| 002-4 | RealSettlement.settleTrade 真实上链 | ✅ tx 0x9d0ff64a…（2 mUSDC，seller 1.99/fee 0.01）|

## 交付物

- `sdk/src/settlement.ts` — `SettlementSDK` 接口 + `AttestationReport`
- `sdk/src/mock.ts` — `MockSettlement`
- `sdk/src/real.ts` — `RealSettlement`（调 facilitator）
- `sdk/src/index.ts` — 统一导出
- `sdk/scripts/test-mock.ts` — MockSettlement 验收

## 给 arena 队友（对接说明）

```typescript
import { MockSettlement, RealSettlement, type SettlementSDK } from "@agent-arena/settlement-sdk";
const settlement: SettlementSDK = new MockSettlement();          // 开发期
// const settlement = new RealSettlement({ facilitatorUrl, deployments }); // 联调期
const { escrowId } = await settlement.lockFunds({ ... });
const { txHash, amountToSeller } = await settlement.settleTrade({ escrowId });
```
