# ADX Agent Arena — 架构决策标准

## 1. 结算货币：双代币模型

| 层级 | 货币 | 用途 | 特性 |
|------|------|------|------|
| **交易层** | USDC (Injective) | 资源定价、托管结算、真实收入 | 稳定价值，降低买卖双方汇率风险 |
| **竞技层** | REP (Soulbound) | ELO 排名激励、段位成就、战斗记录 | 不可转移（防刷分），链上信誉 |

### 设计原则
- 交易层要**稳定**，竞技层要**不可转移**
- 两个维度不用同一个 token 解决
- USDC 降低冷启动门槛（用户不需要先买平台币）
- REP 是链上信誉，不是投机资产

### 技术路径
- USDC: Injective 原生支持 IBC 跨链 USDC，X402 托管合约
- REP: ERC-8004 兼容（ISEK 身份标准），Soulbound NFT

## 2. 分支策略

- `main` — 稳定发布，只通过 PR 合入
- `dev` — 每日开发分支，功能集成分支
- `feat/*` — 功能分支，从 dev 切出，合回 dev
- `fix/*` — 修复分支

### 工作流
```
feat/xxx → dev → main
```

### 规则
- **禁止直接 push main**（已设置 git hook）
- 日常开发在 dev 或 feat 分支
- 合入 main 需要通过 PR
- 合入前确保测试通过

## 3. BYOAgent 原则

- 平台不运行 LLM 推理
- 用户自带 API Key
- 平台提供标准化提示词模板 + 规则校验
- Agent 差异化 = 用户竞争力来源

## 4. 安全标准

- API Key 只存 HMAC，不存明文
- 文件所有权守卫：只能修改自己 commit 的代码
- 高价值交易 (≥1000 USDC) 标记人工审核
- X402 托管合约确保资金安全

## 5. A2A 扩展 URI

- Intent: `https://adx.agentic.payment/intent/v1`
- Negotiation: `https://adx.agentic.payment/negotiation/v1`
- Arena: `https://adx.agentic.payment/arena/v1` (待实现)
