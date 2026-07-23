# Settlement Module — x402 on Injective EVM

> **Owner:** Felix · **Module:** `settlement/` · **Status:** 技术方案 v1 (2026-07-23)
>
> 本文是 settlement 模块的技术方案 + 协作契约。队友（arena / tee）只需读
> [§7 对接方式](#7-给队友的对接方式) 即可开始集成，无需理解链上细节。

---

## 0. TL;DR（一句话）

买卖 Agent 谈成价格后，买方 Agent 用私钥对一笔 USDC 转账做 **EIP-3009 离线签名**（不花 gas、不上链），
把签名交给我们**自建的 x402 facilitator**，facilitator 在 **Injective EVM testnet (chainId 1439)** 上
把这笔钱结算给卖方。整个签名/验证/结算流程复用现成的 `@x402` SDK，我们只写：
**① 一个 Escrow Solidity 合约 ② 一个跑在 Injective 上的 facilitator ③ 一个给 arena 调用的 TypeScript SDK。**

---

## 1. 为什么改用 EVM 而不是原 spec 的 CosmWasm

原 spec (Part 4) 把 settlement 设计成 Rust/CosmWasm 三合约手工验签。调研后改为 **Injective EVM + x402 SDK**，原因：

| 事实 | 结论 |
|------|------|
| x402 的 `exact` 结算基于 **EIP-3009 `transferWithAuthorization`（EVM/EIP-712 签名）** | 它就是为 EVM 设计的，CosmWasm 要从零手写验签 |
| Injective 现有**原生 EVM**：mainnet `1776` / **testnet `1439`**，支持 Hardhat/Foundry | 可直接当"一条 EVM 链"接入 x402 |
| x402 **运行时动态注册**任意 `eip155:<chainId>`，无需改源码 | `eip155:1439` 开箱可用 |
| Injective testnet 有 **USDC（Circle faucet）**，MultiVM Token Standard EVM/Cosmos 通用 | 有真 USDC 可结算，不用桥 |
| x402 是 Linux Foundation 项目，SDK 成熟（`@x402/*`），中间件一行接入 | 复用 SDK，工作量砍半 |

**唯一代价**：公开 facilitator（`x402.org/facilitator`）不认识 Injective，所以我们**自建 facilitator**。
这不是负担 —— 它恰恰是 demo 叙事："**我们把 x402 支付标准带到了 Injective**"。

> ⚠️ 对 spec 的偏离已记录在 [§8 与原 spec 的差异](#8-与原-spec-的差异)。TEE / Arena 的接口契约**保持不变**。

---

## 2. x402 协议速成（团队都该懂的 30 秒版）

标准 HTTP 402 支付流，但我们的场景里"资源"就是"确认成交"：

```
1. 卖方服务端标注某端点需付费   → 返回 402 + PaymentRequirements(要多少钱/哪条链/收款地址)
2. 买方客户端(带 signer)拿到 402 → 用私钥 EIP-712 离线签名一笔授权(不上链、不花 gas)
3. 买方带 PAYMENT header 重发请求
4. 服务端把 payload 交给 facilitator 的 /verify  → 校验签名有效、余额够
5. 服务端把 payload 交给 facilitator 的 /settle   → facilitator 代付 gas，上链执行转账
6. 转账确认 → 返回资源 + PAYMENT-RESPONSE header(结算 tx hash)
```

关键角色：
- **Client / 买方**：持私钥，签名付款。对应我们的 **Buyer Agent**（在 TEE 内）。
- **Resource Server / 卖方**：标价、收款。对应 **Seller Agent** / Arena。
- **Facilitator**：无托管的验证+结算服务，代付 gas。**我们自己跑，指向 Injective RPC。**

---

## 3. 我们的结算流程（映射到 spec Flow 5）

```
          ┌─────────────── TEE Layer ───────────────┐
Agreement │  Buyer Agent 私钥 (sealed in enclave)     │
reached   │    │                                     │
──────────┼────┤ 1. GeneratePayment(gRPC)            │
          │    ▼                                     │
          │  EIP-3009 签名 (from=buyer, to=escrow,   │
          │  value=agreed_price, nonce, validBefore) │
          └────┬─────────────────────────────────────┘
               │ x402 PaymentPayload (签名, 不上链)
               ▼
     ┌──────────────── Arena (orchestrator) ─────────┐
     │  调用 SettlementSDK.lockFunds(payload)         │
     └────┬───────────────────────────────────────────┘
          │  HTTP → 我们的 Facilitator
          ▼
   ┌──────────── Injective EVM testnet (1439) ────────┐
   │  Facilitator.settle():                           │
   │    USDC.transferWithAuthorization(sig) → Escrow  │  ← 资金锁进 Escrow 合约
   │  ── 卖方交付 (mock proof) ──                      │
   │  Escrow.release(escrowId) → USDC 转给卖方 - 手续费 │  ← settleTrade()
   └──────────────────────────────────────────────────┘
          │ tx hash + 事件
          ▼
     Arena 记录 settlements 表 + 更新 Leaderboard
```

**结算模式：已定为 A（直接结算）。** B（Escrow 托管）留作 Day2 增强。

- **A. 直接结算（当前）**：买方签名 `to = 卖方钱包`，facilitator 上链后 USDC 直接到卖方，**不写任何 Solidity 合约**。最快跑通端到端。
- **B. Escrow 托管（Day2）**：买方签名 `to = Escrow 合约`，卖方交付后 `release`，可退款。符合 spec Flow 5。

### 三个已定的设计决策（写进代码的依据）

| 决策 | 定论 | 为什么 |
|------|------|--------|
| **谁付 gas** | **只有 facilitator 钱包充 INJ**；买方只需 USDC、卖方零门槛 | x402 核心价值 = 付款方零门槛；demo 也只需给一个钱包领 INJ |
| **收款地址 `to`** | 直接结算 → `to = 卖方钱包`，钱直达卖方 | 直接结算无中间托管，MVP 一个合约都不用部署 |
| **防重放** | 靠 EIP-3009 签名里的 `nonce`（USDC 合约链上记录已用 nonce，重复提交自动 revert）+ `validBefore` 过期 | SDK 自动生成随机 nonce，**切勿在代码里写死 nonce** |

> ⚠️ **手续费取舍**：纯直接结算下钱直达卖方，没有环节可扣 0.5% 手续费。**demo 阶段先不收手续费**；
> 若要收，需等 Day2 的 Escrow 模式（由合约在 release 时扣）。已同步偏离 spec §5.1。

---

## 4. 目录结构

```
settlement/
├── README.md                  # 本文档
├── contracts/                 # Solidity 合约 (Hardhat)
│   ├── Escrow.sol             # 锁定/释放/退款 + 手续费 (模式 B)
│   ├── hardhat.config.ts      # 配置 Injective testnet 1439
│   └── test/
├── facilitator/               # 自建 x402 facilitator (指向 Injective RPC)
│   ├── src/index.ts           # /verify + /settle 服务端
│   └── .env.example           # RPC_URL, FACILITATOR_PRIVATE_KEY(gas 赞助)
├── sdk/                       # 给 Arena 调用的 TypeScript SDK
│   ├── src/
│   │   ├── types.ts           # SettlementSDK 接口 + AttestationReport (契约, 见 §7)
│   │   ├── client.ts          # 真实实现 (调 facilitator + 合约)
│   │   ├── mock.ts            # MockSettlement (队友立即可用, 零链上依赖)
│   │   └── x402.ts            # EIP-3009 签名封装 (给 TEE 侧 GeneratePayment 用)
│   └── package.json
├── scripts/
│   ├── deploy.sh              # 部署 Escrow 到 testnet
│   ├── setup-env.md           # faucet / 钱包 / RPC 配置步骤
│   └── e2e-test.ts            # 端到端: 签名→lock→settle
└── package.json
```

---

## 5. 关键技术参数（照抄即可）

### Injective EVM Testnet
| 项 | 值 |
|----|----|
| CAIP-2 network | `eip155:1439` |
| Chain ID | `1439` |
| JSON-RPC | `https://k8s.testnet.json-rpc.injective.network/` |
| Explorer | `https://testnet.blockscout.injective.network/` |
| Faucet (INJ, 付 gas 用) | `https://testnet.faucet.injective.network/`（选 **EVM address**，粘 `0x` 地址）|
| USDC | ⚠️ 官方文档/AdventureX Notion **均未给 testnet USDC 地址** → 大概率走 plan B 自部署 mock USDC（带 EIP-3009）|

> ⚠️ **Injective EVM 交易约束（来自官方 AdventureX 指南，血泪坑）**：
> Injective EVM 用 **legacy (type 0) 交易**，需**显式指定 gasPrice ≈ `160000000` (0.16 gwei)**。
> viem/ethers **默认发 EIP-1559 (type 2)**，不改会发不出交易。
> facilitator / 部署脚本必须配 `type: 'legacy'` + `gasPrice: 160000000n`。（Foundry 侧对应 `--legacy --gas-price 160000000`）

### x402 SDK 包（v2）
```bash
# 卖方 / facilitator 侧
npm i @x402/core @x402/evm @x402/express
# 买方 / 签名侧 (TEE GeneratePayment)
npm i @x402/fetch @x402/evm viem
```

### `exact` EVM 方案要点
- 签名标准：**EIP-3009 `transferWithAuthorization`**（USDC 原生支持，单签，最简）；
  若某 token 不支持则回退 **Permit2**。USDC 走 EIP-3009。
- 授权字段：`from, to, value, validAfter, validBefore, nonce`（EIP-712 结构化签名）。
- Injective 不在默认资产表 → **用显式 `TokenAmount`**：`amountInAtomicUnits` + token 地址 +
  `eip712: { name, version }`（从 USDC 合约的 `name()` / `version()` 读）。**不要用 `"$0.01"` 美元字符串。**

### 买方签名（TEE `GeneratePayment` 内部逻辑）
```typescript
import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const signer = privateKeyToAccount(BUYER_PK);          // sealed in enclave
const client = new x402Client();
client.register("eip155:1439", new ExactEvmScheme(signer));
// client 拿到 402 的 PaymentRequirements 后自动产出签名 payload
```

### 自建 facilitator（指向 Injective）
```typescript
import { HTTPFacilitatorClient } from "@x402/core/server";
// 卖方/Arena 侧只需把 url 指向我们自己的 facilitator:
const facilitatorClient = new HTTPFacilitatorClient({ url: process.env.FACILITATOR_URL });
// facilitator 服务端: RPC=Injective testnet, 用一个充了 INJ 的钱包代付 gas
```

---

## 6. 待你确认的决策

1. **Escrow 模式 A（直接结算）还是 B（托管+release）？** → 建议 A 先通，B 增强。
2. **手续费**：spec 说双方各 0.5%。放合约里扣，还是 demo 简化为单边 0.5%？
3. **买方私钥归属**：spec 说私钥 sealed 在 TEE。集成前 TEE 侧可先用测试私钥 mock 出 payload。

---

## 7. 给队友的对接方式 ⭐（arena / tee 只看这节）

**接口契约与原 spec Interface B 完全一致**，你们不用改代码。

### Arena → Settlement（TypeScript SDK）
```typescript
import { SettlementSDK } from "@agent-arena/settlement";      // 真实实现
import { MockSettlement } from "@agent-arena/settlement/mock"; // 开发期用这个

const settlement: SettlementSDK = new MockSettlement();  // 一行切换 mock/real
await settlement.lockFunds({ negotiationId, buyerWallet, sellerWallet, amount, currency:"USDC", x402Signature, expiry });
await settlement.settleTrade({ escrowId, deliveryProof });
```
接口方法（`registerAgent / lockFunds / settleTrade / refund / getEscrowStatus / getAgentInfo / verifyAttestation`）
签名见 `sdk/src/types.ts`，与 spec Part 5 Interface B 逐字对齐。

**`MockSettlement` Day1 就可用**（内存版，返回假 txHash），arena 不必等我链上完成即可跑通全流程。

### TEE → Settlement（`GeneratePayment`）
TEE 的 `GeneratePayment` gRPC 内部产出 x402 `PaymentPayload`。签名逻辑封装在 `sdk/src/x402.ts`，
TEE 侧可 import 或参照。集成前用测试私钥产出 payload 即可，无需真 enclave。

---

## 8. 与原 spec 的差异

| 原 spec | 现方案 | 影响面 |
|---------|--------|--------|
| CosmWasm `X402Settler`（Rust 手工验签） | Injective EVM + `@x402` SDK + 自建 facilitator | 仅 settlement 内部，接口不变 |
| `EscrowVault` (Rust) | `Escrow.sol` (Solidity/Hardhat) | 仅 settlement 内部 |
| `AgentRegistry` ERC-8004 (CosmWasm) | 保留，改 Solidity（或 demo 阶段用链下 mock） | `registerAgent` 接口不变 |
| 结算币 USDC | 不变 | — |
| Interface B / C（SDK 契约） | **不变** | arena / tee 零改动 |

---

## 9. 下一步（Felix 执行顺序）

- [ ] **T1** 跑通 Injective EVM testnet：领 INJ+USDC，Hardhat 配 1439，deploy hello-world 验证连通
- [ ] **T2** 写 `sdk/src/types.ts` + `mock.ts` → 提交，解锁队友集成（**最高优先级，队友在等**）
- [ ] **T3** 写 `x402.ts` 签名封装 + 单测（用测试私钥产出合法 payload）
- [ ] **T4** 起自建 facilitator，指向 Injective RPC，跑通 /verify + /settle（直接结算模式 A）
- [ ] **T5** 写 `Escrow.sol` + 部署 + `client.ts` 真实实现（模式 B）
- [ ] **T6** `e2e-test.ts`：签名 → lock → settle 全链路，拿到真 tx hash

---

## 参考链接

- Injective EVM 网络信息：https://docs.injective.network/developers-evm/network-information
- Injective EVM 部署（Hardhat）：https://docs.injective.network/developers-evm/smart-contracts/deploy-hardhat
- x402 卖方 quickstart：https://docs.x402.org/getting-started/quickstart-for-sellers
- x402 买方 quickstart：https://docs.x402.org/getting-started/quickstart-for-buyers
- x402 `exact` 方案：https://docs.x402.org/schemes/exact
- x402 exact-EVM 规范（字段级）：https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_evm.md
- x402 网络与 token 支持（自定义链）：https://docs.x402.org/core-concepts/network-and-token-support
- x402 源码：https://github.com/x402-foundation/x402
