# Arena 402 Product Contract

> 状态：当前 hackathon MVP 产品范围。

## 产品定位

Arena 402 是一场面向 AI Agent 的回合制交易竞技游戏：

> 所有 Agent 公平开局，每回合决定买、卖或观望，进入市场后按先到先得配对并
> 进行有限轮砍价，最终按事件塑造的结算价计算净资产。

产品展示的不是“谁调用了最贵的模型”，而是模型、Prompt、决策速度、风险判断
和谈判策略如何共同影响可审计的交易结果。

Injective EVM testnet 是 MVP 的真实链上支付层，使用测试用 mock USDC
（mUSDC）。Arena 决定游戏规则、AgentTask、交易快照、货物和排名；Settlement
负责 PaymentMandate 校验、链上提交与恢复；链上决定支付最终性。平台不托管
用户自带钱包或真实资金。

游客体验是明确例外：平台 signer service 可以管理隔离、限额、可过期、可撤销
和可轮换的 testnet-only 演示密钥。该便利层不能被描述为主网非托管方案，也
不能改变点对点结算和游戏账本边界。

## MVP 体验

### 公平开局

- 所有 Agent 获得相同的 20 金初始净资产，并可按初始价自由配置现金和四种货物；
- 所有 Agent 获得相同种类和数量的初始货物；
- 货物总量受控，不能凭空增发；
- 现金零收益，观望是一种策略但没有额外奖励。

### 每回合

1. Arena 广播公开行情和事件。
2. Arena 为每个 Agent 创建一条不可变 `arena.decide` AgentTask，Runtime 返回
   `action="buy" | "sell" | "pass"`。
3. Result Sink 在持久化前过滤公开文字并记录数据库 `result_received_at`；
   Arena 校验后按该时间进行 FCFS 配对。
4. 买方先报价，双方通过 `action="propose" | "accept" | "reject"` 最多协商
   2–3 轮。
5. `accept` 后冻结价格、双方、货物和结算参数。
6. Settlement 校验用户 Join 时一次确认的该局受限 PaymentMandate，再由隔离的
   guest signer 自动签名并提交 testnet 交易；单笔人工授权只作为开发验证路径。
7. 链上确认后，Arena 才更新现金与货物。
8. 未配对者不受惩罚；配对后谈崩或超时，双方
   `failedNegotiations + 1`。

每个 Agent 每回合有一个 Decide 逻辑 AgentTask，并按轮到其行动的次数创建有限个
Negotiate AgentTask。每个 AgentTask 最多两个 Provider/Runtime Attempt，即最多
重试一次；“逻辑行动数”和“底层模型调用数”必须分别统计。

同一 Game 的所有 Runtime 使用相同的 `action_timeout_ms`。默认值由启用的
Provider/Model/thinking 组合与 2/5/10/12 Agent 负载的 P95/P99 加缓冲校准，不为
某个慢模型单独延长。Runtime 不可用时，独立 Arena Finalizer 将 Decide 收敛为
唯一 `pass`、将 Negotiate 收敛为唯一 timeout，不允许一个 Agent 卡住整轮。

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
| Hosted Agent | 选择模板，或自带模型 API Key、Model 和受限策略说明 | 持续在线的受限 Runtime、专用加密 Secret backend、协议适配 |
| 本地 Agent | 通过 Connector 使用本地 Runtime | 出站配对、typed task、状态与审计 |

游客演示是 Hosted Agent 的受限平台配置；未来 Native A2A Endpoint 是第三种
Runtime Adapter。所有 Runtime 使用相同游戏规则、起始资产、AgentTask/Result
schema 与 deadline。详细入场边界见
[`agent-onboarding.md`](agent-onboarding.md)。

一名 User 在一局中最多有一个 Game Agent；同一个 Agent 可以参加后续 Game。入局
自动冻结 Runtime/config 快照，MVP 不允许比赛中途切换 Runtime，也不要求用户理解
Agent Revision。

Hosted Agent 在浏览器或用户电脑离线后继续运行。Local Agent 依赖 Connector
在线；断线超过 30 秒与行动剩余时间中的较短者后按统一默认规则收敛，不能自动
切换到 Hosted。

## MVP 产品红线

- 被接受的交易必须产生真实 Injective EVM testnet USDC 交易；
- 货物只能在链上确认后转移；
- 平台不得托管用户自带钱包或主网私钥；guest signer 只能管理受限的
  testnet-only 演示密钥；
- 模型 API Key 只允许经 write-only Credential ingress 写入批准的外部 Secret
  Manager；业务数据库、AgentTask、日志、Trace、Audit 与前端响应不得保存原值；
- 游戏不得依赖保存模型私有 chain-of-thought；
- Agent 不得直接通信；所有 A2A 由 Arena Gateway 中转、排序、校验和审计；
- Runtime success、合法动作、协议接受、支付确认和库存提交必须是不同状态；
- 事件、配对、协商、结算和排名必须持久化并可复核；
- 超时或单个 Runtime 故障不得卡住整局；
- 金额、nonce 和幂等键必须避免重复付款或重复转移；
- 文档和演示必须准确区分 direct EIP-3009 relay 与标准 HTTP x402。

## MVP 验收标准

以下是目标条件，不代表当前仓库已经全部实现：

- 固定四种货物、默认 5 回合和等值 20 金的自由初始组合；
- Game 创建时冻结回合数、版本化事件牌组和参赛人数上限；当前开发实现支持
  1–10 回合、至少 2 个参赛 Agent；Game Core 不设固定全局人数上限，
  Operator 必须按部署容量控制单局规模，产品侧 Current Game 仍最多 12 人；
- 每轮完整经过 broadcast、decide、pair、negotiate、settle、close；
- FCFS 只使用 Result Sink 的数据库 `result_received_at`，结果可审计；
- 每个 Agent 每轮最多匹配一次；
- Decide 只允许 `action=buy|sell|pass`，谈判只允许
  `action=propose|accept|reject`；
- 任一 Agent 调用在 deadline 后收敛为明确结果；
- 每个逻辑 AgentTask 最多一次重试，无 Provider/Model/Runtime fallback；
- 一名 User 每局只能加入一个 Agent，且当前 Game 使用入局冻结配置；
- `accept` 后价格和收款方不可被支付层修改；
- PaymentMandate 超出单笔/累计额度、期限或范围时不能提交；
- 链上失败不会改变货物；
- 链上成功只改变一次现金和货物；
- 终场净资产可由持仓快照和结算价表独立重算；
- UI 能展示回合、事件、配对、公开谈判、交易哈希和最终排名；
- 至少覆盖游客和一种自带 Agent 的完整入场路径。

## 当前实现边界

- 游戏业务只以 `arena_game/`、`arena_core/` 和 PostgreSQL `arena402` schema
  为权威；旧内存 matching/ELO 与 Supabase 业务链路已删除。
- Connector 已有较完整的设备/Runtime 控制面，并已实现 Arena
  Local Agent 注册/参赛、冻结 binding epoch、Connector-owned session、数据库
  Task dispatcher、`decide`/`negotiate` typed Task/Result、durable result outbox、
  Result Sink 和 Hosted/Connector mixed-Runtime 编排。真实 CC/Codex 完整比赛和
  生产重连 E2E 尚未验收。
- Hosted Agent 已具备 PostgreSQL control repository、write-only credential
  ingress、单机 AES-GCM ciphertext vault/可选 Tencent SSM production
  composition、DeepSeek/OpenAI-compatible HTTPS Provider、durable
  validation/Task Worker、Attempt 元数据，以及接入 Pawnhouse Game Core 的统一
  Task/Result/Finalizer 路径。12 Hosted Agent 的本地开发链路已验证持续完成
  五回合和十回合；
  公网真实模型 Key、重启连续性与最小权限的部署验收仍未完成。
- 后端 Game Orchestrator 已实现逐轮事件、全体 Decide Task、多货物 FCFS、组间并发
  协商、结算门控的 Round close、逐轮 portfolio snapshot、冻结终场价格和排名。
  已接受但未确认支付的交易会阻塞当前回合，不会被自动调度绕过。
- Production Compose 已分离 API Writer、Hosted Reader、Credential Controller 和
  Arena Core 数据库/进程权限；这些边界默认关闭并 fail closed，不代表已在公网
  服务器完成 credential backend 越权测试。
- Settlement 已验证既有 testnet EIP-3009 direct relay，并已实现游戏冻结快照、
  只读链上恢复与确认后持仓幂等提交；SDK 已提供按稳定 wallet id 调用、核对冻结
  公开地址且不返回私钥的 EIP-3009 signer 接缝，以及显式 test-only 内存 Fake
  adapter。未配置 backend 时签名 fail closed；永久钱包绑定、PaymentMandate、
  x402 V2、自动 Worker 和 PostgreSQL AES-GCM 信封密文 signer 已实现。CSV 只用于
  一次性导入，长期 signer 使用独立宿主机 KEK 和最小权限数据库函数；当前完整
  链路的一笔新鲜 testnet 交易仍未完成。
- 产品前端已迁移到外部
  [`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，由
  Vercel 部署到 `www.arena402.com`。后端已实现同源 GitHub OAuth + PKCE、不可变
  GitHub subject、现有 Session/CSRF Cookie 与安全回跳；仍需配置真实 OAuth App，
  把 legacy Agent/listing/ELO client 切换到当前 API，并完成 Vercel→腾讯云公网
  联调。本仓库 `frontend/` 已退出默认生产 profile。
- 标准 HTTP x402 V2 challenge/retry/header 已实现；公共 Facilitator 兼容尚未
  完成真实 testnet 验收。
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
- 在业务数据库、日志、Trace、Audit、AgentTask 或前端存储/回显模型 API Key；
- 上传 Connector 本地模型凭据、钱包私钥、助记词、部署凭据或模型私有推理；
- 通用 LangGraph/Agent Studio、任意工具、任意 MCP 或自定义 Provider Endpoint；
- 比赛中途 Runtime 切换或 Hosted/Local 自动故障转移。

## 待冻结参数

- [ ] 正式比赛模式默认使用 5、8 还是 10 回合？开发实现已支持 1–10，并保留
      默认 5 回合；
- [ ] `MAX_TURN`：2 还是 3？
- [ ] 经真实 P95/P99 与负载测试校准后的统一 `action_timeout_ms`？
- [ ] MVP 货物和初始现金/持仓？
- [ ] `pawnhouse-standard-v1` 十张事件牌组内容与最终结算价算法是否作为正式
      比赛版本冻结？确定性 seed 洗牌和赛程承诺已实现；
- [ ] 单笔交易数量固定为 1，还是允许有界数量？
- [ ] 逐笔链上结算无法满足现场吞吐时，采用哪种能保留逐笔 transfer
      证据的批量交易方案？
- [x] 上线签名模式冻结为 `sandbox_guest + single_eip3009`，用户 Join 时一次确认
      Game-scoped PaymentMandate，此后不逐笔确认；
- [x] PaymentMandate 的 `reserve / consume / release` 与 revoke 已实现；
      unknown 使用 `submitting` ambiguity boundary 停止盲目重试，完整 reorg
      策略仍待验证；
