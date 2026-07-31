# Arena 402 Agent 入场与 Runtime 绑定

> 状态：Hosted Runtime、统一 Task/Result、生产 Game Operator API、通用 Join 的
> Game Core 投影和 Arena 接线已实现；Local Connector 已完成 Local Agent identity、
> 冻结 route、Connector-owned session、数据库 Task
> dispatcher、`arena.decide` / `arena.negotiate` typed transport、终态 Result
> durable 回传、Result Sink 和 Hosted/Connector mixed-Runtime 编排；另已实现默认
> 关闭的 WSS wake + stateless MCP Task Broker、启动/重连与 sequence gap 主动
> cursor sync，并通过隔离 Docker 的 WSS + MCP + PostgreSQL 协议 E2E。真实
> CC/Codex 完整比赛与生产重连仍待 E2E 验收。
>
> Hosted Agent 的详细产品、安全与持久化设计见
> [`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md)。Connector 的当前能力、
> 安全边界与部署行为仍以
> [`local-agent-connector-spec.md`](local-agent-connector-spec.md) 和
> [`self-hosted-connector-deployment.md`](self-hosted-connector-deployment.md)
> 为准。

## 目标

`/agents` 是统一入口，MVP 提供两条清晰路径：

1. **Create hosted Agent**：平台托管受约束 Runtime；用户可选择模板，并在 BYOK
   模式下选择 allowlisted Provider/Model、配置 `thinking_enabled`、填写有长度限制
   的 `strategy_instructions`，再一次性提交模型 API Key；
2. **Connect local Agent**：用户安装 Connector，绑定自己电脑上的受支持 Runtime。

游客演示可以由平台创建受限 Hosted Agent，但不形成第三套 Runtime 协议。未来
Native A2A Endpoint 也是第三种 Runtime Adapter，而不是新的游戏规则。

所有 Runtime 共享相同起始资产、`AgentTask -> AgentTaskResult` 契约、行动时间窗、
Arena 校验与结算边界。接入方式不能带来额外现金、持仓、隐藏行情或更长 deadline。

## 身份层次

| 对象 | 含义 | 生命周期与权威 |
|------|------|----------------|
| User | 平台账户或明确隔离的游客主体 | 跨游戏，身份服务 |
| Agent | 名称、头像、所有权等稳定展示身份 | 跨游戏，Arena identity |
| Hosted Config | Provider、Model、thinking 和私有策略说明 | 私有、可修改 |
| Model Credential | Hosted 模型凭据的安全元数据与 Secret 引用 | 外部 Secret Manager + 元数据表 |
| Runtime Binding | Agent 到 Hosted、Connector 或未来 Native A2A Runtime 的当前路由 | 可撤销；MVP 仅一个当前绑定 |
| Game Agent | Agent 在某一局中的参赛记录与冻结配置快照 | 单局，Arena |
| PaymentMandate | 指定 Game、网络、Token、额度、有效期和 payee 范围的支付授权 | 独立 Settlement 权威 |

Agent Card 只保存相对静态的身份与能力。现金、持仓、`failedNegotiations`、Task、
协商、结算和排名必须放在 Game/Payment 记录中，不能不断重写 Agent Card。

一名 User 在同一 Game 中最多有一个 Game Agent，由
`UNIQUE(game_id, user_id)` 原子保证；同一个 Agent 可以参加后续 Game。入局时平台
自动冻结 Runtime、Provider/Model、effective thinking、策略和 schema 版本。MVP
不向用户暴露 Revision，也不允许活动比赛中途切换 Runtime 或配置。

## Hosted Agent

用户在一个表单中：

1. 登录并命名 Agent；
2. 选择模板或填写受限 `strategy_instructions`；
3. 选择服务端 allowlist 中的 Provider、不可变 Model 和 thinking 开关；
4. 一次性提交 API Key；
5. 平台通过 write-only ingress 将原 Key 写入批准的外部 Secret Manager；
6. 独立 Worker 执行最小连通性验证；
7. Binding 由 `provisioning` 进入 `ready` 或 `degraded`；
8. `ready` 后用户选择该 Agent 加入 Game。

Agent 创建 API 只接受 `credential_id`；只有独立 Credential ingress 可以接收原
Key。业务数据库、日志、Trace、Audit、AgentTask 和前端响应均不得出现原 Key。
Hosted Worker 只有所需 Secret prefix 的读取权限，不能创建、列出、撤销 Secret，
也不能直接写入买卖池、库存、结算或钱包表。

Arena 402 是模型凭据的运行时托管方，并非零知识系统。创建页必须提示用户使用独立、
限模型、限额、限速且可过期的 Provider Project Key。用户关闭网页或电脑离线后，
Hosted Agent 仍由云端 Worker 持续参加已加入的 Game。

平台只记录 thinking 是否生效、Provider/Model、Token 数值、耗时、安全错误类别、
经过过滤的公开文字和最终结构化动作。不得请求、保存或展示 private
chain-of-thought；Provider 若返回 reasoning text 或私有推理载荷，应在解析内存中
丢弃。

## Local Connector

1. 用户安装 `adx-connector`；
2. Connector 发起出站 HTTPS/WSS 配对；
3. 用户在浏览器批准 Device；
4. 平台显示本地 Runtime inventory；
5. 用户选择 Runtime、允许目录和本地能力；
   inventory 会分别展示任务开关、CLI 本地认证状态、安全 flags 兼容性、Arena
   隔离 profile 和 `local_execution_ready`；仅安装、在线或版本可读不等于可执行
   Arena Task。
6. Connector Gateway 通过
   `POST /api/connectors/devices/{device_id}/bindings` 创建自己的 Binding，并记录
   `working_directory`；旧 Binding 可补齐一次该目录，已冻结目录不能静默修改。
   Arena route 只引用
   `connector_binding_id + binding_epoch`；
7. 用户通过 `POST /api/local-agents` 把 owner-scoped Connector Binding 注册为
   Arena Agent；
8. 用户通过
   `POST /api/v1/pawnhouse/games/{game_id}/connector-participants` 加入 Game，
   Arena 创建 Game Agent 并冻结该引用；
9. 回合创建 AgentTask 后，Dispatcher 在没有 Session 时先幂等排队
   `session.start`，获得 Connector-owned `session_id` 后再排队 typed
   `task.dispatch`。Session 和本地 `working_directory` 不复制进 Arena 业务状态。
   typed Task 实际在 Connector 创建的短生命周期空目录运行，不继承参赛时冻结目录
   中的项目指令；Claude 使用 no-tools profile，Codex 使用 read-only sandbox
   profile。两端都会在 readiness 字段不一致时拒绝执行。

默认 `ADX_CONNECTOR_TASK_TRANSPORT=wss` 继续使用上述 Dispatcher。启用
`ADX_ARENA_MCP_ENABLED=true` 并把 Connector 设为 `mcp` 时，WSS 仍负责在线状态、
心跳、`session.start` 和不含任务正文的 `task.available` 唤醒；Connector 用 Device
凭据换取绑定到 `device_id + binding_id + binding_epoch` 的短期 token，再通过
stateless MCP 分别 claim、submit 或 release。MCP 请求不保存协议 Session，真正的
执行租约保存在 PostgreSQL，最终结果仍只经 Arena Result Sink 应用。

该 MCP server 同时提供 status 与有界 cursor sync，供受管客户端启动/重连恢复。
Gateway 的 hello ACK 只向已认证 Device 返回最小 `binding_id + binding_epoch`
快照；Go Connector 记录这些冻结 route，并在启动/重连及检测到 Gateway sequence
gap 时主动执行有界 sync。Gateway 对未完成 Task 的周期 wake 重发继续提供低延迟
提示和额外恢复机会。因此
`task.available.ack`、MCP 成功或本地 Runtime 成功都不是 Arena 业务动作的权威证据。

`adx-connector`、`ADX_*` 和现有协议消息名属于兼容标识，不做破坏性重命名。
Connector 路径的模型凭据、OAuth、钱包私钥和本地环境秘密始终留在用户设备上，
不适用 Hosted BYOK 的 Secret Manager 例外。

Local Agent 依赖 Connector 在线。心跳丢失后的恢复窗口是 30 秒与当前行动剩余时间
中的较短者；窗口内使用同一 Task/idempotency key 恢复。超出窗口后，当前 Decide
由 Arena Finalizer 明确收敛为 `pass`，当前 Negotiate 收敛为 timeout；后续行动也
保留 explicit default 记录，不留下空洞，不自动切换 Hosted Runtime。

## 统一游戏调用

Arena 为每次逻辑行动创建不可变、版本化的 AgentTask，并在创建事务中冻结 participant
view、Game Agent 配置、绝对 `deadlineAt` 和 hash。两种 Task kind 为：

- `arena.decide`：结果 `action="buy" | "sell" | "pass"`；
- `arena.negotiate`：结果
  `action="propose" | "accept" | "reject"`。

所有 Runtime 使用同一 Game 的 `action_timeout_ms`；具体默认值由真实
Provider/Model/thinking 与 2/5/10/12/25/50/100 Agent 负载的 P95/P99 加缓冲校准，而不是在
Adapter 中写死。每个 AgentTask 最多两个 Provider/Runtime Attempt，只在错误可重试
且剩余时间足够时重试，不自动切换 Provider、Model 或 Runtime。

Runtime 只提交候选 `AgentTaskResult`。Connector dispatch ACK、Hosted Provider
success、stdout、`runtime.message` 或 Native A2A Task success 都不等于合法业务
动作。Arena Result Sink 在持久化前过滤公开文字，使用数据库时钟生成
`result_received_at`，再由 Result Consumer 校验并最多应用一次。独立 Deadline
Finalizer 在 Runtime 不可用时也会收敛过期 Task。

Agent 之间不直接通信。所有公开协商都经 Arena Gateway 排序、校验、持久化和审计。
`accept` 只进入 `accepted_pending_settlement`，不能证明付款或库存转移。

## 权限、撤销与支付

- 用户拥有 Agent，Device 重连不能改变所有权；
- Runtime Binding 撤销阻止新任务；在途调用按原 deadline 收敛；
- Device/Binding/Credential 撤销不删除历史游戏和支付证据；
- Hosted Credential 的紧急 revoke 可在活动 Game 执行，但后续行动 default；
- 本地 Runtime task 默认关闭，必须由用户显式启用；
- 平台不得向本地 Agent 下发任意 shell、任意 argv 或秘密值；
- 模型 Runtime 永远不能获得钱包私钥或任意签名能力；
- 完整离线支付要求独立实现受限、可撤销、可审计的 PaymentMandate；
- 当前 EIP-3009 direct relay 是单笔授权原型，不等于 PaymentMandate 或完整 HTTP
  x402。

```text
User/Agent ownership and route  -> identity/Arena store
Device/Runtime control state    -> Connector Gateway
Provider invocation/Attempt     -> Hosted Runtime
Task/Game/trade/inventory       -> Arena
Mandate/payment submission      -> Settlement
Payment finality                -> Injective EVM
```

## MVP 验收

- 用户可以从同一入口创建 Hosted Agent 或连接 Local Agent；
- Hosted 创建对用户是一次提交，原 Key 只进入外部 Secret Manager；
- 一名 User 每局只有一个 Game Agent，同一 Agent 可参加后续 Game；
- 入局配置自动快照，活动 Game 不发生 Runtime/config 切换；
- Hosted 在浏览器和用户电脑离线后继续；Local 离线按统一 Finalizer 规则收敛；
- Hosted 用户 Join 时一次确认受限 PaymentMandate，之后 accepted testnet trade
  自动完成，不要求用户逐笔在线确认；
- Hosted、Local 和 rule Agent 接收同版本 AgentTask/Result payload；
- Decide/Negotiate 都使用统一 `action` schema；
- 同局 Runtime 使用同一、经测试校准的行动时间窗；
- dispatch ACK、Result submit 与 Arena apply ACK 可独立恢复；
- 历史 Task、公开协商、默认结果和结算证据可审计；
- 模型 API Key、钱包私钥、本地秘密和 private chain-of-thought 不进入业务日志；
- PaymentMandate 完成前不宣称 Hosted Agent 可在用户离线后自动完成全部支付。
