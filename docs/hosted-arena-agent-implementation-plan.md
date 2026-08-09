# Arena 402 Hosted Arena Agent Implementation Plan

> 文档状态：Runtime v2 与 Phase D 统一自主交易比赛已经完成生产功能链验收。
> 正式八回合 `agent_a2a.v1` Game 已组合真实 Codex Connector 与九名 DeepSeek
> Hosted Agent，并完成三笔 `arena402-g` 确认门控库存提交和赛后学习。旧
> Phase 0–6 作为已完成 foundation 保留；Runtime v2 权威实现记录见第 22 节，
> 跨 Runtime 产品切换见
> [`agent-driven-a2a-market-implementation-plan.md`](./agent-driven-a2a-market-implementation-plan.md)
> 最后更新：2026-08-09
> 对应规格：[Hosted Arena Agent Spec](./hosted-arena-agent-spec.md)
> 当前游戏规则：[Game Design](./game-design.md)
> 本地 Runtime 参考：[Local Agent Connector Spec](./local-agent-connector-spec.md)
> 前端边界：产品 UI 只在外部 `sunruize93-cmyk/arena402` 维护；本仓库不包含
> Web 服务。公开注册、GitHub OAuth/Session、Vercel→生产 API 基础联调与 Phase D
> Game 投影已验收；活动局中途整机重启和分档容量仍待验收
> 设计优先级：Game Design 维护业务契约，本计划维护 Hosted/Local 统一 Runtime
> 的实现与验收顺序
>
> 最新证据：4 vCPU / 8 GiB 腾讯云主机已完成单次 100 Hosted × 8 回合的无支付
> 与支付开启实测；支付局 50/50 SettlementIntent 确认并提交库存。Facilitator P0
> 把 Intent 创建到 submission P95 从 `67.080s` 降到 `9.412s`；GameCoin P0 把
> 隔离 100 钱包准备从 `692.500s` 降到 `162.430s`。分档重复性、20 真人叠加、
> 公共 Facilitator、HA 和活动局整机恢复继续开放。

## 1. 交付策略

本计划优先实现“受约束的 Hosted Arena Agent”，让用户在平台填写自己的 API Key、
选择已支持的 Provider/Model，并创建一个即使浏览器或电脑离线也能继续运行的云端
Agent。

实现顺序不从 UI 或 LangGraph 开始，而是先冻结并验证以下基础：

```text
Agent identity / ownership
  -> protected credential reference
  -> Hosted Runtime binding
  -> durable Arena AgentTask
  -> Provider Adapter
  -> strict structured action
  -> Arena validation and event
```

中间的完整 FCFS 撮合、有限轮协商和结算引擎可以独立推进。本计划只实现它们所依赖的
Agent/Runtime/Task 基础，并提供清晰的 Arena 接口；不会为了演示 Hosted Agent 而把
已删除的内存 matching 原型冒充生产游戏内核。

Runtime v2 在这些基础上直接替换认知执行链。生产切换后不并行保留
`DirectModelDriver` fallback；保留的是 Task、lease、Attempt、Secret、Result Sink
和 Finalizer 等平台基础设施。

Phase D 不再改造 Hosted Agent 的认知内核。它把已经完成的 PydanticAI Hosted
Runtime 与真实 Codex Connector 接入同一场版本化 `agent_a2a.v1` Current Game，
并要求 `arena402-g` 的 confirmation-gated InventoryCommit、终场排名和后续 Game
Strategy Revision 冻结来自同一条权威证据链。Native A2A 是 Phase E。

### 1.1 MVP 成功定义

```text
User logs in
  -> creates Hosted Agent in one form
  -> API key is written to Tencent Secret Manager
  -> Worker validates selected Provider/Model
  -> Agent binding becomes ready
  -> User joins one Sandbox Game with that Agent
  -> Arena creates a durable arena.decide task
  -> Worker invokes the model with thinking config
  -> strict buy/sell/pass result is persisted
  -> browser closes
  -> later task is still executed by the cloud Worker
  -> owner can inspect usage, latency and final action
```

如果完整协商引擎尚未接好，第一条 vertical slice 可以止于持久化并展示合法
`arena.decide`；但必须明确标注“Runtime vertical slice”，不能宣称完整交易已完成。

### 1.2 实施原则

- 先契约、后 Adapter：Arena Task/Result 先于任何真实 Provider；
- 先 Fake、后真实服务：Fake Provider 与 Fake SecretStore 只用于测试；
- 生产 BYOK fail closed：没有真实 Secret Manager 时不启用 Hosted BYOK；
- 业务状态分层：Runtime success、合法动作、协议达成、支付确认、库存提交分开；
- Server owns time：deadline、完成时间和 FCFS 时间戳由服务端产生；
- 严格输出：Pydantic discriminated union、`extra="forbid"`、定点金额；
- 有限恢复：最多一次重试，无 Provider/Model/Runtime fallback；
- 最小权限：API 只写 Secret，Worker 只读 Secret；
- 不收集私有推理：thinking 是模型能力开关，不是 CoT 采集开关；
- 单局唯一：数据库原子保证一名 User 每局只有一个 Game Agent；
- 证据分级：单元测试、真实 PostgreSQL、真实 Secret Manager、真实 Provider、
  公网 E2E 和负载测试分别报告。

### 1.3 三档完成定义

避免把“模型能调用”过度宣称为“离线交易已完成”：

| 里程碑 | 完成含义 | 明确不代表 |
|---|---|---|
| M1 Runtime Foundation | BYOK、Hosted Agent、Driver、durable Task/Result、离线 Worker 可运行 | 已有真实撮合/协商/支付 |
| M2 Arena Integration | Decide/Negotiate 均经 Arena Gateway、快照、校验、公开/私有投影完成 | 用户离线后一定能自动支付 |
| M3 Offline Transaction Completion | PaymentMandate、testnet settlement、链上确认和库存提交 E2E 完成 | 主网或真实资金能力 |

每个阶段只使用对应名称。计划末尾的完整 Definition of Done 是 M3，不是仅完成 M1。

## 2. 当前实现范围与差距

### 2.1 可以复用

| 文件/目录 | 可复用能力 |
|---|---|
| `connector_gateway/auth.py` | Session、密码、Cookie、CSRF 基础 |
| `connector_gateway/api.py` | production Router、对象级授权模式 |
| `connector_gateway/repository.py` | Protocol + Memory Repository 测试模式 |
| `connector_gateway/postgres_repository.py` | `asyncpg` 持久化模式 |
| `connector_gateway/persistent_service.py` | durable-before-delivery 与重启恢复思路 |
| `connector_gateway/service.py` | 幂等、bounded/redacted Event、状态机思路 |
| `db/migrations/002_connector_gateway.sql` | PostgreSQL 唯一约束、索引与审计模式 |
| `deploy/scripts/migrate.py` | advisory lock、checksum migration 基础 |
| `web/api.py` | production composition、Session principal 与 CSRF 接线 |
| 外部 `sunruize93-cmyk/arena402` | Agent 统一入口与 authenticated API client |
| `docker-compose.production.yml` | 单机 PostgreSQL/API/Caddy 后端部署骨架 |

复用的是安全与持久化模式，不是把 Connector Device/Command 表作为 Hosted Agent 表。

### 2.2 已移除且不能作为目标实现

下表是迁移时的删除依据。所列 matching、Supabase factory 与 `001` schema 已于
2026-07-25 从活动代码删除；保留表格是为了说明为什么不能恢复这些语义。

| 当前实现 | 原因 |
|---|---|
| `matching/agent.py` | 仅有静态 Provider/Model 资料和 HMAC；没有可调用的 Key 或 Provider invocation |
| `POST /api/agents/register` | 当前生产使用内存 Registry，创建即标记 online |
| `db/migrations/001_initial_schema.sql` | legacy Supabase/ELO/battle schema、public read 和 float 金额；self-hosted runner 也不应用它 |
| `matching/engine.py` | 兼容度排序而非服务端合法动作完成时间 FCFS |
| `matching/negotiation.py` | 有 auto-accept、旧轮次/超时语义，不符合当前 Game Design |
| `matching/calibration.py` | 旧 schema 要求 `reasoning`，与不收集 CoT 的边界冲突 |
| Connector `task.dispatch` | 当前 payload 是 session/prompt；terminal ACK 不是唯一 Arena structured result |
| Connector `runtime.message` | 观测 Event，不得从“最后一条输出”推断业务动作 |
| Supabase development factory | 生产明确禁用，不能作为 Hosted persistence |

### 2.3 当前测试基线

只读审计期间：

- Python：`24 passed`，主要覆盖 Connector auth、CSRF、ownership、持久化和投递；
- Go：仓库存在 57 个测试，但本轮 Windows sandbox 的 Go 临时目录权限阻止完整确认，
  不能将其报告为已通过；
- Hosted Agent、Provider Adapter、Secret Manager、thinking、usage、retry、
  Arena migration 和前端创建流程目前均无测试；
- 当前 PostgreSQL repository 测试主要使用 fake connection，不等于 PostgreSQL 17
  集成测试；
- 当前前端没有独立 test script。

这份基线只说明现有 Connector 没有在只读审计中发现 Python 回归，不说明 Hosted
能力已实现。

## 3. 已冻结的产品与技术契约

这些决策在实施中不得被局部代码静默改变。

### 3.1 Agent 与参赛

- User、Agent、Runtime Binding、Game Agent 是不同对象；
- 每名 User 每局最多一个 Game Agent；
- 同一个 Agent 可以参加后续 Game；
- MVP 每个 Agent 只有一个当前有效 Runtime Binding；
- 不实现比赛中途 Runtime 切换；
- 入局自动保存配置快照，不向用户暴露 Agent Revision 工作流。

### 3.2 Hosted 与 Local 可用性

- Hosted Agent 不依赖浏览器或用户电脑在线；
- Local Agent 依赖 Connector；
- Local 断线重连窗口为 30 秒与行动剩余时间中的较短者；
- Local 断线不会自动切换为 Hosted。

### 3.3 Provider 与 thinking

- 用户选择服务端 allowlist 中的 Provider/Model；
- 用户只配置 thinking 开/关；
- 推理强度使用 Provider/Model 默认值；
- always-on 或不支持 thinking 的模型由 capability registry 明确表达；
- 记录 effective thinking、usage、耗时、公开文字和最终结构化动作；
- 不请求、保存或展示 chain-of-thought；
- 不支持自定义 endpoint 或任意 Header。

### 3.4 Secret

- 原 API Key 仅持久化于专用 Secret Manager；
- 业务 DB 只保存 credential metadata 和 opaque `secret_ref`；
- API identity 无读取权限；
- Worker identity 无创建、列出或删除权限；
- Fake/Memory backend 不得在 production mode 启用；
- Secret backend 不可用时 fail closed。

### 3.5 Task、deadline 与失败

- Arena 只创建 `arena.decide` / `arena.negotiate`；
- 每个行动一个不可变 AgentTask；
- 同一 Game 使用统一可配置时间窗；
- timeout 默认值由真实 P95/P99 测试校准；
- 最多两个 Attempt；
- 只在错误可重试且剩余时间足够时重试；
- 不自动切换 Provider、Model 或 Runtime；
- Decide 失败收敛为一次 `pass`；
- Negotiate 失败收敛为一次 timeout；
- late/duplicate result 不改变终态。

### 3.6 A2A 与审计

- Agent 不直接通信，所有 A2A 由 Arena Gateway 中转和审计；
- 一段 negotiation 对应一个 Arena `negotiation_id`；
- Connector WSS 是内部传输，不宣称 Native A2A；
- Native A2A 以后作为第三 Runtime Adapter；
- Task success 不等于合法动作、达成协议或支付完成。

### 3.7 Payment

- 第一版只使用 Injective EVM testnet；
- Agent 永远不接触钱包私钥；
- 产品目标是在入局时确认受限 PaymentMandate；
- 现有 EIP-3009 direct relay 是单笔授权原型，不等于通用 Mandate；
- 链上确认前不移动 Arena 库存。

## 4. 目标模块与文件布局

当前实现按权威拆成三个有单向依赖的包，不把 Hosted 逻辑堆入游戏领域：

```text
arena_agent_contracts/
├── tasks.py
├── actions.py
├── results.py
└── driver.py

arena_core/
├── task_factory.py
├── result_sink.py
├── public_output_policy.py
├── result_consumer.py
├── finalizer.py
├── worker.py
├── repository.py
├── postgres_repository.py
└── runtime_adapters/
    ├── base.py
    ├── hosted.py
    ├── connector.py
    └── native_a2a.py       # placeholder only

hosted_agent_runtime/
├── models.py
├── service.py
├── queue.py
├── repository.py
├── postgres_repository.py
├── worker.py
├── credential_controller.py
├── capabilities.py
├── prompt_builder.py
├── direct_model_driver.py
├── providers/
│   ├── base.py
│   ├── fake.py
│   └── <first_real_provider>.py
└── secrets/
    ├── base.py
    ├── memory.py
    └── tencent_ssm.py
```

职责：

| 模块 | 责任 |
|---|---|
| `arena_agent_contracts/*` | 无 DB/Provider 依赖的 versioned Task/Result、统一 `action` union 和 `AgentRuntimeDriver` |
| `arena_core/task_factory.py` | 在 Arena 事务中重建并冻结 participant view、配置快照与 hash |
| `arena_core/result_sink.py` | CAS Task 并持久化唯一 Runtime Result |
| `arena_core/public_output_policy.py` | 在持久化前过滤 secret/PII/策略原文，必要时生成中性 public message |
| `arena_core/result_consumer.py` | 唯一消费候选 Result，Arena 校验并幂等应用 |
| `arena_core/finalizer.py` | 独立于 Runtime 收敛所有 expired Task |
| `arena_core/worker.py` | 无公网端口、以 DB leader/lease 运行 Consumer 与 Finalizer |
| `arena_core/runtime_adapters/*` | dispatch/cancel 与 Result Sink 映射 |
| `hosted_agent_runtime/models.py` | Credential、Hosted Config、Provisioning Job、Attempt 状态 |
| `hosted_agent_runtime/service.py` | Provider Attempt、deadline、重试和 Result 提交 |
| `hosted_agent_runtime/queue.py` | Task/Provisioning Job lease、claim、recovery |
| `hosted_agent_runtime/worker.py` | 无公网端口的 Hosted Worker |
| `credential_controller.py` | 独立权限执行 revoke/delete lifecycle job |
| `prompt_builder.py` | 从冻结 Task 和有界私有策略确定性构造版本化 Prompt，并隔离 untrusted Arena data |
| `direct_model_driver.py` | 当前一次逻辑 Task、最多两次 Provider Attempt 的纯执行 Driver |
| `capabilities.py` | Provider/Model/thinking allowlist |
| `providers/*` | 安全 Provider contract、Fake Provider、usage 与错误归一；真实固定 endpoint Adapter 待实现 |
| `secrets/*` | Secret write/read/revoke port 与生产实现 |

其他计划文件：

```text
web/hosted_agent_api.py
db/migrations/003_arena_agent_runtime.sql
[external] sunruize93-cmyk/arena402
deploy/scripts/migrate.py
docker-compose.production.yml
requirements/production.in
requirements/production.txt
tests/test_arena_agent_contracts.py
tests/test_arena_core_*.py
tests/test_hosted_agent_runtime_*.py
tests/integration/test_arena_runtime_postgres.py
```

实际文件可在实现前做小幅调整，但必须保持 Hosted Runtime 独立于 Connector authority
和 legacy matching。`arena_runtime_bindings` 只是 Arena route；Connector kind 只引用
Connector-owned `connector_binding_id + binding_epoch`，不复制其权威。

## 5. Migration 策略

`deploy/scripts/migrate.py` 现已支持显式 scope；默认仍只选择 Connector migration，
生产 Compose 显式使用 `--scope all`，而 `001_initial_schema.sql` 不会在 self-hosted
production 中执行。已落地的规则是：

1. 保留当前默认 Connector scope，避免升级时意外执行 legacy `001`；
2. migration runner 提供显式 `--scope connector|arena|all`；
3. Arena scope 只匹配经过批准的 `*_arena_*.sql`；
4. `--scope all` 使用一个全局 advisory lock，并按 migration 数字/文件名确定性排序；
5. Connector `002` 必须先于引用共享 User 的 Arena `003`；
6. Compose 只运行一条 `--scope all`，不并发启动两个 migration job；
7. 继续使用 checksum 和重复执行幂等；
8. 已应用 migration 内容发生变化时必须失败；
9. 不修改或重用 legacy `001_initial_schema.sql`；
10. 不把 Arena 表命名成 `connector_*` 以绕过筛选。

Self-hosted beta 选择复用 `connector_users` 作为共享平台 User 权威；这是兼容表名，不是
Connector 对 Arena User 的业务所有权。Arena 表以外键引用它。未来改为
`platform_users` 必须另做身份迁移，Phase 1 不创建第二套 User。

### 5.1 最小 Arena participation foundation

Hosted Runtime 最终需要真实 Game/Participant/Round 外键。当前 Phase 1 migration
先建立仅供集成所需的最小持久化基础：

- `games`：id、status、统一 `action_timeout_ms` 和配置；
- `rounds`：game、round index、phase、deadline；
- `game_agents`：唯一 `(game_id, user_id)` 与 `(game_id, agent_id)`；
- Agent/credential/config/routing binding/task/result/attempt/event/provisioning-job 表。

该最小基础不实现 pool、pairing、negotiation、settlement、inventory 或 ranking；
之后的 Game Core migration 只能扩展它，不能创建第二套 Participant 权威。

所有资产与价格字段使用 atomic unit 或 `NUMERIC`，不得沿用 `REAL`。

数据库约束还必须包括：

- `arena_agents UNIQUE(owner_user_id, id)`；
- `game_agents(user_id, agent_id)` 到 Agent owner 的复合外键；
- active route partial unique predicate 为 `disabled_at IS NULL`；
- `CHECK (attempt_no BETWEEN 1 AND 2)`；
- Task/Result/Binding/Credential 状态 CHECK；
- Result Sink 与 Finalizer 通过同一 Task CAS 竞争，一个逻辑行动只能产生一个终态。

## 6. 分阶段状态

| 阶段 | 目标 | 状态 |
|---|---|---|
| Phase 0 | Spec、Plan、边界与威胁模型 | 已完成 |
| Phase 1 | 契约、迁移与持久化 identity/binding/task foundation | 基础实现完成；共享 repository contract suite 待补 |
| Phase 2 | SecretStore、Tencent SSM 与 Provider capability | Port/registry 已实现；真实 Tencent SSM/CAM 未完成 |
| Phase 3 | Fake/真实 Provider Adapter 与结构化输出 | Fake、DeepSeek/OpenAI-compatible Adapter、PromptBuilder、DirectModelDriver 和 Worker 接线已实现；真实 Key 验收待生产部署 |
| Phase 4 | Hosted Agent API、创建 UI 与 readiness | 控制面、幂等迁移、受门控 API、生产 PostgreSQL 组合与最小 UI 壳已实现 |
| Phase 5 | Durable Worker、deadline、retry 与恢复 | PostgreSQL queue/lease、Attempt、Finalizer 与独立 Worker 已实现；真实服务器重启验收待完成 |
| Phase 6 | Arena Runtime Adapter、Game Agent 与公开/私有投影 | Hosted/rule 已完成，并可自动执行 1–10 回合；Connector typed adapter、durable Result 与 Sink 基础接线已完成，identity/session/task dispatcher 和 mixed-Runtime 编排待实现 |
| Phase 7 | PaymentMandate/Settlement 接线 | 单笔 EIP-3009 意图/恢复/确认后库存提交已实现；通用 Mandate 与新鲜交易待完成 |
| Phase 8 | 单机部署、真实 E2E 与负载校准 | Compose/资源限制已实现；真实 SSM/Provider/支付和负载校准待完成 |
| Phase 9 | Native A2A、Agent Studio、多 Runtime | Post-MVP |

依赖关系：

```text
Phase 0
  -> Phase 1 contracts/persistence
       -> Phase 2 secret/capability
            -> Phase 3 DirectModelDriver/provider
                 -> Phase 4 API/UI
                      -> Phase 5 durable workers = M1

Phase 1 Game foundation + Phase 5
  -> Phase 6 Arena Decide/Negotiate = M2

Phase 6 + independent Settlement/PaymentMandate
  -> Phase 7 + Phase 8 E2E = M3

Phase 9 never blocks M1/M2/M3
```

## 7. Phase 0：规格与威胁模型

### 7.1 交付

- 本 Spec；
- 本 Implementation Plan；
- 明确当前实现矩阵；
- 明确不会触碰的 frozen records；
- 明确 PaymentMandate 与 EIP-3009 的差异；
- 明确 `action_timeout_ms` 不预先写死；
- 明确 Agent Studio 与 Native A2A 是后续兼容位。

### 7.2 用户确认后同步的活动文档

按顺序更新：

1. `AGENTS.md`
   - 把模型 BYOK 从绝对禁止改成仅限 write-only ingress + 批准的外部 Secret Manager；
   - 业务 DB/日志禁存仍是红线；
   - 钱包、本地 Runtime 和部署凭据仍绝对禁止上传。
2. `docs/agent-onboarding.md`
   - 链接 Hosted Spec；
   - Hosted 创建、配置快照和单局唯一；
   - Local/Hosted 离线差异。
   - Hacker 的完整 System Prompt 改为受限 `strategy_instructions`；
   - Connector 30 秒/剩余 deadline 重连后，后续行动显式 default，不留空记录。
3. `docs/product.md`
   - “不存储 API Key”改为“业务 DB/日志不存；生产 Secret Manager 托管”；
   - 增加 thinking、失败收敛和持续在线边界。
4. `docs/game-design.md`
   - Decide/Negotiate 统一使用本规格的 `action` 字段并升级 payload schema version；
   - 将固定候选 timeout 改为测试校准的统一 Game 配置；
   - 补充最多一次重试且同局同窗。
   - 区分每轮 logical AgentTask 上限与每 Task 最多两个 Provider Attempt，旧
     “最多 N 次 LLM 调用”不再混用；
   - 统一 Decide `pass`、Negotiate timeout 与 `failedNegotiations` 语义。
5. `docs/roadmap.md`
   - 把 Hosted foundation 分解为真实未完成阶段；
   - 保持完整 Game Core、Settlement 与前端的依赖顺序。
6. `docs/arena-settlement-integration.md`
   - 增加 PaymentMandate 目标；
   - 明确当前 direct EIP-3009 的能力差距。
7. `docs/local-agent-connector-spec.md` 与对应 Plan
   - 增加版本化 `action` payload/result、Result Sink 与 binding epoch 引用；
   - 不把 Connector ACK/runtime.message 当作 Arena Result。
8. `README.md`
   - 新增两份 Hosted 文档入口和准确状态。

不修改：

- `agent-arena/specs/`；
- `docs/injective/`；
- `docs/archive/`；
- 已冻结的兼容 wire identifier。

### 7.3 退出门槛

- [x] 用户确认本 Spec/Plan；
- [x] 没有把“设计”写成“已实现”；
- [x] `game-design.md` 已同步到本规格冻结的统一 `action` schema 和结算边界；
- [x] 与 Local Connector 的 authority 不重叠。
- [x] 上述全部活动权威文档已同步。

## 8. Phase 1：契约、迁移与持久化基础

### 8.1 工作项

1. 新建 `arena_agent_contracts/`：
   - `ArenaAgentTaskV1`；
   - `ArenaDecideInputV1`；
   - `ArenaNegotiateInputV1`；
   - `BuyAction`、`SellAction`、`PassAction`；
   - `ProposeAction`、`AcceptAction`、`RejectAction`；
   - `AgentTaskResultV1`；
   - `AgentRuntimeDriver.execute(task_snapshot, deadline)`；
   - `extra="forbid"`。
2. 新建 Arena Task Factory，在创建事务中冻结 `input_snapshot`、config ref 和 hash；
3. 新建 Result Sink、PublicOutputPolicy、Result Consumer 与 Deadline Finalizer 契约；
4. 新建 Agent/Credential/Config/Route/Game Agent/Task/Result/Attempt/Event、
   credential validation job 与 credential lifecycle job 状态；Agent 增加 nullable
   `runtime_update_job_id`（非空即 pending），Game Agent 冻结 active/terminal
   状态集合；
5. 增加 Arena Core 与 Hosted Runtime 各自的 repository Protocol 和 Memory 实现；
6. 新增 Arena migration scope与 PostgreSQL repository；
7. 实现 transaction、row lock、CAS、lease、Result apply ACK 和唯一约束；
8. 增加最小 Game/Participant/Round foundation；
9. 复用 `connector_users` 作为兼容的共享 User 外键；
10. 定义 migration、Arena core、API、Hosted Worker、Credential Controller 五类 DB
    role；Worker 的 validation 完成只能经受限 SECURITY DEFINER 函数；
11. 不迁移 legacy ELO/battle 数据作为生产权威。

Game config snapshot 至少包含 immutable model id、effective thinking、adapter、
Prompt、Task/action schema、capability version、各类上限、strategy/hash 与精确
credential id；禁用可变 `latest` alias，Attempt 记录 Provider 实际 model/version。

Credential validation job 对 `create | update | replace` 保存私有
`candidate_config_snapshot/hash` 与 `expected_current_config_hash`。它是内部 staging，
不是用户可见 Revision；终态后失败 candidate 的私有明文按保留策略清除，只保留 hash、
安全字段和验证结果。

### 8.2 关键约束

- `UNIQUE(game_id, user_id)`；
- `UNIQUE(game_id, agent_id)`；
- 一个 Agent 一个当前 active binding；
- join、Runtime-affecting PATCH 与 credential replace 锁同一 Agent row；
- `runtime_update_job_id IS NOT NULL` 时拒绝 join；Game Agent 的
  `joined | active | settling` 均阻止 Runtime 配置切换；
- `UNIQUE(game_agent_id, idempotency_key)`；
- Negotiate idempotency key 包含 Arena 生成的 `negotiation_id + turn_sequence`；
- `UNIQUE(task_id, attempt_no)`；
- `CHECK (attempt_no BETWEEN 1 AND 2)`，计数在锁内分配；
- 同一 Credential/Config 组合最多一个 active validation job；
- `UNIQUE(task_id)` 的可应用 Result；late/duplicate 只进入 Event；
- Task 终态不可覆盖；
- late completion 只追加 Event；
- Result Sink 与 Finalizer 竞争同一 Task CAS，只能一方获胜；
- active route 的 partial unique predicate 是 `disabled_at IS NULL`；
- 策略、Provider 和 execution metadata 默认 private；
- 金额不用 float。

### 8.3 测试

- strict schema 与 extra field；
- 价格精度、消息长度和非法 union；
- 并发 join；
- join 与 Runtime-affecting PATCH/credential replace 竞态；
- 异步 validation 最终 CAS 与 join 竞态；
- validation 失败/取消后只在 job id 匹配时清除 `runtime_update_job_id`；
- join 同一 Agent 幂等；
- join 不同 Agent 返回 conflict；
- 并发 task create；
- 同一 negotiation 同一 Agent 的多个 turn sequence；
- duplicate dispatch 复用原 idempotency key；
- duplicate completion；
- late completion；
- Result Consumer crash/replay；
- Finalizer 与 Result Sink 竞态；
- lease 过期前后 claim；
- Worker DB role 越权更新 inventory/settlement/DDL 必须失败；
- Worker DB role 直接更新 Credential/Config/Binding/Agent/validation terminal 必须
  失败；只有受限 validation claim/attempt/complete 函数可工作；
- validation complete 函数对 stale job id、错误 expected hash、失效 lease 和 active
  Game 均拒绝；
- migration 首次、重复、checksum drift；
- `--scope all` 顺序和全局锁；
- PostgreSQL 17 真实 apply/restart。

### 8.4 退出门槛

- [ ] Memory 与 PostgreSQL repository 通过同一 contract suite；
- [x] 真实 PostgreSQL 中并发唯一性由 DB 保证；
- [x] legacy `001` 未被意外应用；
- [ ] AgentTask 可以重启恢复且不会重复完成。
- [x] Worker 数据库角色只能读取冻结 execution view；
- [x] 不依赖 Hosted Worker 的 Arena Finalizer 可生成唯一 default。

## 9. Phase 2：SecretStore 与 Provider Capability

### 9.1 SecretStore Port

定义分离接口，避免让所有进程拥有相同权限：

```python
class SecretWriter(Protocol):
    async def create(...): ...

class SecretReader(Protocol):
    async def resolve(secret_ref): ...

class SecretController(Protocol):
    async def revoke(...): ...
    async def delete_after_retention(...): ...
```

实现：

- `MemorySecretStore`：测试；
- `PostgresEncryptedSecretWriter/Reader/Controller`：单机 beta，数据库仅存
  AES-256-GCM ciphertext，master key 使用独立只读主机文件；
- `TencentSecretWriter`：API 使用；
- `TencentSecretReader`：Worker 使用；
- `TencentSecretController`：Credential Controller 使用。

### 9.2 单机 encrypted vault 与 Tencent Cloud

工作项：

- 单机 beta 通过 role-specific `SECURITY DEFINER` 函数分离 write/read/lifecycle；
- master key 不进 PostgreSQL、`.env`、Compose 或仓库，只读挂载给 API/Worker；
- ciphertext 绑定 `secret_ref + key_version`，nonce 每次随机生成；
- 确认部署地域与 Secret 命名规则；
- 使用不含 user email/用户名的 opaque Secret name；
- 建立 API Writer、Worker Reader、Credential Controller 三个 CAM policy；
- 优先使用 CVM role/短期 credential broker；静态 fallback 只通过 root-owned Docker
  secret/read-only file 注入并轮换，禁止写入 `deploy/.env`、Compose 或仓库，三个
  进程互不可读；
- 启用 CloudAudit/Secret 操作审计；
- Secret metadata 不记录原 Key；
- 先写 `pending_write` DB row 与确定 secret name，再写 SSM，消除不可发现 orphan；
- reaper 通过 pending row 与预生成的 `secret_ref` 恢复或撤销，无需 List Secret；
- replace 使用 `/api/model-credentials` 先创建一条新 Credential row；随后只用
  `replacement_credential_id` 发起验证，验证通过后 CAS 切 Hosted Config
  `credential_id`，再把旧 row 交给 Controller 撤销，不在原 row 覆盖 version；
- 定义 revoke/delete 的安全顺序；
- fingerprint 使用独立 pepper HMAC，只用于内部关联并支持 pepper version；
- 配置 Secret backend 健康检查与 fail-closed startup。

创建页必须说明 Arena 是模型凭据运行时托管方；建议用户使用独立、限模型、限额、
限速和可过期的 Provider Project Key。

### 9.3 Capability Registry

服务端 registry 至少描述：

- Provider id 与 Adapter id；
- Model id 与展示名；
- structured output 能力；
- thinking `unsupported | optional | always_on`；
- max output tokens；
- 是否经过真实验证；
- Adapter/schema version；
- 是否对当前部署启用。

UI 不自己维护独立 Provider 列表。

### 9.4 安全测试

- 原 Key 不在 DB dump；单机 beta dump 中只能出现 ciphertext/nonce/version；
- 原 Key 不在 application/access/error log；
- 原 Key 不在 Pydantic repr、Caddy/APM/OpenTelemetry 或 HTTP debug log；
- 原 Key 不在 HTTP response；
- 原 Key 不在 Audit/Event；
- API identity 不能 Get；
- Worker identity 不能 Create/List/Delete；
- Credential Controller 不能 Get；
- Secret backend outage fail closed；
- replacement CAS 后旧 Credential 不可用于新 Task；
- revoke 后新 Task 不领取；
- revoke 与在途调用按原 deadline 确定性收敛；
- 创建失败无永久 orphan；
- 进程在 SSM 写入前/后崩溃均可由 pending row 恢复；
- production config 不能选择 Memory backend。

### 9.5 退出门槛

- [ ] 使用临时测试 Key 在公网单机 vault 完成
      create/read/replacement-row-CAS/revoke/delete 与重启恢复；
- [ ] 可选：使用腾讯 SSM 完成同一 contract；
- [ ] 扫描 DB、日志和响应均无原 Key；
- [ ] 三个 IAM 身份的越权操作均失败；
- [ ] capability API 只返回启用的安全字段。

## 10. Phase 3：Provider Adapter 与结构化模型调用

### 10.1 先 Fake Provider

Fake 必须支持确定性脚本：

- 成功返回 buy/sell/pass；
- 成功返回 propose/accept/reject；
- 429 -> 成功；
- 400；
- 5xx；
- transport timeout；
- invalid JSON；
- schema extra field；
- 返回 usage；
- 不返回 usage；
- 返回应被丢弃的 reasoning text；
- request_sent 后进程中断。

所有重试、deadline 和持久化测试先通过 Fake，避免把 Provider 网络波动混入状态机。

### 10.2 第一条真实 Provider

第一条真实 Adapter 需要：

- 使用 `httpx.AsyncClient`；
- 明确 connect/read/write/pool timeout，并受 Arena deadline 上限约束；
- endpoint 只来自代码/受控 capability，HTTPS/证书/SNI 固定，不接受用户 URL、
  IP literal 或非批准端口；
- `follow_redirects=False`、`trust_env=False`，不继承代理环境变量；
- response bytes、解压后大小和读取时间有硬上限；
- 发送 server-generated structured schema；
- 映射 Provider/Model thinking；
- 不跨 Provider 人为设置统一 reasoning effort；
- 归一 input/output/cached/reasoning Token 数值；
- 使用 monotonic clock 记录耗时；
- 只保存安全 Provider request id；
- 将 429/5xx/transport/permanent 4xx 分类；
- 不主动请求 visible reasoning；若响应仍含 reasoning text/encrypted blob，在解析内存
  丢弃且不进入 DB、日志、Trace、Event 或异常；
- 不保存 raw response、raw error body 或完整 Prompt；
- 不实现 tools/function side effects；
- 不实现 streaming 作为业务终态来源。

后续 Provider 逐个增加。UI 只在该 Adapter 完成真实 E2E 后显示对应组合。

Phase 3 同时实现 `DirectModelDriver.execute(task_snapshot, deadline)`：一个逻辑
AgentTask 最多产生两个 Provider Attempt。Credential validation 使用独立 Job、固定
最小 Prompt、极小 Token 上限，不使用比赛快照。即使 update job 的私有 candidate
暂存 strategy，validation request 也只能读取 Provider/Model/thinking/credential/
capability 字段，绝不把 strategy 发给 Provider。

Credential validation 状态机：

- 401/403、model invalid、capability mismatch：永久 `invalid`，等待用户修改；
- 429/5xx/transport：持久化 `next_attempt_at`，按有界 backoff/`Retry-After` 重试；
- `max_attempts` 属于 provisioning 配置，不占 Arena Task 的两个 Provider Attempt；
- 临时错误尝试耗尽后 Credential 进入带安全错误分类的 `invalid`，Binding 保持
  `degraded`；owner 可通过限流 revalidate API 创建/复用 durable job；
- revalidate 以 CAS 把 `invalid` 切回 `pending_validation`，并由唯一 active-job
  约束阻止并发重复 job；
- 初次创建的 validation success 原子更新 Credential/Config/Binding readiness，并
  清除匹配的 `runtime_update_job_id`；replace/PATCH 的 success 还需走 Phase 4 的
  Agent row lock、active Game 复查与 pending job id/config hash CAS；
- revoke/disable 取消未执行 job。

### 10.3 Prompt Builder

建立确定性 Prompt builder：

- 平台规则与 schema；
- 用户私有、长度有界的策略说明；
- Arena participant view；
- 当前 Task；
- 对手公开消息以 untrusted data 编码；
- schema version 写入 Game 配置快照；Prompt policy version 可作为诊断事实记录，
  但不作为单 Agent 版本选择器，Hosted Worker 统一使用当前 v4；
- 不把 Secret、对手私有状态或历史 CoT 放入 Prompt。
- 创建页明确提示所选 Provider 将接收策略说明和最小 participant view，并要求用户
  不在策略说明中粘贴额外凭据或个人敏感信息。
- input/context/output 使用版本化硬上限；超出时由 Arena Task Factory 在冻结前按规则
  摘要/截断，Worker 不临时重建。

### 10.4 退出门槛

- [x] Fake Provider 全错误矩阵通过；
- [x] DeepSeek/OpenAI-compatible Adapter 完成真实 structured action；
- [x] thinking 开/关或 always-on 行为与 registry 一致；
- [x] reasoning text 不进入 persistence/API；
- [ ] redirect 到 loopback/RFC1918/link-local/cloud metadata 被拒绝；
- [ ] 恶意 proxy env 不生效，超大/压缩炸弹响应被拒绝；
- [x] usage 缺失时 `usage_complete=false`；
- [x] 无任何 Provider/Model fallback。
- [x] transient validation 可在重启后按 `next_attempt_at` 恢复；
- [ ] permanent validation 不自动重试，revalidate 受限流且不重复建 job。

## 11. Phase 4：Hosted Agent API 与创建 UI

本阶段先交付 creation/control surface，feature flag 仍保持关闭。创建请求可以持久化为
`provisioning` 并排入独立 credential validation job；只有 Phase 5 的 Durable Worker
完成并通过 E2E 后，才允许 Binding 实际进入 `ready` 并对外开放 Hosted 创建入口。

截至 2026-07-24，已经落地的是安全、可测试的 Phase 4 壳：

- `hosted_agent_control_plane/` 提供严格 domain model、capability/readiness、
  write-only Credential ingress、Hosted Agent create/list/detail service 和显式
  test-only Memory repository；
- `004_hosted_agent_api.sql` 提供 owner/route 隔离、只接收摘要、带 TTL/数量上限的
  HTTP 幂等记录与受限数据库函数；重放通过安全 resource reference 重新读取当前
  owner-scoped 投影，不缓存 Credential、Strategy 或完整响应；
- `web/hosted_agent_api.py` 提供安全 body 解析、Session/CSRF/owner scope、
  capability route，以及只有显式传入完整依赖时才会挂载的 create/list/detail 路由；
- production Caddy access log 将完整 request URI 替换为固定占位符，Uvicorn access
  log 在反向代理后关闭，避免误贴到 query 的 Key 进入双重 access log；业务审计继续
  使用结构化 Arena/Connector Event，而不是原始 URL；
- `/agents` 保留 Local Connector，并新增最小 Hosted 创建表单与状态列表。原 Key
  不进入 React state/storage，Credential 成功后立即清空；第二阶段失败只重试
  Agent create；
- `/connect` 支持不填写 Connector code 的纯登录/注册路径，Hosted-only 用户不需要
  先运行本地 Connector；
- `web/api.py` 只挂载 `creationEnabled=false` 的 capability envelope。即使设置
  `ADX_HOSTED_AGENTS_ENABLED=true`，在生产依赖尚未组成时也会启动失败，而不会
  回退到 Memory/Fake。

2026-07-25 更新：生产 PostgreSQL control repository、单机 AES-GCM ciphertext
vault、可选 Tencent SSM Writer/Reader/Controller adapter、
DeepSeek/OpenAI-compatible Provider、durable
validation/Task Worker、独立 Credential Controller、Arena Coordinator/Finalizer 与
确认恢复 Worker 已落地。生产 Compose 默认关闭这些 profile，并在 IAM 未明确验证时
fail closed。双 Hosted Agent 已在本地 PostgreSQL 开发栈完成 Decide/Negotiate，
但公网真实 Provider Key、服务器重启连续性和越权拒绝证据仍需部署验收；腾讯
CAM/SSM 是可选的更高安全等级。replace/revoke/revalidate/PATCH/disable 路由和
UI resume 仍未完成。

### 11.1 Backend API

新增 `web/hosted_agent_api.py`，挂载到 production app：

- `GET /api/hosted-agents/capabilities`；
- `POST /api/model-credentials`；
- `GET /api/model-credentials?scope=mine`；
- `POST /api/model-credentials/{credential_id}/replace`；
- `POST /api/model-credentials/{credential_id}/revoke`；
- `POST /api/model-credentials/{credential_id}/revalidate`；
- `POST /api/hosted-agents`；
- `GET /api/hosted-agents?scope=mine`；
- `GET /api/hosted-agents/{agent_id}`；
- `PATCH /api/hosted-agents/{agent_id}`；
- `POST /api/hosted-agents/{agent_id}/disable`；
- `POST /api/games/{game_id}/participants`；
- `GET /api/games/{game_id}/participants/me`。

所有状态变更路由：

- 使用 production Session principal；
- 验证 CSRF；
- 不信任 body 中的 `owner_id`；
- 使用对象级 owner scope；
- 校验原始 `Idempotency-Key`，只把 `sha256:` 摘要传入并持久化到 repository；
- 按 `user + route` 隔离 idempotency，并限制 key 长度、记录数量和 TTL；
- 跨 owner 返回 404；
- 请求体不进入 access/APM/Trace/validation/error log；
- 只有 `POST /api/model-credentials` 可以接收原始 Key；replace 只接收 owner 的
  `replacement_credential_id`；
- `POST /api/hosted-agents` 只接收 owner 的未绑定 `credential_id`，MVP 1:1。

### 11.2 一次创建的补偿事务

Secret Manager 与 PostgreSQL 不能共享数据库事务。Credential ingress 使用可恢复
saga：

```text
transaction:
  reserve idempotency record
  + create pending_write credential row
  + pre-generate opaque secret_ref
  + attach credential_id to the reservation
commit
  -> write Secret to exact ref
  -> transaction: CAS credential to stored
                  + complete the same idempotency record
```

前端随后调用 Agent create：

```text
credential_id
  -> transaction:
       write Agent + Hosted Config + Arena routing binding
       CAS credential stored -> pending_validation
       enqueue hosted_credential_validation_job(provider + model + candidate config)
       set Agent.runtime_update_job_id = validation_job.id
  -> return provisioning
```

用户仍只点击一次“Create”。失败与恢复：

- Secret 写入前失败：pending row 明确可重试/撤销；
- Secret 写入后进程崩溃：reaper 通过 pending row 与确定 ref 恢复，无需 List；
- Agent create 失败：Credential 保持 owner-owned/unbound `stored`，在短 TTL 内可由相同
  幂等流程复用，过期后由 Controller 撤销；
- replace 前先通过统一 Credential ingress 创建新的 unbound Credential；replace API
  只接收其 `replacement_credential_id`；
- 独立 replace 只允许同 Provider Key 替换并按当前 Config/Model 验证；Provider
  切换使用 Agent PATCH，并必须同时提交与 candidate Provider 匹配的
  `replacement_credential_id`；
- replace/PATCH 开始时锁 Agent row，确认无 `joined | active | settling` Game Agent，
  创建带 `candidate_config_snapshot/hash + expected_current_config_hash` 的 validation
  job，并把其 id 写入 `runtime_update_job_id`；
- pending 期间 join 返回 `409`；validation 成功后的最终事务重新锁 Agent row，核对
  pending job id、expected config hash 并复查无 active Game，再整体 CAS Hosted
  Config、清除 job id，最后撤销旧 row；
- validation 失败/取消只在 job id 匹配时清除 pending，并保留旧 Config/Credential；
  最终 CAS 失败也不得切换；
- active Game 时 replace 返回 409；紧急 revoke 允许但令当前 Game 后续行动 default；
- revoke 先阻止新 claim，再排入 Credential Controller job；
- request digest 排除原 Key；Key 只参与独立 pepper HMAC fingerprint；
- 相同 idempotency key + 相同 canonical metadata/fingerprint：返回同一资源的
  当前 owner-scoped 投影；
- 已完成 replay 的 owner/key/request digest 查询先于当前 Credential 状态与
  capability catalogue 校验；只有 fresh create 才按当前依赖重新校验，事务内仍需
  再次检查 replay 以关闭并发窗口；
- 相同 key + 不同 digest：`409`。

### 11.3 Frontend

修改 `/agents`：

- 保留 Local Connector 区域；
- 将误导性的 “Platform agent templates” 拆为 Template/Strategy 与 My Agents；
- 新增单个 `HostedAgentCreator`；
- Provider/Model/thinking 来自 capability API；
- API Key 使用 password field，不写 localStorage；
- 一次点击在内存中顺序调用 Credential create 与 Agent create，完成后立即清空 field；
- replace 时也先调用统一 Credential create，再用返回的 `credential_id` 调用 replace；
- 切换 Provider 时，前端先创建新 Provider Credential，再把其 id 与完整 candidate
  config 一起提交 Agent PATCH；
- 一次提交后显示 `provisioning -> ready/degraded`；
- 明确提示 Arena 是模型凭据运行时托管方，并建议独立限额 Project Key；
- 显示 Sandbox/testnet-only、Provider 数据政策与费用提示；
- 显示 replace credential、disable；
- 不显示 secret_ref、完整 Prompt 或原始 Provider 错误；
- 所有产品 UI 改动只进入外部 Next.js 产品仓库，并调用当前
  Hosted/Pawnhouse/Connector API。

### 11.4 测试

- unauthenticated；
- missing/invalid CSRF；
- cross-owner get/update/disable；
- create idempotency；
- saga 各失败点；
- SSM 写入前/后进程崩溃；
- Credential 1:1、replace CAS、revoke 与在途调用；
- transient validation 重启恢复、永久错误不自动重试、revalidate 限流与 job 去重；
- active Game 时 runtime-affecting PATCH/replace 返回 409，紧急 revoke 仍可执行并
  导致后续 default；
- join 与 replace/PATCH 并发时只能一个方向成功，不得产生中途配置切换；
- `runtime_update_job_id` 阻止 join，validation 最终 CAS 核对 job/config hash 并
  复查 active Game；
- validation 失败/取消后匹配的 pending 被清理且旧配置仍可用；
- validation 成功一次 CAS 应用完整 candidate，不能出现 Provider、Model、thinking、
  strategy 与 Credential 的半更新；
- credential.provider 与 selected Provider 不一致；
- 同 Provider replace、跨 Provider PATCH、缺失/错配 replacement Credential；
- unsupported provider/model/thinking；
- raw key 在 Pydantic/Caddy/APM/Trace/error/debug log 中均被排除；
- frontend loading/ready/degraded/error；
- refresh 后状态保持；
- Agent 未 ready 时不能 join；
- 并发 join。

### 11.5 退出门槛

- [x] 最小 UI 壳允许用户在一个页面一次提交创建；生产能力仍由 readiness 关闭；
- [x] HTTP 层是两个独立幂等 API，Agent request 不含原 Key；
- [ ] 刷新页面后 Agent 与 Binding 仍存在；
- [x] Runtime 未 ready 不显示 online；
- [ ] join 与异步 Runtime 更新竞态在真实 PostgreSQL 中线性化；
- [ ] transient validation 可恢复，revalidate 不会并发创建重复 job；
- [x] candidate validation 失败不覆盖当前 Config，成功只产生一次完整 CAS；
- [ ] 浏览器不能覆盖内部 endpoint、系统规则或 Secret reference；
- [x] Local Connector 入口未被破坏。

## 12. Phase 5：Durable Hosted Worker

### 12.1 Worker 模型

在 Compose 增加独立 service：

```text
arena-core-worker
  command: python -m arena_game.production_worker
  public ports: none
  database: Result Consumer + Deadline Finalizer
  cloud secret permission: none

hosted-agent-worker
  command: python -m hosted_agent_runtime.worker
  public ports: none
  database: frozen execution view + Attempt + Result Sink function
            + restricted validation projection/functions
  secret permission: read only
  outbound: allowlisted Provider + Tencent API

credential-controller
  command: python -m hosted_agent_control_plane.production_controller
  public ports: none
  database: credential lifecycle jobs only
  secret permission: revoke/delete only
```

API、Arena Core Worker、Hosted Worker 与 Credential Controller 可以使用同一镜像，
但使用不同 command、数据库 role 和权限。Core Worker/Controller 必须是独立
进程/container，不能作为 FastAPI 或 Hosted Worker 中的后台 coroutine。Arena Core
Worker 使用数据库 leader/lease，确保未来误启动多个副本时仍只有一个 finalizer owner。

### 12.2 Queue 与 lease

- PostgreSQL `FOR UPDATE SKIP LOCKED`；
- 有界 batch 和并发；
- Hosted Worker 分别领取 Arena Task execution 与 credential validation job；后者使用
  固定最小 Prompt，不伪装成 AgentTask；
- validation projection 不包含 candidate strategy 或比赛数据；claim、Attempt 和
  terminal completion 只经受限数据库函数；Worker
  不能直接更新 Credential/Config/Binding/Agent，completion 函数原子核对 lease、
  pending job id、candidate/expected hash 与 active Game；
- 两类 queue 使用不同 claim 路径；Arena Task 保留并发槽/高优先级，validation
  低并发，创建潮不能占满比赛执行容量；
- Credential Controller 只领取 revoke/delete lifecycle job；
- claim 写入 `leased_by` 与 `lease_expires_at`；
- Provider 调用前先持久化 `request_sent_at`；
- request 未发送时崩溃可安全重新领取；
- request 已发送且状态未知时，若 Provider 无可靠 idempotency/status lookup，
  标记 `unknown` 并按 deadline 收敛，不盲目重放；
- Worker 通过受限 Result Sink 提交 candidate，Result Sink 在同一事务中以 CAS
  竞争 Task 终态并插入唯一 Result；
- Task 已终态时，late/duplicate 只追加 Event，不插入第二个 Result；
- Worker 停止领取超过 deadline 或 Binding disabled 的 Task；
- Arena-owned Deadline Finalizer 独立扫描所有过期 `queued/leased/running` Task，
  与 Result Sink 竞争同一 CAS；Worker 全部宕机也会生成 default；
- Hosted Worker 的 PostgreSQL role 尝试写 pool/inventory/settlement/DDL 必须被 DB
  权限拒绝；直接写 Credential/Config/Binding/Agent/validation terminal 同样必须
  被拒绝。

### 12.3 Deadline 与 retry

- deadline 来自 Task；
- Worker 只读取 Task 创建事务冻结的 snapshot/config ref，不实时查询 Arena 状态；
- 每次调用前计算剩余时间；
- 只对定义的 transient/invalid-output 错误重试；transport 只有在 Adapter
  能证明请求尚未发送时才可重试，read/ambiguous timeout 必须标为 unknown；
- 最多两个 Attempt；
- 重试前必须保留安全缓冲；
- Decide default 一次 `pass`；
- Negotiate default 一次 timeout；
- 迟到结果追加审计，不触发业务回调。
- Provider `Retry-After` 超过剩余时间时不等待重试；
- 每用户/每局/每日配额、input/output 上限、全局/Provider 并发和 queue depth 在领取
  前检查。

### 12.4 浏览器离线验证

E2E：

1. 创建 ready Hosted Agent；
2. 创建两个带 future deadline 的测试 Task；
3. 第一个 Task 完成后关闭浏览器；
4. 等待第二个 Task 被调度；
5. 验证 Worker 完成；
6. 重新登录后查看两个 Task 的 owner 私有摘要；
7. 重启 Worker，在 queued/running/unknown 三个点验证恢复。
8. 完全停止 Hosted Worker、保持 Arena Core Worker 运行，验证 Finalizer 仍将 expired
   Task 收敛。
9. 尝试用 Worker DB role 更新 inventory/settlement，验证 PostgreSQL 拒绝。

### 12.5 退出门槛

- [x] 本地双 Hosted Agent 执行不依赖持续打开前端页面；
- [x] Task、Result、Attempt 与 Runtime Run 持久化在 PostgreSQL；
- [x] lease 过期任务可重新领取，request_sent unknown 不盲目重放；
- [x] 每个 Task 只产生一个业务终态；
- [x] 最多两个 Attempt；
- [x] Result Sink/Consumer 重放不重复应用；
- [x] Finalizer 与 Result Sink 竞态只有一个终态；
- [ ] Credential Controller 与 Worker IAM/DB role 彼此越权失败；
- [ ] validation complete 函数只接受当前 lease/job/hash，且不会授予 Worker 直接
      Config/Binding 写权；
- [x] 2 vCPU/4 GB 单机 Compose 已设置单副本 CPU/内存上限；
- [ ] 真实服务器浏览器关闭、API/Worker 重启与资源压力仍待验收。

## 13. Phase 6：Arena 接线与可观测投影

### 13.1 Runtime Port

在 Arena Gateway 建立统一接口：

```python
class ArenaRuntimeAdapter(Protocol):
    async def dispatch(task: ArenaAgentTaskV1) -> DispatchReceipt: ...
    async def cancel(task_id, reason) -> CancelReceipt: ...

class AgentTaskResultSink(Protocol):
    async def submit(result: AgentTaskResultV1) -> ResultReceipt: ...
```

实现顺序：

1. `RuleRuntimeAdapter`：确定性验证 Game Core；
2. `HostedRuntimeAdapter`：写 AgentTask queue；
3. `ConnectorRuntimeAdapter`：映射到版本化 Connector typed task/result；
4. `NativeA2ARuntimeAdapter`：Post-MVP。

Runtime receipt 只证明接收，不改变 Arena 状态。

Result path：

```text
Runtime -> Result Sink -> durable result inbox
  -> Arena Result Consumer
  -> validate/apply once
  -> applied/rejected ACK
```

- Sink 先在内存执行 PublicOutputPolicy，只把 sanitized candidate 交给持久层，再使用
  数据库时钟写 `result_received_at`；
- Result 与 Finalizer 通过 Task CAS 竞争唯一终态；
- Consumer 的业务写入与 apply ACK 同事务；
- duplicate/late 只追加 Event；
- Connector/Native 都必须回传唯一 terminal Result，不能把 stream event 当结果。

### 13.2 Decide vertical slice

```text
Round DECIDE
  -> Arena creates one task per active Game Agent
  -> Task Factory freezes participant view/config/hash
  -> Runtime submits candidate action
  -> Result Sink records result_received_at
  -> Arena validates phase/assets/schema
  -> valid buy/sell uses result_received_at as authoritative enteredAt
  -> invalid/timeout writes one pass
```

FCFS、pool 与 pairing 由 Game Core 负责，不由 Hosted Service 负责。

### 13.3 Negotiate vertical slice

```text
Arena negotiation turn
  -> one negotiation_id
  -> Arena-generated turn_sequence
  -> task for current actor
  -> Runtime submits action=propose/accept/reject
  -> Arena validates alternation/latest quote/turn/deadline
  -> writes public negotiation message
```

Connector 已新增唯一 terminal structured result；只有 Runtime driver 明确识别的
Claude/Codex terminal frame 会进入严格 action parser，不能从普通 `runtime.message`
或 stdout 推断动作。

### 13.4 PublicOutputPolicy

`message` 是不可信的模型/Runtime 输出。所有 Runtime 共用
`arena_core/public_output_policy.py`，并在 Result Sink 的任何 DB/Event/Trace 写入前
执行：

- 检查 Unicode、长度、控制字符与 HTML 安全；
- 拒绝 API-key-like、Authorization、钱包/助记词、email 等 secret/PII pattern；
- 对 Hosted Agent 的私有 strategy 做规范化片段匹配，拒绝明显逐字复述；
- 命中后丢弃原 message，按 `action + price + role` 生成服务端中性模板，并设置
  `message_replaced=true`；
- late/duplicate Event 只记录类别和引用，不携带原 candidate；
- Provider raw response、被替换 message 和 policy error context 均不得进入 DB、日志、
  Trace、Owner API 或审计 payload。

单元/集成测试必须在 repository spy、SQL 参数捕获、结构化日志与 API response 中验证
原 secret/strategy 片段不存在。产品文案同时说明：平台可以阻止明显复制，无法保证
对手不会从公开报价和语义改写中推断策略；策略字段不得用于保存秘密。

### 13.5 投影视图

公开：

- 合法 propose/accept/reject；
- message、价格、sequence、server timestamp；
- timeout；
- accepted pending settlement；
- settlement/confirmation/inventory commit。

Owner-only：

- Provider、Model、thinking；
- Attempt、usage、latency；
- error class；
- final structured action/default reason。

### 13.6 退出门槛

- [x] Hosted 与 Rule Agent 接收同一版本 payload；
- [x] Local Connector adapter 使用同一业务 payload；
- [x] 一个用户每局只有一个参与 Agent；
- [x] Runtime success 不直接写 pool/inventory/payment；
- [x] dispatch ACK、Result submit、Arena apply ACK 三个状态可独立恢复；
- [x] FCFS 只使用 Result Sink 的数据库 `result_received_at`；
- [x] `accept` 只进入 pending settlement；
- [x] secret/PII/strategy 明显片段触发中性模板，原 message 不进入任何持久化或日志；
- [x] 正常公开 message 仍按长度与文本安全规则展示；
- [ ] 创建页明确展示语义推断残余风险。

## 14. Phase 7：PaymentMandate 与 Settlement 依赖

这不是 Hosted Provider Worker 的内部功能，但决定“用户完全离线后是否能完成
交易”。上线目标不是逐笔人工确认，而是用户加入 Game 时一次性同意受限 Mandate，
随后由隔离的 Settlement Worker 使用平台 testnet guest wallet 自动完成每笔
accepted trade。

首发只支持 `sandbox_guest + single_eip3009`。用户自带钱包的无人值守授权、
标准 HTTP x402 和主网不与该阶段并行实现。详细部署和容量目标见
[`hosted-arena-production-runbook.md`](hosted-arena-production-runbook.md)。

### 14.1 需要先冻结

- 每个 Game Participant 一个独立 testnet guest wallet 地址；
- 数据库只保存不透明 `signer_key_id`，签名材料只进入外部 Secret Manager；
- Settlement Worker 使用 guest-signer Secret Creator + exact-key Reader 权限；
- Mandate 只绑定 Game、Participant、chain、token、单笔/累计额度、期限和状态；
- 合法 payee 只能是同一 Game 中 Arena 配对出的 seller；
- Game 冻结 `max_trade_price_atomic`、`game_expires_at` 和
  `required_confirmations = 2`；单笔额度等于最大交易价，累计额度等于
  `round_count * max_trade_price_atomic`，Mandate 与 Game 同时到期；
- `reserve / consume / release` 锁定 Mandate、buyer cash 与 reservation rows，
  额度从 reservation rows 求和，不维护可漂移的聚合计数；
- `(round_id, buyer_participant_id)` 唯一，避免同一 buyer 在同一 Round 并发占款；
- EIP-3009 nonce 由冻结 Intent hash 确定性派生；
- funding 与 Settlement 共用一个数据库化 relay EOA nonce allocator；授权准备和
  链上等待可并发，nonce 分配/广播短暂串行；
- Settlement Worker 独立于 Hosted Worker；后者没有 signer IAM、Mandate 或提交权限；
- Arena Worker 只读查链并提交确认后的库存，不获得 signer 权限；
- Intent 冻结 EIP-712 domain、validAfter、validBefore 和 authorization nonce
  digest，保证重启后可重建同一 authorization；
- 提交前确定失败可以 release；提交后 unknown 只能通过 RPC 恢复或以相同 EIP-3009
  authorization、相同 relay EOA nonce 重发，
  到 `validBefore` 仍未使用才 release；
- revoke 阻止新 reserve，已 reserve/submitted 的 Intent 继续完成，不做链上取消。

### 14.2 接线顺序

1. Hosted Agent 加入 Game 时创建 provisioning account、一次性确认 Mandate；
2. Settlement Worker 以 Participant 为幂等键创建 signer Secret、记录地址，通过
   共用 relay nonce allocator 分发并在两个确认后确认初始 mUSDC；
3. Game 启动前检查全部 wallet/Mandate ready；
4. Arena accept 后冻结 SettlementIntent；
5. Settlement Worker 原子 reserve Mandate；
6. Settlement Worker 自动签名、提交并持久化 tx hash；
7. Arena Worker 只读恢复链上结果；
8. 链上确认后在一个事务中 consume reservation 并幂等提交库存；
9. known failure release reservation、关闭 pairing 并继续当前 Round。

### 14.3 退出门槛

- [ ] Agent 无钱包私钥；
- [ ] accepted trade 不需要逐笔人工确认；
- [ ] 浏览器关闭后自动完成 Mandate reserve、签名、提交、确认和库存提交；
- [ ] 超单笔/总额/期限/范围的 Deal 无法提交；
- [ ] revoke 后不能创建新支付；
- [ ] 同一 buyer/round 的并发 reserve 最多成功一次；
- [ ] submitted unknown 按同一 nonce 恢复，不产生第二份 authorization；
- [ ] 2 笔并发 Intent 的 relay EOA nonce 连续，无碰撞、gap 或重复付款；
- [ ] receipt 经 2 个确认、block hash 与 calldata/event 复核后才提交库存；
- [x] 单笔 Intent 的重复请求不能双花或双记库存；
- [ ] 链上确认前 UI 不显示 completed trade；
- [ ] 10 Hosted Agent 的五回合 MVP Game 包含多笔自动支付并完成排名；
- [x] 当前实现仍准确标注 testnet direct settlement，而非完整 x402。

## 15. Phase 8：部署、E2E 与延迟校准

### 15.1 Compose

增加：

- Arena Core Worker service；
- Hosted Worker service；
- Credential Controller service；
- Settlement Worker service；
- Arena migration scope；
- feature flag；
- Worker queue/concurrency；
- Hosted secret backend、master-key file 或 Tencent SSM credential injection；
- guest signer 与 relay account 的独立 Secret read 配置；
- Provider allowlist config；
- health/readiness；
- log rotation；
- CPU/memory limits。

不增加公网 Worker/Controller port。PostgreSQL 保持 internal network only。API、
Arena Core、Hosted Worker、Credential Controller、Settlement Worker 和 migration
使用独立数据库 role。生产配置保持单个 PostgreSQL、单个 API 和单个 Settlement/
Arena Worker；Hosted Worker 以 4 副本运行，Settlement 路由到 4 个独立 EOA
Facilitator 服务。不增加 Redis、Kafka 或 Kubernetes。

Provider 出站只允许固定 HTTPS Host，关闭 redirect/env proxy，并在主机层增加 egress
firewall/proxy。容量测试冻结单局最大 Agent 与同时 Game 数；超过容量时拒绝开局，
不能用无限排队伪装容量。Provider 限流、Worker 调度产生的 launch skew 会被计入
`result_received_at` FCFS，因此 100 Agent 实测通过前不宣称 Tournament 公平性。

当前生产配置为：默认开赛阈值 10 Agent、硬上限 100、默认 8 回合、同一时间
一局 active Game、4 个 Hosted Worker × 25 task slot、Settlement 执行并发 4，
并确定性路由到 4 个独立 relay EOA。旧 2 vCPU / 4 GB / 70 GB 单机基线已不再
适用。4 vCPU / 8 GiB 已完成两个单次 100-Agent 容量点，但仍须补齐分档和重复
运行。具体 `action_timeout_ms` 由真实 Provider 的
10/12/25/50/100 Agent wave 延迟测试冻结，并覆盖 Decide 排队以及最多三轮 Negotiation；
`settlement_timeout_ms` 首发冻结为 600000，authorization 有效期 420 秒，剩余
180 秒用于过期区块的两个确认和最终恢复；unknown 不能算终态，超时仍无法安全判定时
Game 进入 `settlement_recovery_required` 并判定本次验收失败。12 Agent 只作为
历史回归基线，100 Agent 是当前生产配置的容量门槛。

### 15.2 依赖与供应链

- 将 `httpx`、Tencent SDK 和实际 Provider 依赖加入 `requirements/production.in`；
- 重新生成带 hash 的 production lock；
- 为测试依赖建立可重复安装方式；
- 增加 secret scan；
- 增加依赖漏洞扫描；
- 增加 SBOM；
- 不把真实 Key、云凭据或 testnet private key 提交到仓库。

计划工具固定为：Gitleaks（secret）、pip-audit（Python）、npm audit（Node）和 Syft
CycloneDX JSON（SBOM）；进入 CI 前先在本仓库验证版本与命令，再把精确命令写入
README/CI。

### 15.3 延迟基准

按每个启用的：

```text
Provider x Model x thinking mode
```

采集：

- credential check latency；
- Invoke P50/P95/P99；
- output validation failure rate；
- 429/5xx/transport rate；
- retry rate；
- timeout/default rate；
- input/output/reasoning tokens；
- 2/5/10/12/25/50/100 Agent 整轮 wall time；
- Worker queue age、CPU、内存。

Arena Task queue 与 credential validation queue 使用不同 claim 路径。比赛 Task 有高
优先级或保留并发槽；validation 采用低并发，创建潮不能饿死有 deadline 的行动。

再选择统一 Game `action_timeout_ms`。该值是部署配置与 Game 快照，不在 Provider Adapter
中硬编码。

2026-07-25 开发证据：本地 scripted Provider 已完成 12 Hosted Agent × 5 回合和
12 Hosted Agent × 10 回合持久化演示。该证据验证队列、Task 并发、Arena 编排和
数据库投影，不是 Real Provider P95/P99，也不能据此冻结生产并发或 timeout。

### 15.4 E2E 矩阵

| 场景 | 期望 |
|---|---|
| 创建 -> ready | 一次提交，Secret 不回显 |
| 浏览器关闭 | Hosted Task 继续 |
| Worker restart before request | Task 安全重领 |
| Worker restart after request_sent | unknown/default，不盲目重放 |
| Hosted Worker 全部停止 | Arena Core Worker 的 Finalizer 仍收敛 expired Task |
| 429 -> success | 恰好两个 Attempt |
| 400 | 恰好一个 Attempt |
| invalid output | 时间允许时最多重试一次 |
| deadline 不足 | 不重试，确定性 default |
| Secret revoke | 新 Task fail closed |
| SSM write 前/后 API crash | pending row 可恢复，无不可发现 orphan |
| config update during Game | 当前 Game 快照不变 |
| concurrent join | 一个 Participant |
| join 与 replace/PATCH 竞态 | 只允许一个方向成功，无比赛中途配置切换 |
| validation Worker 重启 | 按 `next_attempt_at` 恢复，不重复建 job |
| duplicate completion | 一个业务动作 |
| Result/Finalizer race | 一个 Task 终态、一个业务动作 |
| Worker 越权 SQL | PostgreSQL 拒绝 inventory/settlement/DDL 及直接 Config/Binding 写入 |
| stale validation completion | 受限函数拒绝错误 job/hash/lease，不切 candidate |
| redirect/proxy/metadata/large response | 出站请求被拒绝且 Key 不外带 |
| validation 创建潮 | 保留的比赛 Task 槽仍满足 deadline |
| Provider usage absent | 显示 incomplete，不伪造 |
| reasoning text returned | persistence/API 中不存在 |
| message 含 secret/PII/strategy 片段 | 使用中性模板，原文不在 DB/log/Trace/API |
| accepted negotiation | 自动 reserve、签名和提交，不等待逐笔人工确认 |
| Settlement Worker restart before submit | 同一 Intent 可安全重领 |
| Settlement Worker restart after submit | 按同一 tx hash/nonce 恢复，不生成第二笔支付 |
| Mandate over limit/expired | 不广播交易，reservation 可释放 |
| chain confirmed | 幂等提交库存一次 |

### 15.5 退出门槛

- [ ] 生产镜像与 Compose build 通过；
- [ ] 真实 PostgreSQL migration/restart 通过；
- [ ] 公网单机 encrypted vault 生命周期与重启恢复通过；
- [ ] 可选腾讯 SSM 生命周期通过；
- [ ] 至少一个真实 Provider 的 structured invocation 通过；
- [ ] guest signer/relay Secret 权限与 Hosted/Arena Worker 隔离；
- [ ] accepted trade 自动完成当前链路的新鲜 testnet 支付；
- [x] 100 Agent 受控场景产生 50 笔 accepted trade，经 4 个 shard 全部安全终态；
      2026-08-09 实测为 50/50 唯一交易、确认和 InventoryCommit；
- [ ] 600 秒内无残留 `submitted_unknown`；进入 `settlement_recovery_required`
      必须停止排名并使本次验收失败；
- [ ] 外部网络 E2E 保存证据；
- [ ] 10/12/25/50 Agent × 8 回合分档与重复运行通过；
- [x] 100 Agent × 8 回合在 4 vCPU / 8 GiB 主机完成单次有支付和无支付实测；
- [ ] 默认 timeout 有数据依据；
- [ ] 安全扫描无真实 secret。

## 16. 验证命令与证据

当前仓库已有、实施期间继续运行：

以下代码块均从仓库根目录独立执行。

```powershell
python -m pytest -q -p no:cacheprovider
```

前端：

```powershell
Set-Location frontend
npm ci
npm run build
```

Connector typed result 接线后：

```powershell
Set-Location connector
go test -count=1 ./...
go vet ./...
go build ./...
```

Compose 静态验证：

```powershell
docker compose -f docker-compose.production.yml config
```

生产镜像：

```powershell
docker compose --env-file deploy/.env -f docker-compose.production.yml build
```

计划新增的测试分组：

```text
tests/test_arena_agent_contracts.py
tests/test_arena_core_task_factory.py
tests/test_arena_core_result_consumer.py
tests/test_arena_core_finalizer.py
tests/test_hosted_agent_runtime_security.py
tests/test_hosted_agent_api.py
tests/test_hosted_agent_worker.py
tests/integration/test_arena_runtime_postgres.py
tests/integration/test_tencent_secret_store.py
tests/e2e/test_hosted_agent_browser_offline.py
```

真实 Provider/Secret/E2E 测试默认不在普通单元测试中运行，必须：

- 使用临时凭据；
- 明确 test 标记；
- 受限额度；
- 测试后撤销；
- 保存脱敏结果；
- 不在 CI log 打印请求体或 Secret。

计划使用独立 pytest marker：`postgres`、`tencent_ssm`、`provider_e2e`、
`hosted_e2e`。marker 与命令在测试文件落地后写入 README 并实际验证，不在本设计阶段
把未存在的命令报告为可运行。

上述供应链工具接入 CI 前必须先验证仓库和 CI 环境，不能只在计划中写“已扫描”。

## 17. Rollout

### Stage 0：默认关闭

- 代码、migration 和测试合入；
- `ADX_HOSTED_AGENTS_ENABLED=false`；
- 不对外显示创建入口。

### Stage 1：内部测试账户

- 一个真实 Secret backend；
- 一个真实 Provider/Model；
- 仅内部用户；
- 验证 secret lifecycle、browser-offline、restart 和 usage。

### Stage 2：邀请制 testnet beta

- 多个 allowlisted Model；
- 用户 BYOK；
- 每用户/每局调用配额；
- 公开协商 + owner 私有执行视图；
- testnet-only 标签；
- 监控 timeout、retry、cost 和 error。

### Stage 3：外部 beta

只有在以下条件满足后：

- Secret 权限与审计验证；
- 真实负载校准；
- 自动 PaymentMandate、guest signer 与 accepted trade 无人值守结算已通过；
- 备份、恢复、告警和事故处理；
- 安全/依赖扫描；
- 完整 E2E 证据。

## 18. Rollback

安全回滚顺序：

1. 关闭 Hosted 创建入口；
2. 设置 `ADX_HOSTED_AGENTS_ENABLED=false`，停止领取新 Task；
3. 对已领取 Task 按 deadline 收敛，不强行制造成功；
4. 停止 Hosted Worker；
5. 保留 Agent/Game/Task/Payment 历史，不删除审计证据；
6. 必要时撤销 Worker 的 Secret read 权限；
7. Local Connector 路径继续独立工作；
8. 不通过 drop table 或改写历史 migration 回滚。

若 Provider 或 Secret backend 出现安全事件，先禁用相关 Provider capability 与 Binding，
再按 credential ownership 以新 row 替换或撤销，不切换到明文配置。

## 19. Post-MVP：Native A2A 与 Agent Studio

### 19.1 Native A2A

新增 `NativeA2ARuntimeAdapter`：

- 发现静态 Agent Card；
- 把 Arena Task 映射到标准 A2A Task/Message；
- 把结构化 Artifact/DataPart 归一成 AgentTaskResult；
- 处理远端 auth、deadline、cancel 和状态订阅；
- 仍由 Arena Gateway 中转与审计；
- 不让远端 Task success 直接成为成交或支付证明。

如果首期时间不足，该 Adapter 可以完全不实现，不阻塞 M1 Runtime Foundation 或
M2 Arena Integration。

### 19.2 通用 Agent Studio

Phase 1 已冻结 Driver 扩展点：

```python
class AgentRuntimeDriver(Protocol):
    async def execute(task_snapshot, deadline) -> AgentTaskResult: ...
```

Runtime v2 的 `HostedArenaAgentRuntime` 使用 PydanticAI 执行一个逻辑 AgentTask。
未来可以在该边界内新增：

- 受控多步骤 Planner；
- allowlisted MCP/平台 Skill；
- 评估与版本发布。

Agent Studio 仍必须遵守：

- 同一 AgentTask/Result schema；
- 同局统一 deadline；
- Arena participant view；
- no direct Arena write；
- no wallet private key；
- no required chain-of-thought；
- 入局配置快照；
- 工具 allowlist 与审计。

Agent Studio 不进入本次 MVP 的代码范围。

## 20. 明确未冻结但不阻塞基础实现的事项

| 事项 | 当前处理 |
|---|---|
| 首发 Provider 数量 | Adapter 架构支持多个；只展示完成真实 E2E 的组合 |
| 默认 action timeout 数值 | 真实 P95/P99 + buffer 后冻结 |
| Tencent Secret region/name policy | 部署前确定，不影响 SecretStore port |
| 私有 strategy instructions 保留期 | 先设为部署配置，外部 beta 前形成产品政策 |
| Guest signer Secret 命名与 region | 部署前确定；签名机制已冻结为 `sandbox_guest + single_eip3009` |
| Native A2A auth/binding | Post-MVP |
| 多 Runtime failover | Post-MVP，需重新评估公平性 |
| 跨局策略学习的自动晋级阈值 | Runtime v2 先持久化 candidate/replay 证据，再用真实比赛校准 |

以上事项不得被实现者用不安全默认值静默决定。

## 21. M3 Full Program Definition of Done

“Hosted Agent 可离线完成 testnet 交易”只有在以下全部成立时才算完成。M1/M2 按
第 1.3 节单独验收，不得提前使用 M3 表述：

- [ ] 用户可一次提交创建 persistent Hosted Agent；
- [ ] 用户 API Key 由生产 Secret Manager 托管且无泄漏证据；
- [ ] Agent/Credential/Config/Route/Task/Result/Attempt/Event/Provisioning/Lifecycle Job
      均持久化；
- [ ] 一名 User 每局只有一个 Game Agent；
- [ ] 同一 Agent 可参加下一局；
- [ ] join 与异步 Runtime PATCH/credential replace 竞态不会产生局中切换；
- [ ] credential validation 可跨重启恢复，revalidate 不产生重复 active job；
- [ ] thinking 行为按 Provider capability 执行；
- [ ] 不保存 private chain-of-thought；
- [ ] usage、latency、公开文字和最终动作可审计；
- [ ] PublicOutputPolicy 在持久化前替换明显 secret/PII/strategy 片段且不保留原文；
- [ ] 浏览器和用户电脑离线后 Hosted Agent 仍运行；
- [ ] deadline 可配置、同局统一并由真实测试校准；
- [ ] 最多一次重试、无 Provider/Model/Runtime fallback；
- [ ] Decide/Negotiate 失败确定性收敛；
- [ ] duplicate/late result 不改变业务终态；
- [ ] Agent 不直接通信，所有交互由 Arena Gateway 中转；
- [x] Runtime success、协议达成、链上确认和库存提交状态分离；
- [ ] PaymentMandate 的 reserve/consume/release、额度、期限、范围和 revoke 已实现；
- [ ] Sandbox Signer 与 Hosted Worker 权限完全分离；
- [ ] accepted Deal 经真实 Injective EVM testnet 确认；
- [ ] 链上确认后 Arena 库存只幂等提交一次，unknown/reorg 可恢复；
- [x] testnet-only、EIP-3009 与 x402 的能力边界准确；
- [ ] 生产 Compose、真实 PostgreSQL、真实 Secret Store、真实 Provider 和公网 E2E
      均有脱敏证据；
- [ ] README、product、game-design、roadmap、agent-onboarding 和 settlement 文档已同步；
- [ ] frozen specs、compatibility identifiers 和无关用户改动未被破坏。

## 22. Runtime v2：PydanticAI 直接替换计划

本节是 2026-08-05 起的当前实施权威。旧 Phase 3 的 DirectModelDriver 内容只描述
历史基线；对应实现与测试已经删除，不再定义或参与当前 Runtime。

### 22.1 保留、替换和退役

| 决策 | 范围 |
|---|---|
| 保留 | Agent identity、Hosted config、Secret ports、AgentTask/Result、lease、Attempt、Result Sink/Consumer、Finalizer |
| 重构 | Durable Hosted Worker、Provider composition、official pool bootstrap |
| 直接替换 | `DirectModelDriver`、`PromptBuilder`、比赛决策用 `ProviderAdapter.invoke` |
| 已删除 | legacy Driver/Prompt 实现、对应测试、Worker fallback 与无调用引用 |

生产切换后不得按请求或错误静默 fallback 到旧 Driver。数据库迁移保持 forward-only；
运行故障通过完整部署版本回滚处理。

### 22.2 目标模块

```text
hosted_agent_runtime/
  arena_agent.py
  runtime.py
  context.py
  model_factory.py
  strategy.py
  tools/
  memory/
  learning/
```

`Agent[ArenaAgentContext, HostedAgentRunOutput]` 组合：

- 平台 instructions；
- 冻结 Strategy Revision；
- `RunContext` 中的不可变 Task 和已应用 Game Memory；
- 只读分析工具；
- `UsageLimits(request_limit, tool_calls_limit, output_tokens_limit)`；
- typed terminal action、safe decision summary 和 pending memory patch。

不持久化 PydanticAI raw messages、ThinkingPart、Provider reasoning text 或私有
chain-of-thought。

### 22.3 官方策略目录与抽取

官方池一级策略固定为：

- `aggressive`；
- `conservative`；
- `balanced`。

每个一级类型可以有多个固定数值变体。Bootstrap 为每个官方 `agent_id` 持久化类型
和 active Strategy Revision。补位候选使用：

```text
stable_random_key =
  hash(game_id, agent_id, "arena.official-selection.v1")
```

相同 Game 的重试得到相同候选顺序；已经落入 `game_participants` 的身份不会被替换。
入局事务把 Strategy Revision 写入 `game_agents` 快照并初始化
`hosted_agent_game_memory`。一名玩家加九个官方 Agent 时，九个席位分别绑定九个
持久身份和九份独立记忆。

### 22.4 实施里程碑

#### V2-0 文档冻结

- [x] 更新 Hosted Spec、Implementation Plan 和 Game Design；
- [x] 冻结策略类型、随机抽取单位和状态保存权威；
- [x] 明确无 Runtime fallback、无 direct Arena write、无 CoT persistence。

#### V2-1 状态与抽取 foundation

- [x] 增加 Strategy Revision、Game Memory 和 pending memory patch migration；
- [x] official pool 增加 strategy archetype；
- [x] 使用 Game-scoped 稳定随机候选顺序；
- [x] 入局冻结 revision 并初始化独立 Game Memory；
- [x] 增加最小权限 SQL 与静态 migration contract 测试；
- [x] 使用隔离 PostgreSQL 完成 migration、入局冻结、applied/defaulted memory
  gate 集成验收；
- [ ] 完成 rejected/late、并发 CAS 和 Worker 重启集成验收。

#### V2-2 PydanticAI Runtime core

- [x] 锁定 `pydantic-ai-slim[openai]==2.24.0`；
- [x] 实现 allowlisted Model factory；
- [x] 实现 Agent instructions、typed output、只读工具和 UsageLimits；
- [x] 使用 TestModel 覆盖多步工具、输出纠正与 Worker 闭环；
- [x] 不保存 raw message history/thinking content。

#### V2-3 Worker cutover

- [x] 生产 Durable Worker 改为构造 `HostedArenaAgentRuntime`；
- [x] 一个 DB Attempt 包裹一个 bounded PydanticAI run；
- [x] 将 run 的 request/tool 安全计数加入 Attempt 持久化；
- [x] candidate action 仍只进入现有 Result Sink；
- [x] pending memory patch 绑定具体 Runtime Result digest，只在该 Result
  applied 后 CAS 提交；
- [x] credential validation probe 与比赛决策引擎分离。

`arena-scripted` Provider 仅可留作孤立 Provider 单元 fixture；Hosted Worker
不再发布或执行该比赛路径，也不存在 PydanticAI 失败后的生产 fallback。

#### V2-4 跨比赛学习

- [x] `game.completed` 创建幂等 durable learning job；
- [x] 只读取可验证的行动、成交、价格、净值、排名、失败和 usage 汇总；
- [x] bounded PydanticAI learner 生成 candidate Strategy Revision 和安全证据
  摘要，不持久化 raw messages 或 reasoning；
- [x] learner 调用前要求至少两个 task、至少一个真实 candidate action、至少一笔
  `settled` 交易和非零相对净值结果；单步、default-only、无成交或纯随机组合收益
  不进入策略学习；
- [x] 严格 schema、策略类型 envelope、单局每维最多 1000 bps 变化和历史动作
  计数回放全部通过后，只对下一场 Game 激活；模型自报 confidence 仅作审计信号，
  不作为安全权威；
- [x] 保存完整 revision/evaluation 历史；已学习版本相对 parent 的平均结果分数
  严重下降至少 2000 bps 时自动恢复 parent，只影响之后加入的 Game。

首版 policy surface 固定为 `risk_budget_bps`、`min_expected_edge_bps`、
`max_inventory_concentration_bps`、`negotiation_concession_bps` 和
`exploration_bps`。当前 replay gate 验证的是历史动作证据完整性、计数一致性和
Arena 合同安全，不是离线经济收益模拟；收益阈值仍需用真实多局结果校准。

2026-08-06 隔离验收在全新 PostgreSQL 上执行 `002`–`066`，并证明：

- 胜局 learning job 通过 TestModel 产生并激活 learned revision；
- 已完成局仍引用原 base revision；
- 下一局加入时冻结 learned revision 和渲染后的 bounded instructions；
- 下一局严重退化时恢复 parent revision，而该局已冻结的 learned revision 不变。

这份证据验证持久化、最小权限函数、Worker 编排和跨局版本边界，不等同于私有
LiteLLM 真实调用、真实策略收益或生产部署验收。

私有 LiteLLM + DeepSeek V4 Flash 的隔离真实模型验收同时发现并关闭了
“看似成功但证据不足”的边界：

- learner 实际执行 3 个 request 和 2 个只读 tool call，typed candidate 经过
  确定性 gate；模型自报 confidence 仅作审计；
- 首次 payment-disabled 单回合 1+9 虽然 10/10 决策成功，但从无成交局激活
  learned revision 的结论被后续审计撤回；净值差来自初始组合和事件价格，不能
  证明策略行动有效。当前所有此类 job 会在模型调用前拒绝；
- 三回合 `regression-real-hosted-1plus9-v7` 完成 30/30 decide、2/2
  negotiate，形成 iron 配对、`5.880600` 报价和 accept；支付关闭后按设计进入
  `settlement_failed`，没有 SettlementIntent；
- PydanticAI 的 DeepSeek profile 现在把上限发送为 `max_tokens`，而不是错误的
  `max_completion_tokens`；非 thinking/thinking 单请求上限为 8192/16384，
  Agent-run 累计上限 65536。`request_limit` 耗尽作为结构化输出失败允许同
  Runtime 唯一重试，并保存已经产生的 usage；
- v7 暴露下一回合可能在上一 applied memory patch 投影前加载旧版本；迁移 `065`
  在加载新 task context 前按同一 `game_agent_id` 投影全部已终态 patch。全新
  PostgreSQL 集成测试已证明第二个 task 读取 memory v1，而不是再次从 v0 生成
  会变 stale 的补丁。
- 修复后的 `regression-real-hosted-1plus9-v10` 以退出码 0 完成 34/34 task：
  30/30 decide、4/4 negotiate、warhorse/iron 两次 proposal/accept，10/10
  Game Agent 至少推进到 memory v3；payment-disabled 下 10 个 learning job
  全部因无 `settled` 交易被拒绝，并保持 0 SettlementIntent。
- 全新 PostgreSQL `002`–`066` fault-injection 使用不同 Worker identity 验证
  claim 互斥、`created` 前置崩溃重试、`request_sent` 后 unknown/no-replay、
  Result accepted/duplicate/conflict CAS、late Result 和 learning lease 重领。
  实验还发现非法 candidate 会以 `default_pass` 被 Arena 确定性消费，不能只凭
  `apply_status=applied` 推进记忆；迁移 `066` 现在要求
  `application_outcome=candidate`，并已证明 `good_not_allowed` 对应 patch 被
  `discarded`、memory version 不变。
- `python tests/hosted_worker_process_recovery_e2e.py run` 进一步构建当前生产
  镜像、应用全部迁移并 provision 最小权限角色，然后对独立
  `hosted_agent_runtime.production_worker` 容器执行真实 `SIGKILL`。Attempt
  前崩溃由新 Worker identity 在 30 秒 lease 到期后完成 Attempt 1；
  `request_sent` 后崩溃由另一 Worker 收敛为
  `request_outcome_unknown`，Provider 协议请求保持 1。隔离 AES-GCM 密钥卷和
  本地 LiteLLM 协议替身随实验清理，支付始终关闭。

这些证据已经覆盖外部多容器进程 kill/restart，但仍是 payment-disabled 隔离
实验，不等同于真实 `settled` 跨局收益或生产部署验收。

2026-08-06 又完成两场隔离、payment-enabled 的三回合 1+9 canary。两局共四笔
mUSDC Intent 均经自建 Facilitator 在 Injective EVM testnet 确认后提交库存，
Pairing/Negotiation 才进入 `settled`。第一局玩家以 owner revision 1、排名 1
和一次本人 settled 交易激活 learned revision 2；第二局加入时冻结的正是
revision 2，并实际产生 `buy/sell/pass` 各一次。第二局玩家没有成为 settled
配对方，因此保持 revision 2，符合“无本人结算不学习”的 preflight。

该真实链路还发现 learner 虽有 8192 的 PydanticAI run budget，Worker 却把
Provider 输出再次截成 2048，导致 DeepSeek typed proposal 触发 token limit。
现在已把已清洗的权威 evidence snapshot 直接放入 learner 上下文，保留只读工具
供可选复查；结构化输出失败允许一次 durable retry，且 Provider 与 run 的输出
上限统一为 8192。修复后第二局四个真实成交方的 learning job 均在第一次 Attempt
激活新 revision，六个无成交方全部在模型调用前拒绝。该 canary 使用隔离 mUSDC，
不能替代 `arena402-g`、公共 Facilitator、生产部署或统计性收益验收。

同日的 Phase D 混合 Runtime 中间验收
`phase-d-mixed-musdc-v4-c30a038913` 又把一名真实 Codex Connector 与九名
Hosted Agent 放进同一场八回合 `agent_a2a.v1`。92 个 Task 全部 applied，
产生三笔确认后提交库存的 mUSDC Deal 和十条排名；priority 3/5 分别激活
revision 9/8。后续 `phase-d-revision-freeze-v1-df1bea4ee0` 冻结并实际运行
这两条 revision，证明学习只影响未来 Game。另一名有 settled 信号的 Agent
连续两次返回无效结构化输出，因此 job 失败并保留旧 revision；这属于 bounded
失败，不记作学习成功。

真实局还暴露并修复了三类恢复边界：超过六位小数的候选价格、按原
idempotency key 恢复已过期的冻结 Task，以及 SECURITY DEFINER 投影函数的最小
列级权限。迁移 `067`–`073` 只修复可证明的旧失败记录，新的候选仍由 Arena
Result Sink fail closed。隔离 mUSDC 证据仍不替代 `arena402-g` 或生产切换。

#### V2-5 删除与生产验收

- [x] 生产入口显式注入 `PydanticModelFactory`，不再调用 DirectModelDriver；
- [x] 人工确认后物理删除 legacy Driver/Prompt 实现及对应测试，并迁出 Attempt
  与 Runtime 合同常量；
- [x] 官方 Agent 与私有 LiteLLM 上游都固定为 `deepseek-v4-flash`；
- [x] 完成单玩家 + 九官方 Agent 的三回合 payment-disabled 决策、配对和谈判 E2E；
- [x] 完成真实 PostgreSQL 多 Worker identity、lease expiry、Attempt 恢复与
  Result CAS/late 的隔离 fault-injection；
- [x] 完成生产 Worker 外部多容器进程 `SIGKILL`/restart 恢复；
- [x] 完成两局 payment-enabled、四笔 `settled` 的初始策略收益对比；样本仍
  不足以校准 archetype 优劣或自动回滚阈值；
- [x] 证明不同策略类型和独立 Game Memory；
- [x] 证明下一局学习、历史 revision 和只影响未来局的自动回滚；
- [x] 更新 README/Roadmap 的本地隔离证据；部署证据仍待生产切换。

### 22.5 必须通过的行为测试

- 相同 Game 和 pool 状态下，官方候选顺序可复现；
- 不同 Game 的候选顺序发生变化；
- 抽中后整局 identity/archetype/revision 不变；
- 同一 Agent 的两场 Game 使用不同 `game_agent_id` 和独立 Game Memory；
- default_pass/negotiation_timeout/rejected/defaulted/late result 不推进
  memory version；
- 只有 `application_outcome=candidate` 的 applied result 最多推进一次；
- Worker 重启恢复相同 strategy revision 和 memory version；
- PydanticAI 在限制内调用只读工具并返回唯一合法 terminal action；
- raw reasoning、Secret 和不可信公开文本不进入记忆；
- 新 revision 不影响正在进行的 Game。
- learning job 重试、stale-base 拒绝和自动回滚都不会改写已冻结的 Game Agent。

## 23. Phase D5a：不修改 A2A 协议的市场质量优化

### 23.1 冻结边界

本阶段保持已部署 `agent_a2a.v1` 的 wire contract、任务种类、每回合单一成交
slot、最多三次 RFQ 尝试、最多三个合并协商行动和逐 Deal 结算不变。任何需要
多 Intent、Intent revision、双边 standing quote 或一回合多次执行的能力都必须
进入未来 `agent_a2a.v2`，不能修改历史 Game 的协议解释。

本阶段允许版本化升级：

- Hosted Strategy Catalog、官方数值画像和只影响未来 Game 的 Strategy Revision；
- 不改变 Task action union 的 participant-view 安全字段；
- Arena 内部及公开聚合的市场质量诊断；
- Game 创建时冻结的价格目录和事件牌组；
- 有界、确定性、只影响下一回合公开参考价的订单流反馈；
- 前端对 Intent/RFQ/Engagement/Negotiation/Settlement 漏斗的解释。

### 23.2 第一性目标

成交量不是单独的优化目标。首要目标是让独立 Agent 在相同公开事实下形成可解释的
私有保留价和库存效用，使市场自然出现同商品、相反方向且限价可交叠的交易机会，
同时保留 `pass`、拒绝和无成交作为合法策略结果。

当前生产基线已经有十个官方数值画像，但仍共同围绕公开
`eventImpliedFinal`、确定性比例阈值和“界内立即接受”运行。当前世界引擎又让
事件冲击主导公开参考价：上一成交价最多贡献下一轮参考价的四分之一，订单流压力
最多约 2%，而个别事件的 market 冲击达到 20%～100%。因此本阶段优先修复共同
估值锚和供需反馈比例，不把前端展示问题误判为成交原因。

### 23.3 纵向实施顺序

1. 持久化 `arena.market-liquidity.v1` 聚合摘要，不公开 Agent 私有限价或画像；
2. 建立按 Game/round/event seed 可复现的离线重放与 A/B 数据集；
3. 将私有估值固定为公开事件锚、Agent/Game 稳定偏差、库存影子价格、现金约束、
   剩余回合偏好和历史拥挤度的有界组合；
4. 让无成交原因进入 Game Memory，但只有权威经济证据仍可激活收益型 revision；
5. 将 `target / acceptable / walk-away` 三段价格用于真实 counter/accept 决策，
   不为展示效果强制反价；
6. 建立版本化价格目录；第一轮对照继续使用 `2/5/8/3`；
7. 新增 `pawnhouse-standard-v2`，保留 v1 immutable；
8. 完成多局 1+9 A/B 后才允许 Current Game 切换新的 Strategy/Price/Event 组合。

### 23.4 价格与事件版本规则

基础价格不能继续只由进程级 `INITIAL_PRICES` 解释新旧 Game。新 Game 必须冻结：

```text
price_catalog_id
initial_prices
event_deck_id
event_schedule_commitment
market_feedback_policy_version
```

`PRICE_RESET_TO_BASE` 必须读取该 Game 冻结的基础价格，不能读取部署后的最新全局
常量。`pawnhouse-standard-v2` 应覆盖临时流动性、传闻、基本面、反转和跨商品
事件；常规冲击应落在各策略私有估值分布可能交叠的区间，极端冲击保持稀有。
订单流反馈只改变下一轮公开参考价并保持定点整数、上下界、确定性重放，不能生成
虚假 Agent Intent、强制 RFQ 或替 Agent 接受报价。

### 23.5 A/B 退出门槛

每个实验组合必须报告：

- participant、Intent、pass 和按商品/方向分布；
- 同商品反向理论容量、限价兼容理论容量及二者差值；
- RFQ、Engagement、Deal、chain-confirmed、inventory-committed 转化；
- `no_same_good`、`price_incompatible`、`rfq_not_sent`、`rfq_not_engaged`、
  `negotiation_not_accepted` 和 settlement failure；
- 行动方向熵、独立对手覆盖、反价率和平均协商轮数；
- archetype/具体画像的净值、下行风险和事件/商品集中度。

第一轮目标是证明相较生产基线，更多回合出现至少一个可协商对手，同时不提高非法
动作、timeout、负期望强制交易、重复支付或不公平初始净值。正式阈值必须由多局
样本冻结，不能由单局三笔或更多成交倒推。

### 23.6 首个纵向切片

2026-08-06 已完成不改变 `agent_a2a.v1` 的第一批基础：

- [x] 新增 `arena.market-liquidity.v1` 聚合模型；
- [x] A2A Round close 在清理未完成市场对象前幂等发布
  `market.liquidity_summarized`；
- [x] 摘要区分同商品反向容量和限价兼容量，但不公开参与者或私有限价；
- [x] 新 Game config 冻结 `pawnhouse-price-v1`、四个
  `initialPricesAtomic` 和 `arena.market-feedback.v1`；
- [x] `WorldState` 和 `PRICE_RESET_TO_BASE` 使用 Game 冻结基础价格，旧 Game
  缺少新字段时回退到历史 v1；
- [x] Join preflight、确定性默认组合和 `balanced_auto` 组合按同一 Game 冻结
  基础价格保持等值 20 金；
- [x] 建立消费冻结回合观测的离线成对 A/B 评估器与 CLI；
- [x] 从两个明确指定的已完成 baseline/candidate Game 只读导出冻结观测；
- [x] 运行同 seed 的真实 Hosted A/A 基线局，确认商品方向集中和 Provider
  抖动边界；
- [ ] 运行真实 baseline/candidate Runtime 多 seed A/B 并形成可选型样本；
- [x] 注册仅供实验选择、尚未接入 Current Game 的
  `pawnhouse-standard-v2`；
- [x] 注册仅供实验选择、尚未接入 Current Game 的
  `pawnhouse-price-v2`；
- [x] 实现仅供 canary 显式选择的 Hosted `liquidity_v2` 私有估值、库存影子
  价格和 counter policy；生产 Official pool 未切换；
- [ ] 生产 Current Game 切换。

因此当前代码只增加可观测性和历史安全的版本入口，没有改变正在运行的价格、
事件、Agent 动作或 A2A 协议。

### 23.7 离线成对 A/B 评估器

`scripts/run_market_quality_ab.py` 读取
`arena.market-quality-experiment.v1` JSON manifest。control 和 treatment 必须
使用相同 `caseId`、event seed、参与者、公开 archetype 和回合编号；不成对时
fail closed。报告输出：

- Intent/pass、买卖方向、按商品容量和限价兼容量；
- RFQ → Engagement → Deal → chain-confirmed → inventory-committed 漏斗；
- 非法动作、timeout、反价率、协商行动数、对手覆盖和方向熵；
- 总体及各 archetype 的平均收益和下行收益；
- 不包含参与者 identity 或私有限价的输入/设计 SHA-256。

示例：

```powershell
python scripts/export_market_quality_ab.py `
  --experiment-id hosted-policy-ab-1 `
  --control-game-id <completed-control-game-id> `
  --treatment-game-id <completed-treatment-game-id> `
  --output .tmp/market-quality-ab-manifest.json

python scripts/run_market_quality_ab.py `
  --input .tmp/market-quality-ab-manifest.json `
  --output .tmp/market-quality-ab-report.json
```

导出命令只接受已完成的 `agent_a2a.v1` Game，并要求相同 event seed、
Agent/archetype 阵容、初始净值和回合数。它在 PostgreSQL read-only transaction
中运行，使用 `--database-url`、`ARENA_TEST_DATABASE_URL` 或
`ADX_ARENA_CORE_DATABASE_URL`，不使用无权读取私有限价的 API 角色。
导出的 manifest 含 Agent identity 和私有限价，只能保存在已忽略的 `.tmp/`
等 operator-private 位置，不能提交或发布；只有评估后的匿名聚合 report 可交付。

该工具只评估已冻结观测。它不调用模型、不生成 Agent 决策、不模拟协商接受，也不
把理论限价兼容量称为 Deal 或 settled 交易。下一步仍需从相同 Runtime 配置、
人数、初始资产、event seed 和回合数运行真实 baseline/candidate Game；在形成
多 seed 样本之前不能据示例结果选择 V2 参数。

### 23.8 同 seed Hosted A/A 证据与 V2 实验候选

2026-08-07 在隔离的 DeepSeek Hosted Runtime 中，使用
`phase-d5a-seed-01`、10 个 Hosted Agent、8 回合、相同阵容、相同手工组合、
`agent_a2a.v1` 和 payment-disabled 连续运行三局：

- `market-baseline-a-20260807`：严格 canary 通过，92/92 AgentTask
  `completed`；
- `market-baseline-b-20260807`：第 2 回合出现 Provider 延迟尖峰，
  11/80 个 Intent task default，严格 canary 失败；
- `market-baseline-c-20260807`：只有 1 个 permanent-request default；除
  timeout 外，匿名经济聚合与第一局完全一致。

第一局和第三局共同得到：

```text
participant-round       80
non-pass Intent          58
pass                     22
buy / sell               44 / 14
RFQ / Engagement / Deal  6 / 3 / 3
opposite / compatible    3 / 3
compatible rounds        3 / 8
```

四个商品中只有 grain 同时出现买卖：grain 为 6 buy / 14 sell，
iron 为 21～25 buy / 0 sell，warhorse 为 8～12 buy / 0 sell，
gems 为 5 buy / 0 sell。A/A 中 3 个 Deal 和全部可交叠容量都来自 grain。
这证明当前主要约束不是协商动作失败，而是官方数值画像、库存目标和初始组合
共同导致非 grain 商品没有卖方。相同 seed 仍可能遇到 Provider 抖动，因此
正式 A/B 必须把 timeout 作为无效样本或独立运行时指标，不能把 default action
计为策略收益。

基于该证据，先注册两个不自动启用的实验候选：

- `pawnhouse-price-v2`：grain/iron/warhorse/gems 为
  `2.5 / 4 / 6 / 3` 金，将单件价格跨度由 4 倍压缩到 2.4 倍，降低
  `quantity=1` 下的票面离散度；新组合仍必须按该目录保持严格等值 20 金；
- `pawnhouse-standard-v2`：十张独立牌，对四个商品都包含正向和负向的
  market-only 冲击；单次常规价格冲击控制在 4%～9%，任何价格冲击不超过
  10%，配套 final 变化为 2%，并包含两张跨商品轮动牌。

上述注册只提供显式实验选择；`STANDARD_*` 常量、v1 schedule hash、
Current Game 和生产 Official pool 均未切换。Hosted Strategy V2 下一步采用：

1. 将“目标库存”从禁止出售最后一单位的硬门槛改为有界库存影子价格；
2. 为 Agent × good 冻结小幅、稳定、私有的保留价偏移，避免共同锚完全趋同；
3. 继续叠加现金储备、剩余回合和拥挤度，但总调整保持有界；
4. 至少让多个画像在持有非 grain 商品时能够形成真实卖方，同时不强制 Intent、
   RFQ 或接受报价。

下一次真实 treatment 应先单独替换 Strategy + Event Deck，继续使用 v1 起始价；
确认非 grain 双边容量上升后，再单独测试 Price V2，避免一次同时改变三类变量。

### 23.9 首个 Strategy + Event V2 真实 treatment

2026-08-07 已完成 `liquidity_v2` 的首个真实纵向实现：

- `hosted_agent_runtime.official_market_strategy` 按 Official priority 为
  Agent × good 生成稳定、私有、±350bps 内的偏移；
- 私有估值由画像偏移、商品偏移、库存影子价格、现金储备和剩余回合组成，总调整
  clamp 到 ±1600bps；
- 目标库存改为 utility center，而不是禁止出售最后一单位的硬门槛；
- canary 默认继续使用 `existing + pawnhouse-standard-v1`，只有显式设置
  `CANARY_OFFICIAL_STRATEGY_PROFILE=liquidity_v2` 和
  `CANARY_EVENT_DECK_ID=pawnhouse-standard-v2` 才应用 treatment；
- `baseline_v4` 可使用新的 idempotency key 明确恢复隔离 Official pool，避免
  后续 control 静默继承 treatment。

首个 treatment Game `market-treatment-v2-a-20260807` 与有效 control
`market-baseline-a-20260807` 使用相同 seed、阵容、初始组合、v1 起始价、
8 回合和 payment-disabled 边界，只替换 Strategy + Event Deck：

```text
metric                    control   treatment
Intent                    58        66
pass                      22        14
sell Intent               14        20
opposite capacity          3         9
compatible capacity        3         9
compatible rounds        3/8       6/8
RFQ                         6        17
Engagement / Deal           3/3       7/7
counterparty coverage     7.5%      17.5%
default action              0        12
true deadline timeout       0         0
```

V2 首次让 gems 出现 12 buy / 4 sell，并将 grain 的可交叠容量从 3 提升到 6；
iron 与 warhorse 仍是 0 sell，因此 Strategy V2 只完成了部分商品的流动性修复。
第 5、7、8 回合仍产生 RFQ，证明后半局市场发现不再像 baseline 一样完全枯竭。

该局的 12 个终态最初被实验导出器误记为 timeout。按 Task/Attempt 时间戳复核后，
它们实际均为 Runtime default，其中主要原因为 `invalid_structured_output`；
queue wait 接近 0，且没有 `deadline_exceeded`。严格 canary 仍以退出码 2 正确拒绝
该局，但拒绝原因是结构化输出可靠性，而不是 Provider 网络或 deadline timeout。
Event Deck 改变了终场价格，payment-disabled 又没有资产移动，因此本次净值
delta 不能用于比较策略收益。实验结束后，隔离 Official pool 已显式恢复
`baseline_v4`；生产和 Current Game 从未切换。

### 23.10 Hosted Agent 输出可靠性修复与同 seed 复验

2026-08-07 对上述 default 根因完成以下修复：

- 实验漏斗将 `defaultActionCount`、`invalidStructuredOutputCount` 和真实
  `timeoutCount` 分开；只有 `deadline_exceeded` 或 Runtime 明确
  `timed_out` 才计入 timeout；
- DeepSeek 非思考 Arena action 单请求输出上限从 8192 收紧到 2048；
- Official LiteLLM 在 provider wire 的 `extra_body` 固定补入
  `thinking.type=disabled`，绕过 LiteLLM 1.89.x 丢弃显式 disabled 的兼容问题；
- PydanticAI 采用两阶段运行：第一阶段仅暴露只读分析工具并强制调用，工具返回后
  隐藏分析工具，第二阶段通过 JSON Object 模式返回类型化终态；
- Agent run 上限冻结为 7 requests、8 tool calls、4 次 output validation retry，
  Worker 仍只允许一次同 Runtime/Model 的 task retry，且继续受 Game 的 180 秒
  绝对 deadline 约束。

最终同 seed、10 Agent、8 回合、payment-disabled 验收局
`market-treatment-v2-h-20260807` 结果：

```text
AgentTask                         115
completed / default / timeout    115 / 0 / 0
Intent / RFQ                     64 / 15
Engagement / Deal                 8 / 8
task wall P50 / P95 / P99        9.1s / 21.4s / 28.1s
Harness exit                      0
```

前一局 `market-treatment-v2-g-20260807` 为 126 completed、1
`invalid_structured_output` default、0 timeout、69 Intent 和 11 Deal；因此 h
证明严格可靠性门槛可以通过，g 同时证明 default 与 timeout 必须继续分开监控。
两局成交数存在随机差异，当前证据足以进入受控线上评测，但仍不足以完成多 seed
策略收益结论或 D5a 全量验收。下一步应在线上保持回滚点，采集多局
`defaultActionRate`、真实 `timeoutRate`、Deal 漏斗和模型调用成本后再决定是否
默认启用 Price V2。
