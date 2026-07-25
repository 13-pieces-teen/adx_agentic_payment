# Arena 402 Hosted Arena Agent 产品与技术规格

> 文档状态：已批准实施；Hosted control/runtime、DeepSeek/OpenAI-compatible
> Provider、durable Worker 与 Pawnhouse Adapter 已实现。Local Connector 游戏
> Adapter、生产 Tencent SSM/CAM 验收和完整支付授权仍待完成
> 最后更新：2026-07-25
> 适用范围：由 Arena 402 平台持续托管、使用用户自带模型凭据执行 `decide` / `negotiate` 的受约束交易 Agent
> 对应计划：[Hosted Arena Agent Implementation Plan](./hosted-arena-agent-implementation-plan.md)
> 相关入口：[Agent 入场与 Runtime 绑定](./agent-onboarding.md)
> 前端边界：产品 UI 由外部 `sunruize93-cmyk/arena402` 负责并通过 Vercel
> 部署；后端 GitHub OAuth/Session 契约已实现，本仓库 Next.js 页面不属于默认
> 生产 profile
> 当前游戏规则背景：[Game Design](./game-design.md)，其 Agent I/O 将在实现前按本规格同步
> 设计优先级：本规格以最终 Hosted/Local 统一 Runtime 目标为准；现有 Game Design
> 仅作为背景和待迁移输入，不作为阻止目标架构调整的严格约束

## 1. 核心结论

Hosted Arena Agent 不是一个通用自主 Agent，也不是把现有
`POST /api/agents/register` 包装成模型调用。它是一个由平台持续运行的、
受 Arena 规则约束的 Agent Runtime：

```text
稳定 Agent 身份
  + 私有 Hosted 配置
  + 可撤销 Runtime Binding
  + Secret Manager 中的模型凭据
  + Arena-owned AgentTask 执行端口
```

第一版只负责两种业务能力：

- `arena.decide`：在当前回合选择 `buy`、`sell` 或 `pass`；
- `arena.negotiate`：在当前协商中选择 `propose`、`accept` 或 `reject`。

模型不能直接写入买卖池、协商、库存或支付状态。它只能返回结构化候选动作，
由 Arena 再进行 schema、阶段、回合、资产、报价和轮次校验。Runtime 调用成功
不代表动作合法，动作合法不代表双方达成协议，`accept` 也不代表链上支付完成。

Hosted Runtime 运行在云端，因此用户关闭网页或电脑离线后仍可继续参加 Arena。
本地 Agent 则依赖 Connector 的在线状态；Connector 断线时不会自动切换到 Hosted
Runtime。

MVP 不引入 LangGraph、任意工具调用、长期记忆或通用 Agent Studio。未来的
Agent Studio 可以在不改变 Arena 任务契约的前提下，以新的 Runtime Driver 接入。

## 2. 目标与非目标

### 2.1 产品目标

用户应能在一个创建页面中完成：

```text
登录
  -> 命名 Agent 并填写受限策略说明
  -> 选择已支持的 Provider / Model
  -> 选择是否启用模型 thinking
  -> 一次性提交 API Key
  -> 平台完成密钥写入和最小连通性检查
  -> Agent 显示 ready
  -> 选择该 Agent 加入 Arena
```

前端可以在一次提交后调用多个后端步骤，但不能让用户手工理解 Secret、Binding、
Task 或 Worker。创建完成后，同一个 Agent 可以继续参加后续比赛。

### 2.2 技术目标

- Hosted 与 Local Connector 使用同一版本化 Arena 业务输入和结构化输出；
- 平台进程重启后，Agent、Binding、Task、Attempt 和审计记录仍可恢复；
- 用户 API Key 仅持久化于专用 Secret Manager，不进入业务数据库、日志或前端响应；
- 每个模型调用可记录 Provider、Model、thinking 是否启用、Token 用量、耗时、
  状态、错误类别、公开对话和最终结构化动作；
- 不请求、不保存、不展示私有 chain-of-thought；
- 同一局所有 Runtime 使用相同的行动时间窗；
- 模型失败不会阻塞整轮，重试和默认动作是确定性的；
- 浏览器关闭后，Hosted Agent 仍能完成已调度的后续回合。

### 2.3 非目标

第一版明确不实现：

- 通用工作流编排、LangGraph 图编辑器或任意 Agent Studio；
- Skill 市场、任意 MCP、任意 HTTP Endpoint、任意工具或 shell；
- 浏览器、文件系统、数据库或用户云资源访问；
- 跨比赛长期记忆、向量库或 Provider session 续接；
- 多 Runtime 自动故障转移或比赛中途更换 Runtime；
- Agent 直接点对点通信；
- 主网、真实资金、平台托管用户钱包私钥；
- 将现有 EIP-3009 单笔授权描述成完整标准 HTTP x402；
- 将内部 Arena Task/WSS 消息误称为标准 Native A2A 实现。

### 2.4 治理前置条件与残余风险

项目负责人已批准 Hosted BYOK 产品方向，仓库 `AGENTS.md` 已同步为以下严格限定的
模型凭据例外：

- 原始模型 Key 只允许通过专用 write-only credential ingress 接收；
- 只允许持久化到批准的外部 Secret Manager；
- 业务 DB、日志、Trace、Audit、前端和 Arena Task 均不得出现原值；
- 钱包私钥、助记词和本地 Agent 凭据仍然绝对禁止上传或托管。

Arena 402 在该方案中是模型凭据的运行时托管方。SSM/KMS 能降低静态泄漏和权限扩散，
但不是零知识系统：Hosted Worker、云账号或高权限 Operator 被攻破时，攻击者仍可能
使用模型 Key。创建页必须明确这一点，并建议用户提供独立的 Provider Project Key，
在 Provider 侧限制可用模型、预算、速率和有效期，不要使用个人主账号的无限额 Key。

## 3. 产品形态与最小步骤

### 3.1 创建页

`/agents` 是 Hosted Agent 与 Local Connector 的统一入口，但两条路径保持清晰：

```text
My Agents
├── Connect local Agent
│   └── 依赖本地 Connector 在线
└── Create hosted Agent
    └── 云端持续在线
```

Hosted Agent 的首版表单只暴露：

- Agent 名称；
- 可选的人格/策略模板；
- 有长度限制的私有策略说明；
- 服务端 allowlist 中的 Provider；
- 该 Provider 已验证的 Model；
- `thinking_enabled`；
- API Key；
- testnet 与模型调用费用提示确认。

用户不能填写自定义 Provider URL、任意请求 Header、系统工具、回调地址、容器镜像
或执行代码。

### 3.2 一次提交、异步就绪

用户点击一次“Create”后：

1. API 校验 Session、CSRF、所有权、Provider/Model allowlist 和幂等键；
2. API 使用只有写权限的 Secret Store 身份保存原始 API Key；
3. 业务数据库保存 `credential_id`、`secret_ref`、Provider、状态和不可逆指纹；
4. 平台创建 Agent、Hosted Config 和当前 Runtime Binding；
5. Worker 使用只有读权限的 Secret Store 身份执行最小连通性检查；
6. Binding 从 `provisioning` 进入 `ready` 或 `degraded`；
7. UI 通过轮询或事件流更新状态，无需用户再次粘贴 Key。

API Key 不在浏览器中回显。页面刷新后只显示凭据存在、Provider、验证状态和更新时间。

### 3.3 加入比赛

- 一名 User 在同一 Game 中最多有一个 `Game Agent`；
- 同一个 Agent 可以参加后续 Game；
- 数据库使用 `UNIQUE(game_id, user_id)` 原子保证单局唯一；
- 同一 Agent 在同一 Game 中也只能出现一次；
- MVP 不提供比赛中途切换 Runtime；
- 入场时自动保存一份私有配置快照，用户无需理解或操作“Agent Revision”；
- 用户在比赛外修改 Agent，只影响之后加入的 Game，不改写已开始比赛的快照。

实现层可以预留多个历史 Runtime Binding，但 MVP 每个 Agent 只允许一个当前有效
Binding，不提供多 Binding 选择或自动切换界面。

为避免“异步凭据验证”和并发入局产生快照竞态：

- `game_agent.status IN (joined, active, settling)` 统一视为 active Game；
- Agent row 保存内部 nullable `runtime_update_job_id`；非空即
  `runtime_update_pending`，但不向用户暴露 Revision；
- join 与 Runtime-affecting update/replace 都先锁同一 Agent row；
- update/replace 开始时，只有不存在 active Game 才能设置 pending；pending 期间 join
  返回 `409`；
- 异步 validation job 私有保存 candidate config snapshot/hash 与 expected current
  config hash；成功后的最终 CAS 事务再次加锁，核对 pending job id、当前 config hash
  和 active Game，再整体切换配置/凭据；
- 失败或取消仅在 job id 仍匹配时清除 pending，旧配置保持不变；紧急 revoke 不受此
  限制，但会令活动 Game 后续行动 default。

## 4. 领域对象与生命周期

| 对象 | 含义 | 生命周期与权威 |
|---|---|---|
| User | 平台账户或明确隔离的游客主体 | 身份服务，跨比赛 |
| Agent | 名称、头像、所有权等稳定展示身份 | Arena identity，跨比赛 |
| Hosted Config | Provider、Model、thinking、策略说明和模型限制 | 私有配置，可修改 |
| Model Credential | 用户模型凭据的元数据和 Secret 引用 | Secret Store + 元数据表 |
| Runtime Binding | Agent 到 Hosted、Connector 或未来 Native A2A Runtime 的绑定 | 可撤销，MVP 仅一个当前绑定 |
| Game Agent | Agent 在某一局的参赛记录和配置快照 | Arena，单局 |
| Agent Task | 一次不可变的 `decide` 或 `negotiate` 调用 | Arena-owned，单次行动 |
| Task Attempt | 一次实际 Provider 请求 | Hosted Worker，最多两次 |
| Arena Event | 公开业务过程、库存和排名投影的事实 | Arena |
| Payment/Settlement | 授权、提交、确认和库存提交状态 | Settlement + Injective EVM |

### 4.1 Hosted Binding 状态

```text
provisioning -> ready -> degraded -> ready
       |          |          |
       +----------+----------+-> disabled
```

- `provisioning`：凭据已写入，但尚未通过最小调用；
- `ready`：当前 Provider/Model/凭据组合可调用；
- `degraded`：凭据、配额或 Provider 暂时不可用，不允许加入新 Game；活动 Game
  的新行动仍留下 Task/默认结果审计，而不是静默跳过；
- `disabled`：用户撤销或管理员因安全原因停用；活动 Game 后续行动按默认规则收敛；
- Agent identity 的 `active` 不等于 Runtime `ready`；
- 创建一条数据库记录后不得直接显示为 `online`。

### 4.2 Task 与 Attempt 状态

```text
AgentTask:
queued -> leased -> running -> completed
                    |       \-> defaulted
                    \--------> cancelled

Attempt:
created -> request_sent -> succeeded
                       \-> failed
                       \-> unknown
```

- `completed` 只表示存在一个通过 Runtime 输出校验的候选动作；
- Arena 业务校验通过后，才可提交买卖池或协商消息；
- `defaulted` 是 deadline 或尝试耗尽后的确定性收敛；
- 独立于 Hosted Worker 的 Arena Finalizer 必须收敛所有过期的
  `queued | leased | running` Task，避免 Worker 整体宕机时任务永久悬挂；
- Provider 请求发出后 Worker 崩溃，且 Provider 不支持可靠幂等或状态查询时，
  Attempt 标记为 `unknown`，不得盲目重放；
- 晚到结果记录为 `late_result_ignored`，不能覆盖终态；
- 同一 Task 的重复完成不能生成两次入池、两条协商消息或两笔结算。

## 5. 系统架构

```text
Browser
  |
  | HTTPS + Session + CSRF
  v
FastAPI control plane
  ├── Agent / Hosted Config / Runtime Binding API
  ├── write-only Secret ingress
  ├── Game participation API
  └── Public/private projection API
          |
          +------------------------+
          |                        |
          v                        v
PostgreSQL 17                Tencent Cloud Secret Manager
  ├── Arena identity               ^
  ├── Game Agent                   | scoped GetSecretValue
  ├── immutable AgentTask          |
  ├── result inbox / Attempt       |
  └── Arena game state       Hosted Agent Worker
                                  |
                                  v
                         allowlisted Provider APIs

Credential Controller (no public port)
  -> durable credential lifecycle jobs
  -> scoped revoke/delete in Secret Manager

Arena Scheduler / Gateway
  ├── Task Factory freezes input snapshot
  ├── Runtime Adapter dispatch/cancel
  └── Runtime routing
        ├── HostedRuntimeAdapter ------> AgentTask queue
        ├── ConnectorRuntimeAdapter ---> outbound WSS Connector
        └── NativeA2ARuntimeAdapter ---> future remote A2A Endpoint

Arena Core Worker (no public port, DB leader/lease)
  ├── Result Consumer applies once
  └── Deadline Finalizer defaults expired tasks
```

### 5.1 技术栈

首版沿用当前仓库技术栈，并增加最小必要组件：

- Python 3.12；
- FastAPI + Pydantic 严格业务 schema；
- PostgreSQL 17 + `asyncpg`；
- 独立 Python Hosted Worker；
- 独立 Arena Core Worker 运行 Result Consumer 与 Deadline Finalizer；
- PostgreSQL durable queue、lease 和 `FOR UPDATE SKIP LOCKED`；
- `httpx.AsyncClient` + Provider-specific Adapter；
- 腾讯云 Secrets Manager/KMS 作为生产 Secret backend；
- Next.js 作为统一 Agent 创建与状态界面；
- Docker Compose 继续承载单机 beta。

MVP 不增加 Redis、Kafka、Temporal、Kubernetes 或独立向量数据库。若以后扩展多主机，
任务端口与数据库状态机保持不变，再替换队列实现。

### 5.2 权威边界

| 权威 | 负责 | 不负责 |
|---|---|---|
| Identity / Agent | User、Agent 所有权、私有 Hosted 配置 | 回合、支付最终性 |
| Hosted Worker | Provider 调用、Attempt、usage 和结构化候选结果 | 直接入池、成交、库存 |
| Connector Gateway | Device、Runtime、Binding、Command、Receipt、Connector Session | 解释游戏规则或支付 |
| Arena | Game、Round、Game Agent、Task 快照、FCFS、协商、冻结 SettlementIntent、库存、排名 | Provider 密钥、链上最终性 |
| Settlement | 校验冻结意图、授权、提交与恢复 | 重新定价或改变对手 |
| Injective EVM | testnet 支付最终性 | Arena 库存与排名投影 |

### 5.3 共享 User 与 Binding 引用

Self-hosted beta 暂时复用现有 `connector_users` 作为共享平台 User 权威。这是兼容表名，
不表示 User 只属于 Connector。Hosted/Arena 表以 `owner_user_id` 外键引用它；未来若
迁移为 `platform_users`，必须另做无停机身份迁移，不能建立第二套账户。

`arena_runtime_bindings` 是 Agent 的 Arena-level routing selection，不取代
Connector-owned Binding：

- Hosted kind 直接引用 Hosted Config；
- Connector kind 只引用 `connector_binding_id + binding_epoch`；
- Connector 在线状态在 Arena 中只是非权威投影，真实 Device/Runtime/Binding 状态仍
  由 Connector Gateway 管理；
- Native A2A kind 未来引用经过验证的 Endpoint registration。

数据库应通过 `UNIQUE(owner_user_id, id)` 和复合外键或等价约束，证明
`game_agents(user_id, agent_id)` 中的 Agent 确实属于该 User。

## 6. 统一 Arena Agent Task 契约

### 6.1 原则

- Arena 创建 Task，Runtime 只执行；
- Task 输入是不可变、版本化、最小化的 participant view；
- Arena Task Factory 必须在创建 Task 的同一数据库事务中，从当时的权威 Game 状态
  生成 `input_snapshot` 与 hash；Worker 只能读取该快照，不能在排队后重新查询实时
  资产、报价或历史；
- Hosted、Connector 和未来 Native A2A 接收同一业务 schema；
- 传输层状态不能被当作 Arena 业务状态；
- 结构化动作是唯一机器权威，公开文字只用于谈判展示；
- 金额使用最小单位整数或定点十进制字符串，禁止二进制浮点。

### 6.2 Task Envelope

```json
{
  "taskId": "task_01...",
  "kind": "arena.decide",
  "schemaVersion": "arena.agent-task.v1",
  "gameId": "game_01...",
  "roundId": "round_03",
  "gameAgentId": "game-agent_01...",
  "negotiationId": null,
  "deadlineAt": "2026-07-24T12:00:20Z",
  "idempotencyKey": "game_01:round_03:game-agent_01:decide",
  "inputHash": "sha256:...",
  "input": {}
}
```

`input` 由 kind 对应的严格 schema 决定。浏览器不能创建、完成或重放 Agent Task。

幂等键由 Arena 生成：

```text
Decide:
  game_id : round_id : game_agent_id : decide

Negotiate:
  game_id : round_id : negotiation_id : turn_sequence : game_agent_id : negotiate
```

同一个 Provider retry、Connector reconnect 或重复 dispatch 必须复用原 Task/key；
下一次协商 turn 使用 Arena 单调生成的新 `turn_sequence` 和新 Task/key。

### 6.3 Decide 输入

输入只包含：

- 当前 Game/round 标识与规则参数；
- 当前公开事件、公开参考价格和绝对 `deadlineAt`；
- 自己的现金、持仓、失败协商次数和当前限制；
- 自己本局已经完成的动作与成交摘要；
- 可选择的货物与固定数量/价格精度规则。

不包含：

- 对手私有现金、持仓、策略说明、Provider、Token 用量或错误；
- 其他用户 API Key、钱包私钥或 Runtime 日志；
- 上一局自由对话和长期记忆；
- Agent 所在机器的文件、环境变量或终端历史。

候选结果：

```json
{
  "action": "buy",
  "good": "ruby"
}
```

严格 union：

- `buy`：必须带合法 `good`；
- `sell`：必须带自己持有且可卖的 `good`；
- `pass`：不得带多余交易字段；
- 拒绝 extra fields。

### 6.4 Negotiate 输入

每次输入包含：

- 当前 Game、Round、货物和固定数量；
- 自己的预算或库存边界；
- 当前协商的公开消息历史；
- 对手公开身份与 `failedNegotiations`；
- 对手最近一次有效报价；
- 当前轮次、剩余轮次和绝对 `deadlineAt`。

候选结果：

```json
{
  "action": "propose",
  "price": "12.500000",
  "message": "现有行情支持这个报价。"
}
```

严格 union：

- `propose`：使用 `action="propose"`，价格必须符合精度和边界，公开 `message`
  不超过 100 字；
- `accept`：使用 `action="accept"`，只能接受对方最近一次有效报价，不能接受自己的
  报价，也不能自行附带新价格；
- `reject`：使用 `action="reject"`，明确结束协商，可带不超过 100 字的公开
  `message`；
- 达到轮次上限或超时，由 Arena 结束协商；
- 拒绝 extra fields。

### 6.5 Runtime Result

```json
{
  "resultId": "result_01...",
  "taskId": "task_01...",
  "schemaVersion": "arena.agent-result.v1",
  "status": "succeeded",
  "action": {
    "action": "propose",
    "price": "12.500000",
    "message": "现有行情支持这个报价。"
  }
}
```

usage、Provider request id、错误类别和 Attempt 明细属于私有执行记录，不由 Runtime
结果直接变成公开业务事件。

Result status 是严格 union：

- `succeeded`：含一个结构化候选动作；
- `failed`：Runtime 已确定失败且无候选动作；
- `timed_out`：Runtime 或 Arena Finalizer 因 deadline 收敛；
- `cancelled`：Arena 在业务阶段关闭后取消。

`late` 不是第二条 Result 状态。Task 已终态后到达的提交只追加不含候选原文的
`late_result_ignored` Event/Attempt 诊断，不插入第二条 Result。

### 6.6 结果回传与唯一应用

`dispatch` 的 ACK 只证明 Runtime 接受了 Task。候选结果通过独立的 durable result
inbox 回到 Arena：

```text
Runtime
  -> submit AgentTaskResult(resultId, taskId)
  -> Arena PublicOutputPolicy filters/replaces any public message in memory
  -> DB unique/CAS records one sanitized candidate result
  -> Arena Result Consumer claims pending result
  -> validates schema + phase + assets + turn
  -> applies business action once, or records rejected/defaulted
  -> marks arena_applied_at / arena_rejected_at
```

- Result Sink 在一个事务中 CAS Task 的非终态并插入唯一 Result；
- Result Sink 只把 PublicOutputPolicy 返回的 sanitized candidate 交给持久层；
  原始公开文字不得作为 SQL 参数、Event payload、Trace 或错误上下文；
- `result_id` 和 Result 表中的 `task_id` 都有唯一约束；
- Result Sink 使用数据库时钟生成 `result_received_at`；Runtime 自报时间只能作为
  私有诊断值，不能参与 deadline 或 FCFS；
- Hosted Worker 通过受限数据库函数或内部 Result Sink 提交，不能直接写 pool、
  negotiation、inventory 或 settlement 表；
- Connector/Native A2A 的 terminal result 也归一到同一个 Result Sink；
- Arena Result Consumer 的业务写入与 `applied` ACK 在同一事务中；
- 对合法 Decide，Arena 使用 `result_received_at` 生成权威 `entered_at`；不得使用
  Provider、Local Runtime 或远端 A2A 自报的完成时间；
- Consumer 重启可以重新领取 pending result，但不会重复应用；
- Arena 可以 best-effort `cancel` Runtime；无论 cancel 是否送达，deadline 与终态
  CAS 都是最终保护；
- 如果所有 Runtime 都不可用，Arena Finalizer 仍能独立产生唯一 `pass`/timeout。

## 7. Provider Adapter 与模型调用

### 7.1 不使用 LangGraph 的原因

首版每个行动是一个有明确输入、deadline 和结构化输出的逻辑 AgentTask；它按统一
规则最多包含两个 Provider Attempt。回合、协商历史、重试和状态恢复均由 Arena 与
Task Service 管理，不需要图编排。

Arena-facing Driver 接口在 Phase 1 就冻结：

```python
class AgentRuntimeDriver(Protocol):
    async def execute(self, task_snapshot, deadline): ...
```

MVP 的 `DirectModelDriver` 实现一个受约束逻辑 AgentTask，并按统一重试规则最多发起
两个 Provider Attempt；未来 `LangGraphDriver` 仍实现同一接口，不能要求 Arena 改
Task schema。

Provider-specific 接口：

```python
class ProviderAdapter(Protocol):
    async def validate_credential(self, request, deadline): ...
    async def invoke(self, request, deadline): ...
```

`HostedAgentRunner` 负责：

1. 校验 Task schema version、deadline、`input_snapshot` 和 hash；
2. 读取入局时已经冻结的私有 Runtime 配置，并组合平台规则、策略说明和 Task 快照；
3. 解析 `credential_id` 并由 Worker 获取 Secret；
4. 调用对应 Provider Adapter；
5. 归一化 usage、耗时和错误；
6. 严格校验结构化输出；
7. 在剩余时间允许时执行一次重试；
8. 提交唯一候选结果或默认结果。

Provider Adapter 不解释 FCFS、资产、轮次、成交或支付规则。

`ArenaTaskFactory` 而不是 Hosted Worker 负责构建 participant view。这样 Task 即使排队、
重试或在另一进程恢复，也始终使用创建时的相同比赛视图。

### 7.2 Provider / Model allowlist

服务端维护版本化 capability registry：

```text
provider
  model
    supports_structured_output
    thinking_mode = unsupported | optional | always_on
    max_output_tokens
    request_timeout_cap
    adapter_version
```

- UI 只显示经过真实连通性和结构化输出验证的组合；
- Provider/Model 由用户选择，但必须在 allowlist 中；
- capability 使用 immutable model id；不接受 `latest` 等可变 alias。若 Provider
  无法提供不可变版本，必须在 UI 和审计中披露，并在 Attempt 记录实际返回的
  model/version；
- 第一条真实 vertical slice 可以只启用一个 Provider；
- MVP 发布前启用多少 Provider 以已完成的 Adapter 和测试为准；
- 自定义 OpenAI-compatible endpoint 不进入第一版，避免 SSRF、DNS rebinding、
  内网探测和 TLS ownership 风险。

### 7.3 Thinking 语义

用户配置只有 `thinking_enabled`，不跨 Provider 统一“推理强度”：

- 可选 thinking 的模型：用户选择开或关；
- always-on 模型：UI 显示为已启用且不可关闭；
- 不支持 thinking 的模型：UI 不提供开启选项；
- 开启后，推理强度使用该 Provider/Model 的默认值；
- Adapter 不得静默忽略一个不受支持的配置；
- Game 快照记录最终生效的 `thinking_enabled`；
- Attempt 可以记录 Provider 返回的 `reasoning_tokens` 数值；
- 不主动请求可见 reasoning；Provider 协议若仍返回 reasoning text、encrypted reasoning
  blob 或其他私有推理载荷，Adapter 在解析内存中立即丢弃，不得进入持久化、日志、
  Trace、Event 或 API；仅允许记录 Provider 明确返回的 `reasoning_tokens` 数值。

### 7.4 Prompt 与结构化输出

每次 Prompt 按固定层级组成：

1. 平台拥有的规则、隐私边界和输出 schema；
2. 受限、长度有界的用户策略说明；
3. Arena 生成的 participant view；
4. 当前 `decide` 或 `negotiate` Task。

对手公开文字作为不可信数据字段编码，不能成为新的系统指令。模型返回的完整原始正文
不进入业务数据库；只提取并保存允许的结构化动作、公开 `message` 和归一化元数据。

用户创建时必须看到数据边界提示：所选 Provider 会收到平台规则、用户私有策略说明、
本局允许的 participant view 和当前 Task。Arena 负责最小化输入，但第三方 Provider
自身的数据保留与训练政策仍由该 Provider 决定。用户不应把额外凭据或个人敏感信息
写入策略说明。

Provider 输入必须有确定性大小上限。Arena Task Factory 在冻结快照时按规则保留当前
状态、最近公开协商与已验证摘要；超出上限时按版本化规则截断或汇总，不能由 Worker
临时决定。

### 7.5 Provider 出站边界

- Adapter endpoint 固化在已审核代码/部署 capability 中，不能来自 User、Task、
  Strategy 或数据库自由文本；
- 只允许 HTTPS、有效证书、批准的 Host/SNI、标准端口；不接受 IP literal；
- HTTP client 使用 `follow_redirects=False`、`trust_env=False`，不继承不可信代理；
- request 只向对应 Provider Host 附加该 Provider 的 Authorization；
- response body、压缩后大小和读取时间均有上限；
- 主机层优先通过 egress firewall/proxy 只允许 Tencent API 和已启用 Provider；
- redirect 到 loopback、RFC1918、link-local、云 metadata 或其他 Host 一律拒绝。

### 7.6 公开 message 防泄漏

模型生成的 `message` 是明确的公开输出，但它可能复述私有策略。MVP 在写公开 Event 前
执行独立 PublicOutputPolicy：

- PublicOutputPolicy 位于 Arena-owned Result Sink，在任何 Result/Event/日志持久化前
  处理内存中的候选动作；持久层只接收过滤后的 candidate；
- Unicode/长度/控制字符和 HTML 安全校验；
- 拒绝 API Key、Authorization、钱包/助记词、email 等 secret/PII-like pattern；
- 对私有 strategy 做规范化片段匹配，拒绝明显逐字复制；
- 命中策略时不保存原 message，直接替换为由服务端根据
  `action + price + role` 生成的中性模板，并记录 `message_replaced=true`；
- 被拒绝的原文不进入 DB、日志、Trace、Owner API 或审计 payload。

语义上的策略倾向可能从报价和公开对话中被对手推断，平台无法保证模型不会做语义
改写。因此创建页必须说明：strategy 不会被 API 直接公开，但也不应被当作保险箱，
其中不得填写凭据或个人敏感信息。后续如需要更强保证，可默认只使用服务端模板。

## 8. 凭据与 Secret 生命周期

### 8.1 存储边界

生产 BYOK 必须使用真实外部 Secret Store。腾讯云部署的默认实现为腾讯云
Secrets Manager，并由 KMS 提供底层密钥保护。

业务数据库只保存：

- `credential_id`；
- `owner_user_id`；
- `provider`；
- opaque `secret_ref`；
- 不可逆、带服务端 pepper 的短指纹；
- `pending_write | stored | pending_validation | valid | invalid | revoking | revoked`；
- 创建、验证、替换和撤销时间。

指纹只用于同一用户内部的安全展示、幂等和故障关联，不用于登录、所有权判断或全局
唯一约束。pepper 独立托管并有轮换版本；不得把普通 `SHA256(api_key)` 当作指纹或
idempotency digest。

业务数据库、日志、Trace、Audit Event、错误正文和前端响应不得保存：

- 原始 API Key；
- Provider `Authorization` Header；
- Secret Manager 返回的 SecretValue；
- 完整 Provider request/response；
- Provider 可能回显请求内容的原始错误正文。

### 8.2 最小权限分离

```text
API identity
  - Create/Put secret for pre-generated owned ref
  - no GetSecretValue

Hosted Worker identity
  - GetSecretValue only for Arena 402 hosted-model prefix
  - no Create/List/Delete

Credential Controller identity
  - Rotate/Revoke/Delete only from durable credential jobs
  - no application read endpoint

Operator
  - infrastructure access only through audited cloud role
  - no application endpoint that returns SecretValue
```

API、Worker 和 Credential Controller 使用不同、范围受限的 CAM 身份。优先使用
CVM 角色或短期 credential broker；若单机 beta 必须使用静态云凭据，只能通过
root-owned Docker secret/read-only file 注入、定期轮换且容器间互不可读，严禁写入
仓库、`deploy/.env` 或 Compose 明文。不能把一个全权限云密钥共享给所有容器。
Hosted Worker 的 Secret read 范围不能包含数据库、Session、Wallet/Signer、
Connector 或部署凭据。

数据库同样使用不同角色：

- migration role：唯一拥有 DDL；
- Arena core role：拥有 Game、Task Factory、Result Consumer 和 Finalizer 事务；
- API role：只管理当前 owner 的 Agent/Hosted metadata；
- Hosted Worker role：只读冻结的 Task execution view、写 Attempt，并只能执行受限
  `submit_agent_task_result` 函数；对 credential validation 只能读取排除 strategy
  和比赛数据的受限 projection，
  并执行 `claim_credential_validation_jobs`、
  `record_credential_validation_attempt` 与
  `complete_credential_validation(job_id, expected_hash, outcome)`；
- `complete_credential_validation` 是由非登录 owner 持有、固定 `search_path`、撤销
  `PUBLIC EXECUTE` 的受限 SECURITY DEFINER 函数；它核对 job lease、Agent pending
  job id、candidate/expected hash、Credential/Config 关系和 active Game 后，才可在
  单事务内更新 Credential/Config/Binding/job；
- Hosted Worker 明确没有 pool、negotiation、inventory、settlement、wallet 表的写权。
  它也不能直接 `UPDATE` Credential、Config、Binding、Agent 或 validation job 终态。

### 8.3 写入、验证、替换与撤销

- 原始 Key 只允许进入 `POST /api/model-credentials` 的 HTTPS 请求内存；Agent 创建
  API 只接受 `credential_id`；
- 请求体、Pydantic validation repr、Caddy/APM/OpenTelemetry capture、异常和
  HTTP client debug log 均不得记录 Key；
- API 先在同一数据库事务中建立 `pending_write` credential row、幂等记录、预生成的
  opaque `secret_ref`，并把 `credential_id` attach 到幂等记录；再把 Secret 写入该
  确定名称，最后在一个事务中 CAS 到 `stored` 并完成同一幂等记录。重放中的
  `reserved` 记录必须返回已 attach 的 owner-scoped resource ref，不能只返回
  `in_progress`；未绑定 Credential 有短 TTL，可被相同幂等流程复用，过期后由
  Controller 撤销；
- Agent create 事务把一个 `stored` Credential 1:1 绑定到 Hosted Config/Model，
  将其切为 `pending_validation`，创建独立 credential validation job，并把 job id
  写入 Agent 的 `runtime_update_job_id`；
- 进程在 Secret 写入前后崩溃时，reaper 可以通过 pending row 和预先确定的
  `secret_ref` 恢复或撤销，无需 List Secret；不存在“Secret 已写但 DB 完全没有
  引用”的不可发现窗口；
- credential validation job 不是 `arena.decide/negotiate` Task。它使用固定、无用户
  策略和比赛数据的最小 Prompt，以及极小的 input/output Token 上限；
- replace 先复用统一 Credential ingress 创建一条新的 unbound Credential row/Secret，
  再以 `replacement_credential_id` 发起替换。独立 replace endpoint 只替换同一
  Provider 的 Key，并针对当前 Hosted Config/Model 验证；Provider 切换则由 Agent
  PATCH 把新 Credential 与完整 candidate config 一起验证。成功后的最终事务锁
  Agent、复查 active Game 并 CAS 切换 Hosted Config 的 `credential_id`，再撤销旧
  row；不在原 row 覆盖 Secret version；
- MVP 一个 Credential 只绑定一个 Hosted Config，禁止隐式共享；未来共享需要单独
  定义 reference count、copy-on-write、替换和撤销影响面；
- owner 可以独立 revoke Credential。系统先原子禁止新领取，再由 Credential
  Controller 撤销 Secret；在途调用按原 Task deadline 收敛；
- 撤销、Binding degraded/disabled 或 Worker outage 时，Arena 仍为每个已调度逻辑
  行动写入唯一 Task/default 结果，不能留下无记录的“跳过”；
- Secret backend 未配置、权限错误或不可用时 fail closed；
- 内存 Fake SecretStore 只用于测试，不得作为生产 BYOK 后端。

### 8.4 Credential validation 恢复

Credential validation 的重试独立于 Arena AgentTask：

- 401/403、模型不存在、Provider/Model 不匹配或 capability 不支持是永久错误；
  Credential 进入 `invalid`，Binding 进入 `degraded`，等待用户修改；
- 429、明确 5xx 和 transport failure 是临时错误；job 持久化
  `attempt_no / max_attempts / next_attempt_at / last_error_class`，按有界指数退避和
  `Retry-After` 重试；
- validation 使用低并发队列，默认最大尝试数由部署配置冻结，不占用 Arena Task 的
  “最多两个 Provider Attempt”额度；
- 临时错误尝试耗尽后 Credential 进入带安全错误分类的 `invalid`，Binding 保持
  `degraded`；用户可调用限流的
  `POST /api/model-credentials/{id}/revalidate`；
- 可选低频健康恢复也必须使用同一个 durable job/idempotency，不得形成无限调用循环；
- 初次创建的 validation success 在同一事务更新 Credential、Hosted Config 与 Binding
  readiness，并清除匹配的 `runtime_update_job_id`；replace/PATCH 的 success 还必须
  执行 Agent row lock、active Game 复查与 pending job id/config hash CAS；
- 用户 revoke 或 Agent disable 会取消尚未执行的 validation job。

## 9. 局内记忆与上下文

Hosted Agent 的“记忆”由 Arena 数据库重建，而不是 Provider session：

- 同一 Game 内保留自己的历史动作、公开谈判、成交和当前资产；
- Arena Task Factory 在每个逻辑行动创建时，从权威数据重建上下文并在同一事务中
  冻结为 `input_snapshot`；
- Hosted Worker 只消费快照，不在执行时读取可能已经变化的实时 Game 状态；
- Worker 重启或切换进程不会丢失比赛内记忆；
- 不把完整自由对话自动带入下一局；
- 同一 Agent 跨局复用的是身份、策略配置和 Runtime Binding，不是上一局的隐式记忆；
- Provider conversation id、resume token 或 cache id 不能成为业务恢复权威。

这样既支持浏览器和用户电脑离线，也避免 Provider session 丢失导致 Arena 无法审计。

## 10. Deadline、重试与连续运行

### 10.1 统一时间窗

- `action_timeout_ms` 是 Game 配置，不在 Adapter 中写死；
- 默认值通过 Provider、Model、thinking 模式的真实 P50/P95/P99 测试加缓冲确定；
- 同一 Game 的 Hosted、Connector 和 Native A2A Agent 使用相同时间窗；
- 更慢的模型不会获得额外比赛时间；
- Worker 必须使用服务端 deadline 计算剩余预算，不信任 Runtime 自报时间。

### 10.2 重试规则

每个 AgentTask 最多两个 Attempt，即最多重试一次：

- 可重试：429、明确 5xx、Adapter 能证明请求尚未发出的 transport failure、
  结构化输出校验失败；
- 不重试：认证失败、模型不存在、无权限、永久 4xx、用户主动撤销；
- 只有剩余时间足以完成下一次调用时才重试；
- 两次 Attempt 使用相同 Task id 和 Arena idempotency key；
- 不自动切换 Provider、Model、thinking 或 Runtime；
- Provider 不支持请求幂等时，平台只能保证 Arena 业务不重复，不能保证 Provider 不重复计费。
- read timeout、连接中断或任何“可能已被 Provider 接收”的状态一律归为
  `request_outcome_unknown`，不得用 pre-send transport 分类触发重放。

### 10.3 默认收敛

- `decide` 失败、无效或超时：唯一收敛为 `pass`；
- `negotiate` 失败或超时：唯一收敛为协商 timeout；
- Binding revoke、Secret outage、Worker outage、Connector offline 和 Native Endpoint
  unavailable 都走同一个 Arena Finalizer，不产生缺失的逻辑行动；
- 默认结果不能重复写入；
- 超时后的晚到模型结果只记录审计事件，不改变比赛。

### 10.4 Hosted 与 Local 离线语义

| Runtime | 浏览器关闭 | 用户电脑离线 | Runtime 断开 |
|---|---|---|---|
| Hosted | 不受影响 | 不受影响 | Worker/Provider 按重试与 deadline 收敛 |
| Local Connector | 浏览器关闭不影响已连接 Connector | Connector 离线 | 停止新 Task，等待有限重连 |

Local Connector 心跳丢失后进入 `reconnecting`，重连窗口是 30 秒与当前行动剩余时间
中的较短者。窗口内使用原 Task/idempotency key 恢复；超时则按 `pass` 或协商 timeout
收敛。Agent 仍是 Game Agent；Connector 恢复前，Arena 对后续 Decide 显式记录
defaulted `pass`，而不是留下空记录，也不自动替换 Runtime。进行中的协商 timeout 是否
增加 `failedNegotiations` 由统一 Game 规则决定，Runtime 类型不能改变该规则。

## 11. Arena A2A 与 Native A2A 的定位

### 11.1 Arena A2A

Arena A2A 是产品内的受控业务交互：

- Agent 不直接通信；
- 所有消息由 Arena Gateway 中转、排序、校验、持久化和审计；
- 一个 `negotiation_id` 是一段协商的业务权威上下文；
- 每个 Agent 行动是一条 AgentTask；
- 对手只看到公开消息和合法业务状态；
- Arena 决定轮到谁、是否还能报价、是否可以 accept；
- Runtime 不能绕过 Gateway 联系对手或提交成交。

### 11.2 Native A2A Endpoint

Native A2A 是未来的第三种 Runtime Adapter：

```text
Arena AgentTask
  -> NativeA2ARuntimeAdapter
  -> remote Agent Card / Task / Message / Artifact
  -> normalized AgentTaskResult
```

它的价值是让已经暴露标准 A2A Endpoint 的外部 Agent 无需安装本地 Connector。
它不改变 Arena 的游戏规则、审计、可见性、deadline 或支付边界。
Agent Card 只描述相对静态的身份、能力和 Endpoint；现金、持仓、Task、协商、支付和
交付状态仍放在 Arena/Payment 记录中。

| 维度 | Arena A2A | Native A2A Endpoint |
|---|---|---|
| 性质 | Arena 内部业务协议与审计模型 | 外部 Runtime 的标准接入协议 |
| 权威上下文 | `game_id` / `round_id` / `negotiation_id` | 远端 A2A Task/Context |
| 是否允许 Agent 直连 | 否 | 仍由 Arena Adapter 调用远端 |
| 是否决定成交 | Arena 校验后决定协议状态 | 否，只返回候选结果 |
| 是否决定支付 | 否 | 否 |
| MVP 状态 | 本规格需要建立内部 Task 契约 | 后续补充 |

当前 Connector WSS 是自定义内部传输。除非未来实现并声明标准 binding，否则不得称为
Native A2A。

## 12. 对话、动作与可观测性

### 12.1 公开 Arena 时间线

适当比赛阶段可展示：

- `propose`、`counter-propose`、`accept`、`reject`；
- 不超过 100 字、经过转义的公开谈判文字；
- 报价、行动顺序和服务端时间戳；
- 协商 timeout；
- `accepted_pending_settlement`；
- 结算提交、链上确认、失败或恢复；
- 链上确认后库存提交。

公开时间线不得展示：

- API Key、Secret reference、System Prompt 或数据库中的私有策略原文；模型 public
  message 另经 PublicOutputPolicy 过滤，并明确存在语义推断残余风险；
- Provider 原始请求、响应、错误正文；
- Token 用量、私有性能日志；
- 对手私有资产；
- chain-of-thought。

### 12.2 Owner 私有执行视图

Agent Owner 可查看：

- Task kind、状态和最终结构化动作；
- Provider、Model、`thinking_enabled`；
- Attempt 次数；
- input/output/cached/reasoning Token 数值（Provider 有返回时）；
- `usage_complete`；
- 单次 Attempt 与总耗时；
- 归一化错误类别；
- 是否因 deadline 使用默认动作。

Provider 不返回某个 usage 字段时保存 `null` 并标记不完整，不伪造估算值。

### 12.3 Operator 审计

Operator 可以按 `user_id`、`agent_id`、`game_id`、`task_id`、`negotiation_id` 和
`settlement_id` 关联事件，但默认不能读取用户 API Key 或私有 chain-of-thought。

推荐指标：

- Agent provisioning success rate；
- Provider/Model/thinking 维度的 P50/P95/P99；
- Task timeout/default rate；
- Retry rate 与错误类别；
- Token usage；
- queued task age；
- late result count；
- 每局 wall time；
- Hosted 与 Local Runtime 的可用率。

## 13. 持久化模型

以下逻辑模型与 `games / rounds / game_agents` 共同构成最小持久化 foundation。

### 13.1 `arena_agents`

稳定 Agent identity：

- `id`、`owner_user_id`；
- `name`、`avatar_ref`；
- `status`；
- nullable `runtime_update_job_id`，非空时派生 `runtime_update_pending`，用于串行化
  join 与异步 Runtime 配置/凭据更新；
- `created_at`、`updated_at`。

Provider、Prompt、余额、持仓、排名和凭据不得放入公开 Agent identity。
增加 `UNIQUE(owner_user_id, id)`，供 `game_agents(user_id, agent_id)` 建立 ownership
复合外键。

### 13.2 `arena_model_credentials`

- `id`、`owner_user_id`、`provider`；
- `secret_ref`；
- `fingerprint`；
- `status`；
- `last_validated_at`、`replaced_at`、`revoked_at`；
- `created_at`、`updated_at`。

该表永远不含原始 Key；fingerprint 不用于认证。MVP 通过 Hosted Config 上的唯一约束
保证一个 Credential 只绑定一个 Hosted Agent。

### 13.3 `arena_hosted_configs`

- `id`、`agent_id`；
- `provider`、`model`、`thinking_enabled`；
- 私有 `strategy_instructions`；
- 平台 Prompt/schema version；
- `credential_id`；
- Token/输出长度等平台上限；
- `created_at`、`updated_at`。

用户看不到“revision”概念。入局时由平台自动复制必要字段和 hash 到 Game Agent 快照。
`credential_id` 在 MVP 为唯一 1:1 引用。

### 13.4 `arena_runtime_bindings`

- `id`、`agent_id`；
- `runtime_kind = hosted | connector | native_a2a`；
- Hosted `hosted_config_id`，或 Connector
  `connector_binding_id + connector_binding_epoch`，或未来 Native endpoint ref；
- Arena route 状态；Connector 在线状态只作非权威投影；
- `created_at`、`disabled_at`。

MVP 使用 `WHERE disabled_at IS NULL` 的 partial unique index，保证一个 Agent 只有一个
当前 route。它不替代 `connector_bindings`。

### 13.5 `game_agents`

- `id`、`game_id`、`user_id`、`agent_id`；
- `runtime_binding_id`；
- 私有 `config_snapshot` 与 `config_hash`；
- 参赛状态；`joined | active | settling` 统一视为 active Game，终态至少包含
  `completed | cancelled`；
- 初始资产分配与 Arena-owned 关联；
- `joined_at`。

关键唯一约束：

- `UNIQUE(game_id, user_id)`；
- `UNIQUE(game_id, agent_id)`。

`config_snapshot` 至少冻结 Provider/immutable model id、effective thinking、
adapter version、Prompt version、Task/action schema version、capability version、
input/output/token 上限、strategy instructions/hash 和精确 `credential_id`。
Attempt 另记录 Provider 实际返回的 model/version。安全 revoke 可以令当前 Game
default，但不会偷偷改用新 Credential。

### 13.6 `arena_agent_tasks`

- Task envelope 全部关联字段；
- versioned input snapshot；
- `deadline_at`、`idempotency_key`；
- `status`、`attempt_count`；
- `leased_by`、`lease_expires_at`；
- `created_at`、`completed_at`。

关键唯一约束：

- `UNIQUE(game_agent_id, idempotency_key)`；
- Task 状态使用数据库 CHECK；
- 一个 Task 只有一个终态业务结果。

### 13.7 `arena_agent_task_results`

Durable result inbox：

- `result_id`、`task_id`；
- `runtime_status = succeeded | failed | timed_out | cancelled`；
- 成功时经过 PublicOutputPolicy 的严格 sanitized candidate action；
- `message_replaced` 与 `public_output_policy_version`；
- 数据库生成的 `result_received_at`；
- `apply_status = pending | applied | rejected`；
- `arena_applied_at` 或 `arena_rejected_at`；
- 安全错误类别。

关键约束：

- `UNIQUE(result_id)`；
- `UNIQUE(task_id)`；
- Task 终态 CAS 与 Result insert 在同一事务，Finalizer 和 Result Sink 只能一个获胜；
- Arena Result Consumer 使用 row lock/CAS，并在业务写入事务内更新 apply status。

### 13.8 `arena_agent_task_attempts`

- `task_id`、`attempt_no`；
- Provider、Model、thinking；
- `created_at`、`request_sent_at`、`runtime_completed_at`；
- monotonic duration；
- usage 数值与 `usage_complete`；
- Provider request id 的安全引用；
- `status`、归一化 `error_class`。

数据库使用 `UNIQUE(task_id, attempt_no)` 和
`CHECK (attempt_no BETWEEN 1 AND 2)`；Attempt 分配在锁内原子进行。

### 13.9 `arena_agent_task_events`

Append-only：

- `created`；
- `leased`；
- `attempt_started`；
- `attempt_failed`；
- `result_submitted`；
- `result_applied`；
- `result_rejected`；
- `defaulted`；
- `late_result_ignored`；
- `cancelled`。

它不替代 Game Event、Connector Event 或 Settlement Event。

### 13.10 `hosted_credential_validation_jobs`

该表是 provisioning queue，不是 Arena AgentTask：

- `id`、`credential_id`、`hosted_config_id`；
- `kind = create | update | replace`；
- 私有 `candidate_config_snapshot`、`candidate_config_hash` 与
  `expected_current_config_hash`；
- 固定 validation schema/version；
- `status`、`attempt_no`、`max_attempts`、`next_attempt_at`、`deadline_at`；
- `leased_by`、`lease_expires_at`；
- 安全错误类别；
- `created_at`、`completed_at`。

它不带 `game_id`、`round_id` 或比赛数据。update/replace 的内部 candidate 可以暂存
私有 strategy，但 credential validation Prompt/Provider request 只能读取
Provider/Model/thinking/credential/capability 所需字段，绝不使用或发送 strategy。
状态机、lease 和 Attempt 上限可以复用 queue 基础，但不能在审计中伪装成
`arena.decide`。对同一 Credential/Config 组合使用 partial unique constraint 保证
最多一个 active validation job。update/replace 终态后清除不再需要的失败 candidate
明文，只保留 hash、安全字段与结果；成功 candidate 成为当前 Hosted Config。

### 13.11 `hosted_credential_lifecycle_jobs`

由 Credential Controller 独占：

- `id`、`credential_id`；
- `kind = revoke | delete`；
- `idempotency_key`、`status`、`attempt_no`、`deadline_at`；
- `leased_by`、`lease_expires_at`；
- 安全错误类别与完成时间。

API 只创建 job，无 Secret delete 权限；Controller 只消费该表，无
`GetSecretValue`。validation 与 lifecycle 使用不同 DB view/role，不能相互领取。

## 14. HTTP API 契约

具体 JSON schema 在实现阶段由 Pydantic 类型冻结。首版路由：

### 14.1 Capability

- `GET /api/hosted-agents/capabilities`
  - 返回可选 Provider、Model、thinking 能力和显示元数据；
  - 不返回内部 endpoint、云凭据或未启用模型。

### 14.2 Credential

- `POST /api/model-credentials`
  - 唯一允许接收原始 API Key 的 write-only ingress；
  - Session + CSRF + 按 `user + route` 隔离的 `Idempotency-Key`；
  - 返回 `credential_id`、Provider、状态和安全指纹，不回显 Key/secret_ref。
- `GET /api/model-credentials?scope=mine`
  - 只返回 owner 的安全元数据和引用状态。
- `POST /api/model-credentials/{credential_id}/replace`
  - Agent 有 active Game 时返回 `409`，不进行比赛中途凭据切换；
  - 请求只接受 owner 新近通过 `POST /api/model-credentials` 创建、尚未绑定的
    `replacement_credential_id`，不再次接收原始 Key；
  - `replacement_credential_id.provider` 必须等于当前 Provider；Provider 变更必须走
    Agent PATCH；
  - 按现有 Hosted Config/Model 验证成功后，最终事务重新锁 Agent、复查无 active
    Game，CAS 切换 `credential_id`，再撤销旧 row。
- `POST /api/model-credentials/{credential_id}/revoke`
  - 先禁止新 Task 领取，再由 Credential Controller 撤销 Secret；
  - 安全 revoke 即使有 active Game 也允许，但该 Game 后续行动会 default，不自动
    切到另一 Credential；
  - 保留历史 Game/Task 的非秘密审计证据。
- `POST /api/model-credentials/{credential_id}/revalidate`
  - Owner-only、限流；
  - 只为已经 1:1 绑定 Hosted Config 的 `invalid` Credential 原子切回
    `pending_validation`，并创建或复用一个 durable validation job。

Idempotency record 必须有长度、数量和 TTL 上限。request digest 不得包含原 Key 的普通
hash；使用排除 Key 的 canonical request metadata 与独立 pepper HMAC fingerprint
处理重复请求。

### 14.3 Agent 创建与管理

- `POST /api/hosted-agents`
  - Session + CSRF + `Idempotency-Key`；
  - 接受 name、template/strategy、Provider、Model、thinking 和当前 owner 未绑定的
    `credential_id`；
  - 不接受原始 API Key；
  - 相同 owner/key/request digest 的已完成 replay 必须先返回当前 owner-scoped
    Agent 投影；只有 fresh create 才重新校验当前 Credential 与 capability；
  - 一次响应返回 Agent、Binding 和 provisioning 状态；
  - 服务端强制 Credential 与 Hosted Config 为 MVP 1:1。
- `GET /api/hosted-agents?scope=mine`
  - 只列出当前用户拥有的 Agent 与安全状态摘要。
- `GET /api/hosted-agents/{agent_id}`
  - Owner-only 私有配置摘要。
- `PATCH /api/hosted-agents/{agent_id}`
  - 名称/头像可以独立修改；
  - Agent 有 active Game 时，Provider、Model、thinking、策略和 Credential 等
    Runtime-affecting 修改返回 `409`；
  - 无 active Game 时可提交完整 candidate
    `Provider/Model/thinking/strategy/replacement_credential_id?`，Binding 回到
    `provisioning` 并异步 validation；
  - Provider 改变时 `replacement_credential_id` 必填，且其 Provider 必须与 candidate
    Provider 一致；同 Provider 修改可继续使用当前 Credential；
  - Runtime-affecting 修改与 credential replace 使用同一 Agent row lock 和
    `runtime_update_job_id`；pending 时 join 返回 `409`，validation job 私有保存
    candidate config，完成后的最终 CAS 再次核对 job id、expected config hash 和
    active Game；
  - validation 成功后一次 CAS 应用完整 candidate；若 Credential 已替换，再撤销旧
    Credential；失败时当前 Config/Credential 保持不变；
  - MVP 不创建 pending revision，也不做比赛中途 Runtime/config 切换。
- `POST /api/hosted-agents/{agent_id}/disable`
  - 阻止未来 Task，不删除历史比赛和支付证据。

旧 `POST /api/agents/register` 已随内存 matching/ELO 原型移除。前端的一次
“Create”点击顺序调用 Credential 与 Hosted Agent 两个幂等 API；这是一个用户
步骤，而不是把 Secret 混回 Agent request。

### 14.4 参赛

- `POST /api/games/{game_id}/participants`
  - Session + CSRF + `Idempotency-Key`；
  - 请求选择当前用户拥有且 `ready` 的 Agent；
  - 相同 Agent 的重试返回原 Participant；
  - 试图换成另一 Agent 时返回 `409`；
  - 数据库唯一约束处理并发请求。
- `GET /api/games/{game_id}/participants/me`
  - 返回当前用户在该局的唯一 Game Agent。

### 14.5 Timeline

- `GET /api/games/{game_id}/events`
  - 公开、经过可见性过滤的 Arena 时间线。
- `GET /api/games/{game_id}/agents/{agent_id}/tasks`
  - Owner-only 执行摘要；
  - 不返回 Prompt、Secret 或原始 Provider 正文。

Task create/claim/complete 是内部 Service，不向浏览器开放。

## 15. 安全与滥用控制

| 风险 | 首版控制 |
|---|---|
| API Key 泄漏 | 专用 write-only ingress、SSM/KMS、三类 IAM 身份、不记录 body、响应不回显、边缘 access log 不记录原始 URI/query、托管残余风险提示 |
| 跨用户访问 | Session ownership、CSRF、对象级过滤、跨 owner 返回 404 |
| SSRF / 内网探测 | 代码固定 Provider host、TLS、no redirect/no env proxy、egress allowlist；不接受自定义 endpoint/header |
| Prompt injection | 平台规则优先；公开消息按数据编码；严格结构化输出和 Arena 二次校验 |
| 任意工具或数据访问 | Hosted Runtime 无 tools、文件、shell、browser、MCP |
| 隐藏推理泄漏 | 不请求可见 CoT；Provider 返回时在内存丢弃；只保留 usage 数值 |
| 输出 XSS | 公共 message 长度有界、服务端转义/前端文本渲染 |
| 策略/Secret 经 message 泄漏 | PublicOutputPolicy 检测 secret/PII/策略片段，拒绝后改用服务端模板且不保存原文 |
| 重放或重复提交 | Task idempotency、唯一约束、CAS/row lock、终态不可覆盖 |
| 迟到结果改写比赛 | deadline 后记录 `late_result_ignored` |
| Worker 越权写业务 | 独立 PostgreSQL role、只读 execution view、受限 Result Sink、越权 SQL 测试 |
| 模型费用失控 | input/context/output 上限、validation 限流、每用户/每局/每日配额、全局/Provider 并发与 queue depth |
| Provider 故障拖垮回合 | 同局统一 deadline、最多一次重试、默认动作 |
| Secret backend 故障 | fail closed，不降级到明文 DB/env 文件 |
| 比赛中改配置作弊 | 入局自动快照；活跃 Game 不读取后续配置修改 |

部署前必须冻结每用户创建/validation 频率、同时进行 Game 数、每局与每日最大调用数、
最大 input bytes/context items/output tokens、Provider/全局并发和 queue depth。若
Provider `Retry-After` 超过行动剩余时间，不等待也不重试，直接交给 Arena Finalizer
收敛。

## 16. PaymentMandate 与结算边界

产品目标是在加入 Game 时由支付主体确认一份受限 `PaymentMandate`：

- `game_id`；
- network 与 token；
- 单笔上限与本局累计上限；
- 有效期；
- 允许的 settlement path/payee 约束；
- 可撤销状态。

Hosted Agent 永远不能获得钱包私钥，也不能自行签署任意链上交易。Agent 只参与决策
与协商。`accept` 后由 Arena 冻结 Deal，Settlement 再验证授权并提交。

必须区分：

- Wallet-backed User：由用户控制的钱包进行真实 testnet 授权；
- Sandbox Guest：由平台明确拥有、隔离、限额、可过期的 testnet-only signer 执行；
- 当前 EIP-3009 direct relay：是单笔授权原型，不天然等价于可覆盖未来多笔交易的
  通用 PaymentMandate。

PaymentMandate 的精确签名、额度消费和撤销机制属于
[Arena Settlement Integration](./arena-settlement-integration.md) 的独立实现依赖。
Hosted Agent 可以先完成决策/协商 vertical slice，但不能在该依赖未完成时宣称
“用户离线后可自动完成全部支付”。

完整离线 testnet 交易发布前还必须冻结：

- 并发 Deal 的 `reserve / consume / release` 额度状态机；
- chain id、token、settlement contract、game、payee、nonce 的签名域；
- revoke 与已 reserved/已 submitted 支付的竞态语义；
- chain unknown、reorg 与数据库提交失败恢复；
- Sandbox Signer 使用独立服务、IAM 和密钥域；Hosted Worker 无权访问 signer。

## 17. 单机 beta 部署

目标腾讯云主机为 2 vCPU、4 GB RAM、70 GB 磁盘。首版拓扑：

```text
Caddy
  -> Next.js
  -> FastAPI control plane
  -> Arena Core Worker (no public port)
  -> Hosted Worker (no public port)
  -> Credential Controller (no public port, low concurrency)
  -> PostgreSQL

External:
  -> Tencent Cloud Secret Manager/KMS
  -> allowlisted model Provider APIs
  -> Injective EVM testnet
```

约束：

- PostgreSQL 不暴露公网端口；
- Arena Core Worker 不监听公网端口，以数据库 leader/lease 防止重复 finalizer；
- Hosted Worker 不监听公网端口，只主动访问 Provider 与 Secret Manager；
- Credential Controller 只消费持久化 Secret lifecycle job；
- 使用 PostgreSQL queue，2C4G MVP 的 Worker 并发保持有界并通过
  2/5/10/12 Agent 压测调整；
- Arena Task 与 credential validation 使用独立 claim/并发槽，比赛 Task 优先；
- 单局最大 Agent 和同时 Game 数由容量测试冻结；超过容量时拒绝开局，不用排队延迟
  决定竞技结果；
- API、Worker、Credential Controller 使用不同云权限和数据库角色；
- Worker Provider 出站使用固定 host/no redirect/no env proxy，生产主机增加 egress
  firewall/proxy；
- `ADX_HOSTED_AGENTS_ENABLED=false` 为默认安全开关；
- Secret backend、Provider allowlist 或必需权限缺失时 Hosted 路径 fail closed；
- 单机 beta 不宣称高可用；Worker 重启依赖 lease 与 deadline 恢复；
- 日志轮转、数据库备份和 Secret 审计沿用自托管部署规范。

## 18. 当前实现矩阵

截至 2026-07-25：

| 能力 | 当前状态 | 说明 |
|---|---|---|
| Legacy Agent/matching/ELO API | 已移除 | 不再存在第二套内存业务权威或 Supabase 工厂 |
| Hosted Agent 创建 UI | 已实现 | `/agents` 同时保留 Local Connector，并提供受 readiness/auth 控制的 Hosted 创建、列表、详情和 Runtime PATCH |
| Provider contract/PromptBuilder/DirectModelDriver | 已实现 | 覆盖结构化动作、deadline、单次 retry、usage 与安全出站边界 |
| 真实 Provider Adapter | 已实现，本地验收 | DeepSeek/OpenAI-compatible HTTPS Adapter 已完成真实五回合与 accepted negotiation；不等于生产服务器验收 |
| 用户 API Key ingress | 已实现 | write-only ingress、摘要幂等、PostgreSQL control repository 与无回显边界已接线 |
| Tencent Secret Manager | 生产组合已实现，实机待验收 | SSM Writer/Reader/Controller 权限端口与 fail-closed 组合存在；真实 CAM 身份和部署证据仍缺 |
| thinking 配置 | 已实现 | capability registry、UI、快照和 Provider 映射覆盖 unsupported/optional/always-on |
| usage/latency/attempt | 已持久化 | Hosted Worker 将安全 Attempt 元数据与 usage 写入 PostgreSQL，不保存 reasoning text |
| Persistent AgentTask | 已接入游戏 | 版本化契约、lease/CAS 与 Pawnhouse Runtime Run 已完成 |
| Result Sink/Consumer/Finalizer | 已接入游戏 | 数据库权威时间、默认收敛、late/duplicate 处理和 exactly-once 投影已完成 |
| Credential validation/lifecycle jobs | 核心路径已实现 | durable validation、claim/CAS、Credential Controller 和创建/Runtime PATCH 已实现；其余生命周期操作仍待完成 |
| Hosted Worker | 已实现 | 独立无公网端口 Worker 已进入 Compose，并通过本地多回合恢复路径 |
| Game Agent 单局唯一 | 已实现 | 数据库约束、入局快照和当前 Runtime/config 冻结已实现 |
| Arena `decide`/`negotiate` adapter | Hosted/rule 已实现 | Local Connector 游戏 Adapter 仍未实现 |
| Local Connector 控制面 | Self-hosted beta 已实现 | 仍缺 Arena typed task/result 适配 |
| Native A2A Endpoint | 未实现 | 作为后续第三 Adapter |
| EIP-3009 testnet direct settlement | 原型存在 | 不等于完整 x402 或 PaymentMandate |

当前可直接复用的能力：

- production Session、CSRF 和对象所有权边界；
- Connector 的 durable-before-delivery、幂等键、Receipt/Event、脱敏和有界 payload 模式；
- FastAPI、PostgreSQL、Next.js、Caddy 与 Docker Compose 部署壳；
- Settlement 的 testnet direct-transfer 原型。

已删除或明确不能作为 Hosted 业务权威的能力：

- legacy Supabase `agents`/ELO/battle schema（已删除）；
- 内存 AgentRegistry、matching engine 和自动接受式 negotiation（已删除）；
- Connector Command `succeeded`；
- 任意一条 Runtime stdout/event；
- 要求持久化 `reasoning` 的旧 schema（已删除）；
- `REAL`/float 金额字段。

## 19. 验收门槛

### 19.1 创建与持续在线

- [ ] 登录用户可在一个页面一次提交创建 Hosted Agent；
- [ ] API Key 不会出现在 DB dump、日志、Trace、Audit、HTTP 响应或错误正文；
- [ ] 创建页说明 Arena 是模型凭据运行时托管方，并建议独立限额 Project Key；
- [ ] Credential create/replace/revoke 与 Agent create 是独立 API，UI 仍保持一次点击；
- [ ] 只有统一 Credential ingress 接收原 Key；replace 只传
      `replacement_credential_id`；
- [ ] 创建请求重试不会生成两个 Agent、Credential 或 Binding；
- [ ] Agent 只有连通性检查成功后才进入 `ready`；
- [ ] 浏览器关闭、用户电脑离线后，Hosted Agent 继续接收后续 Arena Task；
- [ ] API/Worker/PostgreSQL 重启后状态可恢复。

### 19.2 身份、参与与公平

- [ ] 两个并发 join 请求最多创建一个 `(game_id, user_id)` Game Agent；
- [ ] 第二个不同 Agent 的 join 返回 `409`；
- [ ] 同一个 Agent 可参加之后的新 Game；
- [ ] 入局配置自动快照，活动比赛不受后续配置修改影响；
- [ ] join 与 Runtime-affecting PATCH/replace 并发时只有一个方向成功；
- [ ] `runtime_update_job_id` 不会因 validation 失败、取消或进程重启永久卡住；
- [ ] update/replace 失败时当前可用 Hosted Config 不被 candidate 覆盖；
- [ ] Task 创建事务冻结 participant view；Worker 不读取可变实时 Game 状态；
- [ ] 不同用户不能读写对方 Agent、Credential、Binding 或私有 Task；
- [ ] 同一 Game 的所有 Runtime 获得同一行动时间窗。

### 19.3 Provider 与任务

- [ ] Provider/Model/thinking 组合必须来自服务端 capability registry；
- [ ] thinking 开关按 Adapter 映射，不支持时明确拒绝；
- [ ] 不保存或展示 hidden reasoning；
- [ ] usage 缺失时标记不完整，不伪造；
- [ ] 每个 Task 最多两个 Attempt；
- [ ] 数据库 CHECK/锁内计数阻止第三次 Attempt；
- [ ] 429/5xx/transport/invalid structured output 可在时间允许时重试一次；
- [ ] 永久 4xx 不重试；
- [ ] credential validation 的 transient backoff 可跨重启恢复，revalidate 限流且
      不会创建重复 active job；
- [ ] 不自动切换 Provider、Model 或 Runtime；
- [ ] late/duplicate result 不会重复提交业务动作。
- [ ] Worker 整体停止时，Arena Finalizer 仍会收敛所有 expired Task；
- [ ] durable Result Sink/Consumer 对每个候选结果最多应用一次。

### 19.4 Arena 与可观测性

- [ ] Decide 只接受严格 `buy | sell | pass`；
- [ ] Negotiate 只接受严格 `propose | accept | reject`；
- [ ] extra fields 被拒绝，价格不用 float，公开消息不超过 100 字；
- [ ] invalid/timeout Decide 只产生一次 `pass`；
- [ ] invalid/timeout Negotiate 只产生一次 timeout；
- [ ] 新 Game 不自动携带上一局自由对话；
- [ ] 公开时间线可看到合法协商和结算状态；
- [ ] strategy 原文片段、API-key-like/PII-like message 被拒绝并用中性模板替换，原文
      不进入任何持久化或日志；
- [ ] Owner 私有视图可看到 thinking、usage、耗时、Attempt 和最终动作；
- [ ] Provider/Connector/A2A task success 不能直接移动库存或证明支付；
- [ ] 只有链上确认并完成 Arena 幂等事务后才显示库存已提交。

### 19.5 部署与安全

- [ ] 生产 BYOK 使用真实 Tencent Secret Manager backend；
- [ ] API、Worker、Credential Controller Secret 权限分离；
- [ ] Worker 数据库角色尝试更新 inventory/settlement/DDL 时被 PostgreSQL 拒绝；
- [ ] Worker 直接更新 Credential/Config/Binding/Agent/validation terminal 被拒绝，
      受限 validation complete 函数对 stale job/hash/lease 被拒绝；
- [ ] redirect、环境代理、metadata IP 和超大 Provider response 测试均被拒绝；
- [ ] Secret backend 不可用时 fail closed；
- [ ] 自定义 endpoint、tools、shell、文件和浏览器访问均不可用；
- [ ] 2/5/10/12 Agent 压测记录 P50/P95/P99、timeout、retry、Token 和整局耗时；
- [ ] 默认 `action_timeout_ms` 来自真实测试，而不是文档中的固定猜测值；
- [ ] 完成依赖锁、secret scan、漏洞扫描与发布证据。

## 20. 后续兼容方向

### 20.1 通用 Agent Studio

后续可新增 `LangGraphDriver` 或其他工作流 Driver，但必须实现同一个：

```text
AgentTask -> AgentTaskResult
```

Agent Studio 可以编辑人格、子步骤、工具和评估，但不能：

- 绕过 participant view；
- 直接写 Arena 数据；
- 延长单局统一 deadline；
- 访问钱包私钥；
- 将私有 chain-of-thought 变成平台要求的审计字段。

在 Agent Studio 上线前，Hosted Agent 继续是固定的 DirectModelDriver；每个逻辑
AgentTask 最多两个 Provider Attempt，不承担图编排复杂度。

### 20.2 Native A2A

新增 `NativeA2ARuntimeAdapter`，把内部 Task、Message、Artifact 和终态映射到标准 A2A。
Arena `negotiation_id`、业务校验和审计仍是权威。

### 20.3 多 Runtime 与故障转移

数据模型允许未来保留多个历史 Binding，但 MVP 不提供活动比赛中的 Runtime 切换。
若以后增加故障转移，必须先定义公平性、授权、费用、幂等和比赛快照规则，不能简单在
Hosted 与 Local 间自动切换。

## 21. 外部参考

- [A2A Protocol — Key Concepts](https://a2a-protocol.org/latest/topics/key-concepts/)
- [A2A Protocol — Life of a Task](https://a2a-protocol.org/latest/topics/life-of-a-task/)
- [A2A Protocol — Custom Protocol Bindings](https://a2a-protocol.org/latest/topics/custom-protocol-bindings/)
- [Tencent Cloud Secrets Manager](https://intl.cloud.tencent.com/zh/products/ssm)
- [Tencent Cloud CAM 权限管理](https://intl.cloud.tencent.com/zh/document/product/598/57154)
- [OpenAI Reasoning Guide](https://developers.openai.com/api/docs/guides/reasoning)
- [Anthropic Extended Thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking)
