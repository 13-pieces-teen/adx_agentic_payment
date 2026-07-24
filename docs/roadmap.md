# Arena 402 Roadmap

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
- [ ] Milestone 4 live acceptance: explicitly approved fresh testnet transfer,
  public transaction evidence, and recovery-driven inventory commit.
- [x] Milestone 5 foundation: separate Hosted Worker, Credential Controller,
  Arena Coordinator/Deadline Finalizer/settlement recovery process,
  least-privilege database logins, fail-closed profiles, and operator runbook.
- [ ] Milestone 5 live acceptance: real Tencent CAM/SSM identities, real
  Provider credential, server restart/offline continuity, and permission-denial
  evidence.
- [x] Milestone 6: durable backend-only N-round orchestration, one event per
  round, automatic Hosted/rule execution, four-good FCFS pools, pairing-group
  concurrency, settlement-gated round close, per-round portfolio snapshots,
  frozen final prices, terminal ranking, and completed Game state.

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
- an older accepted Hosted negotiation remained blocked in `settle` with one
  pending settlement. Automatic orchestration did not move inventory or skip
  the chain-confirmation gate.

> 状态：当前跨模块实施状态与建议顺序。

Arena 402 已完成 Hosted Runtime 与五回合 Pawnhouse 游戏的本地开发闭环，并已建立
确认门控的 testnet settlement 和生产 Worker 边界。Local Connector 游戏适配、
通用 PaymentMandate 与生产实机验收仍未完成。Hosted 方向以
[`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md) 和
[`hosted-arena-agent-implementation-plan.md`](hosted-arena-agent-implementation-plan.md)
为当前目标。

王城典当行 clean-slate 实现已经开始。`arena_game/` 与 `006` 迁移是新的游戏业务
内核权威；旧 `matching/` 不再作为目标实现。

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

- [x] Python 内存版 Agent、listing/intent、matching、有限 negotiation、
      Arena/ELO 和 FastAPI wrapper。
- [x] Python A2A/payment 边界类型、fixtures 和 mocks。
- [x] self-hosted Local Agent Connector beta：出站配对/WSS、Runtime
      discovery、typed command、durable receipt/event、PostgreSQL 控制面、
      onboarding 和部署工具。
- [x] Injective EVM testnet 环境验证。
- [x] EIP-3009-compatible mock stablecoin。
- [x] SettlementSDK mock/real adapter。
- [x] 买方授权、项目 Facilitator、nonce replay protection 和 direct mUSDC
      testnet transfer。
- [x] CDN-only 静态 Arena 前端、Supabase Agent/Battle/Market/ELO 视图。
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
- [ ] 生产仍缺经过认证的 Game Operator API。
- [ ] 生产 Tencent CAM/SSM 三身份、真实 Provider Key 与服务器离线连续性尚未实机验收。
- [ ] Connector 尚未适配 `arena.decide` / `arena.negotiate`。
- [ ] Connector 尚未返回与 dispatch ACK 分离的唯一 typed AgentTaskResult。
- [ ] PaymentMandate 的额度、期限、范围、撤销和
      `reserve / consume / release` 尚未实现。
- [ ] 当前完整链路尚未执行一笔新鲜 Injective testnet 交易；现有实现停在显式
      人工确认闸门。
- [ ] 前端尚无 Game Lobby、Game View、Result 和对应 Realtime 数据流。
- [x] 固定五回合事件表、schedule commitment、结束后 seed 揭晓与冻结终场价格已实现；
      可随机洗牌的事件牌组仍属后续扩展。
- [x] `run_dual_hosted_pawnhouse_demo.py --with-settlement-intent` 可一条命令
      运行双 Hosted Agent 至冻结结算意图，并输出安全公开证据。

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
- [ ] 完成跨 HTTP/DB/日志/Trace/真实 SSM 的原 Key 泄漏验证；当前单元测试已覆盖
      secret handle、配置快照、Result/Event 和生产 Memory backend 禁用。Secret backend
   故障时 fail closed。

### Phase 3：DirectModelDriver 与 Provider Adapter

- [x] 用 Fake Provider 覆盖成功、429/5xx/transport、无效输出、usage 缺失和
      request-sent unknown。
- [x] 实现确定性 PromptBuilder 和纯执行 DirectModelDriver；thinking 只按
      capability 开关并记录数值 usage，不保留 reasoning text。
- [x] 每个 AgentTask 最多两个 Attempt，无 Provider/Model/Runtime fallback。
- [ ] 接入至少一个真实、固定 HTTPS endpoint 的 Provider Adapter，并完成真实
      安全出站和结构化调用验证。

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
- [x] 实现生产 PostgreSQL control repository 与 Tencent SSM Secret Writer
      组合；真实 CAM/SSM 与刷新/重启验收仍待部署执行。
- [ ] 实现 replace/revoke/revalidate/PATCH/disable/join 及其并发锁定规则。
- [x] Phase 5 Hosted Worker 可恢复地完成 `provisioning -> ready/degraded`。

### Phase 5：Durable Workers（M1）

- [x] 独立定义 Arena Worker、Hosted Worker 与 Credential Controller，均无公网端口。
- [x] 使用 PostgreSQL queue/lease，比赛 Task、validation 与 lifecycle 分开领取。
- [x] Provider 请求发送前持久化 Attempt；unknown 不盲目重放。
- [x] Arena Worker 独立运行 Finalizer，Hosted Worker 宕机时仍可收敛 expired Task。
- [x] 本地双 Hosted Agent 在客户端脚本仅等待 HTTP 状态的情况下持续完成五回合。
- [ ] 在真实服务器关闭浏览器、重启进程并验证连续性与最小权限拒绝证据。

### Phase 6：Arena 与 Connector 接线（M2）

- [x] 先用确定性 rule Agent 验证 Game Core。
- [ ] Hosted、Connector 与 rule Adapter 共用同一 AgentTask/Result schema；
      Hosted/rule 已完成，Connector 游戏 Adapter 尚未接入。
- [ ] Connector `task.dispatch` ACK 与唯一 terminal Result 分离；不解析
   `runtime.message` 或 stdout 作为动作。
- [x] FCFS 只使用 Result Sink 的数据库 `result_received_at`。
- [x] 实现完整 N 回合的持久化 Round、Pool、Pairing、Negotiation、Inventory、
      Event、Round portfolio snapshot、final settlement price 和排名闭环。
- [x] Arena Worker 自动排队每轮 Hosted Runtime；所有 Decide Task 先创建，
      Provider Task 有界并发，不同 pairing 并发协商、每个 pairing 内保持 turn 顺序。
- [x] 未结算的 accepted pairing 将回合保持在 `settle`，不会进入下一回合。
- [x] 建立公开协商/结算时间线与 owner-only usage/latency/Attempt 投影。

### Phase 7：PaymentMandate 与 Settlement

1. 冻结 Mandate 的 Game/network/token/payee、单笔/累计额度、期限、撤销和签名域。
2. 实现并发 Deal 的 `reserve / consume / release`。
- [x] 单笔 EIP-3009 模式在 `accept` 后冻结唯一 `SettlementIntent`；Mandate
      模式仍待实现。
- [x] 本地桥接现有 SettlementSDK/Facilitator，并由 Arena Worker 只读恢复
      submitted/unknown。
- [x] 链上确认后幂等提交现金和货物。
6. Hosted Worker 与 guest signer 的 IAM、数据库和密钥域完全分离。

详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)。

### Phase 8：前端、部署、E2E 与校准（M3）

1. 增加 Game Lobby、Game View、Result 与公开/私有投影。
- [x] 在单机 Compose 中加入三个无公网端口 Worker 与独立权限。
3. 跑真实 PostgreSQL、Tencent Secret Manager、Provider 和 Injective testnet E2E。
4. 跑 2、4、8、16 Agent，记录 P50/P95/P99、queue age、timeout、retry、Token、
   每轮 wall time 和资源占用。
5. 依据证据冻结统一 `action_timeout_ms`、单局 Agent 上限和并发 Game 上限。
6. 保存脱敏发布证据，并继续准确标注 testnet direct settlement 与 x402 边界。

### Phase 9：Post-MVP

- Native A2A Endpoint Adapter；
- LangGraph/通用 Agent Studio；
- 多 Runtime failover；
- 长期记忆、主网、多链和高可用。

## 可降级但仍可交付

- 3 轮协商降为 1 轮；
- 实时入池改为固定窗口批配对；
- 3 种货物降为 1 种；
- LLM Agent 不足时用明确标注的 rule agent 补位；
- 逐笔链上提交改为包含多笔点对点 transfer 的批量交易，并保留 accepted
  trade 到链上事件的逐笔映射。

不能降级为纯数据库“假支付”，也不能使用无法还原逐笔成交的纯聚合净额。
默认 MVP 为一笔 accepted trade 对应一笔 testnet 转账；批量 fallback 需要
显式启用并保留逐笔链上证据。

## 后续而非 MVP 阻塞项

- 标准 HTTP x402 challenge/retry/header 和公共 Facilitator 兼容；
- TEE key custody 与 remote attestation；
- 链上身份或 ERC-8004 reputation；
- escrow、退款、争议、仲裁和生产手续费；
- 主网、多链和高可用多节点；
- Agent Studio、人格市场和长期赛季系统。
