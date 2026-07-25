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
- [ ] Milestone 5 live acceptance: single-host AES-GCM credential vault, real
  Provider credential, server restart/offline continuity, and permission-denial
  evidence. Tencent CAM/SSM remains an optional higher-security acceptance.
- [x] Milestone 6: durable backend-only N-round orchestration, one event per
  round, automatic Hosted/rule execution, four-good FCFS pools, pairing-group
  concurrency, settlement-gated round close, per-round portfolio snapshots,
  frozen final prices, terminal ranking, and completed Game state.
- [x] Milestone 7: versioned deterministic event deck for 1–10 rounds,
  persisted schedule recovery, configurable 2–64 participant limit with
  database enforcement, batch invitation issuance, and 12-agent local Hosted
  execution.

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

> 状态：当前跨模块实施状态与建议顺序。

Arena 402 已完成 Hosted Runtime 与最多十回合、12 Agent Pawnhouse 游戏的本地开发闭环，并已建立
确认门控的 testnet settlement 和生产 Worker 边界。Local Connector 游戏适配、
通用 PaymentMandate 与生产实机验收仍未完成。Hosted 方向以
[`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md) 和
[`hosted-arena-agent-implementation-plan.md`](hosted-arena-agent-implementation-plan.md)
为当前目标。

产品前端已迁移到
[`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，由
Vercel 部署到 `www.arena402.com`。后端 GitHub OAuth + PKCE、Session/CSRF Cookie
和安全回跳契约已实现；legacy Agent/listing/ELO client 到当前 API 的迁移、
真实 OAuth App、Vercel→腾讯云 Cookie/CORS 联调和部署验收尚未完成。本仓库
`frontend/` 仅保留为本地开发与显式 `legacy-web` profile。

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
- [x] 买方授权、项目 Facilitator、nonce replay protection 和 direct mUSDC
      testnet transfer。
- [x] 本仓库 Compose 过渡壳具备登录/配对、Hosted/Local Agent 管理、Game
      Lobby、Game View、时间线和 Result 页面；旧市场/ELO URL 只保留到 `/game`
      的兼容重定向。
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
- [x] Connector Binding 创建时自动注册 owner-scoped `arena_agents` 与
      `arena_runtime_bindings`，迁移会回填既有 Binding；缺少 Arena 专用 capability
      时 route 保持 `provisioning`。
- [x] 通用 Join API 在同一事务内写入 Runtime/config 冻结记录与
      `arena402.game_participants`、20 gold 初始组合和公开 joined event。
- [ ] 公网单机加密 vault、真实 Provider Key 与服务器离线连续性尚未实机验收；
      腾讯 CAM/SSM 三身份保留为可选高安全验收。
- [ ] Connector 尚未适配 `arena.decide` / `arena.negotiate`。
- [ ] Connector 尚未返回与 dispatch ACK 分离的唯一 typed AgentTaskResult。
- [ ] PaymentMandate 的额度、期限、范围、撤销和
      `reserve / consume / release` 尚未实现。
- [ ] Hosted 上线目标要求 platform-managed testnet guest wallet、入局一次授权和
      每笔 accepted trade 自动结算；当前逐笔人工确认 bridge 只用于开发验证，
      不能作为上线支付路径。
- [ ] 当前完整链路尚未执行一笔新鲜 Injective testnet 交易；现有实现停在显式
      人工确认闸门。
- [x] 后端已实现外部前端契约所需的 GitHub OAuth authorization-code + PKCE、
      不可变 GitHub subject 身份、现有 Session/CSRF Cookie 和安全回跳。
- [ ] 外部前端已完成 Next.js 仓库升级，但尚需移除 legacy Agent/listing/ELO
      API client，完成 Vercel→腾讯云 OAuth/Cookie/CORS 公网联调、Realtime 推送、
      完整 Game Operator UI 与生产级错误恢复；本仓库过渡壳已退出默认生产 profile。
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
      与可选 Tencent SSM 组合；公网真实 Key 与刷新/重启验收仍待执行。
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

- [ ] 每个 GitHub 平台 User 首次登录时永久绑定一个 `sandbox_guest` testnet
      wallet；后续 Game Participant 引用同一钱包，数据库只保存地址和不透明
      signer key 引用，不在游戏结束后把钱包重新分配给其他用户。
- [x] Settlement SDK 已建立最小 guest-wallet signer 接缝：调用方只提交稳定
      `walletId`、冻结公开地址和 EIP-3009 授权字段；内存 Fake adapter 仅在显式
      test-only 组合下启用，未配置 backend 时 fail closed，且没有 CSV/生产密钥接线。
- [ ] 用户加入 Game 时一次性创建受限 Mandate，不做逐笔人工确认。
- [ ] 冻结 Mandate 的 Game/network/token、单笔/累计额度、Game 到期时间和撤销
      状态；payee 只能是同局 Arena 配对出的 seller。
- [ ] 实现并发 Intent 的 `reserve / consume / release`；锁定 Mandate、buyer cash
      与 reservation rows，并以 `(round_id, buyer_participant_id)` 唯一约束关闭
      同一 buyer 的并发占款。
- [x] 单笔 EIP-3009 模式在 `accept` 后冻结唯一 `SettlementIntent`；Mandate
      模式仍待实现。
- [ ] 增加无公网端口 Settlement Worker，自动 reserve、签名、提交并持久化 tx hash；
      第一版直接复用 settlement library，不新增独立 HTTP Facilitator 服务。
- [ ] funding 与 Settlement 共用数据库化 relay EOA nonce allocator；2 笔 Intent
      可同时在途，但 nonce 分配/广播短暂串行，重启只以同一 nonce 恢复。
- [x] 本地 bridge 已验证现有 SettlementSDK/Facilitator；它保留为开发验证工具，
      不作为 Hosted 上线执行路径。
- [x] Arena Worker 可只读恢复 submitted/unknown。
- [x] 链上确认后幂等提交现金和货物。
- [ ] 自动路径按同一 EIP-3009 authorization 恢复 unknown；冻结两个确认，
      复核 receipt block hash、calldata 与 Transfer event 后才提交库存。
- [ ] revoke 阻止新 reserve；已 reserve/submitted 的 Intent 继续完成，不增加链上
      取消或退款路径。
- [ ] Hosted Worker、Arena Worker 与 Settlement Worker 的 IAM、数据库和密钥域
      完全分离。

详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)，上线部署和实现
顺序见
[`hosted-arena-production-runbook.md`](hosted-arena-production-runbook.md)。

### Phase 8：前端、部署、E2E 与校准（M3）

- [x] Compose 过渡壳已有 Game Lobby、Game View、Result 与公开投影。
- [ ] 外部前端完成对应页面、Vercel 部署及 API/CORS 端到端切换。
- [ ] 增加 owner-only 私有投影与 Realtime 推送。
- [x] 在单机 Compose 中加入 Hosted Worker、Credential Controller 和 Arena Worker
      及独立权限。
- [ ] 增加 Settlement Worker；首发保持单个 PostgreSQL、单个 API 和每类 Worker
      一个实例，不增加 Redis/Kafka/Kubernetes。
- [ ] 跑真实 PostgreSQL、Tencent Secret Manager、Provider 和 Injective testnet E2E。
- [ ] 现有 2C4G/70GB 生产机以 10 Hosted Agent × 5 回合为 MVP 必须通过，
      12 Agent 为非阻塞容量验证；记录 P50/P95/P99、queue age、timeout、retry、
      Token、每轮 wall time 和资源占用，16 Agent 推迟到扩容后。
- [ ] 依据 5 并发、10/12 Agent wave 证据冻结统一 `action_timeout_ms`；首发单局
      默认 10、上限 12、同一时间一局 active Game。
- [ ] 2C4G MVP 明确采用 `result_received_at` FCFS，并披露两个 Decide wave 的
      平台排队偏差；不把该部署称为 Tournament 公平性验证。
- [ ] 冻结 `settlement_timeout_ms=600000`，在 10 Agent 受控场景验证 5 笔
      accepted trade 按 2 + 2 + 1 wave 终态，并观察到 2 笔同时在途。
- [ ] authorization 有效期冻结为 420 秒，保留 180 秒做过期确认与恢复；
      `submitted_unknown` 不算终态，超时仍无安全证据时 Game 进入
      `settlement_recovery_required`、停止排名并使 MVP 验收失败。
- [ ] accepted trade 无人工操作自动完成 reserve、签名、提交、确认和库存提交。
- [ ] 保存脱敏发布证据，并继续准确标注 testnet direct settlement 与 x402 边界。

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
