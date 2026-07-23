# ISEK 项目深度解读 & AdventureX 2025 参赛策略

> 本文档基于对 2024 年 AdventureX 区块链赛道获奖项目 [ISEK](https://github.com/isekOS/ISEK) 的完整源码分析，结合 2025-2026 年全球 AI Agent × 区块链的风向转变，为团队 **13-pieces-teen** 在 2025 年 AdventureX 的 Agentic Payment + Credit 项目提供策略建议。

---

## 目录

1. [ISEK 项目深度解读](#1-isek-项目深度解读)
   - 1.1 [项目概览](#11-项目概览)
   - 1.2 [核心架构](#12-核心架构)
   - 1.3 [技术栈拆解](#13-技术栈拆解)
   - 1.4 [为什么 ISEK 能获奖](#14-为什么-isek-能获奖)
   - 1.5 [ISEK 的局限与未完成的部分](#15-isek-的局限与未完成的部分)
2. [2025-2026 AI × 区块链风向转变](#2-2025-2026-ai--区块链风向转变)
   - 2.1 [Agentic AI 成为 Crypto 的 Killer Use Case](#21-agentic-ai-成为-crypto-的-killer-use-case)
   - 2.2 [Agentic Payment 基础设施爆发](#22-agentic-payment-基础设施爆发)
   - 2.3 [AI Agent 的信用层（Credit Layer）](#23-ai-agent-的信用层credit-layer)
   - 2.4 [TEE 与可信执行环境的安全保障](#24-tee-与可信执行环境的安全保障)
   - 2.5 [从"Agent 网络"到"Agent 经济"](#25-从agent-网络到agent-经济)
3. [我们的团队优势分析](#3-我们的团队优势分析)
4. [AdventureX 2025 参赛策略建议](#4-adventurex-2025-参赛策略建议)
   - 4.1 [项目定位：Agentic Payment + Credit + TEE 安全](#41-项目定位)
   - 4.2 [差异化竞争点](#42-差异化竞争点)
   - 4.3 [技术架构建议](#43-技术架构建议)
   - 4.4 [Demo 场景设计](#44-demo-场景设计)
   - 4.5 [Pitch 叙事结构](#45-pitch-叙事结构)
5. [行动计划](#5-行动计划)

---

## 1. ISEK 项目深度解读

### 1.1 项目概览

| 维度 | 详情 |
|---|---|
| **项目名称** | ISEK — Decentralized Agent-to-Agent (A2A) Network |
| **GitHub** | [isekOS/ISEK](https://github.com/isekOS/ISEK) |
| **语言** | Python 88.8% / JavaScript 11.2% |
| **版本** | v0.3.0（2025年10月发布） |
| **许可证** | MIT |
| **社区** | 568 Stars, 41 Forks |
| **定位** | 去中心化 AI Agent 网络框架 |

ISEK 的核心理念可以用一句话概括：**"Agent 不应该作为孤立的执行器存在，它们需要一个去中心化的协作层——能互相发现、组建团队、建立信任，并在没有中心化控制的情况下完成复杂任务。"**

ISEK 试图回答的问题是：当世界上有成千上万个 AI Agent 时，它们如何找到彼此？如何信任彼此？如何协作？它给出的答案是：**Google A2A 协议（通信）+ ERC-8004 智能合约（身份/声誉）+ libp2p（P2P 网络）= 去中心化 Agent 社会。**

### 1.2 核心架构

ISEK 的架构分为四个层次，从上到下依次是：

```
┌─────────────────────────────────────────────────┐
│              Agent 层 (Python)                    │
│  pydantic-ai / OpenAI SDK → PydanticAIAgentWrapper │
│  每个 Agent 有 AgentCard 宣告能力与端点            │
├─────────────────────────────────────────────────┤
│              节点层 (Node ABC)                    │
│  node_v3_a2a.py: 同时作为 A2A Server + A2A Client │
│  uvicorn HTTP 服务 + AgentCard 发现 + 消息路由     │
│  启动时自动调用 ensure_identity() 链上注册          │
├─────────────────────────────────────────────────┤
│            P2P 传输层 (Node.js / libp2p)          │
│  relay.js: circuit-relay v2 HOP 中继 (NAT 穿透)   │
│  p2p_server.js: Express ↔ libp2p 桥接             │
│  传输: WebSocket + WebRTC + Circuit Relay         │
│  加密: Noise  |  多路复用: Yamux                   │
├─────────────────────────────────────────────────┤
│           区块链身份层 (ERC-8004)                  │
│  IdentityRegistry.sol: newAgent / resolveByAddress │
│  wallet_manager.py: 本地密钥管理                   │
│  默认部署: Base Sepolia (EVM 兼容)                 │
└─────────────────────────────────────────────────┘
```

**关键设计决策：**

1. **双模式运行**——Agent 既可以通过标准 HTTP（A2A 协议）通信，也可以通过 libp2p P2P 网络通信（NAT 穿透后），兼顾易用性和去中心化。

2. **身份上链**——每个 Agent 必须在 ERC-8004 IdentityRegistry 合约中注册，获得一个链上 `agentId`，绑定其域名和钱包地址。这为声誉系统奠定了基础。

3. **渐进式开发者体验**——提供了 LV1 到 LV10 的示例学习路径，从单 Agent 工具调用，到本地多 Agent，再到 P2P 跨网络通信，降低上手门槛。

4. **组件可替换**——Chat App、Agent Explorer、Chrome 扩展等生态组件均可被第三方实现替代。

### 1.3 技术栈拆解

| 层面 | 技术选型 | 分析 |
|---|---|---|
| **Agent 框架** | pydantic-ai + a2a-sdk (Google) | 选型合理，Google A2A 是当时最新的 Agent 互操作标准 |
| **LLM 接入** | LiteLLM（统一接入 OpenAI / Anthropic / Gemini） | 灵活的多模型支持 |
| **P2P 网络** | libp2p v2（@libp2p/websockets, @libp2p/webrtc, @chainsafe/libp2p-noise, @chainsafe/libp2p-yamux, @libp2p/circuit-relay-v2） | 成熟的 P2P 技术栈，NAT 穿透方案可靠 |
| **区块链** | EVM（Base Sepolia），web3.py，ERC-8004 | 选择了兼容性最好的 EVM 生态 |
| **HTTP 服务** | uvicorn (ASGI) | 轻量级 |
| **CLI** | click + rich | 良好的开发者体验 |

### 1.4 为什么 ISEK 能获奖

ISEK 在 2024 年 AdventureX 获奖，核心原因可以归结为以下几点：

**1. 抓住了"Agent 网络"这一前沿叙事。** 2024 年正是 Agent 概念爆发的起点——LangChain、AutoGPT、CrewAI 等项目方兴未艾。但大多数项目关注的是"单个 Agent 如何变得更聪明"，ISEK 向前走了一步，关注"Agent 之间如何协作"。这种前瞻性在 Hackathon 评审中非常加分。

**2. 区块链与 AI 的结合点选得精准。** ISEK 没有试图把 AI 模型放到链上（那在当时和现在都不现实），而是把区块链用于它最擅长的事：**身份注册和信任锚定**。ERC-8004 作为一个新鲜出炉的 Ethereum 标准（2024 年提出），为项目增添了技术前沿感。

**3. 完整可运行的 Demo。** ISEK 不是一个 PPT 项目——它有 pip install 就能跑的代码、有 Chat App、有 Agent Explorer、有 Chrome 扩展。在 Hackathon 评审中，一个能实际演示的多组件系统远比一个精美 PPT 更有说服力。

**4. 清晰的叙事和愿景。** "Agent Autonomy = Cooperation + Scale" 这个 tagline 非常有力，把技术主张提炼成了一句可传播的话。整个项目的叙事逻辑自洽：孤立的 Agent 没有未来 → 需要一个去中心化协作层 → Google A2A + ERC-8004 + P2P 就是这个层。

### 1.5 ISEK 的局限与未完成的部分

了解 ISEK 的不足，对我们参赛有直接参考价值——这些就是我们可以超越的方向：

**1. 经济层完全缺失。** ISEK 解决了 Agent 的"通信"和"身份"问题，但没有解决"支付"问题。Agent 之间如何为服务付费？如何定价？如何结算？完全没有涉及。而这正是 **Agentic Payment** 要解决的问题——也是我们项目的核心切入点。

**2. 声誉系统存在但未实现。** ERC-8004 标准设计了三个注册表：Identity、Reputation、Validation。ISEK 只实现了 Identity。Reputation（Agent 的信誉评分）和 Validation（第三方验证）都还是 TODO。这意味着 ISEK 的 Agent 网络有一个关键缺陷：知道 Agent 是谁（身份），但不知道它是否可信（声誉）。

**3. 安全性存疑。** 代码中私钥以明文 JSON 文件存储（`wallet.{NETWORK}.json`），这在生产环境中是完全不可接受的。P2P 通信使用了 Noise 加密，但 Agent 的执行环境没有任何可信保证——无法证明 Agent 真的运行了它声称的代码。

**4. 没有信用/借贷机制。** ISEK 设想了 Agent 之间的协作，但完全没有考虑"如果一个 Agent 暂时没有足够资金怎么办"的问题。信用（Credit）层的缺失限制了 Agent 经济的复杂度。

**5. 治理和经济模型未定义。** 项目没有代币经济学，没有 DAO 治理，没有激励机制。这使得项目更接近一个"开源工具"而非一个"经济系统"。

---

## 2. 2025-2026 AI × 区块链风向转变

2024 年 ISEK 获奖至今，全球 AI 和区块链的交汇处发生了翻天覆地的变化。理解这些变化，是我们在 2025 年 AdventureX 做出差异化项目的关键。

### 2.1 Agentic AI 成为 Crypto 的 "Killer Use Case"

2025 年最具标志性的事件是 **Franklin Templeton（管理 1.8 万亿美元资产）发布研究报告，明确指出 Agentic AI 将成为区块链大规模采用的杀手级应用**。他们的论据直击要害：

> 传统支付轨道（信用卡固定 ~$0.30 手续费）根本无法经济地支持 AI Agent 所需的小额支付（平均 ~$0.001/笔）。区块链的稳定币支付是唯一的规模化答案。

**关键数据点（2025-2026）：**

- Coinbase + Keyrock + Tempo 联合研究记录了 **1.76 亿笔机器对机器交易，总额 $7300 万**，98% 使用 USDC 结算
- 截至 2026 年 Q1，超过 **104,000 个 AI Agent** 在 15+ 个目录中注册
- 平均交易金额 $0.31——验证了 AI Agent 支付的"小额高频"特性
- McKinsey 预测 Agentic Commerce 到 2030 年将达到 **$3-5 万亿**

**这意味着什么：** 2024 年的 ISEK 在解决"Agent 如何通信"，2025-2026 年整个行业已经转向了"Agent 如何交易"。**Agentic Payment 不再是一个可选功能，而是 Agent 经济的核心基础设施。**

### 2.2 Agentic Payment 基础设施爆发

2025 年是 Agentic Payment 基础设施的"军备竞赛"年。三大协议同时出现：

| 协议 | 主导方 | 方案 | 现状 |
|---|---|---|---|
| **x402** | Coinbase | HTTP 402 状态码复活，链上稳定币结算 | 50 万笔/周，Base/Solana/BNB Chain |
| **ACP** | OpenAI + Stripe | Shared Payment Tokens，走传统支付轨道 | Apache 2.0 开源，仅 Stripe 可用 |
| **AP2** | Google | 加密签名的 Mandate 系统，支持所有支付方式 | 60+ 合作伙伴，尚无产品上线 |

此外，**Stripe + Paradigm** 正在构建专为支付优化的 L1 区块链 **Tempo**（融资 $5 亿，估值 $50 亿），旨在达到 10 万+ TPS。**Celer AgentPay** 提供状态通道方案实现毫秒级结算。**Pay3** 推出企业级 Agentic Payments 平台。

**这意味着什么：** Agentic Payment 的标准之争正在激烈进行。作为 Hackathon 参赛者，**不需要自己造轮子**——直接集成 x402（Coinbase 开源且已有 50 万笔/周的真实交易量）作为支付层，然后在上层做创新，是最务实的策略。而且，**ISKE 已经用过 Google A2A 协议，我们使用 x402 是自然的技术栈演进。**

### 2.3 AI Agent 的信用层（Credit Layer）

2025-2026 年最令人兴奋的新方向之一是 **Agent 信用基础设施** 的兴起：

- **ClawCash**（SKALE 上）：定位为 "AI Agent 的信用卡"，实时承销和资助 Agent 交易，处理 $5000 万+ x402 交易量，服务 1000 万+ Agent 实例
- **Cred Protocol**（SKALE 上）：链上信用评分和女巫检测，已服务 35 万+ 信用评分请求，分析 2 亿+ 钱包地址
- **bond.credit**（Circle 联盟成员）：构建 Agent 经济信用层，提供由 AVS 保险支持的 Agent 信用额度，发行稳定币 agUSD
- **Kea Credit**（Hedera 上）：AI 承销 Agent 在 15 秒内分析 200+ 风险指标，已发放 $550 万贷款

**核心洞察：** 链上信用正在从"人类的 DeFi 信用评分"转向"AI Agent 的信用评分"。Agent 作为一个经济主体，也有信用需求——它需要在资金不足时借款完成任务、需要建立交易信誉、需要被对手方信任。

**这对我们意味着什么：** ISEK 只解决了 Agent 的"身份"（Identity），但没有解决"信用"（Credit）。而 **Agent Credit 正是 2025-2026 年最具创新空间的蓝海领域**。结合我们产品描述中提到的"bid-ask matching + 博弈论"定价机制，信用层可以天然嵌入：高信用 Agent 获得更好的交易条件、更低的押金要求、更高的交易限额。

### 2.4 TEE 与可信执行环境的安全保障

2025 年，TEE（可信执行环境）从学术概念走向了 AI Agent 安全的主流生产方案：

- **elizaOS + EigenCloud**：通过 EigenLayer 的再质押安全性提供 Agent 代码的加密可验证性
- **Lumoz**：TEE + ZK 双重验证架构，TEE 提供硬件级机密性，ZK 提供密码学可验证性
- **Phala Network + Mind Network**：TEE + 全同态加密（FHE）混合方案
- **Secret Network**：在 TEE 中运行 20 亿参数的 Solidity-LLM 模型，节点运营商也无法查看输入/输出
- **Oasis ROFL**：在 TEE 中生成 Ed25519/secp256k1 密钥，私钥永不离开安全 enclave
- **Polyhedra**：采用 Google Confidential Space 作为 TEE 基础设施，结合 ZK 用于跨链桥和可验证 AI 市场

**核心洞察：** TEE 解决了 Agentic Payment 中最关键的安全问题——"我如何确保对方的 Agent 真的在执行它声称的逻辑？私钥安全吗？交易意图被篡改了吗？" 学术界的 TIVA 框架（Trustless Intent Verification for Agents）提出了去中心化身份 + 链上意图验证 + ZK 证明 + TEE 证明的四层方案。

**这对我们意味着什么：** 这是我们团队的差异化武器。团队有可信交互背景的队友，完全可以将 **TEE + Agentic Payment** 结合——让支付 Agent 的私钥和交易逻辑运行在 TEE 中，通过链上远程证明（Remote Attestation）向对手方证明"我的 Agent 是可信的，支付会按约定执行"。这是目前市场上还没有被充分解决的问题。

### 2.5 从"Agent 网络"到"Agent 经济"

总结 2024→2026 的风向转变：

| 维度 | 2024 (ISEK 获奖时) | 2025-2026 (现在) |
|---|---|---|
| **叙事** | Agent 网络与协作 | Agent 经济与支付 |
| **核心问题** | Agent 如何找到彼此？ | Agent 如何交易和信任？ |
| **技术焦点** | A2A 通信协议 | 支付协议（x402/ACP/AP2） |
| **信任模型** | 链上身份注册 | TEE + ZK + 链上信用评分 |
| **经济模型** | 无 | 小额支付 + 信用额度 + 稳定币结算 |
| **生态状态** | 实验性项目 | 数十亿美金的基础设施投资 |
| **安全方案** | 明文私钥存储 | TEE enclave + MPC + ZK 证明 |

**关键结论：2024 年的 ISEK 做的是 Agent 的"社交网络"，2025 年需要做的是 Agent 的"银行 + 支付宝"。** 我们从 ISEK 学到了 Agent 网络的架构思路，但要做的是它缺失的那一层——让 Agent 真正能交易的那一层。

---

## 3. 我们的团队优势分析

基于我们团队的人员构成和已有工作，梳理核心优势：

| 团队能力 | 具体体现 | 在项目中的应用 |
|---|---|---|
| **Agentic Payment 理解** | 产品描述.docx 已规划 bid-ask matching + game theory + X402 | 交易匹配引擎 + 博弈论定价机制 |
| **可信交互/TEE 安全** | 队友有可信交互背景 | Agent 私钥保护 + Remote Attestation + 链上可验证执行 |
| **Credit 元素** | 明确要加入 credit 维度 | Agent 信用评分 + 信用额度 + 差异化交易条件 |
| **全栈工程能力** | 有 Srzzz 和 Adkid-Zephyr 两名开发者 | 可以同时推进合约 + 前端 + Agent 逻辑 |
| **Injective 生态理解** | 已选定 Injective + X402 | 生态契合度高 |

**团队优势的核心叙事：**

> "我们不是在做一个普通的 Agentic Payment 项目。我们把传统金融的三大支柱——**支付（Payment）、信用（Credit）、安全（Security）**——完整地搬到了 AI Agent 经济中。我们有 TEE 可信交互能力，能解决 Agent 支付中最关键的安全信任问题；我们有信用建模能力，能让 Agent 像人一样获得信用评估；我们有博弈论定价机制，让 Agent 交易市场真正有效率。"

---

## 4. AdventureX 2025 参赛策略建议

### 4.1 项目定位：Agentic Payment + Credit + TEE 安全

ISEK = **Agent 的社交网络层**（谁是谁，怎么聊）
我们 = **Agent 的金融基础设施层**（怎么付、怎么信、怎么保）

**一句话定位：** 带信用评估和 TEE 安全保障的 AI Agent 去中心化支付协议。

**英文 Pitch：** *An agentic payment protocol with on-chain credit scoring and TEE-based security — the financial layer for the agent economy.*

### 4.2 差异化竞争点

在 AdventureX 评审面前，我们必须回答："凭什么你们能赢？"

| 竞争维度 | 常规 Agentic Payment 项目 | 我们的项目 |
|---|---|---|
| **支付** | 集成 x402，Agent 之间能转账 | ✅ 同，且加入博弈论定价 |
| **信用** | 无 | ✅ 链上 Agent 信用评分 |
| **安全** | 私钥明文存储或简单加密 | ✅ TEE 可信执行环境 |
| **定价** | 固定价格 | ✅ 双向拍卖 + Agent 博弈 |
| **场景** | 抽象 Demo | ✅ 具体 C2C 交易场景 |

**我们的核心差异化是三位一体的：支付 × 信用 × 安全。** 做支付的团队不懂安全，做安全的团队不懂信用，做信用的团队没有支付场景。我们把三者统一在一个协议里。

### 4.3 技术架构建议

```
┌──────────────────────────────────────────────┐
│              用户层 (User Layer)               │
│  卖家: 设定价格区间 + 交易策略 + 信用要求       │
│  买家: 提交需求 + 预算 + 信用门槛              │
├──────────────────────────────────────────────┤
│            匹配引擎 (Matching Engine)          │
│  双向拍卖机制 (Double Auction)                 │
│  Agent 博弈策略（Nash Equilibrium 定价）       │
│  订单簿 + 撮合算法                             │
├──────────────────────────────────────────────┤
│            信用层 (Credit Layer)               │
│  链上 Agent 信用评分模型                       │
│  交易历史 + 履约率 + 对手方评价                │
│  信用额度 = f(评分, 抵押, 交易量)              │
├──────────────────────────────────────────────┤
│          安全层 (TEE Security Layer)           │
│  Agent 私钥在 TEE Enclave 中生成与存储         │
│  Remote Attestation 链上验证                   │
│  交易签名策略在 Enclave 中执行                 │
│  "我的 Agent 可信，密码学可证明"               │
├──────────────────────────────────────────────┤
│          支付层 (Payment Layer)                │
│  X402 协议集成 (Coinbase)                      │
│  Injective 链上结算                            │
│  稳定币 (USDC) 作为结算资产                    │
│  Gas 抽象 / 元交易 (可选)                      │
├──────────────────────────────────────────────┤
│          合约层 (Smart Contracts)               │
│  AgentIdentity: Agent 身份注册                 │
│  CreditScore: 信用评分状态                     │
│  EscrowVault: TEE 验证的托管合约              │
│  MatchOrder: 订单簿撮合                        │
└──────────────────────────────────────────────┘
```

**技术选型建议：**

| 组件 | 建议 | 理由 |
|---|---|---|
| **区块链** | Injective | 团队熟悉，X402 支持 |
| **支付协议** | X402 | Coinbase 开源，已有周 50 万笔交易验证，稳定币原生 |
| **TEE** | Intel TDX 或 Google Confidential Space | 生产级 TEE，Polyhedra 已采用 |
| **Agent 框架** | 可参考 ISEK 的 pydantic-ai，或用 ElizaOS | 降低 Agent 开发成本 |
| **信用模型** | 参考 Cred Protocol 的链上评分思路 | 可快速借鉴 |
| **前端** | Next.js + Web3 钱包 | Hackathon Demo 友好 |

### 4.4 Demo 场景设计

Hackathon 的 Demo 必须具象、可感知、有冲击力。建议设计一个完整交易闭环：

**场景：二手 GPU 算力 Agent 交易市场**

```
卖家 Alice:       买家 Bob:
有闲置 GPU       需要 GPU 做 AI 推理

1. Alice 创建 SellerAgent
   - 设定 GPU 每小时价格范围 [0.5-1.2 USDC]
   - 设定可接受的买家最低信用分: 60
   - 交易策略: Tit-for-Tat (博弈论)
   - SellerAgent 运行在 TEE 中(可远程证明)

2. Bob 创建 BuyerAgent
   - 需求: 4×A100 GPU, 2小时
   - 预算上限: 10 USDC
   - 信用要求: 优先高信用卖家
   - BuyerAgent 也运行在 TEE 中

3. 匹配引擎自动撮合
   ┌──────────────────────────┐
   │  SellerAgent ↕ BuyerAgent │
   │  博弈定价: 0.8 USDC/h    │
   │  信用检查: Alice 评分 85  │
   │  TEE 验证: 双方通过 ✅    │
   └──────────────────────────┘

4. X402 支付执行
   - BuyerAgent 发起 1.6 USDC 支付
   - 资金进入 TEE 验证的 EscrowVault
   - SellerAgent 收到确认，开始提供服务

5. 交易完成
   - 资金释放给 SellerAgent
   - 双方信用评分更新
   - 交易记录上链
```

**Demo 的"WOW Moment"：**
- **可视化博弈过程**：展示两个 Agent 如何在价格上博弈到 Nash 均衡
- **TEE Attestation 验证动画**：展示 TEE 远程证明的密码学过程（简化但直观）
- **信用评分实时更新**：交易完成后信用分即刻变化

### 4.5 Pitch 叙事结构

建议按照以下叙事逻辑组织 Pitch（5 分钟标准 Hackathon Pitch）：

**1. Problem（1 分钟）——"AI Agent 经济缺了什么？"**
> 2026 年，超过 10 万个 AI Agent 在链上运行，每天产生数百万笔交易。但它们面临三个核心问题：(1) 如何安全地支付？(2) 如何评估对手方的信用？(3) 如何确保对方 Agent 不会作恶？去年的 ISEK 解决了 Agent 如何通信，但 Agent 经济的金融基础设施仍然缺失。

**2. Solution（1.5 分钟）——"我们做了 Agent 的支付宝"**
> 我们构建了一个带信用评估和 TEE 安全保障的 Agentic Payment 协议。三个核心模块：第一，双向拍卖匹配引擎，Agent 之间通过博弈定价自动达成交易；第二，链上信用评分系统，每个 Agent 都有基于历史行为的信用分；第三，TEE 安全层，Agent 的私钥和交易逻辑在安全 enclave 中运行，密码学可证明。

**3. Demo（1.5 分钟）——"看一个真实交易"**
> 现场演示 GPU 算力 Agent 交易场景（如上）。

**4. Why Us（0.5 分钟）——"为什么是我们？"**
> 我们有可信交互安全背景，有 Agent 经济理解，有全栈工程能力。我们不是在做教科书项目——我们知道 TEE 怎么部署，信用模型怎么建，博弈机制怎么设计。

**5. Vision（0.5 分钟）——"这个项目的未来"**
> 今天我们在 Injective 上用 X402 做 Agent 的小额支付。明天我们要做的是整个 Agent 经济的金融基础设施——Agent 借贷、Agent 保险、Agent 投资基金。McKinsey 说 Agentic Commerce 是 $3-5 万亿的市场，我们的支付协议是这个市场的底层管道。

---

## 5. 行动计划

### Hackathon 前（建议时间线）

| 阶段 | 时间 | 关键产出 |
|---|---|---|
| **合约层** | Week 1-2 | AgentIdentity + CreditScore + EscrowVault 合约开发 + 测试 |
| **匹配引擎** | Week 2-3 | 双向拍卖算法 + Agent 博弈策略实现 |
| **TEE 集成** | Week 2-4 | TEE Enclave 搭建 + Remote Attestation + 链上验证 |
| **X402 集成** | Week 3-4 | X402 支付流程 + Injective 上部署测试 |
| **前端 Demo** | Week 4 | Next.js 交易界面 + 博弈可视化 + 信用仪表盘 |
| **Pitch 打磨** | Week 4 | Deck + 现场 Demo 演练 |

### 参考资源

- **ISEK 源码**：[github.com/isekOS/ISEK](https://github.com/isekOS/ISEK) — Agent 网络架构参考
- **ERC-8004 标准**：[eips.ethereum.org/EIPS/eip-8004](https://eips.ethereum.org/EIPS/eip-8004) — 了解 ISEK 的身份合约设计
- **X402 协议**：[github.com/coinbase/x402](https://github.com/coinbase/x402) — Coinbase 的 Agentic Payment 标准
- **Cred Protocol**：[credprotocol.com](https://credprotocol.com) — 链上信用评分参考
- **elizaOS + EigenCloud**：TEE + Agent 集成的生产级参考
- **Celer AgentPay**：[github.com/celer-network/AgentPay-docs](https://github.com/celer-network/AgentPay-docs) — 状态通道支付方案
- **Google A2A 协议**：ISEK 使用的 Agent 通信标准
- **AdventureX 2024 回顾**：[learnblockchain.cn/article/8913](https://learnblockchain.cn/article/8913)

---

> **文档作者：** 13-pieces-teen 团队
> **日期：** 2026-07-23
> **ISEK 分析基于：** [github.com/isekOS/ISEK](https://github.com/isekOS/ISEK) v0.3.0
> **本文档与项目源码一起维护于：** [github.com/13-pieces-teen/adx_agentic_payment](https://github.com/13-pieces-teen/adx_agentic_payment)
