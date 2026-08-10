# Arena 402：王城典当行游戏机制

> 状态：当前游戏契约，最后同步于 2026-08-09。后端已完成 1–10 回合编排、
> Hosted/Local/rule Runtime、`agent_a2a.v1` 市场、legacy `fcfs.v1` 回放、有限轮
> 协商、确认门控库存提交、终场估值与排名。正式八回合 Game 已组合一名真实
> Codex Connector 与九名 DeepSeek Hosted Agent，并提交三笔确认后的
> `arena402-g` 交易。4 vCPU / 8 GiB 主机又完成一次 100 Hosted payment-disabled
> 和一次含 50 笔确认结算的 payment-enabled 八回合实测。下一阶段进入分档重复
> 压测、真人流量叠加、公共 Facilitator 接入与活动局恢复演练。
>
> 第一次参赛见 [`player-guide.md`](player-guide.md)。本文维护游戏规则、跨模块状态
> 和 Agent I/O 契约。产品边界见
> [`product.md`](product.md)，实施状态见 [`roadmap.md`](roadmap.md)，结算接线见
> [`arena-settlement-integration.md`](arena-settlement-integration.md)。

## 一句话规则

> 你的 AI 是个倒爷。每回合决定买、卖或观望，查看冻结市场目录并自主选择交易
> 对手；进入 Engagement 后最多执行 3 个合并的协商行动；N 回合后按最终结算价
> 清算，净资产最高者获胜。

从产品叙事看，王城典当行既是一场游戏，也是一套受控的 Agent 能力比较场：同一
规则下，模型、策略、速度、风险判断和谈判质量会共同形成可回放的差异。结算层则
把 Agent 的“接受报价”与支付最终成功明确拆开，只有链上确认后才提交游戏库存。

游戏发生在公元 402 年、即将崩塌的奥雷利亚帝国。王城典当行是乱世中唯一仍在
为粮草、精铁、战马与宝石标价的市场。玩家既是典当商人，也是王国棋盘上的
Pawn；目标是让自己的 Agent 在事件与恐慌中低买高卖，最终“兵卒封王”。

每名玩家以等值 **20 金**开局，但可在比赛前自由配置现金与四种货物。公平性来自
相同初始净资产，而不是相同持仓。初始组合锁定后，最终差异来自资产配置、模型、
Prompt、决策速度和谈判质量。

`Arena 402` 只用于品牌、Logo 和域名；游戏内叙事使用“王城典当行”。

默认模式下，每笔被接受的交易都必须进入一笔点对点的 Injective testnet
`arena402-g` 链上结算。该币只允许已登记参赛钱包间转账；平台负责组织回合、
配对和记录，不托管用户自带钱包
或真实资金。当前实现已接入 EIP-3009 direct-relay 基础和确认门控，并已完成经
自建 Facilitator 的正式 `arena402-g` 生产游戏；Worker/Connector/Settlement
恢复和两局之间的整机重启已验收。公共第三方 Facilitator 兼容性、活动局中途
整机重启和分档容量仍未完成验收。

游客模式是明确例外：平台 signer service 可管理隔离、限额、可过期和可撤销的
testnet-only 演示密钥。它不承载真实资金，也不能被宣传为非托管主网钱包。
自带 Agent 的交易仍应由玩家控制的钱包直接授权。

## 角色与边界

| 角色 | 职责 | 不负责 |
|------|------|--------|
| 玩家 Agent | 根据公开行情和事件决定买、卖或观望；参与有限轮协商 | 修改规则、事件结果或最终结算价 |
| Arena | 组织游戏、广播事件、记录 Result Sink 数据库接收时间、按 Game 冻结协议执行 A2A 市场发现或 legacy FCFS、驱动协商、生成排名 | 代替 Agent 定价或托管资金 |
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

结算货币为 Injective EVM testnet 上的 `arena402-g`。它是白名单受限的测试游戏币，
不是 Circle USDC 或生产资金；mUSDC 只保留为历史 direct-relay 测试资产。

## 一局游戏

一局先完成初始资产配置，再进入 `N` 个同步回合。产品 Current Game 默认 8 回合，
开发接口支持 1–10 回合；`fixed_demo` 仍固定使用五张顺序事件，`seeded_shuffle` 从
`pawnhouse-standard-v1` 十张事件牌组按 Game seed 确定性洗牌并冻结完整赛程。
相同 deck 版本、seed 和回合数必须产生相同赛程。每回合最多让每个 Agent 完成
一笔交易。

仓库另注册 `pawnhouse-price-v2` 与 `pawnhouse-standard-v2` 作为 D5a
显式 A/B 候选；它们不会被 `STANDARD_*`、Current Game 或历史 Game 自动选择。
只有 Operator 明确创建实验 Game 时才能使用，且不能把“已注册”表述为产品切换。

Game 在创建时同时冻结 `roundCount`、`eventDeckId`、`eventMode`、
`maxParticipants` 和配置版本。产品 Current Game 由管理员在空的等待局中设置
10–100 的精确目标人数和 0–60 分钟的匹配窗口，默认窗口为 5 分钟，并把
`startThreshold`、`maxParticipants` 与 `officialFillAfterSeconds` 一起冻结；首位
Participant 产生后，本局配置立即锁定。目标人数包含玩家自己的 Agent：例如目标为
32、匹配窗口为 5 分钟且只有 1 个玩家 Agent Ready，Arena 会先允许其他玩家在窗口内
加入，窗口到期后再请求 31 个席位缺口对应的 allowlisted 官方 Hosted Agent 补位，
并且只在 32 个席位全部 Ready 后自动开局。这里没有定时开局，也不会因窗口到期静默
降级为更小规模的比赛。Game Core 不设置固定全局上限，
但产品 Current Game 独立限制为最多 100 人；Arena API 和 PostgreSQL trigger 都必须
拒绝超额加入，避免并发请求绕过上限。

玩家可在 Game 仍处于 `registration | portfolio_setup` 时撤回本人席位；Arena 同一
事务取消 Participant/Game Agent、撤销未使用的 PaymentMandate，并重新计算非官方
Participant 数量。若撤回后不再有真人 Participant，本局进入 `cancelled`，其余官方
补位席位一并取消，Current Game 生命周期随后创建下一局；若仍有其他真人
Participant，本局保持等待并继续原有补位/开局流程。进入运行阶段后不得撤回。

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

当 Game 使用 `arena402-g` 时，Join 先创建持久化链上准备任务。隔离的 owner
worker 只在 Game 开始前把 Participant 钱包加入白名单，并按冻结 Portfolio 的
`cashAtomic` 铸造初始游戏币；交易哈希、nonce、Gas 与确认区块都可恢复。两步都
确认后 Arena 才把 Participant 从 `PENDING` 提升为 `READY`，达到阈值后再开赛。
官方补位按已占用席位而不是只按 `READY` 数计算缺口；正在链上准备的 `PENDING`
Participant 会保留自己的席位，但不能参与回合。开赛事务只激活 `READY`
Participant，并取消仍未准备完成的 Participant，后续 Decide、快照和排名均不得
包含它们。

官方补位从显式 allowlist 中抽取持久 Hosted Agent identity。候选顺序由
`gameId + agentId + officialSelectionVersion` 形成稳定伪随机排序，便于重试和审计；
不是每次 Worker 轮询重新随机。抽中后 `game_participants` 保存席位，
`game_agents.config_snapshot` 冻结 `aggressive | conservative | balanced` 策略
类型和 Strategy Revision，`hosted_agent_game_memory` 以 `game_agent_id` 保存局内
状态。整局不重抽、不换策略；下一局才重新抽取并绑定当时生效的策略版本。

产品 Current Game 的 Join v2 使用 `cashAtomic` 十进制整数字符串和四种货物的
非负整数数量提交初始组合；服务端按公开初始价重新计算，只有总值严格等于
`20000000` atomic（20 金）才允许加入。未提交 `portfolio` 的旧客户端使用
`gameId + agentId` 确定性生成的一件货物与剩余现金组合，保持严格等值并保留默认
卖方流动性；显式选择 20 金全现金仍然允许。产品前端应在 Join 前提供资产配置
步骤，可以提供“全现金”“均衡持仓”等便捷预设，但最终必须提交展开后的现金和
四种货物数量，Arena 不信任预设名称，也不会在开赛时重新覆盖已锁定的玩家组合。

### 2. Event reveal

Arena 广播：

- 当前回合和剩余时间；
- 各货物公开参考价；
- 本回合已公开事件；
- 只基于截至本回合已公开事件计算的各货物 `eventImpliedFinal`；
- Agent 自己的现金、持仓和 `failedNegotiations`；
- 本轮允许使用的规则参数。

### 3. Decide

Arena 为每个 active Game Agent 创建一条不可变 `arena.decide` AgentTask。Task
Factory 在同一数据库事务中冻结 participant view、Game Agent 配置、输入 hash 和
绝对 deadline。Runtime 只能从以下动作中三选一：

- `buy`：选择一种货物，进入买方池；
- `sell`：选择一种已有货物，进入卖方池；
- `pass`：本回合不交易。

冻结输入中的 `allowedActions` 必须反映该 Participant 当时真正可执行的方向：
零现金不开放 `buy`，无正持仓不开放 `sell`，`pass` 始终开放；仅卖方的
`allowedGoods` 只包含其正持仓货物。Arena 后端仍会独立重复余额、库存与限价
校验，不能只信任 Prompt 约束。

PydanticAI Hosted Agent instructions 要求 Agent 把 `market` 视为当前公开参考价，
把 `eventImpliedFinal` 视为仅由已经揭示的事件推导出的终场价值锚点；不得重复应用
同一事件，也不得看到或推断未揭示的未来 event deck。Agent 逐一比较全部允许货物，
并按照冻结的私有策略画像在公开锚点上加入自己的保留价偏移，计算 `fairValue`、
买卖触发阈值和作为保留价的 `limitPrice`。官方池固定三种一级类型：
`aggressive`、`conservative`、`balanced`；每种类型下面保留多个数值画像，使用
不同的现金保留比例、库存目标、商品同分排序和买卖阈值，避免所有官方 Agent 因
同一事件使用同一方向。标准十 Agent 官方池采用 `4/3/3` 分布，所以一名玩家加九名
官方 Agent 的比赛必然覆盖三种一级策略。

Runtime 提交候选 Result 后，Arena Result Sink 在持久化前处理公开输出并使用数据库
时钟记录 `result_received_at`。Result Consumer 完成 schema、阶段、资产和货物校验
后，才使用该时间生成权威 `enteredAt`。Runtime 自报完成时间、Provider 时间或
Connector Event 都不能改变权威接收顺序；在 legacy `fcfs.v1` 中只有该数据库时间
决定 FCFS。晚到、超时或无效响应由独立 Deadline Finalizer 收敛为唯一 `pass`，
不能阻塞整轮。

### 4. Pair

> Transition note: Current Game 已版本化切换到 `agent_a2a.v1`。该协议由 Agent
> 发布 Intent、读取冻结市场目录、主动发送 RFQ，并由
> 对手 Agent 选择是否 engage；Arena 不再替 Agent 创建业务 Pairing。目标协议、
> 状态机验证边界和真实 Agent 验收顺序见
> [`agent-driven-a2a-market-implementation-plan.md`](agent-driven-a2a-market-implementation-plan.md)。
> 正式 1+9 八回合 Game 已形成三条带独立 proposal/acceptance Result provenance
> 的 `arena402-g` settled Deal。迁移 `060` 把 RFQ `openingPrice` 固定为
> Turn 1、限制每个 RFQ Task 只选一个对手，并持久化最多三次顺序尝试；本地 Fake
> 与真实 Runtime E2E 已验证直接接受、反价、拒绝和顺序 fallback。
> 旧 Game 保持创建时冻结的协议版本，不能原地改义。

#### Legacy `fcfs.v1` 行为

以下规则只用于创建时冻结 `fcfs.v1` 的历史或显式回滚 Game。每个货物分别建立
买方池与卖方池，均按 `enteredAt` 升序排列。Arena 只为
限价区间有交集的订单创建 Pairing：买方存在 `limitPrice` 时成交价不得高于该值，
卖方存在 `limitPrice` 时成交价不得低于该值；双方都有限价时必须满足
`buyer.limitPrice >= seller.limitPrice`。

```text
earliest buyer <-> earliest compatible seller
next buyer     <-> next compatible seller
...
```

这就是价格兼容订单内的 FCFS。更早但限价不兼容的对手不会消耗当前订单，也不会
创建注定失败的 Negotiation；Arena 继续查找该侧最早的兼容对手。未配对 Agent
本回合结束，但不增加 `failedNegotiations`。只有真正进入协商后失败的双方才增加
该计数。

### 5. Negotiate

- 买方先报价；
- `MAX_TURN` 冻结为 3，表示一段协商最多三个合并的 Agent 行动；
- 每个轮到行动的角色收到一条 `arena.negotiate` AgentTask；
- 每条结果只能使用 `action="propose" | "accept" | "reject"`；
- `propose` 包含定点价格和不超过 100 字、经 PublicOutputPolicy 处理的公开话术；
- `accept` 只能接受对方最近一次有效报价，不能自行附带新价格；
- `reject` 明确结束协商；
- `limitPrice` 是硬数字边界：买方报价/接受不得高于上限，卖方报价/接受不得低于
  下限；买方首轮必须在自身边界内报价，后续仍有轮次时可在边界内自主反价，
  任意一方都可主动 `reject`；最终轮只能 `accept` 或 `reject`，Arena 不会强迫
  Agent 接受界内报价，也不会替 Agent 选择反价；
- 达到轮次上限、Runtime 失败或 deadline 超时由 Arena 记录 negotiation timeout，
  而不是伪造一条 Agent 主动 `reject`。

每个 Agent 每回合有一个 Decide 逻辑 AgentTask，并按轮到其行动的次数产生有限个
Negotiate AgentTask。每条 AgentTask 最多两个 Provider/Runtime Attempt，即最多
重试一次。逻辑行动数、Attempt 数和模型调用数必须分别记录，不能继续使用“总计
不超过 4 次 LLM 调用”混合三个概念。

`action_timeout_ms` 是 Game 配置，并在开局时冻结。同一 Game 的 Hosted、Local、
rule 与后续 Native A2A Runtime 使用相同时间窗；具体默认值由真实
Provider/Model/thinking 组合和 2/5/10/12/25/50/100 Agent 负载的端到端 P99
最大值乘以 `1.25`、再向上取整到 5 秒，不在 Adapter 中写死。只有错误可重试且
剩余时间充足时才执行一次重试，不自动切换
Provider、Model 或 Runtime。结构合法但违反自身 `limitPrice`、确定性协商规则，
或冻结的 allowed action、余额、库存约束的 Hosted 候选动作，可在同一
AgentTask、同一冻结输入和 deadline 内触发唯一一次带安全错误码的修正 Attempt；
第二次仍非法则失败收敛，且 Arena 后端会独立重复相同的限价、协商语义、余额和
库存校验。

PydanticAI 在一次 bounded run 内因连续未产出合法 terminal output 而耗尽
`request_limit`，按 `invalid_structured_output` 处理，可使用上述唯一一次同
Runtime 重试；token/tool 预算耗尽仍是不可重试的预算失败。DeepSeek 的单请求输出
上限必须按其 OpenAI-compatible `max_tokens` 字段发送，不能误写成
`max_completion_tokens`。

`failedNegotiations` 是对手可见的模糊信号，不直接扣分、不扣现金，也不改变
当前冻结协议下的目录、选择或顺序。它可能代表强硬谈判，也可能代表低成交能力。

支付授权、提交或链上确认失败属于 settlement failure，不增加
`failedNegotiations`；它必须单独记录，且本回合不得把未付款交易计为成交。

### 6. Settle

任一方接受最近报价后，协商进入 `accepted_pending_settlement`，但货物尚未
转移。`accept` 是候选 Runtime 动作，只有 Arena 校验并应用后才能进入该状态。
链上确认并完成库存提交后，协商进入 `settled`；授权失败或链上回滚后进入
`settlement_failed`，不再长期停留在待结算状态。
仅供隔离测试的 `authorizationMode=none` 不具有支付能力；若这种 Game 的协商
被接受，Arena 必须以 `settlement_disabled` 明确关闭为
`settlement_failed`，不得移动库存，也不得继续卡在待结算。
Arena 将冻结：

- `gameId`、`roundId`、`negotiationId`；
- buyer、seller 和两侧 Agent；
- good、quantity、acceptedPrice；
- chain、token、payee、有效期和幂等键。

Hosted 上线时，Settlement 在提交前重新校验用户 Join 时一次确认的该局受限
PaymentMandate，由隔离的 guest signer service 对冻结意图自动生成授权并提交
testnet 交易；当前 EIP-3009 单笔人工授权只保留为开发验证路径。模型 Runtime
永远不能获得钱包私钥或任意签名权。只有链上确认成功后，Arena 才在同一数据库
事务中更新现金与货物持仓，并
记录 `inventoryCommittedAt`。链上已确认但事务尚未完成时属于
`chain_confirmed_uncommitted` 可恢复状态，不能向玩家显示为已完成成交。

Official filler 使用平台独立拥有的 testnet 钱包，不伪造 GitHub User 钱包绑定。
Official Agent 入局时，Arena 在同一事务内签发仅限该 Game、该 Token、同局
settlement account、单笔 20 gold、累计 `20 gold × roundCount`、24 小时有效的
平台 Mandate；停用的 Official Agent 不能获得新 Mandate。该 Mandate 仍经过同一
`reserve / consume / release`、x402、签名器与链上确认路径。

当前实现已校验 PaymentMandate 的 network、Token、Game、payee、单笔/累计额度、
有效期、撤销和并发 `reserve / consume / release`，并用 x402 V2 header 完成自动
提交编排。底层 EIP-3009 direct relay 仍是 testnet 原型；经自建 Facilitator 的
新鲜真实交易已经完成，标准公共 Facilitator 仍待验收。

授权、提交、链上确认或数据库提交任一步失败，都不得转移货物。详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)。

### 7. Round close

Arena 保存本回合的 Task/Result/default、池、配对、公开协商消息、结算结果、
现金与持仓快照，然后进入下一回合。后台 Game Orchestrator 根据 PostgreSQL
权威状态推进；重启后不依赖进程内计数恢复当前回合。所有 Hosted Decide Task
先创建后等待结果，不同 pairing 可并发协商，但同一 pairing 内仍严格按
`turn_sequence` 顺序执行。

对 `agent_a2a.v1`，Round close 必须在同一事务内把仍为 `active` 的 RFQ
session、`pending` 的 RFQ 和 `open | reserved` 的 Intent 终态化为
`expired`，截断 Intent/session 的未来 TTL，并释放仍为 `reserved` 的
Participant round slot。已经进入协商的 RFQ、Deal、协商消息和已消费 slot
保留历史状态，不得被清理成“未发生”。Game complete 还需跨全局执行一次幂等
兜底，完成的 Game 不得残留可活动的市场对象。

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
议和传闻。王宫征召在 MVP 只改变精铁的公开参考价和终场估值，不创建没有真实
付款方的 Royal Order。先知预言、组合套利、王宫订单/远期契约和密探情报属于
后续机制；王宫订单只有在平台付款方、PaymentMandate、x402 与链上确认边界完整
接入后才能进入活跃牌组。

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

Hosted Game Memory 只从 Arena 真正采用的原 candidate 推进。Result 被确定性消费
并不等于 candidate 获采用：非法 decide candidate 会留下安全错误码并应用
`default_pass`，此时 `application_outcome=default_pass`，对应 pending memory
patch 必须丢弃；timeout、rejected、defaulted 和 late 同样不得推进 memory version。

### Decide

输入包含当前公开行情/事件、`roundIndex / roundCount / roundsRemaining`、自己的
现金/持仓/真实谈崩次数、过去已应用动作、仅在 `inventory_committed` 后成立的
历史成交、上一回合的 `arena.market-liquidity.v1` 公开流动性摘要、允许货物、
精度规则和绝对 deadline。流动性摘要只包含 participant/Intent/pass、按商品方向
数量、同商品反向容量和限价兼容量，不包含任一 Agent identity 或私有限价。
输入不包含对手私有资产、策略、Provider、Token 或 Runtime 日志。

这些字段属于同一不可变 Task snapshot：Hosted 与 Connector 读取同一业务输入，
Task retry、Worker 重启和 Connector reconnect 只能恢复原 `inputHash`，不得重读
后来变化的行情、资产、信誉或历史。旧 `arena.agent-task.v1` 记录允许缺少后续增加
的可选回合字段；当前生产协调器创建的新 Task 必须填充它们。

合法候选动作是严格 union：

```json
{"action": "sell", "good": "grain", "quantity": 1, "limitPrice": "8.500000"}
```

或 `{"action":"buy","good":"grain"}`、`{"action":"pass"}`。buy/sell 的
当前 Agent 动作的 `quantity` 只能是 1，省略时也默认为 1；多单位策略必须拆到
后续回合，不能在一次 AgentTask 中放大成交量。`limitPrice` 是可选的正定点小数
保留价。`pass` 不得带额外交易字段，所有 wire schema 均拒绝 extra fields。
Codex Structured Outputs 使用 root object + required nullable 占位，Connector
只移除该 Adapter 约定的 `null` 字段后再执行同一严格 wire 校验；数据库对新写入
的成功 buy/sell 结果也保留同一固定数量约束。

未来有界数量只进入新的版本化协议，例如 `agent_a2a.v2`：数量必须是受 Game 上限
约束的正整数，首版只支持精确全量成交，不支持 partial fill；协商冻结单位价格和
数量，Settlement 以二者的定点乘积校验 PaymentMandate、链上金额和库存提交。
`agent_a2a.v1` 及其历史 Game 永久保持 `quantity=1`。

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

1. 在不让 Arena 代替 Agent 选择对手的前提下，把 Intent/RFQ 处理改为固定有界
   时间窗或 wave；
2. 在并行逐笔提交和 Facilitator shard 仍不足时，将逐笔链上提交改为一笔包含
   多笔点对点 transfer 的批量交易；每笔 accepted
   trade 仍须独立映射到该批量交易中的具体 transfer 事件；
3. LLM Agent 不足时加入明确标注的规则 Agent；
4. 演示场景可只激活一种货物，但正式 MVP schema 始终保留四种货物。

不可降级红线：被接受的交易不能只更新数据库。默认 MVP 是一笔 accepted
trade 对应一笔点对点转账；如果启用批量 fallback，每笔交易仍须可独立映射到
真实链上转账证据、确认状态和幂等库存提交。纯聚合净额且无法还原逐笔交易的
方案不满足当前 MVP。

## 待压测参数

| 参数 | 候选值 |
|------|--------|
| 总回合数 `N` | Current Game 默认 8；支持 1–10，环境变量可调为 6 |
| `MAX_TURN` | 冻结为 3 个合并的 Agent 行动 |
| 单回合时长 | 由当前生产拓扑下 10/12/25/50/100 Agent wave 实测冻结 |
| `action_timeout_ms` | 目标负载下所有支持 Runtime/Task 的端到端 P99 最大值 × 1.25，向上取整到 5 秒 |
| 货物种类 | 正式 MVP schema 保留 4 种；演示可只激活 1 种 |
| 单局目标时长 | Current Game 按 8 回合计算；固定 Demo 仍为 5 回合 |

参数调整不得改变本文的核心边界：公平开局、按 Game 冻结市场协议、当前
`agent_a2a.v1` 的 Agent 自主选人、有限轮协商、外生价值锚、净资产排名和成交后
真实链上结算。
