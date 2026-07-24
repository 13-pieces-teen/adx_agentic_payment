# Arena 402 游戏机制

> 状态：v1 规则基线。核心机制已锁定，数值参数仍需压测。
>
> 本文维护游戏规则、跨模块状态和 Agent I/O 契约。产品边界见
> [`product.md`](product.md)，实施状态见 [`roadmap.md`](roadmap.md)，结算接线见
> [`arena-settlement-integration.md`](arena-settlement-integration.md)。

## 一句话规则

> 你的 AI 是个倒爷。每回合决定买、卖或观望；进入市场后按先到先得配对，
> 最多砍价 2–3 轮；N 回合后按最终结算价清算，净资产最高者获胜。

所有 Agent 以相同现金和相同初始持仓开局。最终差异应来自模型、Prompt、
决策速度和谈判质量，而不是初始资源优势。

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
| Arena | 组织游戏、广播事件、记录决策完成时间、FCFS 配对、驱动协商、生成排名 | 代替 Agent 定价或托管资金 |
| Agent Runtime | 执行 `decide` 和 `negotiate` 调用；可为托管 Runtime、TEE 或本地 Connector Runtime | 直接写入 Arena 业务状态或链上最终性 |
| Settlement | 校验买方授权、由 Facilitator 提交链上交易、返回交易结果 | 重新定价、决定货物归属或伪造链上确认 |

控制状态、游戏状态和支付状态是三个不同的权威来源：

- Connector/Gateway 只证明设备、Runtime 和任务状态；
- Arena 数据库记录回合、配对、协商、持仓和排名；
- 链上交易及其确认结果决定支付最终性。

## 交易对象

MVP 使用 2–3 种受控总量的符号化货物，例如 `ruby`、`gold`、`spice`。

- 每种货物有公开参考价，但最终结算价开局时不公开；
- 货物总量受控，玩家不能凭空增发；
- 事件逐步改变玩家对终局价值的判断；
- 现金零收益，鼓励玩家承担经过判断的市场风险；
- 单笔交易单位和最小价格精度由游戏配置冻结。

结算货币为 Injective EVM testnet 上的 mock USDC。它是测试资产，不应描述为
Circle USDC 或生产资金。

## 一局游戏

一局由 `N` 个同步回合构成。每回合最多让每个 Agent 完成一笔交易。

```text
BROADCAST
  -> DECIDE
  -> PAIR
  -> NEGOTIATE
  -> SETTLE
  -> ROUND_CLOSE
  -> next round
  -> FINAL_CLEARING
  -> RANKING
```

### 1. Broadcast

Arena 广播：

- 当前回合和剩余时间；
- 各货物公开参考价；
- 本回合已公开事件；
- Agent 自己的现金、持仓和 `failedNegotiations`；
- 本轮允许使用的规则参数。

### 2. Decide

每个 Agent 进行一次 LLM 调用并三选一：

- `buy`：选择一种货物，进入买方池；
- `sell`：选择一种已有货物，进入卖方池；
- `pass`：本回合不交易。

Arena 在收到合法决策时记录 `enteredAt`。晚到、超时或无效响应按游戏配置转为
`pass`，不能阻塞整轮。

### 3. Pair

每个货物分别建立买方池与卖方池，均按 `enteredAt` 升序排列：

```text
buyer[0] <-> seller[0]
buyer[1] <-> seller[1]
...
```

这就是 FCFS。未配对 Agent 本回合结束，但不增加 `failedNegotiations`。只有
真正进入协商后失败的双方才增加该计数。

### 4. Negotiate

- 买方先报价；
- `MAX_TURN` 默认为 3，可压测后调整为 2；
- 每条消息只能是 `propose`、`accept` 或 `reject`；
- `propose` 包含价格和不超过 100 字的公开话术；
- `accept` 接受对方最近一次报价；
- `reject`、达到轮次上限或任一方超时都使协商失败；
- 每个 Agent 每回合最多 1 次决策调用和 2–3 次协商调用，总计不超过 4 次
  LLM 调用。

每次 Agent 调用必须有 20–30 秒候选超时配置。超时默认按 `reject` 处理。

`failedNegotiations` 是对手可见的模糊信号，不直接扣分、不扣现金，也不改变
FCFS 顺序。它可能代表强硬谈判，也可能代表低成交能力。

支付授权、提交或链上确认失败属于 settlement failure，不增加
`failedNegotiations`；它必须单独记录，且本回合不得把未付款交易计为成交。

### 5. Settle

任一方接受最近报价后，协商进入 `accepted_pending_settlement`，但货物尚未
转移。Arena 将冻结：

- `gameId`、`roundId`、`negotiationId`；
- buyer、seller 和两侧 Agent；
- good、quantity、acceptedPrice；
- chain、token、payee、有效期和幂等键。

买方或其明确绑定的 guest signer 生成 EIP-3009 授权，Facilitator 提交
testnet 交易。只有链上确认成功后，Arena 才在同一数据库事务中更新现金与
货物持仓，并记录 `inventoryCommittedAt`。链上已确认但事务尚未完成时属于
`chain_confirmed_uncommitted` 可恢复状态，不能向玩家显示为已完成成交。

授权、提交、链上确认或数据库提交任一步失败，都不得转移货物。详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)。

### 6. Round close

Arena 保存本回合的决策、池、配对、公开协商消息、结算结果、现金与持仓快照，
然后进入下一回合。

不得要求或保存模型的私有 chain-of-thought。可审计证据只包括结构化输入摘要、
合法动作、公开谈判消息、时间戳、错误码和支付凭证。

## 事件与价值锚

最终结算价由平台预先定义的事件系统塑造，玩家不能操纵。

- 确定性事件：例如限量收购，制造有上限的套利窗口；
- 概率性事件：公布概率和影响，随后在指定回合揭晓；
- 事件只改变规则允许的公开信息、流通量或终局价格参数；
- 随机事件必须保存 seed 或等价可复核证据。

终场生成 `settleTable`，每种货物对应唯一最终价格。

## 排名

主榜只按净资产：

```text
netWorth = cash + sum(holding[good] * settleTable[good])
```

交易次数、成交量、`failedNegotiations` 不进入主榜公式。可选副榜包括最大单笔
收益、成交量和最低谈崩次数，但不能改变冠军。

## 参与方式

| 层级 | 用户提供 | 平台提供 | 目标入场时间 |
|------|----------|----------|--------------|
| 游客 | 选择人格卡 | 托管 Agent、测试钱包、默认模型和 Prompt | 约 30 秒 |
| Hacker | API Key、模型、System Prompt | 受限托管 Runtime、游戏协议适配 | 约 5 分钟 |
| 本地 Agent | 本地 Runtime 和明确授权 | Connector/Gateway 连接、任务投递和状态展示 | 完成一次配对后 |

API Key、钱包私钥和本地环境变量不得写入游戏数据库、日志或前端。Agent Card
保持静态身份信息；现金、持仓、回合和结算状态属于动态游戏记录。

## 最小持久化模型

| 表 | 最小职责 |
|----|----------|
| `games` | 游戏配置、状态、总回合、当前回合 |
| `game_agents` | 参赛身份、参与层级、Runtime binding、现金、谈崩次数 |
| `holdings` | 每局每 Agent 每种货物的数量 |
| `rounds` | 回合阶段、开始/结束时间和事件快照 |
| `pools` | 方向、货物、Agent、合法决策完成时间 |
| `pairings` | 买卖双方、货物、配对顺序 |
| `negotiations` | 状态、成交价、已用轮次 |
| `neg_messages` | 公开消息、发送方、类型、价格、时间戳 |
| `settlements` | 授权/提交/确认状态、金额、链、token、交易哈希、错误 |
| `events` | 类型、公开描述、参数、揭晓结果和可复核证据 |
| `settle_table` | 每种货物的终局结算价 |
| `rankings` | 净资产、名次和可选副榜指标 |

生产实现必须通过迁移建立这些表。本文中的字段名是领域基线，不代表当前仓库
已经存在相应数据库迁移。

## Agent I/O

### Decide

```json
{
  "phase": "decide",
  "gameId": "game-1",
  "round": 3,
  "cash": "100.000000",
  "holdings": {"ruby": 5, "gold": 2},
  "market": {"ruby": "9.200000", "gold": "11.000000"},
  "events": [],
  "reputation": {"failedNegotiations": 1},
  "deadline": "2026-07-24T12:00:30Z"
}
```

合法响应：

```json
{"action": "sell", "good": "ruby"}
```

或 `{"action":"buy","good":"ruby"}`、`{"action":"pass"}`。

### Negotiate

```json
{
  "phase": "negotiate",
  "role": "seller",
  "good": "ruby",
  "quantity": 1,
  "counterparty": {"failedNegotiations": 4},
  "events": [],
  "history": [
    {"turn": 1, "from": "buyer", "type": "propose", "price": "7.000000", "message": "先试探市场"}
  ],
  "deadline": "2026-07-24T12:01:00Z"
}
```

合法响应：

```json
{"type": "propose", "price": "9.500000", "message": "现货，今天交割"}
```

或 `{"type":"accept"}`、`{"type":"reject","message":"价格不合适"}`。

金额必须使用定点十进制字符串或最小单位整数，不能用浮点数作为结算权威值。

## 可降级项与红线

按优先级可降级：

1. `MAX_TURN` 从 3 降为 1；
2. 实时入池改为固定时间窗后的批量 FCFS；
3. 逐笔链上提交改为一笔包含多笔点对点 transfer 的批量交易；每笔 accepted
   trade 仍须独立映射到该批量交易中的具体 transfer 事件；
4. LLM Agent 不足时加入明确标注的规则 Agent；
5. 2–3 种货物降为 1 种。

不可降级红线：被接受的交易不能只更新数据库。默认 MVP 是一笔 accepted
trade 对应一笔点对点转账；如果启用批量 fallback，每笔交易仍须可独立映射到
真实链上转账证据。纯聚合净额且无法还原逐笔交易的方案不满足当前 MVP。

## 待压测参数

| 参数 | 候选值 |
|------|--------|
| 总回合数 `N` | 5–10 |
| `MAX_TURN` | 2 或 3 |
| 单回合时长 | 60–120 秒 |
| 单次 Agent 调用超时 | 20–30 秒 |
| 货物种类 | 2–3 |
| 单局目标时长 | 10–15 分钟 |

参数调整不得改变本文的核心边界：公平开局、FCFS、有限轮协商、外生价值锚、
净资产排名和成交后真实链上结算。
