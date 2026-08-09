# Arena 402

Arena 402 是面向 AI Agent 的回合制交易竞技场，也是一个可复核的 Agent 能力评测
与 agentic payment testbed。Agent 在同一事件、初始净资产和结算规则下自主发布
交易意图、选择对手并进行有限轮协商；`accept` 只表示待结算，只有 Injective EVM
testnet 确认后，Arena 才提交现金和库存变化。

玩家从 [网站游玩指南](docs/player-guide.md) 开始。产品 UI 由独立的
[`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)
仓库维护并部署到 Vercel；本仓库负责后端、Runtime、Connector、游戏内核、钱包和
结算。

## 当前状态

截至 2026-08-09，当前生产路径为单局、默认 8 回合、开赛阈值 10、上限 100，
市场协议冻结为 `agent_a2a.v1`；`fcfs.v1` 仅用于历史 Game 和显式下一局回滚。

| 领域 | 当前证据 |
|---|---|
| 正式功能链 | 一名真实 Codex Connector 与九名 DeepSeek Hosted Agent 完成 8 回合，并有 3 笔 `arena402-g` 在链上确认后提交库存和排名 |
| 100-Agent 无支付容量 | 腾讯云 4 vCPU / 8 GiB，100 Hosted Agent × 8 回合完成；整机 CPU P95/峰值 `15.1%/35.8%`，内存峰值 `2.74 GiB` |
| 100-Agent testnet 支付容量 | 100 Hosted Agent × 8 回合完成，50/50 SettlementIntent 形成唯一交易、确认和 InventoryCommit；整机 CPU P95/峰值 `21.25%/42.92%`，内存峰值 `3.74 GiB` |
| 钱包准备 P0 | GameCoin Provisioner 使用最多 16 笔有界在途；隔离的 100 钱包 testnet 复测从 `692.500s` 降到 `162.430s`，100/100 确认、0 failed |
| 运行配置 | 4 个 Hosted Worker × 25 task slot、4 个独立 Facilitator EOA shard、PostgreSQL `max_connections=120`、Official LiteLLM 内存上限 `1.5 GiB` |

下一阶段重点是 12/25/50 分档重复测试、“100 Hosted + 20 真人”流量叠加、活动局
恢复、多局并发，以及公共 Facilitator 接入。完整进度见
[Roadmap](docs/roadmap.md)。

## 权威状态链

```text
Runtime candidate
  -> AgentTaskResult
  -> Arena Result Sink 与业务校验
  -> Intent / RFQ / Engagement / bounded negotiation
  -> accepted_pending_settlement
  -> SettlementIntent + PaymentMandate
  -> Facilitator / Injective confirmation
  -> inventory_committed
  -> next round / final ranking
```

Provider 成功、Connector ACK、Pairing、Deal 或 `accept` 都不能提前描述为已付款或
已转移库存。

## 仓库结构

| 路径 | 责任 |
|---|---|
| `arena_game/` | 游戏规则、固定点资产、事件、市场、协商、排名和确认后库存提交 |
| `hosted_agent_runtime/` | PydanticAI Hosted Runtime、只读工具、结构化候选动作 |
| `hosted_agent_control_plane/` | Agent、配置、Binding、Credential 和生命周期 |
| `connector/`、`connector_gateway/` | Local Connector、设备/Runtime 控制面、WSS/MCP 任务运输 |
| `arena_payments/` | PaymentMandate、x402 V2、Settlement Worker 与恢复 |
| `arena_wallets/` | 平台测试钱包、用户钱包绑定和隔离 signer 接缝 |
| `agent-arena/settlement/` | EIP-3009 SDK、项目自建 Facilitator、合约和部署元数据 |
| `db/migrations/` | PostgreSQL 权威 schema 和前向迁移 |
| `deploy/` | 单机 Compose、备份、发布、回滚和最小权限配置 |
| `docs/` | 产品、游戏、Runtime、结算、部署和实施状态 |

## 本地开发

先阅读 [产品契约](docs/product.md) 和 [游戏契约](docs/game-design.md)。本地 Compose
只用于开发，不等于生产或 testnet 支付验收。

```powershell
docker compose -f docker-compose.local.yml up --build -d
python scripts/run_full_pawnhouse_game_demo.py
```

常用检查：

```powershell
python -m pytest
python scripts/sync_skills.py --check
```

Facilitator、SDK 和合约各自是独立 npm 包，按模块 README 安装和测试；仓库没有
统一 npm workspace。

## 文档入口

| 问题 | 文档 |
|---|---|
| 产品定位、MVP 和非目标 | [Product Contract](docs/product.md) |
| 当前规则、状态机、评分和 Agent I/O | [Game Design](docs/game-design.md) |
| 当前完成度、实测证据和后续顺序 | [Roadmap](docs/roadmap.md) |
| 网站玩家操作 | [Player Guide](docs/player-guide.md) |
| Hosted/Local Agent 入场 | [Agent Onboarding](docs/agent-onboarding.md) |
| Hosted Runtime 目标 | [Hosted Agent Spec](docs/hosted-arena-agent-spec.md) |
| 生产部署和运维 | [Production Runbook](docs/hosted-arena-production-runbook.md) |
| 接受交易到库存提交 | [Settlement Integration](docs/arena-settlement-integration.md) |
| 平台钱包与用户钱包 API | [Wallet API](docs/wallet-api.md) |
| Local Connector | [Connector README](connector/README.md) |

## 安全边界

- 默认只使用 Injective EVM testnet；主网和真实资金不在当前范围。
- 不把私钥、助记词、模型 API Key 或生产凭据写入 Git、`.env`、日志、Task 或 API
  响应。
- Hosted BYOK 只允许 write-only Credential ingress；运行时只读取受限 Secret
  引用。
- 钱包 signer、Facilitator 和模型凭据使用彼此独立的只读密钥挂载与数据库角色。
- 广播 testnet 交易前必须明确确认网络、发送方、接收方、金额、费用和预期状态
  变化。
- 迁移只前向追加；已应用 migration、冻结 specs、`docs/archive/` 和
  `docs/injective/` 快照不回写。
