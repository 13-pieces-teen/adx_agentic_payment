# Arena 402 Roadmap — Agentic Market Sandbox

> 2026-07-28 · 从"AI Agent 交易游戏"重新定位为"可编程多智能体市场沙盒 + Agentic Payment 协议"。
> 王城典当行保留为沙盒的内置演示场景，不再作为独立 C 端产品运营。

---

## 产品定位重构

Arena 402 对外叙事从三层游戏叙事调整为**两层产品线 + 一个演示场景**：

### A 线：Agentic Market Sandbox（量化/金融机构）

用真实 LLM agent 替代随机 agent 跑市场模拟。传统蒙特卡洛的局限在于随机 agent 不读信号、不形成预期、不谈判——LLM agent 能读事件、做策略推演、在撮合中形成价格发现，更接近真实市场微观结构。

- **目标用户**：量化研究员、风险建模团队、金融科技实验室
- **核心价值**：可配置场景 → 可回放决策链 → 可批量运行 → 可导出归因分析
- **当前状态**：游戏闭环已验证（12 agent × 10 rounds），场景配置化为待建能力

### B 线：Agentic Payment Protocol（Web3 infra）

x402 V2 + A2A + 链上结算的组合，实现"AI agent 自主支付"的合规路径——决策与资金分离、每笔交易可审计。

- **目标用户**：钱包/交易所/DeFi 协议的 AI agent 团队、链上支付基础设施方
- **核心价值**：PaymentMandate（reserve/consume/release）、私钥不存平台、逐笔链上证据
- **当前状态**：自建 Facilitator 跑通一笔 testnet 闭环，公共第三方 Facilitator 兼容性待验证

### 演示场景：王城典当行（King's Pawnhouse）

沙盒自带的内置 demo，证明平台能跑。不维护排行榜赛季、反作弊、客服或 C 端社区。游戏是产品文档里最生动的案例，不是需要持续运营的独立产品。

---

## 状态标注约定

文档中严格区分三种验证状态：

| 标注 | 含义 |
|------|------|
| `[x] local` | 本地开发闭环已验证 |
| `[x] testnet` | testnet 结算基础已跑通 |
| `[x] prod` | 公网生产验收已完成 |

这三种状态不能互相推导。Fake E2E、历史交易恢复、`accepted_pending_settlement` 或 Provider success 都不能等同于 prod。

---

## 已完成基础（黑客松 MVP）

以下能力已在黑客松期间实现并验证，是后续所有工作的地基：

### 游戏核心 `[x] local`
- 王城典当行：四种货物（粮草/精铁/战马/宝石）、20 金等值初始组合、受限事件 DSL
- PostgreSQL `arena402` schema：Game、Good、Participant、Balance、Holding、Event、Round、Ranking
- 1–10 回合可配置、FCFS 撮合、2–3 轮协商、回合快照、冻结终场价格、终局排名
- 版本化事件牌组、确定性 seed 洗牌、schedule commitment、结束后 seed 揭晓

### Agent 运行时 `[x] local`
- 三种 Runtime 统一接口：Hosted Agent（云端）、Local Connector（用户设备）、Rule Agent（确定性）
- 版本化 `AgentTaskV1` / `AgentTaskResultV1` / `AgentRuntimeDriver` 契约
- DeepSeek / OpenAI-compatible HTTPS Provider Adapter
- 每个 AgentTask 最多两个 Attempt，无 Provider/Model/Runtime fallback
- BYOK 模型：API Key 只存 HMAC / AES-GCM vault，不进业务库、不进日志
- Hosted Agent 的 create/PATCH/provisioning→ready 生命周期

### 撮合与协商 `[x] local`
- Hosted + Connector + Rule Agent 共用同一 AgentTask/Result schema
- FCFS 使用数据库 `result_received_at`，价格兼容订单内配对
- 协商按 pairing group 并发，每个 pairing 内保持 turn 顺序
- Arena Worker 自动编排 N 回合：Decide → Pair → Negotiate → Settle → Close

### 结算 `[x] testnet`
- 每笔 accepted trade 冻结唯一 `SettlementIntent`
- EIP-3009 direct-relay 原型，自建 Facilitator
- 一笔新鲜 Injective EVM testnet (`eip155:1439`) 交易完成链上确认 + 库存提交（2026-07-27 验证）
- PaymentMandate：额度/期限/范围/撤销 + 幂等 `reserve / consume / release`
- 沙盒 guest wallet：每用户永久绑定，私钥不入库/不入日志/不进 API
- 公开交易账本：跨 Game 的 SettlementIntent 投影，含链上 tx hash、确认数、Facilitator 收据

### 基础设施 `[x] prod`
- FastAPI 组合根：Connector / Hosted Agent / Arena / Pawnhouse API
- GitHub OAuth + PKCE + Session/CSRF Cookie
- Docker Compose（local + production），GitHub Actions CI/CD → `git archive` 部署
- 产品前端（Next.js）外部仓库 `sunruize93-cmyk/arena402`，Vercel 部署到 `arena402.com`
- Current Game 单例模式：自动生命周期、3 秒轮询、Join v2 + 手动组合

### Founding 402 NFT `[x] testnet`
- 独立 ERC-721 合约，402 个预生成 testnet 钱包
- GitHub 注册分配 rank + token ID，外部 claim/status UI 已上线
- 2026-07-27 完成 token ID 0–11 的批量铸币

---

## 里程碑重新规划

旧 M1–M7 已基本完成。以下按沙盒方向重新规划里程碑，分为两条并行轨道：

### 轨道 A：沙盒可配置化（量化场景）

使外部用户（不写代码的量化研究员）能自定义场景、运行模拟、导出结果。

### 轨道 B：协议化与 SDK（Web3 infra）

把 settlement/payment 层抽成独立 SDK，使第三方能集成 agentic payment 到自己系统。

### 共享轨道：生产稳定性与扩容

两条线共同依赖的基础能力。

---

## 轨道 A：Agentic Market Sandbox

### A1 — 场景配置引擎 `[ ]` 目标：2026 Q3

> 当前 game core 的 goods/events/初始条件全部硬编码。沙盒用户需要定义自己的市场。

- [ ] Scenario DSL：YAML/JSON 格式的场景定义文件，包含：
  - Goods 列表（名称、初始价格、是否为投机品、流动性乘数）
  - Event 牌组（事件文本、影响的价格/商品、概率权重）
  - 初始条件（开局资金、可选组合约束）
  - 回合参数（回合数、协商轮数上限、超时时间）
  - 结算规则（终场价格算法：event-driven / 外生输入 / 最后一轮均价）
- [ ] Scenario validator：上传时校验 DSL 合法性、价格非负、goods 数量≥1、event 牌组非空
- [ ] Scenario store：PostgreSQL 持久化 + 版本号，不可变快照（跑完的模拟可追溯到精确配置）
- [ ] 内置 Scenario 库：至少 3 个预设场景（王城典当行 / 单商品供需 / 多商品关联事件）

### A2 — 批量运行与参数扫描 `[ ]` 目标：2026 Q4

> 量化机构的刚需：同一个 scenario 换不同 agent 模型/温度/策略，跑 100 次看出价分布。

- [ ] Batch runner API：`POST /api/v1/sandbox/runs` 接受 scenario_id + agent_configs[] + batch_size
- [ ] 参数扫描语法：支持对 agent model、temperature、初始组合、event seed 的网格搜索
- [ ] 异步执行 + 进度轮询：`GET /api/v1/sandbox/runs/{run_id}` 返回 completed/total
- [ ] 结果聚合 API：所有 run 的价格序列、成交量、排名分布的统计摘要

### A3 — 可回放决策链 `[ ]` 目标：2026 Q4

> 当前已有 AgentTask → Result → SettlementIntent 链路，需强化为完整归因工具。

- [ ] Decision trace exporter：单次 run 的完整事件时间线导出（JSON Lines + CSV）
  - 每条记录：round → event → agent → task deadline → action → pair → negotiation → settlement
- [ ] Agent reasoning 存档：公开消息（sanitized，不含 private CoT）的持久化与回放
- [ ] 对比视图：同一 scenario 下两次 run 的 diff——哪个 agent 在哪一轮做了不同决策、结果差异

### A4 — Agent 行为分析面板 `[ ]` 目标：2025 Q1

> 不是游戏排行榜，是研究工具——"agent 在什么信号下做了什么决策、结果如何"。

- [ ] Agent 画像：每个 agent 的买卖倾向、谈判风格（首次报价偏离度、让步速度）、破产率
- [ ] 事件敏感度：哪些 event 触发了最大幅度的出价变化
- [ ] 价格发现可视化：每轮 bid/ask spread、成交价序列、与"理性价格"的偏离
- [ ] 导出：PNG/SVG 图表 + CSV 原始数据

### A5 — Python SDK `[ ]` 目标：2025 Q1

> 量化用户不碰 HTTP API，要 `pip install arena402-sandbox`。

- [ ] `arena402` pip 包：
  - `arena402.Scenario.from_yaml("my_market.yaml")`
  - `arena402.Run(scenario, agents=[...], batch=100).start()`
  - `run.results.to_pandas()` → DataFrame
  - `run.traces.export("traces.jsonl")`
- [ ] Jupyter Notebook 集成示例

---

## 轨道 B：Agentic Payment Protocol

### B1 — 结算层独立 SDK `[ ]` 目标：2026 Q3

> 当前 settlement 逻辑耦合在 Arena Worker 里。需要抽成第三方可直接集成的 SDK。

- [ ] `arena402-settlement` Python 包：
  - `PaymentMandate.create(wallet, token, limit, expiry)` → mandate_id
  - `mandate.reserve(intent)` → reservation
  - `mandate.consume(intent)` → tx_hash
  - `mandate.release(intent)` → released
- [ ] TypeScript 镜像包 `@arena402/settlement`
- [ ] 不依赖 Arena Game 的独立 test：创建 Mandate → 冻结 Intent → EIP-3009 签名 → 链上确认

### B2 — 公共 Facilitator 兼容性 `[ ]` 目标：2026 Q4

> 当前只有自建 Facilitator。公共第三方 Facilitator 兼容是"协议"与"原型"的分界线。

- [ ] 与至少一个公共 x402 V2 Facilitator 完成互联测试
- [ ] 文档化 Facilitator 集成接口：Facilitator 需要实现什么、返回什么、超时/重试约定
- [ ] Facilitator registry：让用户选择使用哪个 Facilitator（自建 / 公共 A / 公共 B）

### B3 — Technical Whitepaper `[ ]` 目标：2026 Q3

> 黑客松获奖 + 机构兴趣 = 窗口期。用 whitepaper 把信号变成 concrete inbound。

- [ ] "Agentic Payment: A Protocol for Verifiable AI Agent Settlement"
  - 问题定义：AI agent 自主支付需要什么（决策/资金分离、可审计、不可篡改）
  - 协议设计：PaymentMandate 三态机、x402 V2 流程、EIP-3009 relay
  - 安全模型：私钥不存平台、Mandate 额度上限、revoke 语义
  - 与现有方案的对比：传统托管 / MPC / 智能合约钱包 / 多签
  - 应用场景：量化沙盒结算、agent-to-agent 市场、DeFi 策略执行
- [ ] 中英文两个版本
- [ ] 发布在 arena402.com/whitepaper + GitHub

### B4 — 多链适配 `[ ]` 目标：2025 H1

> Injective EVM testnet 验证了协议可行性。机构可能要求其他链。

- [ ] Settlement backend 抽象层：EIP-3009 chain adapter interface
- [ ] 适配第二条测试链（优先 Arbitrum Sepolia 或 Base Sepolia）
- [ ] 多链 trade ledger：跨链 SettlementIntent 的统一投影

---

## 共享轨道：生产稳定性

### S1 — 100 Agent 容量验证 `[ ]` 目标：2026 Q4

> 当前验证了 12 agent。沙盒场景可能需要 50–100 agent 模拟真实市场深度。

- [ ] 按 10/12/25/50/100 Agent 分档记录 P50/P95/P99、queue age、timeout、retry、Token 消耗、每轮 wall time
- [ ] 4 × 25 Hosted task slot 的生产容量规划（已设计，未实测）
- [ ] 4 Facilitator EOA shard 的并发 settlement 验证（最坏 50 笔 accepted trade）
- [ ] `submitted_unknown` 自动恢复：按同一 EIP-3009 authorization 从 RPC/Blockscout 恢复

### S2 — 外部 Provider 生产验收 `[ ]` 目标：2026 Q3

- [ ] 真实 DeepSeek API Key 的生产 AES-GCM vault 部署（当前仅本地已验证）
- [ ] 服务器重启后 credential 连续性验证
- [ ] 公网 provider 出站 + 最小权限拒绝证据
- [ ] Tencent CAM/SSM 三身份（可选高安全路径）

### S3 — 前端完整化 `[ ]` 目标：2026 Q4

> 当前前端以游戏为主。沙盒需要场景管理、批量运行、分析面板。

- [ ] Scenario 管理页：创建/编辑/导入/导出 YAML
- [ ] Batch run 控制台：提交批量模拟、查看进度、下载结果
- [ ] Trace 回放页：单次 run 的时间线可视化
- [ ] Agent 分析页：行为画像、事件敏感度、对比视图
- [ ] 游戏页降级为 "Demo Scenario" 入口

### S4 — 文档矩阵 `[ ]` 目标：2026 Q3–Q4

| 文档 | 目标读者 | 状态 |
|------|---------|------|
| Quickstart（5 分钟跑起来） | 开发者 | 待写 |
| Scenario 配置指南 | 量化用户 | 待写 |
| Architecture Decision Records | 贡献者 | 待写 |
| Agentic Payment Whitepaper | 机构/Web3 | 待写 |
| API Reference (OpenAPI) | 集成方 | 已有 FastAPI `/docs`，需补充沙盒端点 |
| CONTRIBUTING.md | 开源贡献者 | 待写 |

---

## 实施顺序（建议）

```
2026 Q3 (7–9月)
├── S2: 外部 Provider 生产验收 ← 解除所有验证的阻塞项
├── B3: Technical Whitepaper ← 利用当前窗口期
├── A1: 场景配置引擎 ← 沙盒 MVP
└── S4: 文档矩阵（Quickstart + CONTRIBUTING）

2026 Q4 (10–12月)
├── A2: 批量运行与参数扫描
├── A3: 可回放决策链
├── B1: 结算 SDK（Python + TypeScript）
├── B2: 公共 Facilitator 兼容性
├── S1: 100 Agent 容量验证
└── S3: 前端完整化（Scenario 管理 + Batch 控制台）

2025 H1 (1–6月)
├── A4: Agent 行为分析面板
├── A5: Python SDK (arena402 pip 包)
├── B4: 多链适配（第二条测试链）
└── Design partner 反馈驱动的迭代
```

---

## 不做的事（Non-Goals）

明确列出以避免精力分散：

- ❌ C 端游戏运营：排行榜赛季、反作弊、客服、社区管理、Discord
- ❌ 主网资金：MVP 及沙盒阶段只跑 testnet，不上 mainnet
- ❌ 通用 Agent 框架集成：不做 LangGraph / Agent Studio 适配（除非 design partner 付费需求）
- ❌ 多局并发：维持同时一局 active Game，沙盒批量跑串行队列已够用
- ❌ 杠杆/做空/衍生品：沙盒保持现货交易
- ❌ ELO 排名系统：沙盒用户不需要竞技排名
- ❌ 长期记忆/人格市场：游戏向功能，不是沙盒核心

---

## 设计原则（约束继承）

以下原则从原 roadmap 保留，贯穿所有新里程碑：

1. **每笔 accepted trade 必须产生链上证据**——不可降级为纯数据库"假支付"
2. **私钥不入库、不入日志、不入 API 响应**——长期 signer 仅拥有密文读取函数
3. **BYOAgent**——平台不强制提供 LLM 推理，用户自带 API Key
4. **Agent 决策与资金结算严格分离**——Runtime 不持有 signer 权限
5. **所有状态变更可回放**——event → task → decision → pair → negotiation → settlement 链路完整

---

## Design Partner 计划

当前最重要的一步不是写代码，是找到至少一个 design partner：

- **量化机构方向**：找 1–2 家表示过兴趣的量化团队，给他们看 Scenario DSL 设计草稿，问"你实际想测什么"
- **Web3 方向**：找 1–2 个做 AI agent 框架或钱包的团队，给他们看 PaymentMandate 协议草稿，问"你愿不愿意集成"

Design partner 的意义：用真实需求倒逼 roadmap，不猜"量化机构想要什么"。

---

## 当前状态总览

| 能力域 | 状态 |
|--------|------|
| Game Core（王城典当行） | `[x] local` — 完全实现 |
| Agent Runtime（Hosted + Connector + Rule） | `[x] local` — 12 agent × 10 rounds |
| Settlement（自建 Facilitator） | `[x] testnet` — 1 笔闭环 |
| 公网 Provider / 加密 Vault | `[ ]` 待生产验收 |
| 公共 Facilitator 兼容 | `[ ]` 未验证 |
| 100 Agent 容量 | `[ ]` 未验证 |
| 场景配置引擎 | `[ ]` 待建 |
| 批量运行 / 参数扫描 | `[ ]` 待建 |
| 结算 SDK（Python + TypeScript） | `[ ]` 待建 |
| 文档矩阵 | `[ ]` 待补 |
| 前端（游戏 → 沙盒管理面板） | `[ ]` 待改 |

---

> 旧 roadmap（黑客松版本）归档在 `docs/archive/roadmap-hackathon.md`。
> 产品规格见 [`product.md`](product.md)，游戏设计见 [`game-design.md`](game-design.md)，
> 扩容设计见 [`arena-scale-out-design.md`](arena-scale-out-design.md)。
