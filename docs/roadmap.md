# Arena 402 Roadmap

## Hosted Agent Runtime v2

- [x] 冻结 PydanticAI 直接替换方案：保留 AgentTask/Result、lease、Secret、Result
  Sink 和 Finalizer，退役 DirectModelDriver/PromptBuilder 认知执行链。
- [x] 冻结官方策略目录为 `aggressive | conservative | balanced`，随机抽取持久
  `agent_id`，入局后冻结 identity、Strategy Revision 和独立 Game Memory。
- [x] 完成 Strategy Revision、Game Memory、pending patch 与 official pool migration。
- [x] 完成 PydanticAI Agent、只读工具、typed output 和 allowlisted Model factory。
- [x] 生产 Durable Hosted Worker 已切换到 bounded PydanticAI run；candidate action
  仍只进入原 Result Sink，pending patch 只在 Arena 实际应用原 candidate 后 CAS
  投影；`default_pass`、timeout、rejected 和 late 都不推进记忆。
- [x] 经人工确认物理删除 PromptBuilder/DirectModelDriver 及其测试，Attempt
  元数据迁入独立模块，Worker 不再保留 legacy/scripted 比赛分支。
- [x] 官方 PydanticAI Agent 与私有 LiteLLM 上游统一选择
  `deepseek-v4-flash`，不再使用已停用的 `deepseek-chat` 名称。
- [x] 使用隔离 PostgreSQL 验证完整迁移、Hosted 入局冻结、PydanticAI Worker、
  Result applied 推进记忆，以及 defaulted task 不学习；Agent run 的
  request/tool 安全计数已进入 Attempt 元数据。
- [x] 使用真实 DeepSeek V4 Flash BYOK 直连验证 Thinking + tools + typed
  terminal action；三种官方策略各连续执行两个 `arena.decide` 回合，六次任务均
  succeeded，且每个 Agent 的 Game Memory 从 v0 单调推进至 v2。该证据是独立的
  直连验证，不等同于生产部署验收。
- [x] 完成 `game.completed` 跨比赛学习闭环：完成局会幂等创建 durable learning
  job，只读取可验证的排名、净值、终场价格、动作、成交、失败和 usage 汇总；
  bounded PydanticAI learner 生成五维 policy candidate，经严格 schema、策略类型
  envelope、单局每维最多 1000 bps 变化和历史动作计数回放后生成新 Strategy
  Revision；模型自报 confidence 只保存为审计信号。新 revision 只供后续 Game
  冻结；严重退化时自动恢复 parent revision，同样不改写已开始 Game。
- [x] 在 2026-08-05 的全新隔离 PostgreSQL 上从 `002` 迁移至 `064`，验证胜局
  candidate 激活、旧局继续冻结 base revision、下一局冻结 learned revision，以及
  learned revision 相对 parent 下降至少 2000 bps 后仅对未来局回滚。该证据使用
  PydanticAI TestModel，不等同于私有 LiteLLM 或真实比赛收益证明。
- [x] 使用私有 LiteLLM + DeepSeek V4 Flash 完成真实 learner 调用，模型实际执行
  3 个 request、2 个只读 tool call。随后发现最初 payment-disabled 单回合 1+9
  试跑把“无成交但因初始组合获利”误当成学习信号；该次 learned revision 的验收
  结论已撤回。当前 preflight 要求至少两个 task、一个真实 candidate action、一笔
  `settled` 交易和非零相对净值，在调用模型前拒绝单步、default-only、无成交和
  随机组合收益。
- [x] 2026-08-06 的三回合真实 1+9 回归先暴露并修复三类运行时问题：
  `eventImpliedFinal` 缺失导致同向决策、PydanticAI 将 DeepSeek 输出上限误发为
  `max_completion_tokens`、以及 `request_limit` 耗尽被误归类为不可重试。
  DeepSeek 现在接收 `max_tokens`，非 thinking/thinking 单请求分别限制为
  8192/16384，Agent-run 累计限制为 65536；合法 retry 会保留第一 Attempt usage。
- [x] `regression-real-hosted-1plus9-v7` 完成 30/30 decide 和 2/2 negotiate，
  形成 iron 配对、模型报价 `5.880600` 后由对手接受；payment-disabled 按设计
  收敛为 `settlement_failed`，0 SettlementIntent。该局还暴露两个跨回合 memory
  patch 因投影时序变为 stale；迁移 `065` 已在下一 task 加载上下文前投影同一
  Game Agent 的已应用 patch，并在全新 PostgreSQL 集成测试通过。
- [x] 修复后的 `regression-real-hosted-1plus9-v10` 以退出码 0 完成 30/30
  decide、4/4 negotiate、warhorse/iron 两次报价与接受；34/34 task 均完成，
  30/30 decide 都携带四货物 `eventImpliedFinal`，10/10 Agent 的 Game Memory
  至少为 v3，所有 learning job 因无 `settled` 交易被确定性拒绝，且保持
  0 SettlementIntent。
- [x] 在全新 PostgreSQL `002`–`066` 上完成 Hosted fault-injection：两个 Worker
  并发领取同一 Task 时仅一个成功；`Attempt.created` 后崩溃可由第二 Worker 执行
  唯一重试；`request_sent` 后崩溃收敛为 `request_outcome_unknown` 且不重放
  Provider；相同 Result 并发提交得到一条 accepted 和一条 duplicate，不同 hash
  得到 conflict；Finalizer 获胜后的 Result 为 late；learning lease 到期后由新
  Worker 以第二次也是最后一次 Attempt 完成。该验收使用不同 Worker identity 和
  真实 PostgreSQL lease/CAS，但仍不是外部多容器进程 kill 验收。
- [x] fault-injection 发现 Arena 对非法 candidate 确定性应用 `default_pass` 时，
  `apply_status=applied` 不能代表原 candidate 获采纳。迁移 `066` 现在联合
  `arena_applied_agent_actions.application_outcome`，只有 `candidate` 才推进
  Game Memory；`default_pass` 的 Result 保留 `good_not_allowed` 审计，但对应
  memory patch 必须 `discarded`。
- [x] 使用 `tests/hosted_worker_process_recovery_e2e.py` 在全新 PostgreSQL 和
  当前生产镜像上完成外部多容器 `SIGKILL`：Attempt 创建前被杀的 Worker 由新
  identity 在真实 30 秒 lease 到期后执行 Attempt 1；durable `request_sent`
  后被杀的 Worker 由另一 identity 收敛为 `request_outcome_unknown`，本地
  LiteLLM 协议替身的 Provider 请求计数保持 1，证明没有重放。测试使用独立
  AES-GCM 密钥卷、最小权限 Worker login、迁移 `002`–`066`，且支付关闭。
- [x] 2026-08-06 在隔离 PostgreSQL、私有 LiteLLM、真实 DeepSeek V4 Flash、
  专用 wallet signer 和自建 Facilitator 上完成两场三回合 1+9
  payment-enabled canary。四笔 mUSDC Intent 分别以交易
  `0x5cb511d683f86c5b6348b1f8cac2d90e1bde0082ba272af78a36fdc0ea9414b1`、
  `0x414f1da2c7025e6b9d00a6288e0a92ecfb1cdba34335f28972c85cbab9bf81db`、
  `0x47c80cfba90f7c3e4b79758c0ca5bcafc3368b8215fa1ba89da85d41b2546137`
  和
  `0x35deed2b2c23295bcd7da85030e4a57dc08005c83b8ea54af8609b30b3f0993e`
  在 Injective EVM testnet 确认，确认数分别为 3/4/3/2；四个 Intent 均到达
  `inventory_committed`，对应 Pairing/Negotiation 才进入 `settled`。该证据
  使用隔离 mUSDC canary，不替代 `arena402-g`、公共 Facilitator 或生产发布验收。
- [x] 第一局玩家以 owner revision 1、一次本人 settled 交易和排名 1 完成，
  learner 激活 learned revision 2；第二局的 Game Agent 确实冻结 revision 2，
  并实际产生 `buy/sell/pass` 各一次，排名 7 且没有本人 settled 交易，因此按
  门禁不再次学习。这证明跨局 revision 绑定与“无本人结算不学习”，不证明该次
  learned revision 带来收益提升。
- [x] 真实 canary 暴露 learner 将 Provider 输出上限再次压到 2048，导致成交方
  的 DeepSeek typed proposal 在 token limit 前无法完成。当前权威证据会直接
  注入 learner 上下文，只读工具保留为可选复查；`invalid_structured_output`
  允许一次 durable retry，安全诊断只记录归一化原因；Provider 输出上限与
  PydanticAI run budget 统一为 8192。修复后第二局四个真实成交方均在第一次
  learner Attempt 激活 revision，另外六个无成交方在调用模型前确定性拒绝。
- [x] 保存两局初始策略收益基线：aggressive/balanced/conservative 各 6 个
  Game Agent 样本的平均排名为 `5.83/5.83/5.33`，平均净值为
  `20,123,983 / 20,376,667 / 20,407,683` atomic；custom 玩家两局平均排名
  `4.00`、平均净值 `21,300,000` atomic。样本仅两局，且模型决策具有随机性，
  不能据此宣布某个 archetype 更优。
- [ ] 以多局 `settled` 样本校准学习激活和严重退化回滚阈值，并完成
  aggressive/conservative/balanced 的统计性策略收益对比验收；上述两局只作为
  初始基线。

## Phase D：统一自主交易比赛

> 2026-08-06 已批准并开始实施。Phase D 不再新增一套认知 Runtime，而是把已经
> 分别成立的 PydanticAI Hosted Agent、Codex Local Connector、`agent_a2a.v1`、
> Injective testnet 结算与跨局 Strategy Revision 收敛到同一个 Current Game。
> 原计划中的 Native A2A Endpoint 顺延为 Phase E/Post-MVP。

- [x] 冻结目标拓扑：一名真实 Codex Connector 玩家与九名从官方池稳定随机抽取的
  PydanticAI Hosted Agent，完成八回合 `agent_a2a.v1` Current Game。
- [x] 冻结支付与学习验收链：
  `Intent → RFQ → Select → Negotiate → Deal → SettlementIntent →
  PaymentMandate → Injective confirmation → InventoryCommit → ranking →
  learning job → next-game revision`。
- [x] D1：为 Current Game 增加 allowlisted、版本化 `market_protocol` 部署配置；
  新 Game 冻结 `fcfs.v1 | agent_a2a.v1`，活动和历史 Game 不被改义，并保留只影响
  下一局的 `fcfs.v1` 回滚。生产 Worker、Compose、env generator、release wrapper
  和 GitHub Environment variable 已贯通，定向生命周期/发布测试通过。
- [x] D2 前置：payment-enabled Hosted canary 不再写死三回合 `fcfs.v1`；
  `CANARY_MARKET_PROTOCOL` 严格限制为两个已知版本，`CANARY_ROUND_COUNT` 严格限制
  为 1–10，并把冻结协议和回合数纳入验收摘要。该 harness 能运行八回合
  Hosted-only A2A canary，但不等同于下一项混合 Runtime 证据。
- [x] D2 中间验收：`phase-d-mixed-musdc-v4-c30a038913` 以一名真实 Codex
  Connector CLI 0.146.0 和九名 DeepSeek V4 Flash PydanticAI Hosted Agent
  完成八回合 `agent_a2a.v1`。92 个 Task 全部 applied，其中 91 completed、
  1 defaulted；共产生 65 Intent、6 RFQ、3 Engagement、3 Deal、3 个
  `inventory_committed` SettlementIntent 和 10 条排名。三笔自建 Facilitator
  mUSDC testnet 交易为
  `0x1d76b460ea120e723c9eb5c3851d0fddbfc01449784739c157d0e95517919a18`
  （block 135896825，2,000,000 atomic）、
  `0xe45b8c816d0e6c8245ef18eb8835e7ccfba5241e9fed1439f4f10dde35d5db10`
  （block 135896994，2,000,000 atomic）和
  `0x5a9b8ce5a5306c1ec4c682baa111e15eff064668002e1a0687913886d560b5a2`
  （block 135897417，2,525,000 atomic）。Blockscout 均返回 `ok` 和
  `transferWithAuthorization`；Arena 均保存 3 confirmations 后提交库存。
  Round 1 买方的下一回合快照由 `cash=20/grain=0` 变为 `18/1`，卖方由
  `0/10` 变为 `2/9`；Round 6 的 2.525 mUSDC 提交也在 Round 7 快照和终场
  排名中可见。
- [x] D2 正式币生产验收：Current Game
  `game-20260806-110040-099857d6f841` 由一名真实 Codex Connector 与九名
  DeepSeek V4 Flash PydanticAI Hosted Agent 完成八回合
  `agent_a2a.v1`。89 个 AgentTask 全部
  `completed/succeeded/applied`，包括 `80 Intent / 3 RFQ / 3 Select /
  3 Negotiate`；形成 3 个 Engagement/Deal 和 10 条排名。三笔正式测试游戏币
  `arena402-g` SettlementIntent 均经自建 Facilitator 到达
  `inventory_committed`：iron `5,000,000` atomic
  `0x558d105b8d40c9f8d10f070d468f82dba7886b6c78a6bb02f37a484099bd83cf`
  （block 135914653）、grain `2,105,600` atomic
  `0x1c63c5716a6eee78ebf48990488f0e7807641110d2361fb8e204496721326f6a`
  （block 135914656）、gems `3,000,000` atomic
  `0x7f3254497f16f6323d72d15373bcb2498d5822a90fcc769f9b23173cf856e68c`
  （block 135914967）。Blockscout 均返回 `ok` 和
  `transferWithAuthorization`；终场排名读取已提交库存，Codex 位列第 6。
  九个 Hosted learning job 随后全部终态化，5 个 learned revision 激活、4 个
  候选由门控拒绝，9 条 evaluation 的 outcome score 为
  `-478..378 bps`。该证据完成 `arena402-g`、混合 Runtime、A2A、支付与赛后学习
  的同局验收；不替代公共第三方 Facilitator 或 100 Agent 容量验收。
- [x] D3：成交参与者在完成局后生成 durable learning evaluation；后续 Game
  冻结新 revision，同局不切换，未成交 Agent 不学习，官方 Agent 的持久
  `agent_id`、archetype、revision history 与每局独立 Game Memory 可审计。
  上述八回合局的九个 Hosted learning job 中，成交的 priority 3/5 Agent
  分别激活 revision 9/8；六个无本人经济信号的 Agent 在模型前确定性拒绝，
  一个成交方因 DeepSeek 连续两次无效结构化输出按两次上限失败。
  `phase-d-revision-freeze-v1-df1bea4ee0` 随后以新的 1+9 payment-disabled
  Game 冻结并实际使用 priority 3 revision 9 与 priority 5 revision 8，
  完成 14 Task、1 Deal、0 SettlementIntent 和 10 条排名。
- [x] 修复真实恢复边界：migration `067` 保留 learned revision 的非 learned
  策略 foundation；`068` 在权威 apply 与旧投影处拒绝超过六位小数，
  `069`–`070` 只恢复已证明的精度/过期任务失败 Run，Task Factory 只允许按原
  idempotency key 重取完全相同的过期任务；`071` 恢复旧 learner 重复拼接造成的
  大 foundation 溢出，`072`–`073` 补齐 SECURITY DEFINER 的最小列级权限并仅
  恢复全部 Result 尚未应用的权限失败 Run。
- [x] D4 生产切换与恢复验收：生产 Official pool 已在维护窗口切到
  `official-deepseek`，正式局实际抽取的九个 Hosted 席位全部冻结
  `deepseek/deepseek-v4-flash`；玩家和九个 Official 席位的
  owner/allowlist/mint provisioning 全部到达 READY。第一场正式币 FCFS 回归中，
  Connector 在漏传 `--task-transport mcp` 后按 deadline 安全 default，使用正确
  MCP 参数重连后后续 Task 恢复；Arena Worker 中途重启后同局继续，Settlement
  Worker 在 Intent 冻结后、提交前重启仍只产生一个 tx 和一次 inventory commit。
  随后的整机重启在两局之间执行，Current Game 指针、四个 Hosted Worker 和本地
  Connector 均恢复；该项不冒充活动局中途整机重启。
  后端功能发布 `01c13805ad32cbe33765feb6c1b18967d9bd595b`
  经 GitHub protected production environment 完成，archive SHA-256 为
  `a338cc09b231c2cd9310fa188a64d3306df1587d53700885d091580cf9065073`，
  回滚目录为 `/opt/arena402.pre-01c13805ad32-20260806T114506Z`，数据库备份为
  `/var/backups/adx/adx_20260806T114506Z.sql.gz`；public health、Current Game
  和 SSE 均通过。外部 Vercel 前端 `a0b33d665de952ec569e38e8e2f4071d3fde6a88`
  已在同一权威 Game 上验证 Intent 目录、RFQ Engagement、谈判文本、支付四阶段
  和终场排名；历史 FCFS Game 仍由冻结协议回放。
- [ ] D5a 市场质量：保持 `agent_a2a.v1` 的 Intent → RFQ → Engagement →
  Negotiation → Deal 合同不变，先为每回合持久化不暴露私有限价的流动性摘要，
  区分 `pass`、无同商品反向 Intent、限价不兼容、未发送 RFQ、RFQ 未 Engage、
  协商未接受和结算失败。随后以版本化 Strategy Catalog 增加稳定私有估值、
  库存影子价格、现金约束、剩余回合偏好和拥挤度反馈；未成交结果只进入安全
  Game Memory 和探索门控，不被伪装成经济收益学习。
  首个纵向切片已实现 `arena.market-liquidity.v1`：
  A2A Round close 幂等发布 `market.liquidity_summarized`，包含 participant、
  Intent、pass、按商品买卖数量、同商品反向理论容量、限价兼容理论容量和最小
  未匹配数；payload 不包含 Agent identity 或私有限价。
  2026-08-07 已把该摘要连入下一回合不可变 AgentTask，并同时接通
  `roundCount / roundsRemaining`、自己的历史 applied action、仅
  `inventory_committed` 的历史成交以及从 Pairing 终态幂等推导的真实
  `failedNegotiations`；Hosted/Connector retry 继续复用同一 input hash。
  生产八回合观察进一步修复了两个系统恢复缺口：首回合启动现在与后续回合一样
  先写入幂等 `round.started`；生产部署在迁移前停止 Hosted/Arena/Settlement
  等任务领取 Worker。前向 migration `080` 只识别事件账本中已证明的旧
  context-query 失败、无任何 AgentTask/业务进度的 Run，在同一事务中重置
  Arena/Public round deadline、重排 Run 并写入 recovery queue event，不改写
  已应用的 `079`。
- [ ] D5a 价格与事件：先用当前 `2/5/8/3` 起始价建立对照基线，再增加冻结到
  Game 的 `price_catalog_id`、基础价格快照和 `pawnhouse-standard-v2` 事件牌组。
  新牌组降低常规事件相对私有估值分布过大的单边冲击，增加临时、传闻、基本面、
  反转和跨商品事件；订单流只以有界、确定性的下一轮参考价反馈吸引对手，不由
  Arena 强制成交。不得原地修改 `pawnhouse-standard-v1` 或历史 Game。
  兼容骨架已完成：新 Game 冻结 `pawnhouse-price-v1`、四个原子基础价格和
  `arena.market-feedback.v1`；`WorldState` 与 `PRICE_RESET_TO_BASE` 从该 Game
  快照重放，Join preflight、默认组合和 `balanced_auto` 也按相同价格保持等值
  20 金；缺少新字段的旧 Game 回退到原 v1。2026-08-07 已注册仅供显式实验
  选择的 `pawnhouse-price-v2`（`2.5/4/6/3`）和
  `pawnhouse-standard-v2`（十张、四商品双向、单次不超过 10% 的温和冲击）；
  `STANDARD_*`、Current Game 和生产配置均未切换。
- [ ] D5a A/B：在相同人数、回合数、初始净值和 event seed 控制下比较当前基线
  与新 Strategy/Price/Event 组合，至少报告非 pass Intent、同商品反向容量、
  限价兼容量、RFQ/Engagement/Deal/Settlement 漏斗、方向熵、反价率、对手覆盖、
  archetype 收益和事件/商品集中度；不能只以成交数作为成功标准。离线
  `arena.market-quality-experiment.v1` 成对评估器与
  `scripts/run_market_quality_ab.py` 已完成，能 fail closed 校验实验设计并输出
  匿名聚合报告；`scripts/export_market_quality_ab.py` 也已能从两个明确指定的
  已完成 A2A Game 在只读事务中导出 manifest。真实 baseline/candidate 多 seed
  对照局及其样本仍未完成。首个同 seed Hosted A/A 已完成：严格有效基线为
  58 Intent、22 pass、44 buy/14 sell、6 RFQ、3 Engagement、3 Deal，
  8 回合只有 3 回合具备可交叠容量，且全部来自 grain；另两局分别观察到
  11 个 deadline default 和 1 个 permanent-request default，因此 Provider
  timeout 必须与策略效果分开报告，不能用 default 局选择 V2。
  首个真实 `liquidity_v2 + pawnhouse-standard-v2` treatment 也已完成：
  Intent `58→66`、pass `22→14`、可交叠容量 `3→9`、可成交回合
  `3/8→6/8`、RFQ `6→17`、Deal `3→7`，并首次让 gems 形成双边市场；
  iron/warhorse 仍无卖方。该局的 12 个终态经复核均为 default，其中主要是
  `invalid_structured_output`，真实 deadline timeout 为 0；旧导出器把所有
  `defaulted` 错算为 timeout，现已拆分为 default、structured-output failure
  和真实 timeout 三个指标。
  PydanticAI/DeepSeek 输出链现采用“强制一次只读分析工具 → JSON Object 终态”，
  Official LiteLLM 固定透传 non-thinking，单请求 action 输出上限为 2048，
  Agent run 上限为 7 requests / 8 tool calls / 4 output retries。最终同 seed
  `market-treatment-v2-h-20260807` 严格通过：115/115 AgentTask completed、
  0 default、0 timeout、64 Intent、15 RFQ、8 Engagement/Deal，task wall
  P50/P95/P99 为 `9.1s/21.4s/28.1s`，Harness 退出码 0。前一复验局为
  126 completed、1 structured-output default、0 timeout、11 Deal，说明当前
  已达到受控线上评测门槛，但仍需多 seed 统计 default、成交随机性和调用成本，
  尚不能关闭 D5a。生产 Official Agent 的 bootstrap/refresh 实现现已统一选择
  `arena.official-market-strategy.liquidity-v2`，但只允许在局间刷新，已加入或
  运行中的 Game 继续冻结旧 revision。生产切换仍应保留回滚点并单独验收。
  2026-08-07 的后续 Current Game 在 Round 6 暴露 Coordinator 与后台市场投影
  Worker 同时处理同一 RFQ Result 的竞态：后到者可能在看到回执前先看到已经递增
  的 RFQ session，误报 `agent_market_rfq_attempt_sequence_invalid`。投影边界现
  以 Result ID 的事务级 advisory lock 串行化，并在锁后重读 durable receipt；
  migration `078` 只重排同时存在“已投影 RFQ”和“completed/pending RFQ”的
  `runtime_pawnhouserepositoryerror` Match Run，不泛化恢复其他 Repository 错误。
  Production Worker 同时补接通通用 Arena Result Consumer；若恢复时 Round 已经
  关闭，迟到的已应用市场 Result 会获得 `market_stage_closed` no-op receipt，
  不重新打开历史 RFQ，也不残留 `apply_status=pending`。
- [ ] D5b 容量：市场质量的功能正确性通过后，再完成 12/25/50/100 Agent 分档、
  每 Runtime/Task 至少 100 条真实终态样本、4 Facilitator shard 和
  timeout/公平性冻结。
- [x] Phase D 的统一功能链已由同一场权威 Game
  `game-20260806-110040-099857d6f841` 完成，不再由 FCFS Hosted 支付 canary、
  payment-disabled Codex A2A 或历史交易拼接声明。D5a 的市场质量、策略/价格/
  事件 A/B，D5b 的 12/25/50/100 Agent 容量、正式 timeout/公平性冻结，以及
  公共第三方 Facilitator仍是独立未完成验收，不属于本次 1+9 功能链完成声明。

## Product narrative baseline

Arena 402 的对外叙事固定为三层：

- **游戏**：王城典当行中的公平开局、事件驱动市场、Intent/RFQ 市场发现和有限轮协商；
- **评测场**：相同规则、起始资产、事件牌组和排名口径下，比较 Agent 的决策、
  速度、风险判断与谈判质量；
- **agentic payment 实验场**：把接受报价、冻结支付意图、链上确认和库存提交
  作为不同状态，逐笔保留可复核证据。

文档中应严格区分三种状态：本地开发闭环已验证、testnet 结算基础已实现、以及
新鲜 live testnet/公网生产验收已完成。第三种状态不能由 Fake E2E、历史交易恢复、
`accepted_pending_settlement` 或 Provider success 推导出来。

## Clean-slate implementation status

- [x] Milestone 1: King's Pawnhouse world, four goods, exact 20-gold
  portfolios, restricted event DSL, round state, valuation, and ranking.
- [x] Milestone 2: PostgreSQL pool entries, database-clock FCFS pairing,
  three-turn public negotiation, deterministic Rule Runtime, dev HTTP control,
  and public timeline.
- [x] Milestone 3: two isolated users and two Hosted Agents through the
  durable validation worker, immutable AgentTask/Result path, Result Sink,
  database-clock FCFS pairing, and sequential public negotiation.
- [x] Milestone 4 foundation: accepted negotiation freezes a single-payment
  testnet SettlementIntent; a local EIP-3009 bridge has an explicit human
  confirmation gate; read-only chain recovery verifies the exact ERC-20
  transfer; Arena commits cash and inventory exactly once only after a
  persisted confirmation.
- [x] Milestone 4 live acceptance: explicitly approved fresh testnet transfer,
  public transaction evidence, and recovery-driven inventory commit.
  Verified on 2026-07-27 for Intent
  `sha256:bc6cbaaae93403dc934a4b8c1d22618c645e91fd63f9eca24186596502577f93`:
  the self-hosted Facilitator submitted
  `0x2c2d708fc41c5f6ce7e866b187b21506c210a69e6524588f7e8bbc60f22a1e45`,
  transferred `2500000` atomic `arena402-g` on `eip155:1439`, persisted chain
  confirmation, and committed the frozen grain trade exactly once.
- [x] Public trade ledger: cross-Game SettlementIntent projection with
  game/Agent/good filters, opaque cursor pagination, backend-owned
  chain/Explorer metadata, persisted block/confirmation/Facilitator receipt
  fields, confirmed-only aggregate totals, and settlement-account disclosure
  for direct ERC-20 Transfer verification.
- [x] Milestone 5 foundation: separate Hosted Worker, Credential Controller,
  Arena Coordinator/Deadline Finalizer/settlement recovery process,
  least-privilege database logins, fail-closed profiles, and operator runbook.
- [x] Milestone 5 live acceptance: single-host AES-GCM credential vault, real
  DeepSeek Provider credential, permission-denial evidence and between-Game
  server restart/offline continuity have been accepted. Active-Game full-host
  restart remains a separate recovery test; Tencent CAM/SSM remains an optional
  higher-security acceptance.
- [x] Milestone 6: durable backend-only N-round orchestration, one event per
  round, automatic Hosted/rule execution, four-good FCFS pools, pairing-group
  concurrency, settlement-gated round close, per-round portfolio snapshots,
  frozen final prices, terminal ranking, and completed Game state.
- [x] Milestone 7: versioned deterministic event deck for 1–10 rounds,
  persisted schedule recovery, frozen per-Game participant limit without a
  repository-wide upper bound, with
  database enforcement, batch invitation issuance, and 12-agent local Hosted
  execution.
- [x] Concurrency hardening foundation: Connector entity-level incremental
  persistence, batched decide-result polling/application, batched FCFS pairing
  writes and Deadline Finalizer, shared per-Game SSE fan-out, bounded API
  database pools, a dedicated single-worker Connector/WebSocket service plus
  multi-worker stateless API, cross-replica Provider admission/fair
  scheduling, Runtime Run lease fencing/renewal, broadcast/confirmation
  decoupling, readiness and Prometheus metrics, plus a read-only load probe.
  This is implementation evidence, not 100-Agent production capacity
  acceptance.
- [x] Database P0 hardening: API、Connector API、Arena Worker、Hosted Worker 和
  Settlement Worker 在接流量或领取任务前验证镜像内完整 migration manifest；
  `/api/ready` 同时检查 migration 名称、缺失/额外记录和 SHA-256，`053` 仅向运行
  角色授予 registry 只读权限。Game Orchestrator 改为一条 set-based SQL 只发现
  当前可执行 transition，不再按所有 running Game 每 250ms 执行 N+1 状态查询。
  2026-07-31 已在全新 002–053 临时库验证受限角色与真实 SQL，并在现有 12 个
  running Game 上只读证明新旧 actionable 集合一致。
- [x] Founding 402 backend foundation: isolated soulbound ERC-721 contract,
  402 pre-generated testnet wallets, deterministic first-402 GitHub
  registration allocation, public-only inventory import, authenticated user
  status/public aggregate APIs, and review-gated asynchronous mint manifests.
- [x] Founding 402 claim launch: the soulbound ERC-721 is deployed on Injective
  EVM testnet, all 402 public wallet addresses are active, GitHub registrations
  allocate ranks and token IDs, and the external claim/status UI is live.
- [x] Founding 402 mint acceptance: the opt-in real-time minter uses an isolated
  read-only owner-key mount, a single-process advisory lock, deterministic
  signed-transaction recovery, and automatic receipt recording. On 2026-07-27,
  explicit human approval authorized a bounded production run that minted token
  IDs `0..11` in 12 confirmed Injective EVM testnet transactions. Blockscout
  receipts, on-chain `ownerOf` results, and PostgreSQL records were reconciled
  before the minter container was removed. Continuous minting remains disabled.

The Milestone 2 demonstration is:

```powershell
docker compose -f docker-compose.local.yml up --build -d
python scripts/run_rule_pawnhouse_demo.py
```

The Milestone 3 local demonstration uses two fresh one-use invitations and:

```powershell
$env:ARENA_BUYER_INVITE="<first invite>"
$env:ARENA_SELLER_INVITE="<second invite>"
python scripts/run_dual_hosted_pawnhouse_demo.py
```

Verified local evidence on 2026-07-25: four Hosted tasks completed and were
applied (`buy`, `sell`, `propose 7.000000`, `accept`); their private Attempt
records retained provider/model, thinking-enabled, duration, token counts, and
usage completeness without reasoning text. The public timeline contained two
decisions, one FCFS pairing, and two negotiation messages. The final pairing
and negotiation state was `accepted_pending_settlement` at
`7000000` atomic gold. This verifies the development Runtime/Arena boundary,
not production Secret Manager, a real external model, or chain settlement.

An accepted negotiation is deliberately terminal only at
`accepted_pending_settlement`; no balance or holding changes before confirmed
settlement.

The Milestone 4 no-broadcast demonstration is:

```powershell
python scripts/run_dual_hosted_pawnhouse_demo.py --with-settlement-intent
```

Verified local evidence on 2026-07-25: the dual Hosted Agent flow froze one
immutable intent at `authorization_requested`; balances and holdings were
unchanged, and there were no submission, confirmation, or inventory-commit
records. A rollback-only PostgreSQL verifier proved the confirmation-gated
cash/holding deltas and replay idempotency. A read-only Injective testnet
recovery check also matched a historical successful ERC-20 transfer. No fresh
transaction was signed or broadcast.

The Milestone 6 backend-only demonstrations are:

```powershell
python scripts/run_full_pawnhouse_game_demo.py

$env:ARENA_BUYER_INVITE="<fresh first invite>"
$env:ARENA_SELLER_INVITE="<fresh second invite>"
python scripts/run_full_hosted_pawnhouse_demo.py
```

Verified local PostgreSQL evidence on 2026-07-25:

- eight Rule Agents completed five rounds across all four goods: 40 decisions,
  20 FCFS pairings, 60 negotiation messages, five portfolio-close snapshots
  per participant, four frozen final prices, and eight terminal rankings;
- two Hosted Agents completed five durable Runtime runs: 10 decisions, five
  pairings, 10 negotiation messages, five closed rounds, and two rankings;
- a real DeepSeek V4 Flash Hosted Agent, updated through the same-provider
  Runtime PATCH without resending its credential, completed five rounds
  against a scripted counterparty: 10 decisions, five pairings, 10 public
  negotiation messages, and 20 completed AgentTasks with no defaults;
- the same real Hosted Agent completed an accepted one-round negotiation at
  `7000000` atomic mUSDC and froze one Injective testnet SettlementIntent.
  The intent remains at `authorization_requested`; no transaction is counted
  as accepted evidence until explicit approval, broadcast, public
  confirmation, and recovery-driven inventory commit all succeed;
- an older accepted Hosted negotiation remained blocked in `settle` with one
  pending settlement. Automatic orchestration did not move inventory or skip
  the chain-confirmation gate.

The Milestone 7 larger-game demonstration is:

```powershell
$env:ARENA_HOSTED_INVITES = docker compose -f docker-compose.local.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1 --count 12 --json
python scripts/run_many_hosted_pawnhouse_demo.py --agents 12 --rounds 10
```

Verified local PostgreSQL evidence on 2026-07-25:

- 12 Hosted Agents completed five rounds with 60 decisions, 30 pairings,
  60 public negotiation messages, five round closes, and 12 rankings;
- the same load completed ten rounds with 120 decisions, 60 pairings,
  120 public negotiation messages, ten round closes, and 12 rankings;
- both runs used the local-only scripted Provider and deliberately rejected
  negotiations, so no fake settlement was created;
- a rollback-only real PostgreSQL check proved an accepted Game changes from
  `wait_settlement` to `advance_round` only after confirmation-gated,
  idempotent inventory commit. The synthetic confirmation was rolled back and
  no transaction was signed or broadcast.

> 状态：以下是 Phase D 前的 foundation 快照；当前权威完成状态见本文顶部
> `Hosted Agent Runtime v2` 与 `Phase D`，不得把本节较早的未完成措辞解释为
> 当前结论。

Arena 402 已完成 Hosted Runtime 与最多十回合、12 Agent Pawnhouse 游戏的本地开发闭环，并已建立
确认门控的 testnet settlement 和生产 Worker 边界。Local Connector 已完成
owner-scoped Arena Agent identity、参赛快照、Connector-owned session、数据库
Task dispatcher、typed Task/Result、durable result outbox、Gateway inbox、Arena
Result Sink 与 Hosted/Connector mixed-Runtime 回合编排，并完成默认关闭的 WSS
wake + stateless MCP Task Broker、启动/重连与 sequence gap 主动 cursor sync，
并通过隔离 Docker 的 WSS + MCP + PostgreSQL 协议 E2E；2026-08-02 已用真实
Claude Code 2.1.170 与 Codex CLI 0.146.0 完成一回合 Connector-only 比赛，
四项 decide/negotiate 结果均经 Result Sink 应用，并形成 FCFS pairing、proposal
和 accept。该隔离局未提供 PaymentMandate，故以 `settlement_disabled` 关闭且
0 链写入；后续 mixed-Runtime 恢复、payment-enabled Connector、通用
PaymentMandate 和生产实机验收已在 Phase D 完成。活动局中途整机重启、公共
Facilitator 与分档容量仍未完成。Hosted 方向以
[`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md) 和
[`hosted-arena-agent-implementation-plan.md`](hosted-arena-agent-implementation-plan.md)
为当前目标。

产品前端已迁移到
[`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，由
Vercel 部署到 `www.arena402.com`。后端 GitHub OAuth + PKCE、Session/CSRF Cookie
和安全回跳契约已实现。2026-07-26 已在公网验证真实 OAuth App 跳转、OAuth state
Cookie、前端直连 API、精确 Origin CORS 和带凭证预检；外部前端也已迁移到当前
Arena API。公开 Game Event SSE 与前端断线轮询降级已经实现，但腾讯云部署、
登录后创建 Agent/Join 的人工浏览器验收及新鲜 testnet 自动结算仍未完成。产品
前端只在外部 `sunruize93-cmyk/arena402` 仓库维护，本仓库不再包含 Web 服务。

王城典当行 clean-slate 后端闭环已经形成。`arena_game/`、`arena_core/` 与
PostgreSQL `arena402` schema 是游戏业务权威；旧 `matching/`、Supabase 业务
适配、ELO API 和根静态前端已删除。

## 目标垂直切片

```text
create game
  -> join equal-start agents
  -> broadcast event
  -> immutable arena.decide AgentTask
  -> action=buy/sell/pass through Result Sink
  -> FCFS pair
  -> action=propose/accept/reject through Arena Gateway
  -> accepted pending settlement
  -> PaymentMandate check + EIP-3009 testnet settlement
  -> confirmed inventory transfer
  -> final net-worth ranking
```

## 已完成基础

- [x] 王城典当行 Milestone 1：四种货物、20 金自由初始组合、六位定点金额、
      受限事件 DSL、5 回合固定演示事件表、事件 schedule commitment、回合状态机、
      终场估值和兵卒封王排名。
- [x] 新增隔离的 PostgreSQL `arena402` schema，包含 Game、Good、Participant、
      Balance、Holding、Event Schedule/Occurrence、Round、Price Snapshot、
      Game Event 与 Ranking 基础表。

- [x] FastAPI 组合根只挂载 Connector、Hosted Agent、Arena participation 与
      Pawnhouse 表面；旧 Agent/listing/intent/negotiation/ELO 路由已移除。
- [x] Python A2A/payment 边界类型、fixtures 和 mocks。
- [x] self-hosted Local Agent Connector beta：出站配对/WSS、Runtime
      discovery、typed command、durable receipt/event、PostgreSQL 控制面、
      onboarding 和部署工具。
- [x] Injective EVM testnet 环境验证。
- [x] EIP-3009-compatible mock stablecoin。
- [x] SettlementSDK mock/real adapter。
- [x] 买方授权、项目 Facilitator、nonce replay protection 和 direct
      `arena402-g` testnet transfer；mUSDC 路径保留为历史兼容验证。
- [x] 原 Compose 过渡壳曾覆盖登录/配对、Hosted/Local Agent 管理、Game
      Lobby、Game View、时间线和 Result 页面；产品 UI 迁至外部仓库后，本仓库
      已移除该壳。
- [x] Hosted Agent Spec/Plan 已形成，并完成活动文档对统一 `action` schema、
      Secret Manager BYOK 例外、单局唯一、Deadline Finalizer 和 PaymentMandate
      边界的 Phase 0 同步。
- [x] 版本化 `ArenaAgentTaskV1`、Decide/Negotiate action、
      `AgentTaskResultV1` 与 `AgentRuntimeDriver` 契约。
- [x] Arena Agent/Config/Binding/Game Agent/Task/Result/Attempt/Event 与
      credential job 的 `003` PostgreSQL migration、最小权限角色和 CAS 函数。
- [x] Task Factory、PublicOutputPolicy、Result Sink/Consumer、独立 Finalizer，
      以及 Memory/PostgreSQL repository。
- [x] 测试专用 SecretStore 权限分离 port、生产 fail-closed 组合和
      Provider/Model/thinking capability registry 基础。
- [x] 安全 Provider contract、完整错误脚本的 Fake Provider、确定性有界
      PromptBuilder，以及受 capability/deadline 约束的 DirectModelDriver 测试基础；
      每个 Task 最多两个 Attempt、无 Provider/Model/Runtime fallback，usage 缺失
      不伪造，request-sent unknown 不重放。
- [x] Hosted Agent 严格控制模型/service、显式 test-only Memory repository、
      `004` HTTP 幂等迁移、默认关闭的 capability/API 壳，以及保留 Local Connector
      的最小 `/agents` 创建 UI。

这些完成项已构成可运行的后端游戏闭环，但不代表真实支付或生产部署已验收。

## 当前缺口

- [x] 后端已完成 N 回合自动推进、事件揭晓、Round close、冻结终场价格和排名。
- [x] 生产已提供 Session + CSRF 保护的 Game Operator API：
      `GET/POST /api/v1/pawnhouse/games` 与
      `POST /api/v1/pawnhouse/games/{game_id}/start`；只有创建者可启动 Game。
- [x] 单一当前游戏后端 Phase 1 已增加 `024` 数据库单例权威指针和公开
      `GET /api/v1/games/current` 安全投影；接口将内部阶段映射为
      `WAITING / RUNNING / COMPLETED`，支持匿名缓存和登录态 `joinedByMe`，
      不返回 User、Runtime 配置或结算账户。
- [x] Arena Worker 已增加幂等 Current Game 生命周期循环：首次启动和上一局终态后，
      在事务级 advisory lock 内创建产品规格的新 Game 并原子切换单例指针；外部前端
      已接入 Current Game 三态、3 秒轮询、404 准备态和 RUNNING 自动观战，并在
      Phase D 的同一权威 Game 上完成 Vercel 公网投影验收。
- [x] 单一当前游戏的 Join v2 preflight、动态同局 Mandate payee、显式 Ready
      投影、Withdraw、阈值原子自动启动、公开交易账本和结果投影已经实现；安全
      对外契约以 `product.md`、`game-design.md` 和实际 API 为准。
- [x] Current Game Join 不再假设 Hosted Runtime：preflight 同时校验 ready 的
      Hosted 与 owner-scoped Connector route，统一 Join 按冻结的 Runtime Kind
      分流；Connector Participant 复用同一 PaymentMandate、Join Authorization、
      settlement account、game-coin provision 和 Ready 门控。Phase D 已经人工
      授权并完成真实 Connector 的 `arena402-g` 支付与库存提交验收。
- [x] Current Game Join v2 已支持玩家提交 `cashAtomic` 与四种货物数量；
      Arena 按冻结初始价校验总值严格等于 20 金并在 Join 时锁定。Current Game
      使用 `manual` portfolio mode，开赛不再用 `balanced_auto` 覆盖玩家组合；
      旧客户端省略组合时使用 `gameId + agentId` 确定性生成的一件货物与剩余现金
      等值组合，官方补位 Agent 使用同一兜底，避免默认状态没有卖方流动性。
- [x] Connector Binding 创建时自动注册 owner-scoped `arena_agents` 与
      `arena_runtime_bindings`，迁移会回填既有 Binding；缺少 Arena 专用 capability
      时 route 保持 `provisioning`。
- [x] 通用 Join API 在同一事务内写入 Runtime/config 冻结记录与
      `arena402.game_participants`、20 gold 初始组合和公开 joined event。
- [x] 公网单机加密 vault、真实 Provider Key 与服务器离线连续性已完成实机验收；
      Phase D 在两局之间执行整机重启并恢复 Current Game、Hosted Worker 和
      Connector。该证据不冒充活动局中途整机重启；腾讯 CAM/SSM 三身份保留为
      可选高安全验收。
- [x] Connector 已严格解析 `arena.decide` / `arena.negotiate` typed Task，并把
      deadline、binding epoch 和固定业务 prompt 传给本地 CC/Codex child。
- [x] Connector 已返回与 dispatch ACK 分离的唯一 typed AgentTaskResult；结果先写
      本地 durable outbox，再经 WSS/Gateway PostgreSQL inbox 进入 Arena Result Sink。
- [x] Local Arena Agent identity 创建、owner-scoped Connector route 解析、
      Connector-owned Arena session 启动、leased AgentTask 自动 dispatch，以及
      Hosted/Connector mixed-Runtime 回合编排已实现。
- [x] 增加默认关闭的 `wss + stateless MCP` Task transport：WSS 保留 Device
      presence、heartbeat、Session control 与安全 wake，MCP 使用短期
      Device/Binding/epoch token 和 PostgreSQL lease 提供 claim/status/submit/
      release/sync；submit 复用 Arena Result Sink，生产 `/mcp` 只路由到单 Worker
      Connector service。
- [x] Go Connector 已支持 `ADX_CONNECTOR_TASK_TRANSPORT=mcp`，在
      `task.available` 后完成 token exchange、claim、既有受管 Runtime 执行、
      durable result submit/ack，以及本地拒绝前的 lease release。
- [x] Go Connector 会从已认证 Device 的 hello 绑定快照恢复冻结 route，并在
      启动/重连和 Gateway sequence gap 时主动执行有界 MCP cursor sync；Gateway
      周期性重发未完成 Task wake 继续作为低延迟提示和额外恢复路径。
- [x] 隔离 Docker 协议 E2E 已覆盖全新迁移、登录/配对、WSS hello/session/wake、
      Device token、MCP discover/list/sync/claim/submit/status、PostgreSQL Result
      Sink 与零链写入。
- [x] 真实 Claude Code 2.1.170 与 Codex CLI 0.146.0 已通过宿主机 Connector
      参加隔离 Docker 的一回合 Connector-only 比赛；Codex `buy grain`、Claude
      `sell grain`，随后真实 Runtimes 完成 `propose` 与 `accept`。四项 Task 均
      成功返回并由 Result Sink 应用，Arena 形成一个 FCFS pairing 和两条公开
      negotiation message。该局 `authorizationMode=none`，因此以
      `settlement_disabled` 终结且 0 链写入，不构成支付证据。
- [x] Hosted/Connector mixed 比赛、真实 Codex 任务执行中断线重连和不重连的
      deadline default 已完成故障注入；恢复任务只生成一条 Result 并只应用一次。
- [x] 真实 lease-expiry takeover 与已持久化 terminal Result 的 durable
      outbox replay 已完成隔离故障注入。
- [ ] 仍需带 PaymentMandate 的真实 Connector settlement E2E。
- [x] Connector 进程重启会递增持久化的 `session_generation`，使原进程的
      Session 失效并用新的 session incarnation 重建；处理中 typed AgentTask 仅在
      旧 receipt 明确为 `connector_restarted` 时以新 Command 重试一次，总 Attempt
      仍限制为两次，普通 Command 的幂等语义不变。
- [x] typed AgentTask 已使用 Arena 专用隔离 profile：Claude 强制 no-tools、
      safe-mode、空 MCP、无会话持久化及严格 JSON Schema；Codex 强制独立临时目录、
      read-only sandbox、ephemeral、忽略用户 config/rules 及严格 JSON Schema。
      Arena Codex Task 额外使用代码固定且不注入 endpoint/credential 的
      HTTPS-only Provider profile，避免模型 WebSocket 超时后再回退 HTTPS；
      Generic managed task 不改变用户自己的传输配置。
      inventory 分离 installed/task-enabled/auth-status/compatible/isolation/
      local-ready，Gateway 与 Connector 交叉校验并对未就绪 Runtime fail closed。Codex CLI
      当前没有等价 no-tools 开关，该差异保留为明确限制。
- [x] 使用 Hosted scripted + 真实 Codex 跑通 mixed 比赛，并保存执行中断线重连
      与 deadline default 证据；Claude Code 待外部 API/证书路径健康后补跑。
- [x] 保存 terminal Result outbox replay 证据。
- [x] 保存 payment-enabled settlement 证据；Phase D 正式
      `game-20260806-110040-099857d6f841` 由真实 Codex Connector 完成一笔本人
      `arena402-g` settled trade，同局共三笔交易链上确认后提交库存。
- [x] PaymentMandate 已实现额度、期限、范围、撤销和幂等
      `reserve / consume / release`；自动路径由独立 Settlement Worker 执行。
- [x] 平台 `user_id` 永久绑定 platform-managed testnet guest wallet；`045`
      允许密码账号不依赖 GitHub subject，一局一次
      Mandate 授权后，每笔 accepted trade 自动结算；逐笔人工确认 bridge 仅保留
      为开发验证工具。
- [x] 当前自动完整链路已在显式确认和单 Intent Worker 约束下执行一笔新鲜
      Injective testnet 交易，并完成链上确认与库存提交；公共 Facilitator
      兼容性和完整生产验收仍须单独通过。
- [x] 后端已实现用户名/密码平台注册登录，以及可选 GitHub OAuth
      authorization-code + PKCE；两者使用现有 Session/CSRF Cookie，业务所有权
      统一使用内部 `user_id`。平台注册已默认开放，无需邀请码；
      `ADX_PUBLIC_REGISTRATION_ENABLED=false` 仅保留为运营侧紧急关闭开关。
      新建密码账号和首次 GitHub OAuth 账号进入纪念币领取页，已有账号按原目标
      进入平台。
- [x] 外部前端已完成 Next.js 仓库升级和当前 Arena API 迁移；Vercel→腾讯云
      OAuth 跳转、Cookie/CORS 公网基础联调已通过。
- [ ] 公开 Game Event SSE 与前端轮询降级代码已完成本地回归，尚待腾讯云和
      Vercel 部署后完成实时投影、登录创建 Agent、Join/开局和 testnet 自动支付
      的生产验收；本仓库前端过渡壳已移除。
- [x] 固定五回合事件表、版本化十张牌组、确定性 seed 洗牌、schedule
      commitment、结束后 seed 揭晓与冻结终场价格已实现。
- [x] `run_dual_hosted_pawnhouse_demo.py --with-settlement-intent` 可一条命令
      运行双 Hosted Agent 至冻结结算意图，并输出安全公开证据。

2026-07-25 真实本地 PostgreSQL/HTTP 验证：两个独立 Session 创建 Hosted Agent，
通过生产 Operator API create/list、通用 Join、创建者 start，Arena/Hosted Worker
自动完成 1 回合；终态为 `completed`，2 名参与者、2 条排名。

## 实施顺序

三个可独立验收的里程碑：

| 里程碑 | 完成含义 | 不代表 |
|---|---|---|
| M1 Runtime Foundation | BYOK、Hosted Agent、Driver、durable Task/Result 与离线 Worker | 已有完整撮合、协商或支付 |
| M2 Arena Integration | Hosted/Local/rule Agent 经同一 Gateway、快照、Result Sink 与投影完成 Decide/Negotiate | 用户离线后一定能自动付款 |
| M3 Offline Transaction Completion | PaymentMandate、testnet settlement、链上确认和库存提交 E2E | 主网或真实资金能力 |

### Phase 0：活动文档与边界

- [x] 批准 Hosted Spec 与 Implementation Plan。
- [x] 统一 Decide/Negotiate `action` schema。
- [x] 明确 BYOK 仅限 write-only ingress + 外部 Secret Manager。
- [x] 明确单 User 单局 Agent、配置快照、统一可校准 deadline。
- [x] 明确 Result Sink/Consumer/Finalizer 与 PaymentMandate 边界。
- [x] 保持 frozen specs、`docs/injective/`、archive 和兼容标识不变。

### Phase 1：契约、迁移与持久化基础

- [x] 建立无 Provider/DB 依赖的版本化 AgentTask、action、Result 和 Driver 契约。
- [x] 增加 Arena migration scope，以及 Agent/Credential/Config/Binding/Game Agent/
   Task/Result/Attempt/Event/provisioning/lifecycle job。
- [x] Task Factory 冻结 participant view/config/hash，PostgreSQL repository
      再与入局配置核对。
- [x] 实现 Result Sink、PublicOutputPolicy、Result Consumer 和 Deadline Finalizer
   contract。
- [x] 通过唯一约束、row lock、CAS 和 lease 保证单局唯一、终态唯一和最多应用一次。

### Phase 2：Secret 与 Provider capability

- [x] 实现 API write-only、Worker read-only、Controller revoke/delete-only 的分离
   SecretStore port。
- [ ] 接入并真实验证 Tencent Secret Manager/KMS，以及不同 CAM 身份。
- [x] 建立 server-side Provider/immutable Model/thinking capability registry。
- [ ] 完成跨 HTTP/DB/日志/Trace/公网 encrypted vault 的原 Key 泄漏验证；当前单元测试已覆盖
      secret handle、配置快照、Result/Event 和生产 Memory backend 禁用。Secret backend
   故障时 fail closed。

### Phase 3：DirectModelDriver 与 Provider Adapter

- [x] 用 Fake Provider 覆盖成功、429/5xx/transport、无效输出、usage 缺失和
      request-sent unknown。
- [x] 实现确定性 PromptBuilder 和纯执行 DirectModelDriver；thinking 只按
      capability 开关并记录数值 usage，不保留 reasoning text。
- [x] 每个 AgentTask 最多两个 Attempt，无 Provider/Model/Runtime fallback。
- [x] 已接入 DeepSeek/OpenAI-compatible 固定 HTTPS Provider Adapter，并完成
      真实结构化调用、五回合执行与 accepted negotiation；生产服务器出站验收仍
      属于 Phase 8。

### Phase 4：Hosted Agent API 与创建 UI

- [x] 实现严格 Credential ingress、Hosted Agent create/list/detail service，以及
      默认拒绝非 durable repository 的生产边界。
- [x] 增加 `004` owner/route 隔离的摘要幂等表与受限数据库函数；资源在业务事务
      commit 前 attach，`reserved` 重放可恢复同一 owner-scoped resource。
- [x] 增加 capability/readiness API 和显式依赖门控的 mutation router；主应用默认
      只暴露 `creationEnabled=false`。
- [x] 用户可在一个最小 `/agents` 表单一次提交两个幂等 API；原 Key 不回显、不进
      React state/storage，Local Connector 入口保留，Hosted-only 用户可不填
      Connector code 直接登录。
- [x] 实现生产 PostgreSQL control repository、单机 AES-GCM ciphertext vault
      与可选 Tencent SSM 组合；后续 Phase D 已完成公网真实 Key、刷新和两局间
      重启连续性验收，活动局中途整机重启仍是独立缺口。
- [x] 实现 owner-scoped、同 Provider 的 Hosted Agent Runtime `PATCH`：
      复用已验证 Credential，候选配置先经 durable validation，成功后原子切换，
      失败时保留旧配置与可用 Credential；活动 Game 继续使用 join 时冻结的快照。
- [ ] 实现 replace/revoke/revalidate/disable/join 的其余生命周期操作及并发锁定规则。
- [x] Phase 5 Hosted Worker 可恢复地完成 `provisioning -> ready/degraded`。

### Phase 5：Durable Workers（M1）

- [x] 独立定义 Arena Worker、Hosted Worker 与 Credential Controller，均无公网端口。
- [x] 使用 PostgreSQL queue/lease，比赛 Task、validation 与 lifecycle 分开领取。
- [x] Provider 请求发送前持久化 Attempt；unknown 不盲目重放。
- [x] Arena Worker 独立运行 Finalizer，Hosted Worker 宕机时仍可收敛 expired Task。
- [x] 本地双 Hosted Agent 在客户端脚本仅等待 HTTP 状态的情况下持续完成五回合。
- [ ] 在真实服务器关闭浏览器、重启进程并验证连续性与最小权限拒绝证据。

### Phase 6：Arena 与 Connector 接线（M2，进行中）

- [x] 先用确定性 rule Agent 验证 Game Core。
- [x] Hosted、Connector 与 rule Adapter 共用同一 AgentTask/Result schema；
      Connector 已实现 frozen route adapter 和 typed WSS 映射。
- [x] Connector `task.dispatch` ACK 与唯一 terminal Result 分离；只有 Runtime 的
      terminal structured result 进入严格 action parser，普通 stdout/Event 不作为动作。
- [x] terminal Result 使用本地 durable outbox、Gateway PostgreSQL inbox 与 Arena
      Result Sink；重复提交按 Task/Result hash 幂等恢复。
- [x] 实现 Local Arena Agent identity bridge、Arena session lifecycle、Task
      dispatcher 和 Hosted/Connector mixed-Runtime Round coordinator；`015`
      迁移增加最小 Local Agent 幂等函数和 mixed Runtime Run。
- [x] 在保持 WSS 控制面的前提下实现默认关闭的 stateless MCP 数据面；MCP ACK
      不直接改变 Game 状态，task claim 使用冻结 route/epoch，terminal result
      仍通过同一 Result Sink 和 Deadline Finalizer 收敛。
- [x] Go Connector 已接入启动/重连与 sequence gap 的主动有界 cursor sync；
      隔离 Docker 已保存 WSS wake、sync、claim、submit、status 与 Result Sink
      证据，且未触发链写入。
- [x] 补充真实 Codex Runtime 进程的 lease expiry、断线恢复与 durable result
      replay 隔离 E2E。
- [ ] 在外部多实例部署继续验证同一恢复矩阵；本地故障注入不等于生产验收。
- [x] FCFS 只使用 Result Sink 的数据库 `result_received_at`。
- [x] 实现完整 N 回合的持久化 Round、Pool、Pairing、Negotiation、Inventory、
      Event、Round portfolio snapshot、final settlement price 和排名闭环。
- [x] Arena Worker 自动排队每轮 Hosted/Connector task-driven Runtime；所有 Decide
      Task 先创建，分别由 Hosted Worker 或 Connector Dispatcher 按冻结 Binding
      领取，不同 pairing 并发协商、每个 pairing 内保持 turn 顺序。
- [x] 未结算的 accepted pairing 将回合保持在 `settle`，不会进入下一回合。
- [x] 建立公开协商/结算时间线与 owner-only usage/latency/Attempt 投影。

### Phase 7：PaymentMandate 与 Settlement

- [x] 每个 GitHub 平台 User 首次钱包读取或入局时永久绑定一个 `sandbox_guest` testnet
      wallet；后续 Game Participant 引用同一钱包。Arena 业务表只保存地址和不透明
      signer key 引用；隔离 vault schema 保存信封密文，不在游戏结束后把钱包重新
      分配给其他用户。
- [x] Settlement SDK 已建立最小 guest-wallet signer 接缝：调用方只提交稳定
      `walletId`、冻结公开地址和 EIP-3009 授权字段；内存 Fake adapter 仅在显式
      test-only 组合下启用，未配置 backend 时 fail closed；生产路径将仓库外 CSV
      逐项核对后一次性导入 AES-256-GCM 信封密文，运行时 signer 不再挂载 CSV。
- [x] 用户可通过认证 API 为已加入 Game 创建一次受限 Mandate，不做逐笔人工确认。
- [x] Official filler 钱包通过独立的 `platform_official` authority 接入同一
      PaymentMandate/x402 路径；不伪造 GitHub subject，Mandate 按 Game、
      Token、同局动态 payee、单笔/累计额度和 24 小时窗口受限。
- [x] 冻结 Mandate 的 Game/network/token、单笔/累计额度、Game 到期时间和撤销
      状态；payee 只能是同局 Arena 配对出的 seller。
- [x] 实现并发 Intent 的幂等 `reserve / consume / release`；PostgreSQL 锁定
      Mandate row，`settlement_intent_id` 唯一约束关闭重复占款，累计金额由数据库
      CHECK 和事务更新双重限制。
- [x] 单笔 EIP-3009 模式在 `accept` 后冻结唯一 `SettlementIntent`；同一
      Game-scoped Mandate 可自动授权多笔互相独立的 Intent。
- [x] Join Authorization 的 10 分钟有效期与 PaymentMandate 的 24 小时整局
      有效期已拆分；自动 Worker 在 `authorization_requested` 阶段找不到有效
      Mandate 时以 `payment_mandate_not_active` 安全终态化，不进入签名、
      Facilitator 或链上提交；Migration `076` 仅补齐 `adx_settlement` 对
      negotiation 终态化所需的 `SELECT, UPDATE` 权限；Migration `077`
      同步并回填 A2A engagement 的 `settled / settlement_failed` 终态，避免
      权威 Settlement 已结束但市场投影仍显示 `settling`。
- [x] 增加无公网端口的可选 testnet signer service 与 Settlement Worker，自动
      reserve、签名、x402 `/verify`/`/settle`、持久化 tx hash；`submitting` 之前
      写入 lease/ambiguity boundary，未知结果不会盲目重付。
- [x] Migration `044` 要求新签名尝试保存规范化
      `payment_payload_digest`，并以部署时从只读 CSV 校验出的 Facilitator EOA
      作为 PostgreSQL durable broadcast fence；同一 EOA 的广播跨 Worker/重启
      串行，`unknown` 在找到原交易前持续阻止重播。
- [ ] funding 与 Settlement 共用数据库化 relay EOA nonce allocator；2 笔 Intent
      可同时在途，但 nonce 分配/广播短暂串行，重启只以同一 nonce 恢复。
- [x] 本地 bridge 已验证现有 SettlementSDK/Facilitator；它保留为开发验证工具，
      不作为 Hosted 上线执行路径。
- [x] Arena Worker 只读恢复 submitted；unknown 保持额度锁定，自动按同一
      authorization 恢复仍是上线前缺口。
- [x] 链上确认后幂等提交现金和货物。
- [x] 自动路径可按同一 EIP-3009 authorization 恢复 unknown：RPC 精确筛选
      token/from/to/amount `Transfer`，RPC 缺失交易正文时从 Blockscout
      `raw_input` 复核 nonce；后续仍由统一确认 Reader 冻结两个确认并复核
      receipt/block hash 后才提交库存。生产故障切换演练仍单独验收。
- [x] revoke 阻止新 reserve；已 reserve/submitted 的 Intent 继续完成，不增加链上
      取消或退款路径。
- [x] Hosted Worker 无 signer 权限；长期 signer 仅拥有密文读取函数和独立
      `0400` KEK mount，CSV 只进入一次性 `wallet-admin` profile。API/Arena Worker
      只使用 bearer-authenticated 窄签名端口；支持只重包 DEK 的 KEK 版本轮换。

详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)，上线部署和实现
顺序见
[`hosted-arena-production-runbook.md`](hosted-arena-production-runbook.md)。

### Phase 8：前端、部署、E2E 与校准（M3）

- [x] 原 Compose 过渡壳的页面能力已迁交外部前端，本仓库已移除该壳。
- [x] 外部前端已完成对应页面、Vercel 部署及 API/CORS 端到端切换；Phase D
      正式 Game 已验证 Intent 目录、RFQ Engagement、谈判文本、支付阶段和排名。
- [x] 增加 owner-only 私有 Game 投影：认证 `GET /api/v1/games/{game_id}/me`
      返回初始/当前/终场资产、逐回合资产快照、真实 Pairing 信誉与最终排名；
      公共 Game state 同时投影事件参考价、已提交库存的成交统计、SettlementIntent
      状态和实时净值，不把 accept 或 chain confirmation 冒充库存完成。
- [ ] 在 owner-only 私有 Game 投影上增加独立 Realtime 推送；当前公共 SSE 与
      3 秒只读状态刷新继续承担观战流，私有字段不进入公共缓存或 SSE。
- [x] 在单机 Compose 中加入 Hosted Worker、Credential Controller 和 Arena Worker
      及独立权限。
- [x] 增加仅供 Official Agent 使用的私有 LiteLLM Gateway：
      `official-deepseek` 在公开 capability API 中隐藏，玩家 `deepseek` BYOK
      仍直连 DeepSeek；多个上游 key 作为同名 LiteLLM deployment 由
      `simple-shuffle` 在请求级分发。
- [x] 上游 DeepSeek key 与 LiteLLM token 均经现有 write-only Secret Store
      port 写入版本化平台专用 ref，不复用带短 TTL 的未绑定玩家 Credential；
      磁盘 manifest 只含 opaque secret ref 和模型别名。LiteLLM 不开放宿主端口，
      Proxy retry/fallback 均关闭，Arena 保留唯一的 AgentTask retry 语义。
- [x] 生产 Official pool 已在无活跃 Game 的维护窗口切换到
      `official-deepseek`；Phase D 正式 Game 的九个 Hosted 席位均冻结并执行
      `deepseek/deepseek-v4-flash`，旧 Game 的 Runtime/config 快照未被改义。
- [x] 增加独立数据库角色、无公网端口的 Settlement Worker；生产配置保持单个
      PostgreSQL、单个 API，Hosted Worker 以 4 副本 × 25 task slot 起步，
      Settlement Worker 以 4 个执行 slot 驱动 4 个独立 EOA Facilitator shard，
      不增加 Redis/Kafka/Kubernetes。
- [x] Current Game 代码、数据库新 migration 与生产默认值已把硬上限从 12
      提高到 100；`041` 删除旧部署遗留的 `current_game_check` 容量别名，历史
      migration 保持不变。
- [x] 2026-07-26 在腾讯云真实 PostgreSQL 与 Injective EVM testnet 上完成
      10 Official Agent、五回合生产批次：14 笔 provision 交易确认，一笔
      accepted trade 经 PaymentMandate、x402 V2、自建 Facilitator、EIP-3009、
      链上确认和库存提交完成闭环。
- [ ] Tencent Secret Manager、真实外部 Provider、公共第三方 Facilitator 和
      100 Agent 容量仍需分别验收；上述批次不证明这些边界。
- [ ] 重新做生产主机容量规划；旧 2C4G/70GB、10/12 Agent 验收只保留为回归基线，
      不能用于证明 100 Agent 容量。按 10/12/25/50/100 Agent 记录 P50/P95/P99、
      queue age、timeout、retry、Token、每轮 wall time 和资源占用。
- [ ] 依据 4 × 25 Hosted task slot 的真实 Provider wave 证据冻结统一
      `action_timeout_ms`；公式冻结为所有支持 Runtime/Task/目标负载端到端 P99
      最大值乘以 `1.25`、向上取整到 5 秒。生产单局默认开赛阈值 10、硬上限
      100，同一时间一局 active Game。每个支持的 Runtime/Task 组合至少保留
      100 个真实端到端样本，并验证合法 Task 的 deadline timeout 不超过 1%。
- [x] 增加 fail-closed `scripts/calibrate_action_timeout.py`：显式 Game allowlist，
      从 `arena_runtime_binding → connector_binding → connector_runtime` 解析真实
      Runtime 身份，以 Arena 的 created/first leased/result received/applied
      时间戳计算分组合 P50/P95/P99、timeout 和 retry；样本或成功证据不足、
      timeout 超标时拒绝输出推荐值。报告已增加逐 Game/Round/Runtime/Task 的
      launch skew、result receipt skew 和 stage wall time。
- [x] 2026-08-05 完成三个 payment-disabled 10-Agent Codex-only canary。
      `real-runtimes-a2a048b555` 验证 10 个并发 Intent；
      `real-runtimes-d95129aafc` 以五买五卖、四商品等值资产完成
      `10 Intent / 5 RFQ / 2 Select / 2 negotiate / 2 Deal`，整轮
      `82.11s`；`real-runtimes-61ba000c4b` 以 `fcfs.v1` 完成 10 个 Decide，
      整轮 `20.58s`。所有 Task succeeded/applied，0 timeout、0 retry、
      0 SettlementIntent、0 资产变更、0 链写入；`--runtime-kind codex`
      确保未执行 Claude 探针。中途资源快照为 API `63 MiB`、Worker `37 MiB`、
      PostgreSQL `107 MiB`、10 Connector 合计 `238 MiB`、当时 3 个 Codex
      子进程合计 `388 MiB`，不宣称为峰值上界。
- [ ] 与既有无故障局和两场十 Agent、八回合完整 Codex 游戏合并后，五类终态
      样本为 `decide=10 / intent=195 / rfq=79 / select=33 / negotiate=36`，
      均为 0 deadline timeout、0 retry。Intent 已超过 100 样本，但其余组合、
      12/25/50/100 Agent 分档和真实 Hosted Provider wave 仍未完成，统一
      `action_timeout_ms` 继续不冻结。
- [x] 隔离 API `/api/ready` 的 1000 请求串行基线已覆盖并发 25/50/64：
      P95 分别为 `63.72/107.47/161.51 ms`，错误率 `0/0/0.3%`，通过
      1%/500ms 门槛。并发 100 超过测试 Compose 的
      `ADX_API_MAX_CONCURRENCY=64` 并产生入口 503；该结果只描述 HTTP 控制面，
      不计入 AgentTask timeout 公式。
- [ ] 100 Agent `agent_a2a.v1` 场景记录各 Task/Result queue age、Intent/RFQ/
      Select 到达偏差、对手覆盖和 deadline；披露 Provider 限流与 Worker wave
      带来的平台排队差异。legacy `fcfs.v1` 仅继续验证其冻结数据库时间语义；未通过
      分档验收前不把该部署称为 Tournament 公平性验证。
- [ ] 冻结 `settlement_timeout_ms=600000`，先回归 10/12 Agent，再在 100 Agent
      最坏 50 笔 accepted trade 场景验证 4 shard 路由、在途并发、终态与恢复。
- [ ] authorization 有效期冻结为 420 秒，保留 180 秒做过期确认与恢复；
      `submitted_unknown` 不算终态，超时仍无安全证据时 Game 进入
      `settlement_recovery_required`、停止排名并使 MVP 验收失败。
- [x] 10 Official Agent 回归批次中的 accepted trade 已在开赛确认后无回合内
      人工操作地自动完成 reserve、签名、提交、确认和库存提交。
- [x] 保存本批脱敏交易、确认和库存提交证据，并继续准确标注 testnet direct
      settlement、自建 Facilitator 与公共 x402 兼容性边界。
- [x] 修复 Current Game 官方补位席位计算：`PENDING` provision Participant
      保留席位但不能参赛；开赛只激活 `READY` Participant，未 Ready 记录被取消，
      且回合快照和最终排名只读取 Ready/active 参与者。
- [x] 将 Runtime 成功结果继续视为候选动作，并在 Python 与 PostgreSQL CAS
      投影中统一拒绝库存/现金不足、无对手报价的 `accept`、买方首轮非报价以及
      末轮继续报价；分别收敛为 `pass` 或 negotiation timeout。
- [x] 下线 MVP 王宫征召中的未结算 Royal Order effect；空的 registration
      Current Game 可安全轮换到新牌组，已有参与者的冻结赛程不被迁移修改。
- [x] 为无自定义策略的 Hosted Agent 提供受限市场默认策略，把官方池升级为十种
      带现金保留、库存目标、商品排序和买卖阈值的数值画像，并将 Arena 动作输出
      预算按真实 DeepSeek tool loop 校准为非 thinking 8192、thinking 16384 Token；
      生产可在不重新接触 Provider key 的情况下刷新并重新验证官方配置。
- [x] 历史 Game 公共投影返回冻结优先的 `displayName + agentId`，独立前端结果页
      以 Agent 名称为主、短 ID 为辅，不再把 UUID-like `agentId` 当作名称。
- [x] FCFS 改为价格兼容订单内配对；Hosted Prompt 明确事件不得重复计价、
      全货物比较和保留价语义，越过自身限价的结构化动作只允许一次有界修正
      Attempt。`059` 移除了 Arena 强迫界内报价必须接受、界外报价必须按极限价
      反价的策略性规则；Arena 只保留顺序、末轮闭合、限价、余额和库存校验。
- [x] 产品 Current Game 默认从五回合调整为八回合，从十张版本化事件牌组中按
      Game seed 无重复抽取八张；固定五回合 Demo 和 1–10 回合配置能力保持不变。

### Phase 9：Post-MVP

- [ ] 按
      [`agent-driven-a2a-market-implementation-plan.md`](agent-driven-a2a-market-implementation-plan.md)
      将中心 `fcfs.v1` 迁移为版本化 `agent_a2a.v1`：Agent 自主发布 Intent、
      发现市场、发送 RFQ、选择请求和协商；Arena 只负责 Gateway、校验、并发占位、
      Deal 冻结与 Settlement handoff。该切换现在属于上文 Phase D，不再等待
      Native A2A。
- [x] Phase A foundation：已加入严格的 Intent/RFQ/Engage wire contracts、
      无策略协议状态机、`055_arena_agent_driven_a2a_market.sql` 持久化约束和
      不变量测试；它只证明协议、所有权、跨动作 Result 幂等、私有限价与
      Participant round-slot，不得描述为真实 Agent 自主撮合。
- [x] Phase A Runtime integration：`056_arena_agent_driven_runtime_tasks.sql`
      已使新任务类型共用 PostgreSQL AgentTask Repository、Result Consumer 和
      Deadline Finalizer；旧 `arena.decide/negotiate` 仍委托冻结的
      `fcfs.v1` apply policy。
- [x] Phase A Market projection：生产 Worker 会重扫尚无 receipt 的 applied
      market Result，并原子、幂等地投影 Intent、单目标顺序 RFQ 和带双方
      Participant round-slot 的 Engagement；公开事件不含私有限价。
- [x] Phase A Round integration：`057` 以 Game 冻结
      `market_protocol=agent_a2a.v1`，编排 intent → RFQ → select →
      negotiate；规则状态机不能误入该路径。此处记录 Phase A 当时 Current Game
      继续使用 `fcfs.v1` 的切换前边界；Phase D 已完成生产切换。
- [x] Phase B substrate：已加入 `arena.market.intent/rfq/select`、Hosted
      Prompt/Driver 结构化输出和 Local Connector 通用任务投递；Fake Provider
      测试只证明 transport/schema/Result Sink，不是真实 Agent 证据。
- [x] Phase B E2E：真实 Codex 验收已完成；Claude Code 的外部连接问题被隔离为
      非 Arena 阻塞。早期本机 Claude Code + Codex
      `agent_a2a.v1` Intent/Discovery 局中，买方上限 `3.600000` 低于卖方下限
      `4.300000`，因此按协议无 Engagement；另一次角色互换验证了 180 秒 deadline
      的确定性 pass。第三次局中限价区间已经相交，但 Claude RFQ 在约 291 秒后
      返回 `runtime_failed`；保留的 Runtime Event 显示 Claude Code 因
      `UNKNOWN_CERTIFICATE_VERIFICATION_ERROR` 无法连接其 API，并在客户端内部
      重试 10 次。该失败属于本机 Claude Code/API 连接环境，不作为 Arena 或
      Connector 机制 Bug；Arena 正确收敛为 `market_timeout`。随后使用两个独立
      Codex CLI 0.146.0 Connector 的 `real-runtimes-9efb7dc941` 已完成两条
      Intent、一条 RFQ、一条 Engage 和三轮 negotiation：买方先报
      `3.600000`，卖方还价 `4.500000`，超过买方 `4.200000` 上限后买方自主
      reject。7 个真实 Runtime Result 均成功应用，形成 1 个 Engagement、
      1 个兼容 Pairing、3 条 negotiation message、0 Deal、0 SettlementIntent、
      0 inventory commit 和 0 chain write。随后双 Codex 局
      `real-runtimes-e8c3b2d723` 在修复 Codex `accept` 回显兼容字段后，由买方
      `2.550000`、卖方 `2.900000`、买方自主 accept 形成 1 个带两个不同
      proposal/acceptance Result ID 的真实 Agent Deal。随后
      `mixed-fallback-7f15a77f8c` 以 Hosted scripted buyer/rejecting seller +
      真实 Codex CLI 0.146.0 seller 完成 mixed-Runtime 顺序 fallback：Codex
      自主发布 iron sell Intent，第二次 RFQ 后自主 Engage 并 accept；9 个
      AgentTask 全部成功应用，形成 2 个 Engagement、1 个 Deal、0
      SettlementIntent 和 0 链写入。该局结束后重启 API/Arena worker，session
      与各实体计数保持不变。随后 `mixed-fallback-a865aba66f` 在真实 Codex
      seller 的第二个 `arena.market.select` 已 leased 时终止并重启 Connector；
      修复 MCP command 未区分新 Session，以及旧 `session.start` receipt
      重放恢复失效进程 Session 的两个问题后，同一 Task 有 5 次 lease 事件、
      1 条 Result、1 次 apply，最终仍形成第二个 Engagement 和 1 个 Deal。
      `mixed-fallback-5f00bae33a` 在相同边界终止且不重连，使用全局统一的
      60 秒 action timeout，由 Finalizer 将 Task 精确收口为
      `defaulted/timed_out/applied/market_timeout`，第二个 RFQ 为 `expired`，
      0 Deal。两局均为 0 SettlementIntent、0 资产变更和 0 链写入。
      随后 `mixed-fallback-8af2ba9c8c` 在真实 MCP claim 前注入 5 秒 orphan
      lease；binding-scoped MCP worker 在到期后约 42 ms 接管，Task 记录两个
      worker、3 次 lease event、1 条 Result 和 1 次 apply。
      `mixed-fallback-4f99467b24` 则在 Connector 已将 terminal Result 写入
      本地 outbox 后拒绝第一次 submit；重启前本地 1 条、Arena 0 条，重启后
      本地清零且 Arena 只有 1 条权威 Result 和 1 次 apply。两局最终均形成
      Deal，且为 0 SettlementIntent、0 资产变更、0 链写。Connector transport
      Result ID 与 Arena 规范化 authoritative Result ID 按边界分别保留。
      Claude Code 待其外部 API/证书路径健康后补跑，不阻塞该阶段。
- [x] Phase C payment-disabled foundation：只有 buyer RFQ Result + seller
      Engage Result 才能物化兼容 Pairing/Negotiation；接受后冻结包含 proposal /
      acceptance Result ID 的 Deal，并复用现有 Settlement 边界。Fake E2E 与
      `real-runtimes-e8c3b2d723` 双 Codex E2E 均已完成一笔 Deal；真实局使用
      `authorizationMode=none`，所以谈判安全终结为 `settlement_failed`，且为
      0 SettlementIntent、0 inventory commit、0 现金/持仓变更、0 chain write。
      后续 Phase D 正式 `arena402-g` Game 已完成 payment-enabled Injective
      testnet A2A；本条保留为 Phase C 当时的 payment-disabled 证据边界。
- [x] Phase C protocol implementation：迁移 `060`–`061` 和
      Runtime/Coordinator 已将
      RFQ `openingPrice` 作为 Engage 后不可变的 Turn 1 proposal；每个 RFQ
      Task 只联系一个对手，冻结目录、尝试序号、最多三次尝试和两次
      Agent-selected fallback 均持久化，同一买方只能有一个 pending/engaged
      RFQ。busy、reject、selection/negotiation timeout 会释放下一次选择；
      accepted Deal 和 settlement failure 会关闭 RFQ session，不能 fallback。
      Fake scripted 局 `full-hosted-1785853139-cd4e22d1` 已验证 request/result
      级 binding proposal Deal、卖方直接 accept、0 SettlementIntent 和 0 链写入。
      三 Hosted scripted 局 `full-hosted-1785897607-5cd29355` 进一步验证首个
      卖家 reject 后，同一买方由第二个 RFQ Task 从冻结剩余目录选择另一卖家并
      accept：3 个 Intent、2 个 RFQ、2 个 Engagement、4 条 negotiation
      message、1 个 Deal、0 SettlementIntent。该局暴露并修复了旧 FCFS
      compatibility pool entry “每参与者每轮唯一”与多次顺序 Engagement 的
      冲突；`061` 为每个 A2A Engagement 建独立 compatibility entry，同时用
      partial unique index 保留 `fcfs.v1` 的原唯一约束。服务重启后 session
      保持 `completed / 2 of 3`，请求、Engagement、Deal 和 entry 计数均未增长。
- [x] Phase C payment-disabled protocol acceptance：Hosted + Codex mixed 顺序 fallback、
      终局 projection recovery、中途 reconnect 和 deadline default 已由
      `mixed-fallback-7f15a77f8c`、`mixed-fallback-a865aba66f` 与
      `mixed-fallback-5f00bae33a` 完成；lease-expiry takeover 与 terminal
      Result outbox replay 又由 `mixed-fallback-8af2ba9c8c` 和
      `mixed-fallback-4f99467b24` 完成。`mixed-fallback-87fc3f3217`
      进一步以两个独立真实 Codex seller 完成 Primary `engage → counter`、
      buyer reject、Secondary `engage → accept` 的两次顺序 RFQ；10 个
      AgentTask 全部 succeeded/applied，形成 1 个 Deal。API/Arena worker
      重启后保持 `10 tasks/results/applies、2 RFQ、2 Engagement、1 Deal、
      4 entries、completed 2/3`，且为 0 SettlementIntent、0 资产变更、
      0 链写入。
      完成真实 P95/P99 负载校准前不切换 Current Game。
- [x] Phase C full-game market terminalization：十个独立 Codex CLI 0.146.0
      Connector 在 `real-runtimes-4b8fd267d0` 完成八回合
      `agent_a2a.v1`：140 个 AgentTask 全部
      `completed/succeeded/applied`，形成 78 个 Intent、36 个 RFQ、
      11 个 Engagement/Deal、24 条协商消息和 10 条终局排名。迁移 `062`
      与 Round close/Game complete 双层幂等清理使每个关闭回合及终局的
      `open | reserved` Intent、`pending` RFQ、`active` RFQ session 和
      `reserved` round slot 均为 0；历史 `engaged` RFQ、Deal 与 consumed
      slot 保持不变。该局为 `authorizationMode=none`，因此仍是
      0 SettlementIntent、0 资产变更和 0 链写入。
- [ ] Future `agent_a2a.v2`：增加正整数有界数量、精确全量成交、无 partial
      fill 的新 schema 与 reservation/mandate/settlement/inventory 不变量；
      `agent_a2a.v1` 永久保持 `quantity=1`。
- [ ] Phase E 实现标准 Native A2A Endpoint Adapter，并完成
      Hosted/Connector/Native A2A 混合局；内部 WSS 或 Fake 状态机不得称为标准
      Native A2A。
- 100 Agent 单局与 4 Facilitator shard 的生产配置基础已落地；D5b 的
  12/25/50/100 Agent 分档、每 Runtime/Task 样本、四 shard 故障恢复和 live
  testnet 按本文顶部 Phase D5b 及
  [`hosted-arena-agent-implementation-plan.md`](hosted-arena-agent-implementation-plan.md)
  分阶段验收。300 active Agent、多局并发仍是 Post-MVP；
- Native A2A Endpoint Adapter；
- LangGraph/通用 Agent Studio；
- 多 Runtime failover；
- 长期记忆、主网、多链和高可用。

## 可降级但仍可交付

- 协商冻结为最多 3 个合并的 Agent 行动，不再以缩短为 1 轮作为交付降级项；
- 实时入池改为固定窗口批配对；
- 正式 schema 保留 4 种货物，演示时可只激活 1 种；
- LLM Agent 不足时用明确标注的 rule agent 补位；
- 逐笔链上提交改为包含多笔点对点 transfer 的批量交易，并保留 accepted
  trade 到链上事件的逐笔映射。

不能降级为纯数据库“假支付”，也不能使用无法还原逐笔成交的纯聚合净额。
默认 MVP 为一笔 accepted trade 对应一笔 testnet 转账；批量 fallback 需要
在真实吞吐证据表明逐笔提交不足后显式启用，并保留逐 Deal transfer、确认状态
和幂等库存提交证据。

## 后续而非 MVP 阻塞项

- 公共第三方 Facilitator 的真实 testnet x402 V2 兼容验收；
- TEE key custody 与 remote attestation；
- 链上身份或 ERC-8004 reputation；
- escrow、退款、争议、仲裁和生产手续费；
- 主网、多链和高可用多节点；
- Agent Studio、人格市场和长期赛季系统。
