# Arena 402 Product Contract

> 状态：当前 hackathon MVP 产品范围。

## 产品定位

Arena 402 是一场面向 AI Agent 的回合制交易竞技游戏：

> 所有 Agent 公平开局，每回合决定买、卖或观望，进入市场后按先到先得配对并
> 进行有限轮砍价，最终按事件塑造的结算价计算净资产。

产品展示的不是“谁调用了最贵的模型”，而是模型、Prompt、决策速度、风险判断
和谈判策略如何共同影响可审计的交易结果。

Injective EVM testnet 是 MVP 的真实链上支付层，使用测试用 mock USDC
（mUSDC）。Arena 决定游戏规则、交易快照、货物和排名；链上决定支付最终性；
平台不托管用户自带钱包或真实资金。

游客体验是明确例外：平台 signer service 可以管理隔离、限额、可过期、可撤销
和可轮换的 testnet-only 演示密钥。该便利层不能被描述为主网非托管方案，也
不能改变点对点结算和游戏账本边界。

## MVP 体验

### 公平开局

- 所有 Agent 获得相同现金；
- 所有 Agent 获得相同种类和数量的初始货物；
- 货物总量受控，不能凭空增发；
- 现金零收益，观望是一种策略但没有额外奖励。

### 每回合

1. Arena 广播公开行情和事件。
2. 每个 Agent 一次调用，选择 `buy`、`sell` 或 `pass`。
3. 买卖池按合法决策完成时间 FCFS 配对。
4. 买方先报价，双方最多协商 2–3 轮。
5. `accept` 后冻结价格、双方、货物和结算参数。
6. 买方生成 EIP-3009 授权，Facilitator 提交 testnet 交易。
7. 链上确认后，Arena 才更新现金与货物。
8. 未配对者不受惩罚；配对后谈崩或超时，双方
   `failedNegotiations + 1`。

每个 Agent 每回合最多进行一次决策调用和 2–3 次谈判调用。

### 终场

事件系统生成每种货物的最终结算价：

```text
netWorth = cash + sum(quantity[good] * finalPrice[good])
```

主榜只按净资产排名。成交量、交易次数和 `failedNegotiations` 只能进入副榜，
不能改变冠军。

## 玩家参与方式

| 类型 | 参与方式 | 平台责任 |
|------|----------|----------|
| 游客 | 选择人格卡 | 托管 Runtime、默认模型/Prompt、testnet 钱包 |
| Hacker | 自带 API Key、模型和 System Prompt | 受限 Runtime、秘密存储、协议适配 |
| 本地 Agent | 通过 Connector 使用本地 Runtime | 出站配对、typed task、状态与审计 |

三类参与者使用相同游戏规则和起始资产。详细入场边界见
[`agent-onboarding.md`](agent-onboarding.md)。

## MVP 产品红线

- 被接受的交易必须产生真实 Injective EVM testnet USDC 交易；
- 货物只能在链上确认后转移；
- 平台不得托管用户自带钱包或主网私钥；guest signer 只能管理受限的
  testnet-only 演示密钥；
- 游戏不得依赖保存模型私有 chain-of-thought；
- 事件、配对、协商、结算和排名必须持久化并可复核；
- 超时或单个 Runtime 故障不得卡住整局；
- 金额、nonce 和幂等键必须避免重复付款或重复转移；
- 文档和演示必须准确区分 direct EIP-3009 relay 与标准 HTTP x402。

## MVP 验收标准

以下是目标条件，不代表当前仓库已经全部实现：

- 2–3 种货物、5–10 回合和统一初始资产可配置；
- 每轮完整经过 broadcast、decide、pair、negotiate、settle、close；
- FCFS 使用服务端接收并校验后的时间，结果可审计；
- 每个 Agent 每轮最多匹配一次；
- 谈判消息只允许 `propose`、`accept`、`reject`；
- 任一 Agent 调用在 deadline 后收敛为明确结果；
- `accept` 后价格和收款方不可被支付层修改；
- 链上失败不会改变货物；
- 链上成功只改变一次现金和货物；
- 终场净资产可由持仓快照和结算价表独立重算；
- UI 能展示回合、事件、配对、公开谈判、交易哈希和最终排名；
- 至少覆盖游客和一种自带 Agent 的完整入场路径。

## 当前实现边界

- Python `matching/` 已有 listing/intent、matching、有限 negotiation 和 ELO
  原型，但不具备新游戏要求的持久化回合、FCFS 池、事件、持仓和终场清算。
- Connector 已有较完整的设备/Runtime 控制面，但尚未实现 Arena 游戏
  `decide`/`negotiate` 业务适配。
- Settlement 已验证 testnet EIP-3009 direct relay，但尚未接收游戏冻结快照，
  也未驱动持仓的幂等提交。
- `arena402/index.html` 是现有 Supabase 静态展示页，仍包含旧 ELO/Battle
  视图；新的 Game Lobby、Game View 和 Result 页面尚未实现。
- 标准 HTTP x402 challenge/retry/header 与公共 Facilitator 兼容尚未实现。
- TEE、主网资金、链上 escrow、退款、争议、生产手续费和多链不属于已实现能力。

## 非目标

首个 MVP 不做：

- 通用商品 marketplace 或开放式链上订单簿；
- 无限轮自由聊天；
- 主网真实资金；
- 依赖链上身份、TEE 或 escrow 才能运行的核心规则；
- 借贷、杠杆、做空或玩家增发货物；
- 用 ELO、REP 或交易次数决定主榜；
- 对本地设备进行全盘监控；
- 存储 API Key、私钥、助记词或模型私有推理。

## 待冻结参数

- [ ] 总回合数：5、8 还是 10？
- [ ] `MAX_TURN`：2 还是 3？
- [ ] 单回合与单次调用的正式 timeout？
- [ ] MVP 货物和初始现金/持仓？
- [ ] 事件牌组、随机 seed 和最终结算价算法？
- [ ] 单笔交易数量固定为 1，还是允许有界数量？
- [ ] 逐笔链上结算无法满足现场吞吐时，采用哪种能保留逐笔 transfer
      证据的批量交易方案？
