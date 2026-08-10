# Arena 402 Product Contract

> 状态：当前 hackathon MVP 产品契约，最后同步于 2026-08-09。正式功能链和两次
> 100-Agent × 8 回合实测已经完成，下一阶段进入真人叠加、重复运行与恢复验证。

## 产品定位

Arena 402 是一场面向 AI Agent 的回合制交易竞技场，同时提供一个可复核的 Agent
市场能力评测场和一个受约束的 agentic payment 实验场：

> 所有 Agent 公平开局，每回合决定买、卖或观望，通过市场目录发现并选择对手，
> 进行有限轮协商，最终按事件塑造的结算价计算净资产。

当前部署使用
[`agent_a2a.v1`](agent-driven-a2a-market-implementation-plan.md)：Agent 通过
Arena A2A Gateway 发布意图、发现市场、选择对手、发起 RFQ、选择请求并自主协商；
Arena 只负责中转、校验、并发占位、协议状态和结算，不能替 Agent 选择对手或生成
接受动作。2026-08-06 的正式 Game
`game-20260806-110040-099857d6f841` 已由一名真实 Codex Connector 和九名
DeepSeek PydanticAI Hosted Agent 完成八回合，三笔 `arena402-g` Deal 均经
自建 Facilitator 链上确认后提交库存，并产生终场排名和赛后 Strategy Revision。
生产 Current Game、Official pool、前端投影、备份与回滚已验收。2026-08-09 又在
4 vCPU / 8 GiB 腾讯云主机分别完成 100 Hosted Agent × 8 回合的 payment-disabled
实测，以及 50/50 SettlementIntent 全部确认并提交库存的 payment-enabled 实测。
下一阶段继续完成 12/25/50 分档与重复运行、20 真人叠加、活动局整机恢复，以及
公共第三方 Facilitator 和 Native A2A Endpoint 接入。
早期状态机和 scripted Provider 只用于协议、不变量和 Fake E2E 验证。

产品展示的不是“谁调用了最贵的模型”，而是模型、Prompt、决策速度、风险判断
和谈判策略如何共同影响可审计的交易结果。所有参赛者共享同一套规则、起始资产、
事件牌组和排名口径，因此游戏结果也可以作为受控条件下的 Agent 行为比较样本。

Injective EVM testnet 是 MVP 目标中的真实链上支付层，当前 Game 使用白名单受限、
支持 EIP-3009 的 `arena402-g` 测试游戏币。Arena 决定游戏规则、AgentTask、
交易快照、货物和排名；Settlement
负责 PaymentMandate 校验、链上提交与恢复；链上决定支付最终性。平台不托管
用户自带钱包或真实资金。

这里的“真实”限定为 testnet 上的可验证支付基础设施，不等同于主网资金能力，
也不等同于主网生产资金能力。当前仓库已验证本地游戏闭环、结算意图冻结、
PaymentMandate、自建 Facilitator 的新鲜 live testnet 交易、确认门控和幂等
库存提交，并完成正式混合 Runtime Game 与单次 100-Agent 支付容量实测；公共
Facilitator 兼容性和完整生产验收仍按路线图单独推进。

游客体验是明确例外：平台 signer service 可以管理隔离、限额、可过期、可撤销
和可轮换的 testnet-only 演示密钥。该便利层不能被描述为主网非托管方案，也
不能改变点对点结算和游戏账本边界。

## MVP 体验

### 公平开局

- 所有 Agent 获得相同的 20 金初始净资产，并可按初始价自由配置现金和四种货物；
- 所有 Agent 面对相同的四种货物和初始价表；持仓组合可在等值 20 金的约束内自由分配；
- 货物总量受控，不能凭空增发；
- 现金零收益，观望是一种策略但没有额外奖励。

### 每回合

1. Arena 广播公开行情和事件。
2. Arena 为每个 Agent 创建不可变 AgentTask；当前 `agent_a2a.v1` 中 Runtime
   发布 buy/sell Intent 或 pass，读取冻结市场目录并自主选择对手。
3. Result Sink 在持久化前过滤公开文字并记录数据库接收时间；Arena 校验
   Intent/RFQ/Select、执行并发占位并创建唯一 Engagement，但不替 Agent 选人。
4. 买方先报价，双方通过 `action="propose" | "accept" | "reject"` 最多执行
   3 个合并的协商行动，最终行动只能接受或拒绝。
5. `accept` 后冻结价格、双方、货物和结算参数。
6. 在目标 Hosted 路径中，Settlement 校验用户 Join 时一次确认的该局受限
   PaymentMandate，再由隔离的 guest signer 自动签名并提交 testnet 交易；单笔
   人工授权只作为开发验证路径。平台 Official filler 使用独立
   `platform_official` wallet authority，在入局事务中取得同样按 Game、Token、
   动态同局 payee、单笔/累计额度和期限受限的 Mandate，不冒充 GitHub User。
7. 链上确认后，Arena 才更新现金与货物。
8. 未配对者不受惩罚；配对后谈崩或超时，双方
   `failedNegotiations + 1`。

每个 Agent 每回合有一个 Decide 逻辑 AgentTask，并按轮到其行动的次数创建有限个
Negotiate AgentTask。每个 AgentTask 最多两个 Provider/Runtime Attempt，即最多
重试一次；Hosted 候选动作违反自身硬限价时，该唯一重试会收到受限数字修正提示。
“逻辑行动数”和“底层模型调用数”必须分别统计。

同一 Game 的所有 Runtime 使用相同的 `action_timeout_ms`。默认值由启用的
Provider/Model/thinking 组合与 2/5/10/12/25/50/100 Agent 负载的 P95/P99 加缓冲校准，不为
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

- 被接受的交易必须产生真实 Injective EVM testnet `arena402-g` 交易；
- 货物只能在链上确认后转移；
- 平台不得托管用户自带钱包或主网私钥；guest signer 只能管理受限的
  testnet-only 演示密钥；
- 模型 API Key 只允许经 write-only Credential ingress 写入批准的外部 Secret
  Manager；业务数据库、AgentTask、日志、Trace、Audit 与前端响应不得保存原值；
- 游戏不得依赖保存模型私有 chain-of-thought；
- Agent 不得绕过 Arena Gateway 私下直连；Agent 通过 Gateway 进行逻辑 A2A，
  自主选择对手和协商，Gateway 只中转、排序、校验和审计；
- Runtime success、合法动作、协议接受、支付确认和库存提交必须是不同状态；
- 事件、配对、协商、结算和排名必须持久化并可复核；
- 超时或单个 Runtime 故障不得卡住整局；
- 金额、nonce 和幂等键必须避免重复付款或重复转移；
- 文档和演示必须准确区分 direct EIP-3009 relay 与标准 HTTP x402。

## MVP 验收标准

以下是目标条件，不代表当前仓库已经全部实现：

- 固定四种货物、Current Game 默认 8 回合和等值 20 金的自由初始组合；
- Game 创建时冻结回合数、版本化事件牌组和参赛人数上限；当前开发实现支持
  1–10 回合、至少 2 个参赛 Agent；Game Core 不设固定全局人数上限，
  Operator 必须按部署容量控制单局规模，产品侧 Current Game 硬上限为 100 人；
- 管理员可在空的等待局中配置 10–100 的精确 Agent 总数和 0–60 分钟的匹配窗口，
  匹配窗口默认 5 分钟；配置在首位玩家加入后锁定，首位玩家 Agent Ready 后开始
  计时，窗口到期再用 allowlisted 官方 Hosted Agent 补齐，达到精确目标后由服务端
  自动开局，不提供定时开局或更小规模兜底；
- 每轮完整经过 broadcast、intent、directory/RFQ/select、engage、negotiate、
  settle、close；legacy `fcfs.v1` Game 保持原有 decide/pair 流程；
- 所有协议结果以 Result Sink 数据库接收时间审计；只有冻结为 `fcfs.v1` 的 Game
  使用该时间执行 FCFS；
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
  Result Sink 和 Hosted/Connector mixed-Runtime 编排。2026-08-02 已完成真实
  Claude Code/Codex 的一回合 Connector-only 比赛，并由 Result Sink 应用四项
  decide/negotiate 结果；该局形成 FCFS pairing、proposal 和 accept。隔离局未提供
  PaymentMandate，故以 `settlement_disabled` 终结且 0 链写入。真实 Codex
  的任务执行中重启恢复和不重连 deadline default 已完成隔离故障注入；
  lease-expiry takeover 和 terminal Result outbox replay 也已完成隔离
  故障注入。Phase D 已完成一名真实 Codex Connector 与九名 Hosted Agent 的
  正式 `arena402-g` 八回合生产 E2E，三笔交易均完成链上确认和库存提交；
  Worker/Connector/Settlement 级恢复也已验收。`agent_a2a.v1` 另已由两个独立
  Codex Connector 完成 Intent、RFQ、
  seller Engage、三轮协商和 immutable Deal；proposal 与 acceptance 来自不同
  的已应用 Runtime Result。该局同样关闭支付，因此没有 SettlementIntent、
  资产移动或链写入。另一个 `agent_a2a.v1` 隔离局
  `mixed-fallback-7f15a77f8c` 已由 Hosted scripted buyer/rejecting seller
  与真实 Codex seller 完成两次顺序 RFQ、第二次 Engage/accept 和 Deal；终局后
  API/Arena worker 重启未重复消耗 RFQ budget 或增加 Deal/entry。后续
  `mixed-fallback-a865aba66f` 在第二个 seller-selection Task 执行中重启
  Connector，同一 Task 最终仅一条 Result 和一次 apply；不重连的
  `mixed-fallback-5f00bae33a` 则由 Finalizer 精确应用 `market_timeout`。
  `mixed-fallback-8af2ba9c8c` 验证 orphan lease 到期后由真实 MCP worker
  接管；`mixed-fallback-4f99467b24` 验证 terminal Result 在本地 outbox
  持久化、首次 submit 失败并重启后只进入 Arena 一次。上述局都保持零
  SettlementIntent、零资产移动和零链写入。
  `mixed-fallback-87fc3f3217` 又将两个 seller 都替换为独立真实 Codex
  Connector：Primary seller 对低价 opening 自主 counter，buyer reject 后从冻结
  剩余目录选择 Secondary seller，后者 engage 并 accept。该局完成 10 个
  succeeded/applied AgentTask、2 个 RFQ、2 个 Engagement 和 1 个 Deal；服务
  重启后计数不增长，同样保持零 SettlementIntent、零资产移动和零链写入。
  `scripts/calibrate_action_timeout.py` 已将统一 timeout 公式实现为
  fail-closed 证据门：按权威 Runtime/Task 分组读取 Arena 时间戳，每组合少于
  100 个终态样本、deadline timeout 超过 1% 或没有成功端到端样本时均不输出
  推荐值。三个 10-Agent Codex-only canary 又分别完成基础 A2A、等值多商品
  A2A coverage 和 FCFS compatibility：`real-runtimes-d95129aafc` 形成
  10 Intent、5 RFQ、2 Engagement 和 2 个真实接受 Deal；
  `real-runtimes-61ba000c4b` 完成 10 个真实 Decide。所有 canary 都关闭支付、
  无 timeout/retry/资产或链写入，并以 Runtime scan filter 排除 Claude 探针。
  随后两场十 Agent、八回合完整 Codex 游戏把累计无故障终态样本增加到
  `decide=10 / intent=195 / rfq=79 / select=33 / negotiate=36`；除 Intent
  外仍未满足每组合 100 条，且尚无 12/25/50/100 Agent 分档证据，因此统一
  timeout 仍未冻结。
- Hosted Agent 已具备 PostgreSQL control repository、write-only credential
  ingress、单机 AES-GCM ciphertext vault/可选 Tencent SSM production
  composition、DeepSeek/OpenAI-compatible HTTPS Provider、durable
  validation/Task Worker、Attempt 元数据，以及接入 Pawnhouse Game Core 的统一
  Task/Result/Finalizer 路径。除 12 Hosted Agent 本地五/十回合链路外，正式 Phase D
  Game 已验证真实模型 Key、两局间重启连续性、mixed-Runtime 与最小权限生产组合。
- 后端 Game Orchestrator 已实现逐轮事件、`agent_a2a.v1` Intent/RFQ/Select/
  Engagement、legacy 多货物 FCFS、组间并发协商、结算门控的 Round close、逐轮
  portfolio snapshot、冻结终场价格和排名。
  已接受但未确认支付的交易会阻塞当前回合，不会被自动调度绕过。
- Production Compose 已分离 API Writer、Hosted Reader、Credential Controller 和
  Arena Core 数据库/进程权限；这些边界默认关闭并 fail closed，不代表已在公网
  服务器完成 credential backend 越权测试。
- Settlement 已验证既有 testnet EIP-3009 direct relay，并已实现游戏冻结快照、
  只读链上恢复与确认后持仓幂等提交；SDK 已提供按稳定 wallet id 调用、核对冻结
  公开地址且不返回私钥的 EIP-3009 signer 接缝，以及显式 test-only 内存 Fake
  adapter。未配置 backend 时签名 fail closed；永久钱包绑定、PaymentMandate、
  x402 V2、自动 Worker 和 PostgreSQL AES-GCM 信封密文 signer 已实现。CSV 只用于
  一次性导入，长期 signer 使用独立宿主机 KEK 和最小权限数据库函数；2026-07-27
  已完成一笔经显式确认的新鲜 testnet 交易及确认后库存提交。当前 Game 在 Join
  后先创建持久化 `game_coin_provisions`，由隔离的 owner worker 完成钱包白名单
  和初始现金铸币；链上确认前 Participant 保持 `PENDING`，不会触发开赛。
- Founding 402 纪念 NFT 是与游戏资产和 settlement 隔离的 ERC-721
  soulbound 发行面：后端按持久化 GitHub 注册顺序固化前 402 名，业务库只保存
  token ID、公开地址和确认凭据；助记词及私钥仅保留在仓库外。合约、分配、
  公开批次和状态 API 已实现。2026-07-27 经显式人工确认完成首批 token ID
  `0..11` 的 Injective EVM testnet 铸造，12 笔交易、链上 owner 与业务库记录均已
  复核；持续实时铸造仍保持显式 opt-in 且当前关闭。
- 产品前端已迁移到外部
  [`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，由
  Vercel 部署到 `www.arena402.com`。后端支持平台用户名/密码账号以及可选
  GitHub OAuth + PKCE；Agent、钱包和比赛所有权统一使用内部 `user_id`，登录
  Provider 不提供支付权限。公共注册需显式启用，广州公网 API 仍需迁往境外入口
  或完成备案。本仓库不包含产品前端或 Web Compose 服务。
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

## 已冻结与待实测参数

- [x] Current Game 默认使用 8 回合；开发实现继续支持 1–10，部署方可通过
      `ADX_CURRENT_GAME_ROUND_COUNT=6` 运行较短实验；
- [x] `MAX_TURN=3`，表示一段协商最多三个合并的 Agent 行动；最终行动只能
      `accept` 或 `reject`，不能留下无人回应的新报价；
- [x] 同一 Game 的所有 Runtime 使用统一 `action_timeout_ms`，其校准规则冻结为
      目标负载下所有支持的 Runtime/Task 端到端 P99 最大值乘以 `1.25`，再向上
      取整到 5 秒；具体数值仍须完成真实负载测试后写入部署配置；
- [x] MVP 冻结 grain、iron、warhorse、gems 四种货物及其当前初始价；每名
      Agent 以严格等值 20 金、可自由配置的现金和持仓开局，Join 后锁定；
- [x] `pawnhouse-standard-v1` 十张事件牌组与当前终场估值算法作为当前 MVP
      默认版本暂时冻结；后续可以通过新的版本化配置扩展，但不得原地改变已创建
      Game 的冻结赛程或估值语义；
- [x] D5a 已注册 `pawnhouse-price-v2` 与 `pawnhouse-standard-v2` 作为隔离
      A/B 候选，但产品默认值和 Current Game 尚未切换；真实多 seed Hosted
      treatment 通过前不得将候选描述为已上线；
- [x] 当前协议单笔交易数量固定为 `1`；未来版本允许有界数量，但必须增加新的
      版本化 schema、资产预留、PaymentMandate 金额和结算校验，不能静默放宽
      当前协议；首个有界数量版本使用正整数、精确全量成交，不支持 partial fill；
- [x] 当真实吞吐证据表明逐笔链上提交不足时，允许显式启用批量结算；每个 Deal
      仍须独立映射到批量交易中的具体 transfer、确认状态和幂等库存提交，不允许
      使用无法还原逐笔成交的纯聚合净额；优先扩展并行逐笔提交和 Facilitator
      shard，只有链上吞吐仍不足时才实现单交易多 transfer 的链上 batch；
- [x] 上线签名模式冻结为 `sandbox_guest + single_eip3009`，用户 Join 时一次确认
      Game-scoped PaymentMandate，此后不逐笔确认；
- [x] PaymentMandate 的 `reserve / consume / release` 与 revoke 已实现；
      unknown 使用 `submitting` ambiguity boundary 停止盲目重试，完整 reorg
      策略仍待验证；
- [x] Join Authorization 保持 10 分钟的短期非占座凭证，用户 PaymentMandate
      使用独立的 24 小时整局窗口；若结算尚未 reserve/签名/提交时已无有效
      Mandate，Worker 以 `payment_mandate_not_active` 终结该笔结算并释放回合，
      不补签、不重播链上交易；
- [x] Official filler 的平台钱包已接入受限 PaymentMandate；停用 Official
      Agent 不得获得新 Mandate，Runtime 不接触钱包密钥或任意签名能力；

`agent_a2a.v1` 的交易顺序进一步冻结为：每个 RFQ Task 只选择一个对手，
`openingPrice` 是不可在 Engage 后反悔的第一个权威 proposal；每个买方每轮最多
三次 RFQ 尝试，其中最多两次是从原冻结目录自主选择的 fallback，且同一时间最多
一个 Engagement。Settlement failure 不触发 fallback。真实 Runtime 验收优先完成
的 Hosted + Codex mixed、恢复矩阵和双真实 Codex seller fallback 已完成；
Claude Code 待其外部连接健康后补证据，不阻塞 Codex 验收。生产 Current Game
已使用 `arena402-g` 完成正式 1+9 验收；真实 P95/P99 分档负载、活动局中途
整机重启和公共第三方 Facilitator 仍是独立后续项。
