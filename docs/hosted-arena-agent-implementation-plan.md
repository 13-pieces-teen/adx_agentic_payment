# Arena 402 Hosted Arena Agent Implementation Plan

> 文档状态：已批准实施；Phase 0–6 的 Hosted 后端路径与 1–10 回合游戏编排已完成，
> Connector typed Task/Result 与 Result Sink 基础接线已完成；Local identity/session/
> task dispatcher、mixed-Runtime 编排、生产 Tencent SSM/CAM 和完整生产 E2E 待验收；
> DeepSeek/OpenAI-compatible Provider 已在本地真实链路验证
> 最后更新：2026-07-25
> 对应规格：[Hosted Arena Agent Spec](./hosted-arena-agent-spec.md)
> 当前游戏规则背景：[Game Design](./game-design.md)，其 Agent I/O 将在实现前按本计划同步
> 本地 Runtime 参考：[Local Agent Connector Implementation Plan](./local-agent-connector-implementation-plan.md)
> 前端边界：产品 UI 已迁移到外部 `sunruize93-cmyk/arena402`；后端 GitHub
> OAuth/Session 契约已实现，本计划中的 `frontend/` 路径只记录本地开发与
> 显式 `legacy-web` profile，Vercel→腾讯云公网联调尚未验收
> 设计优先级：以本计划定义的最终 Hosted/Local 统一 Runtime 目标为准；现有
> Game Design 是待同步的背景输入，不是限制目标架构调整的硬约束

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
| `frontend/src/app/agents/page.tsx` | Agent 统一入口页面 |
| `frontend/src/lib/connector-api.ts` | 同源 authenticated API client 模式 |
| `docker-compose.production.yml` | 单机 PostgreSQL/API/Caddy 默认后端部署骨架；Web 仅为显式 legacy profile |

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
frontend/src/lib/hosted-agent-api.ts
frontend/src/components/HostedAgentCreator.tsx
frontend/src/app/agents/page.tsx
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
- Prompt/schema version 写入 Game 配置快照；
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
- 本仓库 Compose 过渡页只调用正式 API；外部 Next.js 产品仓库仍需移除 legacy
  Agent/listing/ELO client 并切换到当前 Hosted/Pawnhouse/Connector API。

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

当前生产配置为：默认开赛阈值 10 Agent、硬上限 100、固定 5 回合、同一时间
一局 active Game、4 个 Hosted Worker × 25 task slot、Settlement 执行并发 4，
并确定性路由到 4 个独立 relay EOA。旧 2 vCPU / 4 GB / 70 GB 单机基线已不再
适用，必须重新定容。具体 `action_timeout_ms` 由真实 Provider 的
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
- [ ] 100 Agent 受控场景产生最多 50 笔 accepted trade，经 4 个 shard 全部安全
      终态，且监控至少观察到 4 笔 Settlement 同时在途并分属 4 个 EOA；
- [ ] 600 秒内无残留 `submitted_unknown`；进入 `settlement_recovery_required`
      必须停止排名并使本次验收失败；
- [ ] 外部网络 E2E 保存证据；
- [ ] 10/12 Agent × 5 回合回归通过；
- [ ] 25/50/100 Agent × 5 回合在重新定容的生产机上通过；
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

当前 `DirectModelDriver` 执行一个逻辑 AgentTask，并按统一规则最多发起两个 Provider
Attempt。未来可以新增：

- `LangGraphDriver`；
- 受控多步骤 Planner；
- 平台 Skill；
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
| 跨局长期记忆 | 不在 MVP |

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
