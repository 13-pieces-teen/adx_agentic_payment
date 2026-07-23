# Specifications Index (SDD)

> Spec-Driven Development：先写规范，再写实现。改需求先改这里。

## 规范编号规则

`SETTLE-NNN`（settlement 模块）· `TEE-NNN`（TEE）· `ARENA-NNN`（arena）

## 状态图例

`📝 Draft`（草稿）· `✅ Approved`（已定，可实现）· `🚧 In Progress`（实现中）· `✔️ Done`（实现并验证）· `🧊 Deferred`（延后）

## Settlement 规范（Felix）

| Spec | 标题 | 状态 | 依赖 |
|------|------|------|------|
| [SETTLE-000](settle-000-overview.md) | Settlement 架构与决策总览 | ✅ Approved | — |
| [SETTLE-001](settle-001-env-verification.md) | Injective EVM 环境验证 + USDC EIP-3009 探测 | ✔️ Done | — |
| [SETTLE-002](settle-002-sdk-contract.md) | Settlement SDK 接口契约 + MockSettlement | ✔️ Done | — |
| [SETTLE-002.5](settle-002_5-mock-usdc.md) | 部署 mock USDC（ERC-20 + EIP-3009）+ 给买方铸币 | ✔️ Done | 001 |
| [SETTLE-003](settle-003-005-x402-loop.md) | x402 EIP-3009 买方签名封装 | ✔️ Done | 002.5 |
| [SETTLE-004](settle-003-005-x402-loop.md) | 自建 x402 Facilitator（指向 Injective, legacy tx）| ✔️ Done | 002.5 |
| [SETTLE-005](settle-003-005-x402-loop.md) | 端到端直接结算闭环（A→B 打款）| ✔️ Done | 003,004 |

## 里程碑

- **M1（地基）**：✔️ 达成 — EVM 通（chainId 1439）+ facilitator gas 就绪 + token 方案确定（自部署 mock USDC）
- **M2（契约）**：✔️ 达成 — SettlementSDK 接口 + Mock/Real 双实现，队友可 import 集成（Real 已真实上链 tx 0x9d0f…1b6a）
- **M3（点到点）**：✔️ 达成 — 真实 mUSDC 从买方签名 → facilitator 上链 → 卖方到账，防重放+零买方gas 已验证（tx 0x2458…ef55）
- **M4（对接）**：替换 mock，接入 TEE 的 `GeneratePayment` 与 arena 的成交事件

## 待加入

- TEE-*、ARENA-* 规范由队友加入本目录。
