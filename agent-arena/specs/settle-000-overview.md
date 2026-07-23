# SETTLE-000 · Settlement 架构与决策总览

- **状态**: ✅ Approved
- **负责人**: Felix
- **详细技术方案**: [`../settlement/README.md`](../settlement/README.md)

## 目标

在 Injective EVM 上实现 x402 agentic payment：买方 Agent 离线签名 → 自建 facilitator 上链 → 卖方 USDC 到账。作为整个 Arena 的结算地基，先独立跑通点到点，再对接 TEE / 撮合。

## 已批准的关键决策（Decision Record）

| # | 决策 | 定论 | 理由 |
|---|------|------|------|
| D1 | 结算链路 | **Injective EVM (chainId 1439) + `@x402` SDK**，弃用原 spec 的 CosmWasm 手写验签 | x402 的 exact 方案本就是 EVM/EIP-3009；Injective 有原生 EVM；SDK 复用省一半工作量 |
| D2 | 结算模式 | **直接结算（模式 A）**：买方签名 `to = 卖方`，钱直达，MVP 不写合约 | 最快跑通端到端；Escrow（模式 B）留 Day2 |
| D3 | gas 承担 | **仅 facilitator 钱包充 INJ**；买方只需 USDC，卖方零门槛 | x402 核心价值 = 付款方零门槛 |
| D4 | 防重放 | 依赖 EIP-3009 的 `nonce`（USDC 合约链上记录）+ `validBefore`；SDK 自动生成，**禁止写死 nonce** | token 合约原生保证，不自造轮子 |
| D5 | 手续费 | **demo 阶段不收**（直接结算无环节可扣）；如需收，等 Escrow 模式由合约扣 | 偏离原 spec §5.1，已记录 |
| D6 | 协作/交付 | 纯本地 git + 硬盘拷贝，无 GitHub；接口契约与原 spec Interface B 逐字一致 | 网络安全 + 队友零改动集成 |
| D7 | EVM 交易类型 | 所有链上交易用 **legacy (type 0) + gasPrice≈160000000 (0.16 gwei)**，禁用 EIP-1559 | 官方 AdventureX 指南明确；viem/ethers 默认 EIP-1559 会发不出交易 |
| D8 | testnet USDC 来源 | 官方未提供 testnet USDC 地址 → **自部署 mock USDC（带 EIP-3009）为主路径** | 文档+Notion 均无地址；自部署可控且保证 EIP-3009 |
| D9 | 点到点实现方式 | 闭环先用 **viem 手写 EIP-3009 签名 + 自建 Express facilitator**，不直接依赖 `@x402` SDK | 自有合约+自建facilitator+Injective(x402默认不支持) 场景下 viem 最可控；核心机制与 x402 一致，HTTP 402 语义包装留 SETTLE-006 可选 |

## 三钱包模型

```
买方钱包(USDC，无需 INJ) ──签名──▶ facilitator(充 INJ，代付 gas) ──上链──▶ 卖方钱包(收 USDC)
                                         │
                                  USDC.transferWithAuthorization(签名)
```

## 待验证的地基假设（由 SETTLE-001 验证）

- **A1**: Injective EVM testnet (1439) JSON-RPC 可连、可部署/调用合约。
- **A2**: ~~Injective testnet 上的 USDC 实现了 EIP-3009~~ → **官方未提供 testnet USDC（Notion 已确认）**，
  改为 **自部署 mock USDC 合约（内置 EIP-3009）** 作为主路径（见 D8）。风险由"探测官方 USDC"转为"写好一个标准 EIP-3009 合约"，可控性更高。
