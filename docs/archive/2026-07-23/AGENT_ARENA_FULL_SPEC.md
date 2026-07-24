# Agent Arena — Complete Project Specification

> **Confidential Agent-to-Agent Trading Arena on Injective × x402 × Intel TEE**  
> Last updated: 2026-07-23 | Version: 0.1 (Hackathon MVP)  
> Hackathon: AdventureX 2026 (Hangzhou, July) | Prize Pool: $150K+

---

# Table of Contents

- [Part 1: Overview & Team](#part-1-overview--team)
- [Part 2: Vision & Principles](#part-2-vision--principles)
- [Part 3: Product Specification](#part-3-product-specification)
- [Part 4: Architecture & Technical Design](#part-4-architecture--technical-design)
- [Part 5: Module Interfaces (协作契约)](#part-5-module-interfaces-协作契约)
- [Part 6: Work Log](#part-6-work-log)

---

# Part 1: Overview & Team

## One-liner

A decentralized marketplace where AI agents autonomously negotiate, bid, and settle trades — with strategies kept confidential inside hardware-level trusted execution environments.

## Team

| Member | Role | Module | Key Deliverables |
|--------|------|--------|-----------------|
| **Felix** | x402 Settlement + Product Lead | `settlement/` | x402 集成、链上合约(CosmWasm)、产品协调 |
| **Teammate 1** | Confidential Computing | `tee/` | TEE enclave、attestation、安全通道、gRPC server |
| **Teammate 2** | Matchmaking & Arena | `arena/` | 撮合引擎、订单簿、数据库、Leaderboard、协商引擎 |

## Project Structure

```
agent-arena/
├── docs/                  # 文档
├── contracts/             # CosmWasm 智能合约 (Injective)
├── settlement/            # x402 支付集成 (Felix)
├── tee/                   # TEE enclave runtime (Teammate 1)
├── arena/                 # 撮合引擎 + 数据库 (Teammate 2)
├── agent/                 # Agent SDK + 策略引擎
├── frontend/              # Web UI (最后阶段)
└── scripts/               # 部署/测试脚本
```

## Tech Stack

| Layer | Technology | Language | Why |
|-------|-----------|----------|-----|
| Chain | Injective (CosmWasm) | Rust | iAgent 原生生态 |
| Settlement SDK | @agent-arena/sdk | TypeScript | 易于集成 |
| TEE Runtime | Intel TDX/SGX | Rust/C++ | 性能 + 安全 |
| TEE Interface | gRPC | Protobuf | 跨语言 + 高效 |
| Arena Backend | FastAPI/Gin | Python/Go | 快速开发 |
| Database | PostgreSQL | SQL | 关系型，适合订单簿 |
| Pub/Sub | Redis | - | 实时消息 |
| Frontend | Next.js | TypeScript | 最后阶段快速搭 |
| LLM | OpenAI/Anthropic/DeepSeek API | - | Agent 策略引擎 |

---

# Part 2: Vision & Principles

## Why This Exists

### The Problem
现有的 Agentic Payment（x402、B402、OKX APP）都是 **Agent → Service** 的单向购买模式：
- Agent 付费买 API/内容 → 固定价格，无协商
- 像自动售货机，不是市场

### The Opportunity
没有人在做 **Agent ↔ Agent** 的双向博弈交易市场：
- 买卖双方都是 Agent
- 价格通过多轮协商/竞价动态发现
- Agent 的"聪明程度"（模型能力 × 策略设计）决定交易收益
- 策略本身是一种竞争资产，需要保密

### Why Now
1. **LLM 能力到位** — Agent 可以理解交易意图并做出有策略深度的决策
2. **Injective 基础设施到位** — x402 主网 + iAgent SDK 2.0 + 原生链上订单簿 + ERC-8004 Agent 身份
3. **可信计算成熟** — Intel TDX/SGX 可以硬件级保护策略隐私并生成可验证证明
4. **行业空白** — Agent-to-Agent marketplace with confidential strategies = 新品类

## Core Beliefs (设计原则)

### 1. Strategy is Alpha
选模型、调 prompt、设计协商策略 = 竞争优势。就像量化基金的 alpha 来自策略，我们的 arena 中 alpha 来自 Agent 设计。**推论：策略必须保密，泄露 = 失去优势。**

### 2. Confidential by Hardware, Not by Trust
隐私不是靠"相信平台不偷看"，而是靠 TEE 硬件保证**物理上不可能偷看**。Remote attestation 让任何人都能验证公平性。

### 3. Agent Autonomy with Human Guardrails
Agent 在约束内自由博弈，但永远不能越过人类设定的底线：
- 买方：不超过预算上限
- 卖方：不低于底价
- Human 可随时 kill Agent

### 4. Negotiation is Price Discovery
砍价/博弈不是"低效"，而是去中心化的价格发现机制。比固定定价更能反映真实供需和信息不对称。

### 5. On-chain Settlement, Off-chain Intelligence
- 链做它擅长的：记账、转账、不可篡改
- TEE 做它擅长的：保密计算、策略执行
- LLM 做它擅长的：理解意图、制定策略

### 6. Arena = Meritocracy
不看你有多少钱，看你的 Agent 有多聪明。用便宜模型但好策略的人可以赢过用贵模型但烂策略的人。**存在一个最优平衡点：模型强度的边际收益 = 模型的边际 API 成本**

## Success Metrics (Hackathon)

| Metric | Target |
|--------|--------|
| End-to-end demo | 完整 flow: 注册 → 挂单 → 发现 → 协商 → 成交 → 链上结算 |
| 博弈真实性 | 协商不是走流程，Agent 真的有策略差异 |
| TEE 可验证 | 能展示 attestation proof + 证明策略不可见 |
| Pitch 清晰度 | 评委 3 分钟内理解"为什么这很酷" |
| 技术完整度 | 三个模块（x402 + TEE + Arena）能联通 |

## Non-Goals (明确不做)

- ❌ 不做通用 DEX / AMM
- ❌ 不做 AI 炒币 bot
- ❌ 不做 SaaS 平台（这是 protocol + demo）
- ❌ 不做真实资源交付验证（demo 阶段 mock）
- ❌ 不做多链支持（只 Injective）
- ❌ 不做生产级安全审计

## Long-term Vision (Beyond Hackathon)

1. **Agent Economy** — 每个人/公司都有自己的 trading Agent，7×24 自动寻找最优交易
2. **Strategy Marketplace** — 策略本身可以交易（加密卖 prompt/model config）
3. **Cross-chain Settlement** — x402 on multiple chains
4. **Reputation as Collateral** — Agent 信誉分可以减少 escrow 要求
5. **DAO Governance** — Arena 规则由参与者投票决定

---

# Part 3: Product Specification

## 1. Product Summary

**Agent Arena** 是一个基于 Injective 链的 Agent-to-Agent 自主交易撮合平台。

- **买方**把需求、预算、交易规则交给自己的 AI Agent
- **卖方**把资源/服务、底价、交易规则交给自己的 AI Agent
- Agent 在 Arena 中自主发现对手方、多轮协商博弈、达成交易
- 策略在 Intel TEE 中执行，硬件保证保密性
- 成交后通过 x402 协议在 Injective 链上即时 USDC 结算

## 2. Core Concepts (领域模型)

### 2.1 Actors

| Actor | 描述 | 运行环境 |
|-------|------|---------|
| **Human Principal** | 委托人，设定目标和约束 | 浏览器/CLI |
| **Buyer Agent** | 代表买方搜索、评估、砍价、成交 | TEE Enclave |
| **Seller Agent** | 代表卖方挂单、应答、报价、交付 | TEE Enclave |
| **Matchmaker** | 撮合引擎，维护 Order Book，广播市场 | TEE Enclave (neutral) |

### 2.2 Tradable Item (可交易物)

```typescript
interface TradableItem {
  id: string;
  category: ItemCategory;        // compute | data | ai_service | digital_good
  name: string;
  description: string;           // natural language
  delivery_method: DeliveryMethod; // api_endpoint | file_transfer | on_chain_proof | manual_confirm
  quantity: number;
  unit: string;                  // "hour", "request", "item"
  metadata: Record<string, any>;
}

enum ItemCategory {
  COMPUTE = "compute",           // GPU/CPU 算力
  DATA = "data",                 // 数据集
  AI_SERVICE = "ai_service",     // AI 模型推理、翻译、写作等
  DIGITAL_GOOD = "digital_good"  // 数字商品
}
```

### 2.3 Orders

```typescript
interface AskOrder {
  id: string;
  seller_agent_id: string;
  item: TradableItem;
  price_range: { min: number; max: number }; // min = 底价 (secret, in TEE)
  listed_price: number;           // 公开挂牌价
  currency: "USDC";
  expiry: timestamp;
  terms: string[];
  attestation_proof: string;
}

interface BidOrder {
  id: string;
  buyer_agent_id: string;
  requirements: {
    category: ItemCategory;
    description: string;
    min_quality: number;          // 0-100
  };
  budget: { max: number };        // 最高预算 (secret, in TEE)
  offered_price: number;          // 公开出价
  currency: "USDC";
  expiry: timestamp;
  terms: string[];
  attestation_proof: string;
}
```

### 2.4 Agent Configuration (Owner 设定)

```typescript
interface AgentConfig {
  // --- 公开信息 ---
  agent_id: string;               // ERC-8004 on-chain identity
  owner: string;                  // wallet address
  role: "buyer" | "seller";
  
  // --- 机密信息 (只存在于 TEE 内) ---
  model_config: {
    provider: string;             // "openai" | "anthropic" | "deepseek" | "local"
    model: string;                // "gpt-4o" | "claude-sonnet" | "deepseek-v4" | ...
    api_key: string;              // encrypted, sealed in TEE
  };
  strategy_prompt: string;        // 核心策略 prompt (the "alpha")
  constraints: {
    price_floor?: number;         // seller: 绝对不低于此价
    price_ceiling?: number;       // buyer: 绝对不高于此价
    max_rounds: number;           // 最大协商轮数
    personality: "aggressive" | "balanced" | "conservative";
    custom_rules: string[];
  };
}
```

## 3. Core Flows

### Flow 1: Agent 注册

```
Human → Platform: 创建 Agent 账户
Platform → Injective: Mint ERC-8004 NFT (Agent 链上身份)
Human → TEE: 部署 AgentConfig (model + strategy + constraints)
TEE → Injective: 提交 remote attestation proof
Chain: 记录 Agent 身份 + attestation hash
```
**Owner**: Felix (x402/链上) + Teammate 1 (TEE 部署)

### Flow 2: 卖方发布

```
Human → Seller Agent (TEE): "我有 X 资源，底价 Y，挂牌价 Z"
Seller Agent → Matchmaker: register_ask(item, listed_price, expiry)
Matchmaker → Order Book DB: 存储 Ask Order
Matchmaker → 广播: 新的 Ask 可用 (不含底价)
```
**Owner**: Teammate 2 (Matchmaker + DB)

### Flow 3: 买方搜索 & 匹配

```
Human → Buyer Agent (TEE): "我需要 X 类资源，预算 Y"
Buyer Agent → Matchmaker: search(category, budget_hint)
Matchmaker → Buyer Agent: [matching asks with listed prices]
Buyer Agent (TEE 内部, LLM): 评估哪些值得协商
Buyer Agent → Matchmaker: request_negotiation(ask_id)
Matchmaker → Seller Agent: incoming_negotiation_request(buyer_agent_id)
```
**Owner**: Teammate 2 (搜索/匹配逻辑)

### Flow 4: 协商 (核心创新 ⭐)

```
┌─ Round 1 ─────────────────────────────────────────┐
│ Buyer TEE:                                         │
│   Input: ask_price, my_budget, strategy_prompt     │
│   LLM reasoning: "挂牌价太高，先出低价试探..."       │
│   Output: propose(price=$0.6, reason="试探")       │
│                                                    │
│ Seller TEE:                                        │
│   Input: buyer_offer, my_floor, strategy_prompt    │
│   LLM reasoning: "太低了，但不想直接拒，让一步..."    │
│   Output: counter(price=$1.2, reason="有诚意可谈") │
└────────────────────────────────────────────────────┘

┌─ Round 2 ─────────────────────────────────────────┐
│ Buyer TEE:                                         │
│   LLM reasoning: "对方让步了，我也让一点..."         │
│   Output: propose(price=$0.85)                     │
│                                                    │
│ Seller TEE:                                        │
│   LLM reasoning: "接近我的底线了，可以接受..."       │
│   Output: accept(price=$0.85)                      │
└────────────────────────────────────────────────────┘

→ Agreement reached! Proceed to settlement.
```

**协商规则：**
- 最大轮数：configurable (default 5)
- 每轮超时：30s
- 终止条件：accept / reject / timeout / exceed_max_rounds
- 硬约束：Agent 永远不能超出 Human 设定的 price_floor/ceiling

**Owner**: Teammate 2 (协商协议引擎) + Felix (Agent 策略 prompt)

### Flow 5: 成交结算

```
Agreement → Buyer Agent (TEE): 生成 x402 payment authorization
Buyer Agent → Escrow Contract (Injective): lock USDC
Seller Agent: 交付资源/服务 (或 mock proof)
Buyer Agent: confirm_delivery (或 auto-confirm after timeout)
Escrow Contract → x402 settle: USDC 释放给 Seller
Chain: 更新双方 Agent 信誉 + 记录交易
```
**Owner**: Felix (x402 + Escrow 合约)

### Flow 6: Leaderboard 更新

```
Settlement complete → Arena DB: 记录交易结果
Arena: 计算 Agent performance metrics
  - Win rate (成交率)
  - Avg profit (平均收益 = 成交价 vs 预算/底价 的差)
  - Speed (平均协商轮数)
  - Volume (总交易量)
Leaderboard: 公开排名 (不暴露策略细节)
```
**Owner**: Teammate 2 (Leaderboard + 统计)

## 4. Agent Negotiation Protocol

### 4.1 消息格式

```typescript
interface NegotiationMessage {
  type: "propose" | "counter" | "accept" | "reject" | "withdraw";
  negotiation_id: string;
  from: string;                 // agent_id
  to: string;                   // agent_id
  round: number;
  timestamp: number;
  offer?: {
    price_per_unit: number;
    quantity: number;
    currency: "USDC";
    terms: string[];
    valid_until: number;
  };
  public_reason?: string;       // Agent 可选的公开解释
  attestation: {
    enclave_id: string;
    signature: string;
    report: string;
  };
}
```

### 4.2 协商状态机

```
INITIATED → PROPOSING → COUNTERING → ... → AGREED | FAILED
     │                                          │        │
     └── WITHDRAWN (任何时候可退出)              │        │
                                                ▼        ▼
                                          SETTLING   CANCELLED
                                                │
                                                ▼
                                            COMPLETED
```

### 4.3 策略引擎 (LLM-driven, in TEE)

**输入：** 当前对方 offer、历史 offer 轨迹、我方约束 (floor/ceiling)、市场参考价、策略 prompt、personality setting

**输出：** Action (accept/counter/reject/withdraw) + Offer params + Public reasoning (optional)

**策略 prompt 模板：**
```
你是一个 {personality} 风格的交易 Agent。
你的底线是 {price_floor}，绝对不能低于此价格。
你的目标是最大化成交价，同时保持合理的成交率。
当前市场均价参考：{market_avg}。

策略指导：
- 第一轮：{first_round_strategy}
- 如果对方让步超过 10%：{concession_strategy}
- 如果接近底线：{near_floor_strategy}
- 最后一轮：{final_round_strategy}
```

## 5. Arena Economics (经济模型)

### 5.1 费用结构

| 项目 | 谁付 | 多少 | 说明 |
|------|------|------|------|
| Agent LLM API 费用 | Agent Owner | 按量计 | Owner 自备 API key，这是"竞争成本" |
| 链上 Gas | 平台补贴 (testnet) | ~$0.0003/tx | Injective gas 极低 |
| 撮合手续费 | 成交双方各 0.5% | 从成交金额扣 | 平台收入来源 |
| TEE 运行费 | 平台承担 (hackathon) | N/A | 未来可向 agent owner 收费 |

### 5.2 收益逻辑

```
Seller 利润 = 成交价 - 底价 - API成本 - 手续费
Buyer 节省 = 预算上限 - 成交价 - API成本 - 手续费

→ 更聪明的 Agent (更好的模型/策略) = 更多利润/节省
→ 但更好的模型 = 更高 API 成本
→ 存在最优平衡点！
```

### 5.3 Arena 竞争动态

- **Meta-game**：如果所有人都用 GPT-4o，那用 DeepSeek+好策略的人反而赚（低成本）
- **策略迭代**：看到 Leaderboard 排名后，Owner 会调整策略 → 市场进化
- **信息不对称**：Seller 不知 Buyer 底线，Buyer 不知 Seller 底价 → 经典博弈论

## 6. TEE Integration (可信计算)

### 6.1 TEE 解决什么问题

| 没有 TEE | 有 TEE |
|---------|--------|
| 平台能看到所有 Agent 策略 | 策略在 enclave 中，平台不可见 |
| 无法证明撮合公平 | Remote attestation 可验证 |
| 对手可能知道你的底线 | 硬件隔离，信息泄露不可能 |
| 模型选择暴露（别人能抄） | 模型调用在 TEE 内，外界只看到结果 |

### 6.2 TEE 架构

```
┌─────────────────────────────────────────────┐
│           TEE Enclave (Intel TDX/SGX)        │
│                                             │
│  ┌─────────────────────────────────────┐   │
│  │  Agent Runtime                       │   │
│  │  • strategy_prompt (sealed)          │   │
│  │  • model_config + api_key (sealed)   │   │
│  │  • constraints (sealed)              │   │
│  │  • LLM API call (encrypted channel)  │   │
│  │  • Decision logic                    │   │
│  │                                      │   │
│  │  Outputs (公开):                      │   │
│  │  → NegotiationMessage                │   │
│  │  → Attestation Proof                 │   │
│  └─────────────────────────────────────┘   │
│                                             │
│  Security Properties:                       │
│  • Code integrity (未被篡改)                │
│  • Data confidentiality (数据不可泄露)       │
│  • Remote attestation (可验证执行环境)       │
└─────────────────────────────────────────────┘
```

### 6.3 Attestation 链上验证

```
TEE → generate attestation report
Report contains:
  - enclave measurement (code hash)
  - enclave data (agent_id + session hash)
  - platform info (Intel TDX/SGX version)
  
Report → Injective CosmWasm Contract: verify_attestation()
  - 验证签名有效性
  - 验证 enclave code 未被篡改
  - 记录验证结果 → Agent 身份可信
```

## 7. x402 Settlement Integration

### 7.1 x402 Flow

```
1. Agreement reached (both agents accept)
2. Buyer Agent (TEE) → generate x402 payment authorization
   - amount: agreed_price
   - recipient: seller_wallet
   - token: USDC on Injective
   - expiry: 10 minutes
3. Authorization → Escrow Contract (Injective)
4. Escrow verifies: Valid x402 signature + Buyer balance sufficient → Lock funds
5. Seller Agent: deliver service/proof
6. Delivery confirmed → Escrow: x402 settle() → Release USDC to Seller → Deduct fee
7. Both agents receive settlement confirmation
```

### 7.2 链上合约 (Injective CosmWasm)

| Contract | 功能 | Owner |
|----------|------|-------|
| `AgentRegistry` | ERC-8004 Agent 身份注册 + attestation 存储 | Felix |
| `EscrowVault` | 锁定资金 + 条件释放 | Felix |
| `X402Settler` | x402 签名验证 + 结算执行 | Felix |
| `ArenaFeeCollector` | 手续费收取 + 分配 | Felix |

## 8. MVP Scope (72h Hackathon)

### ✅ Must Have (必须交付)

- [ ] 2 个 Agent 完成完整协商 + 成交 (至少 3 轮博弈)
- [ ] Agent 策略由 LLM 驱动 (不是硬编码 if-else)
- [ ] TEE enclave 运行 Agent + 生成 attestation
- [ ] 链上 USDC 结算 (Injective testnet, x402)
- [ ] 协商过程可视化 (最简 Web UI)
- [ ] 不同模型/策略的 Agent 表现有可见差异

### 🔶 Should Have (尽量做)

- [ ] 多 Agent 竞争 (3+ agents in arena)
- [ ] Leaderboard 排名展示
- [ ] Agent 信誉系统 (ERC-8004 identity + history)
- [ ] Human 实时干预 ("别接受低于 $1 的")
- [ ] 链上 attestation 验证合约

### ❌ Won't Have (不做)

- [ ] 真实资源交付验证 (mock)
- [ ] 完整争议解决机制
- [ ] 多链/跨链
- [ ] 生产级安全
- [ ] 完善的前端 UI

## 9. Demo Scenario

### 推荐场景: AI Service Marketplace

**故事线：**
> "Alice 有一个闲置的 GPT-4o API 额度，想出租赚钱。Bob 需要 AI 翻译服务但想省钱。他们各自设定策略后让 Agent 去 Arena 博弈。Alice 的 Agent 用保守策略开高价，Bob 的 Agent 用激进策略砍价。3 轮协商后，以一个双方都能接受的价格成交。全程策略保密，结算链上完成。"

**演示流程 (3 分钟)：**
1. (30s) 展示 Alice 和 Bob 分别配置自己的 Agent
2. (60s) 实时展示协商过程 (对话 + 价格变化图)
3. (30s) 成交! 链上 tx hash 展示
4. (30s) Leaderboard 更新
5. (30s) 展示 TEE attestation proof (证明策略保密)

## 10. Data Model (数据库)

```sql
-- Agent 注册表
CREATE TABLE agents (
  id VARCHAR PRIMARY KEY,
  owner_wallet VARCHAR NOT NULL,
  role ENUM('buyer', 'seller'),
  enclave_id VARCHAR,
  attestation_hash VARCHAR,
  reputation_score DECIMAL DEFAULT 50.0,
  total_trades INT DEFAULT 0,
  win_rate DECIMAL DEFAULT 0.0,
  created_at TIMESTAMP,
  status ENUM('active', 'paused', 'destroyed')
);

-- 订单簿
CREATE TABLE orders (
  id VARCHAR PRIMARY KEY,
  agent_id VARCHAR REFERENCES agents(id),
  type ENUM('ask', 'bid'),
  item_category VARCHAR,
  item_description TEXT,
  listed_price DECIMAL,
  quantity DECIMAL,
  unit VARCHAR,
  expiry TIMESTAMP,
  status ENUM('open', 'matched', 'expired', 'cancelled'),
  created_at TIMESTAMP
);

-- 协商记录
CREATE TABLE negotiations (
  id VARCHAR PRIMARY KEY,
  buyer_agent_id VARCHAR REFERENCES agents(id),
  seller_agent_id VARCHAR REFERENCES agents(id),
  ask_order_id VARCHAR REFERENCES orders(id),
  status ENUM('active', 'agreed', 'failed', 'timeout'),
  rounds_completed INT DEFAULT 0,
  final_price DECIMAL,
  started_at TIMESTAMP,
  ended_at TIMESTAMP
);

-- 协商消息流
CREATE TABLE negotiation_messages (
  id VARCHAR PRIMARY KEY,
  negotiation_id VARCHAR REFERENCES negotiations(id),
  round INT,
  from_agent VARCHAR,
  type ENUM('propose', 'counter', 'accept', 'reject', 'withdraw'),
  offered_price DECIMAL,
  public_reason TEXT,
  attestation_hash VARCHAR,
  timestamp TIMESTAMP
);

-- 结算记录
CREATE TABLE settlements (
  id VARCHAR PRIMARY KEY,
  negotiation_id VARCHAR REFERENCES negotiations(id),
  escrow_tx_hash VARCHAR,
  settle_tx_hash VARCHAR,
  amount DECIMAL,
  fee DECIMAL,
  status ENUM('locked', 'settled', 'refunded'),
  created_at TIMESTAMP,
  settled_at TIMESTAMP
);

-- Leaderboard 视图
CREATE VIEW leaderboard AS
SELECT 
  a.id, a.owner_wallet, a.role,
  a.reputation_score, a.total_trades, a.win_rate,
  AVG(CASE WHEN a.role = 'seller' 
      THEN n.final_price - o.listed_price * 0.7
      ELSE o.listed_price - n.final_price END) as avg_profit
FROM agents a
JOIN negotiations n ON (a.id = n.buyer_agent_id OR a.id = n.seller_agent_id)
JOIN orders o ON o.id = n.ask_order_id
WHERE n.status = 'agreed'
GROUP BY a.id;
```

---

# Part 4: Architecture & Technical Design

## System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         HUMAN LAYER                                   │
│                                                                      │
│   Buyer Human                              Seller Human              │
│   "我要买 X, 预算 Y,                       "我有 Z 资源, 底价 W,     │
│    策略：激进砍价"                           策略：稳健报价"           │
│         │                                        │                   │
└─────────┼────────────────────────────────────────┼───────────────────┘
          │ (encrypted config)                     │ (encrypted config)
          ▼                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     TEE LAYER (Intel TDX/SGX)                         │
│                     [Teammate 1 负责]                                  │
│                                                                      │
│  ┌──────────────────┐              ┌──────────────────┐             │
│  │ Buyer Agent       │◄── msg ────►│ Seller Agent      │             │
│  │ Enclave           │             │ Enclave           │             │
│  │                   │             │                   │             │
│  │ • LLM Strategy    │             │ • LLM Strategy    │             │
│  │ • Constraints     │             │ • Constraints     │             │
│  │ • Decision Logic  │             │ • Decision Logic  │             │
│  │ • Attestation Gen │             │ • Attestation Gen │             │
│  └────────┬──────────┘             └────────┬──────────┘             │
│           │ (actions + attestation)          │                        │
└───────────┼──────────────────────────────────┼───────────────────────┘
            │                                  │
            ▼                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     ARENA LAYER (Matchmaking)                          │
│                     [Teammate 2 负责]                                  │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │
│  │ Order Book   │  │ Negotiation │  │ Leaderboard │                 │
│  │ (asks/bids) │  │ Engine      │  │ & Stats     │                 │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                 │
│         │                │                 │                         │
│         └────────────────┼─────────────────┘                         │
│                          │                                           │
│                    ┌─────▼─────┐                                     │
│                    │ Arena DB  │ (PostgreSQL)                         │
│                    └─────┬─────┘                                     │
└──────────────────────────┼───────────────────────────────────────────┘
                           │ (settlement request)
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     SETTLEMENT LAYER (Injective)                      │
│                     [Felix 负责]                                       │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │
│  │ Agent        │  │ Escrow      │  │ x402        │                 │
│  │ Registry     │  │ Vault       │  │ Settler     │                 │
│  │ (ERC-8004)  │  │ (lock/      │  │ (verify/    │                 │
│  │             │  │  release)   │  │  settle)    │                 │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘                 │
│         │                │                 │                         │
│         └────────────────┼─────────────────┘                         │
│                          │                                           │
│                 Injective Blockchain (USDC)                           │
└──────────────────────────────────────────────────────────────────────┘
```

## Module A: Settlement (Felix)

**Tech Stack:** Rust (CosmWasm) + TypeScript (SDK wrapper)

```
settlement/
├── contracts/
│   ├── agent-registry/      # ERC-8004 Agent NFT 注册
│   │   └── src/ (contract.rs, state.rs, msg.rs)
│   ├── escrow-vault/        # Escrow 锁定/释放
│   │   └── src/ (contract.rs, state.rs, msg.rs)
│   └── x402-settler/        # x402 签名验证 + 结算
│       └── src/ (contract.rs, x402.rs, msg.rs)
├── sdk/                     # TypeScript SDK (供 Arena 调用)
│   └── src/ (client.ts, types.ts, x402.ts)
├── scripts/ (deploy.sh, test.sh)
└── README.md
```

## Module B: TEE (Teammate 1)

**Tech Stack:** Rust/C++ (enclave code) + gRPC (interface)

```
tee/
├── enclave/
│   └── src/ (agent_runtime.rs, llm_client.rs, strategy_engine.rs,
│             attestation.rs, sealed_storage.rs, main.rs)
├── host/
│   └── src/ (enclave_manager.rs, grpc_server.rs, main.rs)
├── proto/
│   └── tee_service.proto
├── attestation/ (verifier.rs, intel_dcap.rs)
├── scripts/ (setup_sgx.sh, run_enclave.sh)
└── README.md
```

## Module C: Arena (Teammate 2)

**Tech Stack:** Python/Go (backend) + PostgreSQL (DB) + Redis (pub/sub)

```
arena/
├── src/
│   ├── matchmaker/ (order_book.py, matcher.py, discovery.py)
│   ├── negotiation/ (engine.py, protocol.py, state_machine.py, timeout.py)
│   ├── leaderboard/ (scorer.py, ranking.py, stats.py)
│   ├── api/ (routes.py, websocket.py, middleware.py)
│   └── db/ (models.py, migrations/)
├── config/ (arena_config.yaml)
├── tests/
├── docker-compose.yml
└── README.md
```

## Communication Patterns

```
Sync (协商):  [Arena API] ──gRPC──> [TEE Service] ──internal──> [Agent Enclave] ──encrypted──> [LLM Provider]
Async (结算): [Arena] ──SDK call──> [Settlement SDK] ──tx──> [Injective Chain] ──event──> [Arena]
```

## Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Agent 策略泄露 | TEE 硬件隔离 + sealed storage |
| 撮合作弊 | Matchmaker 逻辑可在 TEE 中运行 |
| x402 重放攻击 | Nonce + expiry in payment auth |
| Agent 越权 | Constraints 硬编码在 enclave，LLM 输出经 validator |
| DDoS | Rate limiting on Arena API |

## Deployment (Hackathon Demo)

```
┌─────────────────────────────────────────┐
│         Demo Machine (single host)       │
│  Docker Compose:                        │
│  ├── arena-service (port 8080)          │
│  ├── postgres (port 5432)              │
│  ├── redis (port 6379)                 │
│  └── frontend (port 3000)              │
│  TEE Service:                           │
│  └── SGX-enabled host (or simulated)    │
│  Chain: Injective Testnet (remote)      │
└─────────────────────────────────────────┘
```

---

# Part 5: Module Interfaces (协作契约)

> ⚠️ **这是三人协作最重要的部分。**  
> 任何接口变更必须通知相关方。  
> 开发时 mock 对方模块，基于此接口集成。

## 核心原则

```
              ┌─────────────┐
              │   Frontend  │ (最后阶段, 调用 Arena API)
              └──────┬──────┘
                     │ REST + WebSocket
                     ▼
┌────────────────────────────────────────────┐
│              Arena Service                  │
│           (Teammate 2 = Orchestrator)       │
└───────┬───────────────────────┬────────────┘
        │ gRPC                  │ TypeScript SDK
        ▼                       ▼
┌───────────────┐      ┌───────────────────┐
│  TEE Service  │      │ Settlement Service │
│ (Teammate 1)  │      │    (Felix)         │
└───────────────┘      └───────────────────┘
```

**Arena 是编排者（orchestrator），TEE 和 Settlement 是被调用的服务。**

## Interface A: Arena → TEE (gRPC)

```protobuf
syntax = "proto3";
package agent_arena.tee;

service TEEAgentService {
  rpc DeployAgent(DeployAgentRequest) returns (DeployAgentResponse);
  rpc Negotiate(NegotiateRequest) returns (NegotiateResponse);
  rpc GeneratePayment(PaymentRequest) returns (PaymentResponse);
  rpc DestroyAgent(DestroyAgentRequest) returns (DestroyAgentResponse);
  rpc GetAttestation(AttestationRequest) returns (AttestationResponse);
}

message DeployAgentRequest {
  string agent_id = 1;
  bytes encrypted_config = 2;         // 加密的 AgentConfig (只有 TEE 能解)
  string owner_pubkey = 3;
}

message DeployAgentResponse {
  string enclave_id = 1;
  AttestationReport attestation = 2;
  bool success = 3;
  string error = 4;
}

message NegotiateRequest {
  string enclave_id = 1;
  NegotiationMessage incoming_message = 2;
  MarketContext market_context = 3;
}

message NegotiateResponse {
  NegotiationMessage outgoing_message = 1;
  AttestationReport attestation = 2;
  bool terminated = 3;
}

message NegotiationMessage {
  string type = 1;              // "propose"|"counter"|"accept"|"reject"|"withdraw"
  string negotiation_id = 2;
  string from_agent = 3;
  string to_agent = 4;
  int32 round = 5;
  Offer offer = 6;
  string public_reason = 7;
  int64 timestamp = 8;
}

message Offer {
  double price_per_unit = 1;
  double quantity = 2;
  string currency = 3;
  repeated string terms = 4;
  int64 valid_until = 5;
}

message MarketContext {
  double avg_price = 1;
  double lowest_ask = 2;
  double highest_bid = 3;
  int32 active_sellers = 4;
  int32 active_buyers = 5;
}

message PaymentRequest {
  string enclave_id = 1;
  string negotiation_id = 2;
  double amount = 3;
  string recipient_wallet = 4;
  string currency = 5;
  int64 expiry = 6;
}

message PaymentResponse {
  bytes x402_signature = 1;
  string payment_hash = 2;
  AttestationReport attestation = 3;
}

message AttestationReport {
  string enclave_id = 1;
  bytes enclave_measurement = 2;  // MRENCLAVE
  bytes enclave_data = 3;
  bytes signature = 4;            // Intel signed
  string platform_info = 5;
  int64 timestamp = 6;
}

message AttestationRequest { string enclave_id = 1; }
message AttestationResponse { AttestationReport report = 1; }
message DestroyAgentRequest { string enclave_id = 1; string reason = 2; }
message DestroyAgentResponse { bool success = 1; }
```

### TEE Mock (Teammate 2 开发用)

```python
# arena/mocks/tee_mock.py
class MockTEEService:
    """模拟 TEE 服务，直接用 LLM API 生成回复（无真实 enclave）"""
    async def negotiate(self, request):
        response = await call_llm(request.incoming_message, strategy="balanced")
        return NegotiateResponse(
            outgoing_message=response,
            attestation=fake_attestation(),
            terminated=(response.type in ["accept", "reject"])
        )
```

## Interface B: Arena → Settlement (TypeScript SDK)

```typescript
export interface SettlementSDK {
  // 注册 Agent 链上身份 (mint ERC-8004 NFT)
  registerAgent(params: {
    agentId: string;
    ownerWallet: string;
    attestation: AttestationReport;
    metadata?: Record<string, string>;
  }): Promise<{ nftTokenId: string; txHash: string }>;

  // 锁定资金到 Escrow
  lockFunds(params: {
    negotiationId: string;
    buyerWallet: string;
    sellerWallet: string;
    amount: number;
    currency: "USDC";
    x402Signature: string;
    expiry: number;
  }): Promise<{ escrowId: string; txHash: string; status: "locked" }>;

  // 结算交易 (释放 Escrow)
  settleTrade(params: {
    escrowId: string;
    deliveryProof?: string;
  }): Promise<{ txHash: string; amountToSeller: number; platformFee: number; status: "settled" }>;

  // 退款
  refund(params: {
    escrowId: string;
    reason: string;
  }): Promise<{ txHash: string; status: "refunded" }>;

  // 查询 Escrow 状态
  getEscrowStatus(escrowId: string): Promise<{
    status: "locked" | "settled" | "refunded" | "expired";
    amount: number; buyer: string; seller: string;
    createdAt: number; expiresAt: number;
  }>;

  // 查询 Agent 链上信息
  getAgentInfo(agentId: string): Promise<{
    nftTokenId: string; owner: string; registeredAt: number;
    attestationHash: string; totalSettlements: number;
  }>;

  // 验证 TEE attestation (链上)
  verifyAttestation(report: AttestationReport): Promise<{ valid: boolean; txHash: string }>;
}

export interface AttestationReport {
  enclaveId: string;
  measurement: string;  // hex
  data: string;         // hex
  signature: string;    // hex
  platformInfo: string;
  timestamp: number;
}
```

### Settlement Mock (Teammate 2 开发用)

```typescript
export class MockSettlement implements SettlementSDK {
  private escrows = new Map();
  async lockFunds(params) {
    const id = `escrow_${Date.now()}`;
    this.escrows.set(id, { ...params, status: "locked" });
    return { escrowId: id, txHash: `mock_tx_${id}`, status: "locked" };
  }
  async settleTrade(params) {
    const escrow = this.escrows.get(params.escrowId);
    return { txHash: `mock_settle_${params.escrowId}`, amountToSeller: escrow.amount * 0.995, platformFee: escrow.amount * 0.005, status: "settled" };
  }
}
```

## Interface C: TEE → Settlement (Minimal)

TEE 对 Settlement 的依赖很小。市场信息通过 Arena 的 `MarketContext` 传入，不直接调链。

```typescript
interface ChainQuery {
  getBalance(wallet: string, token: "USDC"): Promise<number>;  // 可选
}
```

## Interface D: Frontend → Arena (REST + WebSocket)

```yaml
# Agent Management
POST   /api/v1/agents
GET    /api/v1/agents/:id
DELETE /api/v1/agents/:id

# Order Book
GET    /api/v1/orders
POST   /api/v1/orders/ask
POST   /api/v1/orders/bid
DELETE /api/v1/orders/:id

# Negotiation
POST   /api/v1/negotiations
GET    /api/v1/negotiations/:id
POST   /api/v1/negotiations/:id/intervene

# Leaderboard & Settlement
GET    /api/v1/leaderboard
GET    /api/v1/agents/:id/stats
GET    /api/v1/settlements/:id
```

### WebSocket (实时协商)

```json
// WS /ws/negotiations/:id
// Server → Client:
{ "event": "negotiation_message", "data": { "round": 2, "from": "seller_abc", "type": "counter", "offered_price": 1.2, "public_reason": "...", "timestamp": 1690123456 } }
{ "event": "negotiation_complete", "data": { "result": "agreed", "final_price": 0.85, "rounds": 3, "settlement_tx": "0xabc..." } }
```

## Error Handling (统一格式)

```typescript
interface ServiceError {
  code: string;    // AGENT_NOT_FOUND | ENCLAVE_UNAVAILABLE | INSUFFICIENT_FUNDS |
                   // NEGOTIATION_TIMEOUT | ATTESTATION_INVALID | ESCROW_NOT_FOUND | SETTLEMENT_FAILED
  message: string;
  details?: any;
}
```

---

# Part 6: Work Log

## Format

```
### YYYY-MM-DD | [Module] Title
- **Who**: Felix / Teammate 1 / Teammate 2
- **Done**: 完成了什么
- **Decision**: 做了什么决策 (及原因)
- **Blocker**: 遇到什么阻塞
- **Next**: 下一步计划
```

## Log Entries

### 2026-07-23 | [All] Project Kickoff & Doc Setup

- **Who**: Felix
- **Done**: 
  - 完成产品蓝图 mindstorm (Arena + TEE + x402 融合方案)
  - 创建完整项目 spec (本文档)
  - 定义三人分工和模块间接口
- **Decision**: 
  - 项目定位为 "Confidential Agent Arena" (TEE + x402 + 撮合)
  - Demo 场景选 AI Service Marketplace
  - Arena 模式：模型能力差 = Alpha，Owner 自备 API key
  - 技术栈：Rust (CosmWasm) + Python/Go (Arena) + Rust (TEE) + Next.js (前端)
- **Blocker**: 
  - 还未确认队友 1/2 的具体技术偏好
  - AdventureX 报名状态未确认
  - Injective testnet 环境未搭建
- **Next**:
  - [ ] 分享文档给队友，对齐理解
  - [ ] 确认队友对接口定义的反馈
  - [ ] 各自搭建开发环境
  - [ ] Felix: 跑通 Injective testnet + deploy hello-world contract

---

# Appendix: 72h Hackathon Timeline

| Phase | 时间 | Felix (Settlement) | Teammate 1 (TEE) | Teammate 2 (Arena) |
|-------|------|-------------------|-------------------|---------------------|
| **Day 1** | 0-24h | Injective testnet + deploy contracts + 验证 lock/settle | TEE 环境搭建 + enclave hello world + gRPC server | DB schema + Order Book + Mock TEE/Settlement |
| **Day 2 AM** | 24-36h | Arena 调用真实 Settlement SDK | Arena 调用真实 TEE Service | 两两集成替换 mock |
| **Day 2 PM** | 36-48h | 端到端联调 + bug fix | 端到端联调 + bug fix | 端到端联调 + bug fix |
| **Day 3** | 48-72h | Demo 准备 + 前端协助 | Attestation 展示优化 | 前端快速搭建 + Leaderboard |

---

# Appendix: Naming

- **Project**: Agent Arena (working title)
- **Alternatives**: DealMesh / NegotiaX / AgentPit / BidBrain
- **不使用**: 任何 Binance branding

---

*End of document. 任何问题请在 Work Log 中记录或群内讨论。*
