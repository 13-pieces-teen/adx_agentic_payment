# Arena 402：王城典当行游戏机制

> 状态：王城典当行新业务内核的当前游戏契约；后端已完成 1–10 回合可配置自动编排、
> Hosted/rule Runtime 接线、FCFS、多组协商、Round close、终场估值与排名。
> Local Connector 游戏 Adapter、通用 PaymentMandate 和真实生产验收尚未完成。
> 核心产品机制稳定，行动时间窗与其他数值参数仍需真实压测。
>
> 本文维护游戏规则、跨模块状态和 Agent I/O 契约。产品边界见
> [`product.md`](product.md)，实施状态见 [`roadmap.md`](roadmap.md)，结算接线见
> [`arena-settlement-integration.md`](arena-settlement-integration.md)。

## 一句话规则

> 你的 AI 是个倒爷。每回合决定买、卖或观望；进入市场后按先到先得配对，
> 最多砍价 2–3 轮；N 回合后按最终结算价清算，净资产最高者获胜。

游戏发生在公元 402 年、即将崩塌的奥雷利亚帝国。王城典当行是乱世中唯一仍在
为粮草、精铁、战马与宝石标价的市场。玩家既是典当商人，也是王国棋盘上的
Pawn；目标是让自己的 Agent 在事件与恐慌中低买高卖，最终“兵卒封王”。

每名玩家以等值 **20 金**开局，但可在比赛前自由配置现金与四种货物。公平性来自
相同初始净资产，而不是相同持仓。初始组合锁定后，最终差异来自资产配置、模型、
Prompt、决策速度和谈判质量。

`Arena 402` 只用于品牌、Logo 和域名；游戏内叙事使用“王城典当行”。

默认模式下，每笔被接受的交易都必须产生一笔点对点的 Injective testnet mock
USDC（mUSDC）链上结算。平台负责组织回合、配对和记录，不托管用户自带钱包
或真实资金。

游客模式是明确例外：平台 signer service 可管理隔离、限额、可过期和可撤销的
testnet-only 演示密钥。它不承载真实资金，也不能被宣传为非托管主网钱包。
自带 Agent 的交易仍应由玩家控制的钱包直接授权。

## 角色与边界

| 角色 | 职责 | 不负责 |
|------|------|--------|
| 玩家 Agent | 根据公开行情和事件决定买、卖或观望；参与有限轮协商 | 修改规则、事件结果或最终结算价 |
| Arena | 组织游戏、广播事件、记录 Result Sink 数据库接收时间、FCFS 配对、驱动协商、生成排名 | 代替 Agent 定价或托管资金 |
| Agent Runtime | 执行版本化 `arena.decide` 和 `arena.negotiate` AgentTask；可为 Hosted、Local Connector 或后续 Native A2A Runtime | 直接写入 Arena 业务状态或链上最终性 |
| Settlement | 校验买方授权、由 Facilitator 提交链上交易、返回交易结果 | 重新定价、决定货物归属或伪造链上确认 |

以下控制、执行、游戏、结算和链上权威来源必须分开：

- Connector/Gateway 只证明设备、Runtime、Binding、Command 和 Connector Session
  状态；
- Hosted Worker 只证明 Provider Attempt 和候选 AgentTaskResult 状态；
- Arena 数据库记录 AgentTask 快照、Result 接收/应用、回合、配对、协商、持仓和排名；
- Settlement 记录 PaymentMandate 校验、提交和恢复；
- 链上交易及其确认结果决定支付最终性。

## 交易对象

MVP 固定使用四种货物：

| good | 游戏名称 | 初始价 | 金融角色 |
|---|---|---:|---|
| `grain` | 粮草 | 2 金 | 民生必需品，危机时抗跌 |
| `iron` | 精铁 | 5 金 | 战争与工业周期品 |
| `warhorse` | 战马 | 8 金 | 高单价、低流通的稀缺硬资产 |
| `gems` | 宝石 | 3 金 | 预期驱动的投机与泡沫资产 |

- 每种货物有公开参考价，但最终结算价开局时不公开；
- 货物总量受控，玩家不能凭空增发；
- 事件逐步改变玩家对终局价值的判断；
- 现金零收益，鼓励玩家承担经过判断的市场风险；
- 单笔交易固定为 1 单位；
- 金额使用六位 atomic 定点整数，不使用二进制浮点数。

开局组合必须满足：

```text
cash + grain*2 + iron*5 + warhorse*8 + gems*3 = 20 gold
```

结算货币为 Injective EVM testnet 上的 mock USDC。它是测试资产，不应描述为
Circle USDC 或生产资金。

## 一局游戏

一局先完成初始资产配置，再进入 `N` 个同步回合。默认演示基线为 5 回合，开发
接口支持 1–10 回合；`fixed_demo` 固定使用五张顺序事件，`seeded_shuffle` 从
`pawnhouse-standard-v1` 十张事件牌组按 Game seed 确定性洗牌并冻结完整赛程。
相同 deck 版本、seed 和回合数必须产生相同赛程。每回合最多让每个 Agent 完成
一笔交易。

Game 在创建时同时冻结 `roundCount`、`eventDeckId`、`eventMode`、
`maxParticipants` 和配置版本。当前 `maxParticipants` 默认 16、允许 2–64；
Arena API 和 PostgreSQL trigger 都必须拒绝超额加入，避免并发请求绕过上限。

```text
REGISTRATION
  -> PORTFOLIO_SETUP
  -> PORTFOLIO_LOCKED
  -> EVENT_REVEAL
  -> DECIDE
  -> PAIR
  -> NEGOTIATE
  -> SETTLE
  -> ROUND_CLOSE
  -> next round
  -> FINAL_CLEARING
  -> RANKING
```

### 1. Portfolio setup

每名玩家在开局价格下自由配置等值 20 金的现金和持仓。Arena 校验组合、锁定
Portfolio，并冻结进 Game Agent 快照。比赛开始后不能重新配置。

### 2. Event reveal

Arena 广播：

- 当前回合和剩余时间；
- 各货物公开参考价；
- 本回合已公开事件；
- Agent 自己的现金、持仓和 `failedNegotiations`；
- 本轮允许使用的规则参数。

### 3. Decide

Arena 为每个 active Game Agent 创建一条不可变 `arena.decide` AgentTask。Task
Factory 在同一数据库事务中冻结 participant view、Game Agent 配置、输入 hash 和
绝对 deadline。Runtime 只能从以下动作中三选一：

- `buy`：选择一种货物，进入买方池；
- `sell`：选择一种已有货物，进入卖方池；
- `pass`：本回合不交易。

Runtime 提交候选 Result 后，Arena Result Sink 在持久化前处理公开输出并使用数据库
时钟记录 `result_received_at`。Result Consumer 完成 schema、阶段、资产和货物校验
后，才使用该时间生成权威 `enteredAt`。Runtime 自报完成时间、Provider 时间或
Connector Event 都不能决定 FCFS。晚到、超时或无效响应由独立 Deadline Finalizer
收敛为唯一 `pass`，不能阻塞整轮。

### 4. Pair

每个货物分别建立买方池与卖方池，均按 `enteredAt` 升序排列：

```text
buyer[0] <-> seller[0]
buyer[1] <-> seller[1]
...
```

这就是 FCFS。未配对 Agent 本回合结束，但不增加 `failedNegotiations`。只有
真正进入协商后失败的双方才增加该计数。

### 5. Negotiate

- 买方先报价；
- `MAX_TURN` 默认为 3，可压测后调整为 2；
- 每个轮到行动的角色收到一条 `arena.negotiate` AgentTask；
- 每条结果只能使用 `action="propose" | "accept" | "reject"`；
- `propose` 包含定点价格和不超过 100 字、经 PublicOutputPolicy 处理的公开话术；
- `accept` 只能接受对方最近一次有效报价，不能自行附带新价格；
- `reject` 明确结束协商；
- 达到轮次上限、Runtime 失败或 deadline 超时由 Arena 记录 negotiation timeout，
  而不是伪造一条 Agent 主动 `reject`。

每个 Agent 每回合有一个 Decide 逻辑 AgentTask，并按轮到其行动的次数产生有限个
Negotiate AgentTask。每条 AgentTask 最多两个 Provider/Runtime Attempt，即最多
重试一次。逻辑行动数、Attempt 数和模型调用数必须分别记录，不能继续使用“总计
不超过 4 次 LLM 调用”混合三个概念。

`action_timeout_ms` 是 Game 配置，并在开局时冻结。同一 Game 的 Hosted、Local、
rule 与后续 Native A2A Runtime 使用相同时间窗；具体默认值由真实
Provider/Model/thinking 组合和 2/4/8/16 Agent 负载的 P95/P99 加缓冲校准，不在
Adapter 中写死。只有错误可重试且剩余时间充足时才执行一次重试，不自动切换
Provider、Model 或 Runtime。

`failedNegotiations` 是对手可见的模糊信号，不直接扣分、不扣现金，也不改变
FCFS 顺序。它可能代表强硬谈判，也可能代表低成交能力。

支付授权、提交或链上确认失败属于 settlement failure，不增加
`failedNegotiations`；它必须单独记录，且本回合不得把未付款交易计为成交。

### 6. Settle

任一方接受最近报价后，协商进入 `accepted_pending_settlement`，但货物尚未
转移。`accept` 是候选 Runtime 动作，只有 Arena 校验并应用后才能进入该状态。
Arena 将冻结：

- `gameId`、`roundId`、`negotiationId`；
- buyer、seller 和两侧 Agent；
- good、quantity、acceptedPrice；
- chain、token、payee、有效期和幂等键。

Settlement 在提交前重新校验该局受限 PaymentMandate，或明确要求当前
EIP-3009 单笔授权。模型 Runtime 永远不能获得钱包私钥或任意签名权。钱包用户、
或隔离的 guest signer service 对冻结意图生成授权，Facilitator 再提交 testnet
交易。只有链上确认成功后，Arena 才在同一数据库事务中更新现金与货物持仓，并
记录 `inventoryCommittedAt`。链上已确认但事务尚未完成时属于
`chain_confirmed_uncommitted` 可恢复状态，不能向玩家显示为已完成成交。

完整无人值守支付还要求单独实现 PaymentMandate 的网络、Token、Game、payee、
单笔/累计额度、有效期、撤销和并发 `reserve / consume / release`。当前
EIP-3009 direct relay 是单笔授权原型，不等于该 Mandate 或完整 HTTP x402。

授权、提交、链上确认或数据库提交任一步失败，都不得转移货物。详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)。

### 7. Round close

Arena 保存本回合的 Task/Result/default、池、配对、公开协商消息、结算结果、
现金与持仓快照，然后进入下一回合。后台 Game Orchestrator 根据 PostgreSQL
权威状态推进；重启后不依赖进程内计数恢复当前回合。所有 Hosted Decide Task
先创建后等待结果，不同 pairing 可并发协商，但同一 pairing 内仍严格按
`turn_sequence` 顺序执行。

只要存在 `accepted_pending_settlement` 或 `settling` pairing，Round 就保持在
`settle`，不得关闭或进入下一回合。最后一轮关闭后，Arena 将最后的
`final_price_atomic` 冻结为独立结算价表，再用最后一轮 portfolio snapshot
生成唯一排名并将 Game 标记为 `completed`。

不得要求或保存模型的私有 chain-of-thought。可审计证据只包括结构化输入摘要、
合法动作、经过过滤的公开谈判消息、时间戳、Attempt/Token 数值、安全错误类别和
支付凭证。Provider 原始响应、reasoning text 和被策略/Secret 过滤器替换的原文
不得进入数据库、日志、Trace 或 API。

## 事件与价值锚

最终结算价由平台预先定义的事件系统塑造，玩家不能操纵。

- 每回合只揭晓一个主事件；
- `marketReferencePrice` 表示本回合公开市场参考价；
- `finalValuationPrice` 表示终场估值，两者必须分开；
- 效果使用受限、版本化的整数 basis-point DSL，事件不得执行任意代码；
- 确定性事件：例如限量收购，制造有上限的套利窗口；
- 概率性事件：公布概率和影响，随后在指定回合揭晓；
- 事件只改变规则允许的公开信息、流通量或终局价格参数；
- 随机事件必须在开局前提交 schedule commitment，并在结束后公开 seed。

MVP 事件库为：王宫征召、新矿开采、粮仓失火、贵族狂热、加冕取消、蛮族围城和
议和传闻。先知预言、组合套利、王宫远期契约和密探情报属于后续机制。

终场生成 `settleTable`，每种货物对应唯一最终价格。

## 排名

主榜只按净资产：

```text
netWorth = cash + sum(holding[good] * settleTable[good])
```

交易次数、成交量、`failedNegotiations` 不进入主榜公式。可选副榜包括最大单笔
收益、成交量和最低谈崩次数，但不能改变冠军。

## 参与方式

| 层级 | 用户提供 | 平台提供 | 在线语义 |
|------|----------|----------|----------|
| Hosted Agent | 模板，或 allowlisted Provider/Model、受限策略说明和一次性 API Key | 云端受约束 Runtime、Secret Manager、任务执行与私有指标 | 浏览器和用户电脑离线后继续 |
| Local Agent | 本地 Runtime 和明确授权 | Connector/Gateway 连接、任务投递和状态展示 | 依赖 Connector 在线 |
| Native A2A | 后续经过验证的远端 Endpoint | 标准协议 Adapter 与 Arena 中转审计 | Post-MVP |

游客演示属于受限 Hosted Agent。模型 API Key 只允许经 write-only ingress 写入批准
的外部 Secret Manager；业务数据库、日志、Trace、Audit、AgentTask 和前端不得
出现原值。钱包私钥、本地 Runtime 凭据和环境变量值不得上传。Agent Card 保持静态
身份信息；现金、持仓、回合和结算状态属于动态游戏记录。

一名 User 在同一 Game 中最多有一个 Game Agent；同一个 Agent 可以参加后续 Game。
入局时自动冻结当前 Runtime/config 快照，MVP 不允许比赛中途切换 Runtime。Agent
之间不直接通信，所有 A2A 均由 Arena Gateway 中转、排序、校验和审计。

## 最小持久化模型

| 表 | 最小职责 |
|----|----------|
| `games` | 游戏配置、状态、总回合、当前回合 |
| `arena_agents` | 跨局稳定 Agent identity 与 owner |
| `arena_runtime_bindings` | 当前 Hosted/Connector/Native A2A route；Connector 只引用 binding id + epoch |
| `game_agents` | 单局参赛身份、冻结 Runtime/config、现金、谈崩次数；唯一 `(game_id, user_id)` |
| `arena_agent_tasks` | 不可变 Decide/Negotiate 输入、deadline、幂等键与终态 |
| `arena_agent_task_results` | 唯一 sanitized Runtime candidate、数据库接收时间与 Arena apply 状态 |
| `arena_agent_task_attempts` | Provider/Runtime Attempt、thinking、usage、耗时和安全错误 |
| `holdings` | 每局每 Agent 每种货物的数量 |
| `rounds` | 回合阶段、开始/结束时间和事件快照 |
| `pools` | 方向、货物、Agent、合法结果的数据库 `result_received_at` |
| `pairings` | 买卖双方、货物、配对顺序 |
| `negotiations` | 状态、成交价、已用轮次 |
| `neg_messages` | 公开消息、发送方、action、价格、时间戳 |
| `settlements` | 授权/提交/确认状态、金额、链、token、交易哈希、错误 |
| `payment_mandates` | Game/网络/Token/额度/有效期/payee 范围与可撤销状态 |
| `events` | 类型、公开描述、参数、揭晓结果和可复核证据 |
| `settle_table` | 每种货物的终局结算价 |
| `rankings` | 净资产、名次和可选副榜指标 |

生产实现必须通过迁移建立这些表。本文中的字段名是领域基线，不代表当前仓库
已经存在相应数据库迁移。

## Agent I/O

完整字段以
[`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md) 的版本化契约为准。
Hosted、Connector、rule 与后续 Native A2A 都接收同一业务 envelope：

```json
{
  "taskId": "task-01",
  "kind": "arena.decide",
  "schemaVersion": "arena.agent-task.v1",
  "gameId": "game-1",
  "roundId": "round-3",
  "gameAgentId": "game-agent-1",
  "negotiationId": null,
  "deadlineAt": "2026-07-24T12:00:30Z",
  "idempotencyKey": "game-1:round-3:game-agent-1:decide",
  "inputHash": "sha256:...",
  "input": {}
}
```

`input` 是 Task 创建事务冻结的最小 participant view。Worker 不得在排队或重试时
重新读取可变的实时现金、持仓、行情或协商历史。

### Decide

输入包含当前公开行情/事件、自己的现金/持仓/谈崩次数、允许货物、精度规则和
绝对 deadline，不包含对手私有资产、策略、Provider、Token 或 Runtime 日志。

合法候选动作是严格 union：

```json
{"action": "sell", "good": "grain"}
```

或 `{"action":"buy","good":"grain"}`、`{"action":"pass"}`。`pass` 不得带额外交易
字段，所有 schema 均拒绝 extra fields。

### Negotiate

输入包含角色、货物、固定数量、自己的预算/库存边界、公开协商历史、对手公开身份、
对手最近有效报价、Arena 生成的 `turn_sequence`、剩余轮次和绝对 deadline。
公开历史和结果统一使用 `action`，不再使用旧 `type`：

```json
{
  "role": "seller",
  "good": "grain",
  "quantity": 1,
  "history": [
    {
      "turnSequence": 1,
      "from": "buyer",
      "action": "propose",
      "price": "7.000000",
      "message": "先试探市场"
    }
  ]
}
```

合法候选动作：

```json
{"action": "propose", "price": "9.500000", "message": "现货，今天交割"}
```

或 `{"action":"accept"}`、`{"action":"reject","message":"价格不合适"}`。
`accept` 不能附带价格；`propose` 的价格必须符合当前边界与精度；公开消息不超过
100 字并在任何持久化前通过 PublicOutputPolicy。

Runtime Result 使用 `arena.agent-result.v1`，且 dispatch ACK 与 Result 分离：

```json
{
  "resultId": "result-01",
  "taskId": "task-01",
  "schemaVersion": "arena.agent-result.v1",
  "status": "succeeded",
  "action": {
    "action": "propose",
    "price": "9.500000",
    "message": "现货，今天交割"
  }
}
```

`succeeded` 只表示存在候选动作。Result Sink、Arena 业务校验、协议接受、链上确认
与库存提交是后续独立状态。金额必须使用定点十进制字符串或最小单位整数，不能用
浮点数作为结算权威值。

## 可降级项与红线

按优先级可降级：

1. `MAX_TURN` 从 3 降为 1；
2. 实时入池改为固定时间窗后的批量 FCFS；
3. 逐笔链上提交改为一笔包含多笔点对点 transfer 的批量交易；每笔 accepted
   trade 仍须独立映射到该批量交易中的具体 transfer 事件；
4. LLM Agent 不足时加入明确标注的规则 Agent；
5. 演示场景可只激活一种货物，但正式 MVP schema 始终保留四种货物。

不可降级红线：被接受的交易不能只更新数据库。默认 MVP 是一笔 accepted
trade 对应一笔点对点转账；如果启用批量 fallback，每笔交易仍须可独立映射到
真实链上转账证据。纯聚合净额且无法还原逐笔交易的方案不满足当前 MVP。

## 待压测参数

| 参数 | 候选值 |
|------|--------|
| 总回合数 `N` | 5–10 |
| `MAX_TURN` | 2 或 3 |
| 单回合时长 | 60–120 秒 |
| `action_timeout_ms` | Provider/Model/thinking 与 2/4/8/16 Agent 负载的真实 P95/P99 + buffer |
| 货物种类 | 2–3 |
| 单局目标时长 | 10–15 分钟 |

参数调整不得改变本文的核心边界：公平开局、FCFS、有限轮协商、外生价值锚、
净资产排名和成交后真实链上结算。
