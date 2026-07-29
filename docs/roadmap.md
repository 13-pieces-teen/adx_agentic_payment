# Arena 402 Roadmap

## Product narrative baseline

Arena 402 的对外叙事固定为三层：

- **游戏**：王城典当行中的公平开局、事件驱动市场、FCFS 配对和有限轮协商；
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
- [ ] Milestone 5 live acceptance: single-host AES-GCM credential vault, real
  Provider credential, server restart/offline continuity, and permission-denial
  evidence. Tencent CAM/SSM remains an optional higher-security acceptance.
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

> 状态：当前跨模块实施状态与建议顺序。

Arena 402 已完成 Hosted Runtime 与最多十回合、12 Agent Pawnhouse 游戏的本地开发闭环，并已建立
确认门控的 testnet settlement 和生产 Worker 边界。Local Connector 已完成
owner-scoped Arena Agent identity、参赛快照、Connector-owned session、数据库
Task dispatcher、typed Task/Result、durable result outbox、Gateway inbox、Arena
Result Sink 与 Hosted/Connector mixed-Runtime 回合编排；真实 CC/Codex 完整比赛和
生产重连 E2E 尚未验收。通用
PaymentMandate 与生产实机验收也仍未完成。Hosted 方向以
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
      本地代码已接入 Current Game 三态、3 秒轮询、404 准备态和 RUNNING 自动观战，
      但尚未部署到 Vercel 或完成公网 E2E。
- [ ] 单一当前游戏的 Join v2 preflight、动态同局 Mandate payee、显式 Ready
      投影、Withdraw、阈值原子自动启动、交易列表和结果接口
      仍按 `prd-current-game-backend.md` 顺序实施。旧 Participant 在这些校验落库前
      只显示为 `PENDING`，不计入 `readyCount`。
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
- [ ] 公网单机加密 vault、真实 Provider Key 与服务器离线连续性尚未实机验收；
      腾讯 CAM/SSM 三身份保留为可选高安全验收。
- [x] Connector 已严格解析 `arena.decide` / `arena.negotiate` typed Task，并把
      deadline、binding epoch 和固定业务 prompt 传给本地 CC/Codex child。
- [x] Connector 已返回与 dispatch ACK 分离的唯一 typed AgentTaskResult；结果先写
      本地 durable outbox，再经 WSS/Gateway PostgreSQL inbox 进入 Arena Result Sink。
- [x] Local Arena Agent identity 创建、owner-scoped Connector route 解析、
      Connector-owned Arena session 启动、leased AgentTask 自动 dispatch，以及
      Hosted/Connector mixed-Runtime 回合编排已实现。
- [x] Connector 进程重启会递增持久化的 `session_generation`，使原进程的
      Session 失效并用新的 session incarnation 重建；处理中 typed AgentTask 仅在
      旧 receipt 明确为 `connector_restarted` 时以新 Command 重试一次，总 Attempt
      仍限制为两次，普通 Command 的幂等语义不变。
- [x] typed AgentTask 已使用 Arena 专用隔离 profile：Claude 强制 no-tools、
      safe-mode、空 MCP、无会话持久化及严格 JSON Schema；Codex 强制独立临时目录、
      read-only sandbox、ephemeral、忽略用户 config/rules 及严格 JSON Schema。
      inventory 分离 installed/task-enabled/auth-status/compatible/isolation/
      local-ready，Gateway 与 Connector 交叉校验并对未就绪 Runtime fail closed。Codex CLI
      当前没有等价 no-tools 开关，该差异保留为明确限制。
- [ ] 使用真实 CC/Codex 跑通 Connector-only 与 Hosted/Connector mixed 完整比赛，
      并保存断线重连、deadline default 与 Result replay 证据。
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
      统一使用内部 `user_id`。公共注册由
      `ADX_PUBLIC_REGISTRATION_ENABLED` 显式启用，默认 fail closed。
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
- [ ] 外部前端完成对应页面、Vercel 部署及 API/CORS 端到端切换。
- [ ] 增加 owner-only 私有投影与 Realtime 推送。
- [x] 在单机 Compose 中加入 Hosted Worker、Credential Controller 和 Arena Worker
      及独立权限。
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
      `action_timeout_ms`；生产单局默认开赛阈值 10、硬上限 100，同一时间一局
      active Game。
- [ ] 100 Agent 场景继续采用 `result_received_at` FCFS，并披露 Provider
      限流和 Worker wave 带来的平台排队偏差；未通过 launch-skew 验收前不把该
      部署称为 Tournament 公平性验证。
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
      预算限制为非 thinking 256、thinking 2048 Token；生产可在不重新接触
      Provider key 的情况下以 `market-v4` 刷新并重新验证官方配置。
- [x] 历史 Game 公共投影返回冻结优先的 `displayName + agentId`，独立前端结果页
      以 Agent 名称为主、短 ID 为辅，不再把 UUID-like `agentId` 当作名称。
- [x] FCFS 改为价格兼容订单内配对；Hosted Prompt v5 明确事件不得重复计价、
      全货物比较、保留价语义和确定性协商收敛规则，越过自身限价的结构化动作只
      允许一次有界修正 Attempt，Arena 的独立限价、余额与库存校验保持不变。
- [x] 产品 Current Game 默认从五回合调整为八回合，从十张版本化事件牌组中按
      Game seed 无重复抽取八张；固定五回合 Demo 和 1–10 回合配置能力保持不变。

### Phase 9：Post-MVP

- 100 Agent 单局与 4 Facilitator shard 的生产配置基础已落地；容量、故障恢复和
  live testnet 仍按 [`arena-scale-out-design.md`](arena-scale-out-design.md)
  分阶段验收。300 active Agent、多局并发仍是 Post-MVP；
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

- 公共第三方 Facilitator 的真实 testnet x402 V2 兼容验收；
- TEE key custody 与 remote attestation；
- 链上身份或 ERC-8004 reputation；
- escrow、退款、争议、仲裁和生产手续费；
- 主网、多链和高可用多节点；
- Agent Studio、人格市场和长期赛季系统。
