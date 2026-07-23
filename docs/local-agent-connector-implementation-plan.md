# ADX 本地 Agent Connector Implementation Plan

> 文档状态：执行计划与变更清单
> 最后更新：2026-07-23
> 对应规格：[`local-agent-connector-spec.md`](./local-agent-connector-spec.md)

## 1. 交付策略

优先交付出站 Connector，把“设备如何接入、Runtime 如何发现、平台如何有限控制并观察 session”做成独立控制面；Native A2A Endpoint 作为后续补充入口。

本计划不改写现有 ADX 撮合逻辑。Connector 通过 `binding_id/agent_id/task_id` 与业务面建立引用，但不把 Runtime 状态和交易状态混为一体。

### 1.1 MVP 成功定义

一次可演示的端到端链路：

```text
Login
  → Start local Connector
  → Connector creates pairing and shows user code
  → Logged-in user approves
  → Connector exchanges device code
  → Outbound WSS online
  → Detect Claude/Codex
  → Bind ADX Agent to Runtime
  → Dispatch typed command
  → Receive ack + normalized events
  → Show status in Agent page
```

### 1.2 实施原则

- 协议先行：先冻结身份、状态机、typed command 和事件 envelope，再扩展 driver。
- 安全默认：没有定义的 command 一律拒绝；没有 ownership 的 session 一律不接管。
- 本地凭据本地使用：平台只接收认证状态，不接收原始模型凭据。
- 状态分层：Connector control state 与 ADX business state 分开存储和审计。
- 渐进增强：MVP 内存后端用于演示；生产持久化、设备密钥和高可用另设上线门槛。
- 能力声明：Driver 通过 capabilities 明确可做什么，平台不按 Runtime 名称猜能力。

## 2. 仓库基线与本次文件范围

### 2.1 变更前基线

- `matching/`：ADX Agent 注册、订单、撮合、谈判和 Arena。
- `web/api.py`：FastAPI app factory 和业务 REST API。
- `frontend/src/app/agents/page.tsx`：现有 Agent 列表入口。
- `docs/A2A-X402-链路对接方案与共创协议.md`：A2A/x402 业务链路文档。

这些内容属于业务面。Connector 不能把 `matching.AgentRegistration.status` 直接当作 Device 在线状态，也不能通过改 Agent ELO 或订单状态来表示 Runtime command 成功。

### 2.2 本次 MVP 目标文件

| 文件/目录 | 改动 | 责任 |
|---|---|---|
| `connector_gateway/__init__.py` | 新增 | 导出 Gateway 入口 |
| `connector_gateway/models.py` | 新增 | Pairing、Device、Runtime、Binding、Command、Event 数据模型与枚举 |
| `connector_gateway/service.py` | 新增 | 内存状态、配对、设备认证、inventory、binding、command/event 状态转换 |
| `connector_gateway/api.py` | 新增 | REST + WebSocket `/api/connectors` 路由 |
| `web/api.py` | 修改 | 两个 app factory 仅在显式 `ADX_CONNECTOR_UNSAFE_DEMO=true` 时挂载未认证 Connector demo router；生产默认关闭，启用后按直连 peer address 强制 loopback |
| `tests/test_connector_gateway.py` | 新增 | Gateway 单元/API/WebSocket 契约测试 |
| `tests/connector_go_e2e.py` | 新增 | 构建真实 Go binary，验证 discovery、WSS、binding 与无模型 `runtime.probe` |
| `tests/connector_ui_smoke.py` | 新增 | Playwright 验证入口页、identity 边界、binding 和 session guard |
| `connector/cmd/adx-connector/main.go` | 新增 | `scan`、`doctor`、`pair`、`run`、`version` CLI 与本地 runtime-specific task opt-in |
| `connector/internal/protocol/` | 新增 | v1 envelope、typed command、Runtime Event |
| `connector/internal/enrollment/` | 新增 | HTTPS pairing 创建、轮询交换和 URL 安全校验 |
| `connector/internal/discovery/` | 新增 | Claude Code/Codex 路径、版本和能力探测 |
| `connector/internal/driver/` | 新增 | Claude/Codex 固定 argv task runner |
| `connector/internal/supervisor/` | 新增 | allow-root、owned child、session/task 控制和事件采集 |
| `connector/internal/transport/` | 新增 | WSS、Device header、重连、heartbeat、inventory、command/event |
| `connector/internal/store/`、`redact/` | 新增 | 0600 MVP state、未终态不淘汰的幂等 receipt、event outbox、fail-closed 持久化和本地脱敏 |
| `connector/README.md`、`go.mod`、`go.sum` | 新增 | 构建、使用、安全边界与固定依赖 |
| `frontend/src/lib/connector-api.ts` | 新增 | Connector REST client 和 UI 类型 |
| `frontend/src/components/ConnectorConsole.tsx` | 新增 | pairing、设备、Runtime、Binding、command/event 控制台 |
| `frontend/src/app/agents/page.tsx` | 修改 | 本地 Connector 入口与平台模板 Agent 明确分区 |
| `frontend/src/app/globals.css`、`layout.tsx` | 修改 | Connector 页面所需视觉样式与布局支持 |
| `.env.example` | 修改 | pairing/Web API base，以及默认关闭的 `ADX_CONNECTOR_UNSAFE_DEMO` |
| `frontend/.env.example` | 新增 | `NEXT_PUBLIC_API_URL=http://localhost:8000` |
| `docs/local-agent-connector-spec.md` | 新增 | 产品与技术规格 |
| `docs/local-agent-connector-implementation-plan.md` | 新增 | 本实施计划 |

本次没有数据库 migration 时，Gateway 状态就是**进程内演示状态**。不能把它描述为生产持久化、可靠队列或不可篡改审计。

配置契约：

- `ADX_PUBLIC_APP_URL`：Gateway 生成 pairing `verification_uri` 的公开 Web 根地址，当前目标为 `${ADX_PUBLIC_APP_URL}/agents#connect`。
- `NEXT_PUBLIC_API_URL`：浏览器访问 FastAPI Connector API 的 base URL。
- `ADX_CONNECTOR_UNSAFE_DEMO`：只在 loopback/单用户开发环境显式开启未认证、内存态 Connector Router；共享或远程部署必须保持 false，直到真实登录与对象授权接入。
- 二者在生产必须使用 HTTPS；不要把 device token、模型凭据或钱包凭据写进这些公开配置。

## 3. 协议冻结项

在扩展 Runtime driver 前先冻结以下契约：

### 3.1 REST/WSS API

- `POST /api/connectors/pairings`
- `POST /api/connectors/pairings/{user_code}/approve`
- `POST /api/connectors/pairings/exchange`
- `GET /api/connectors/devices`
- `GET /api/connectors/devices/{device_id}`
- `POST /api/connectors/devices/{device_id}/revoke`
- `POST /api/connectors/devices/{device_id}/bindings`
- `GET /api/connectors/bindings`
- `POST /api/connectors/bindings/{binding_id}/commands`
- `GET /api/connectors/bindings/{binding_id}/commands`
- `GET /api/connectors/bindings/{binding_id}/events`
- `GET /api/connectors/audit`
- `WS /api/connectors/ws?device_id=...`，并使用 `Authorization: Device <token>` header

### 3.2 Typed command allowlist

- `runtime.probe`
- `session.start`
- `task.dispatch`
- `task.cancel`
- `session.stop`
- `session.resume`

任何通用 `shell.exec`、任意 executable、任意 argv、外部 PID attach/kill 都不进入 v1。

| Action | v1 payload |
|---|---|
| `runtime.probe` | `{}` |
| `session.start` | `{working_directory, initial_prompt?, environment_refs?}` |
| `task.dispatch` | `{session_id, prompt, request_id?}`；省略时 Gateway 生成 |
| `task.cancel` | `{session_id, request_id}` |
| `session.stop` | `{session_id, reason?}` |
| `session.resume` | `{session_id}`；仅使用 Connector 从该 owned session 的 Runtime 输出中捕获的 provider token，云端不能提交或覆盖 token |

Connector command ack 只能使用 Gateway 接受的 `accepted|running|succeeded|failed|rejected`；完成态是 `succeeded`，不是内部临时命名 `completed`。

参数解析边界：

- `environment_refs` 只能是本地环境变量的**名称引用**，不是 `NAME=value`，也不能在 command 中携带值。Production strict Gateway 必须拒绝不在平台配置 allowlist 中的名称；当前 Gateway 仅做字符串、数量和长度约束，所以服务端 allowlist 落地前该字段不能对生产开放。Connector 始终再用 `--allow-env NAME` 做最终强制校验。
- Connector 只从自身本地进程环境读取获准变量的值，并只传给 owned child；值不写入 command ack、Runtime Event、Gateway 或普通日志。未通过 `--allow-env`、名称非法或本地不存在均拒绝 session；未配置 `--allow-env` 时，没有 secret-bearing variable 可被引用。
- `working_directory` 必须由云端显式提供为非空字符串；Gateway 与 Connector 都拒绝缺失、空串或纯空白值，不自动回退到任一 allow-root。Connector 解析绝对路径与 symlink 后，必须确认其位于一个 `--allow-root` 内；未显式配置 `--allow-root` 时只允许 Connector 启动时的当前工作目录。
- `task.dispatch.request_id` 可选；缺失时 Gateway 在校验/入队前生成，再把生成后的值纳入幂等 fingerprint 与下发 payload。
- `conversation_id` 与 `resume_token` 是显式禁止字段；Gateway 不接受，Connector 也做 defense-in-depth 拒绝。Resume token 只取 Connector-owned child 首次报告的 `session_id/thread_id`。
- 每个既有 session command 都要匹配 session registry 固化的 `binding_id/agent_id/runtime_id/binding_epoch`；Gateway 也先拒绝与 binding 当前 `last_session_id` 不一致的请求。

### 3.3 WSS 消息类型

Connector → Gateway：

- `hello`
- `heartbeat`
- `inventory.snapshot`
- `command.ack`
- `runtime.event`

Gateway → Connector：

- `welcome`
- `command`
- `ack`
- `event.ack`
- `error`

当前处于 v1 wire 兼容窗口。Gateway 为兼容早期客户端而接受缺少部分 envelope metadata 的消息；这不是新客户端可依赖的长期契约：

| Outer field | 当前 Gateway 入站 | 当前 Go Connector 发出 | Production strict |
|---|---|---|---|
| `type` | 必填 | 必填 | 必填 |
| `payload` | 可省略，按 `{}` 处理 | 必填 | 必填 |
| `protocol_version` | 可选；提供时必须为 `1.0` | 必填、固定 `1.0` | 必填 |
| `device_id` | 可选；提供时必须匹配 Device credential | 必填 | 必填 |
| `message_id` | 可选；缺失时 Gateway 不返回 message ack | 必填 | 必填 |
| `sent_at` | 可选 | 必填 | 必填 |
| `sequence` | 可选 | Runtime Event 必填；其他消息为 0 时可省略 | Runtime Event 和 command 必填；其他消息按类型定义 |

`hello.payload.protocol_version` 当前也必须等于 `1.0`。Production strict 需要 Gateway 强制 envelope 字段并持久化 sequence 状态。Command envelope 的 `payload` 内另含 `command_id/idempotency_key/binding_id/binding_epoch/expires_at` 等 command 字段。

Runtime Event 分两层：

- Connector 发送：`event_id/binding_id/session_id/task_id/event_type/sequence/occurred_at/level?/data`。
- Gateway 保存/返回时补充：新的服务端 `event_id`、原始 `source_event_id`、`device_id`、`received_at`；event ack 另返回 `through_sequence`。

Go 内部 RuntimeEvent 可以保留 driver-friendly 命名，但 transport 必须映射为上述 wire schema。当前 `runtime.message` 只是对 Runtime JSON line 做本地 secret redaction 后的 passthrough，并不是 ADX 已完成语义归一化的业务事件；跨 Runtime 的高级 event taxonomy 属于后续。

### 3.4 幂等和租约

- `pairing exchange` 一次性；创建阶段的 owner 只是 hint，Device 所有权在登录用户批准时绑定。
- `command_id + idempotency_key` 去重。
- 同一 binding 更换 Runtime 后递增 `binding_epoch`。
- Device online 由 heartbeat lease 决定。
- 当前 Gateway 对未 ack 的 delivered command 在 socket 重连时重新排队，并依赖 Connector 幂等；生产版需用 durable command outbox 实现跨 Gateway 重启的 at-least-once delivery。
- Runtime Event 以 Device-scoped `event_id` 或 `sequence` 任一重复即去重；`sequence` 同时推进累计 ACK watermark。
- Connector 当前通过 staged event 原子化地保留 sequence 与待落盘事件，崩溃恢复时幂等补入 outbox；有序重放使用最多 64 个已发送未 ACK 的 in-flight window 和 8-event burst，本地 backlog 可更大，期间仍处理 command/ACK，并拒绝本地 outbox sequence gap。
- `pair`/`run` 对同一 state 使用 OS singleton lock（Windows `LockFileEx`、Unix `flock/fcntl`）；Gateway 的 `4409` replacement 会让旧 Connector 退出而不是重连抢占。
- Gateway 以 connection generation + per-device sender lock 串行化 handover、revoke、ACK 与 command frame；发送期间发生连接切换时不把只到达旧 socket 的 command 永久提交为 delivered。所有 inbound 状态变更也在状态锁内复核 generation，replacement 后的旧 socket 不能再提交 ACK/event/inventory。
- 启动恢复把未终态 `accepted` receipt 固化为 `failed / connector_restarted` 并重放；所有未终态 receipt 都保留，终态只压缩为最新 512 条，生产 retention/TTL 必须覆盖最大重投窗口。`task.cancel` 自身到达终态，原 task 的取消结果另行收敛。
- receipt lookup/claim/final save 或 event/outbox 持久化失败会锁存 `persistence-degraded`，best-effort 返回错误后停止接收新 command、退出并 shutdown owned task。
- Windows owned task 使用 `CREATE_SUSPENDED`、`KILL_ON_JOB_CLOSE` Job Object 和 fail-closed resume，可清理整个 Job tree。Linux 使用 process group + `Pdeathsig`：显式 cancel 可清理 group，Connector crash 可清理直接 child，但尚不能保证清理 Runtime 自行派生的全部 descendant；其他 Unix 的 crash containment 也仍是生产 gate。
- Gateway 比较 `hello.started_at`；同一 Device 的 Connector 实例变化时，清空进程内 session/task projection 并把受影响 binding 标为 degraded，避免 UI 在 Connector 重启后继续显示不存在的 running session。

## 4. 分阶段实施

当前代码快照（最终测试结果应以合并说明为准）：

| Phase | 当前状态 |
|---|---|
| Phase 0 | 规格与边界文档已新增 |
| Phase 1 | app-scoped in-memory Gateway、REST/WSS 和专项测试已实现；未认证 Router 默认关闭，仅 unsafe loopback demo 显式开启；真实 auth/tenant 与持久化未实现 |
| Phase 2 | Go CLI、pairing、discovery、transport、supervisor、outbox 和测试已实现；session ownership 当前为进程内，重启不恢复 session |
| Phase 3 | Claude/Codex 固定 argv owned-child runner 已实现但默认不装载；Codex 需本机 opt-in、Claude 需 unsafe 开发 opt-in；Codex app-server、认证 probe 与完整原生 approval 仍待实现 |
| Phase 4 | Connector API client、控制台、现有 ADX Agent 选择和 Agent 页面分区已实现；Connector-only demo identity 仍需替换为真实业务 Agent 注册，生产隐私开关与真实登录待实现 |
| Phase 5 | 未实现 |

### Phase 0：规格、威胁模型与契约

目标：所有团队对“能控制什么、不能控制什么”达成一致。

交付：

- 完成本规格和 implementation plan。
- 固化 control plane / business plane 边界。
- 固化 Pairing、Device、Runtime、Binding、Session、Command、Event 模型。
- 固化状态机、typed command allowlist 和错误码。
- 明确 Claude 认证合规门槛与钱包密钥隔离。

退出条件：

- 前后端、本地 Connector 和 ADX 业务服务使用同一命名。
- API 中不存在 shell 或任意进程控制能力。
- 产品文案不再声称“接管已经打开的 Agent 窗口”。

### Phase 1：Connector Gateway MVP

目标：完成平台侧配对、设备、Binding、命令、事件和 WSS 闭环。

#### 4.1 数据模型

在 `connector_gateway/models.py`：

- 定义枚举和模型校验。
- 对命令 payload 按 type 做 schema 校验。
- 为 `environment_refs` 增加服务端变量名称 allowlist，默认关闭该能力；当前仅有长度/数量校验，不能作为生产安全边界。
- 设置文本、数组和事件 payload 大小上限。
- 将 secret/token 字段与响应 DTO 分开，避免序列化泄露。

#### 4.2 领域服务

在 `connector_gateway/service.py`：

- 创建和过期 pairing；创建者传入的 owner 只保存为 hint，不授予所有权。
- 批准 pairing 与一次性交换；批准时从当前登录用户/tenant 绑定 Device owner。
- 生成高熵 device token，服务端只存摘要。
- 认证 Device，记录 connect/disconnect/heartbeat。
- 更新 inventory，并保持稳定的 runtime identity。
- 创建 Binding 并验证 ADX Agent/Device/Runtime 所属关系。
- 创建 typed command、执行状态转换和幂等去重。
- 写入按 v1 wire 映射的 Runtime Event，按 binding 查询；当前 `runtime.message` 仍是脱敏 passthrough，高级语义规范化后续实现。
- 撤销/过期资源时做一致性清理；Device revoke 使 token 失效、关闭活跃 socket、停用相关 binding 并递增 binding epoch。

MVP 使用内存 store 时，service API 设计为可替换 repository，避免业务路由直接操作全局 dict。

#### 4.3 API 与 WebSocket

在 `connector_gateway/api.py`：

- 提供 `/api/connectors` router。
- 生产身份从登录态依赖注入；当前 `demo-user` 与未做对象级授权的 list/get/binding/audit 只能用于本地演示。未接认证层前，Router 默认不得挂载。
- REST 错误返回稳定 code，而非只返回自由文本。
- WebSocket 在 accept 前/后按框架能力做设备认证。
- `hello` 完成协议版本和 replay position 协商。
- 并行处理 heartbeat、inventory、ack/event 与 pending command 下发。
- 断开时更新 connection lease，不删除 Device。

在 `web/api.py`：

- `create_app()` 与 `create_app_with_db()` 共用同一 mount helper，但默认都不挂载；只有 `ADX_CONNECTOR_UNSAFE_DEMO=true` 才为 loopback/单用户开发显式挂载。
- Connector router 使用自己的 service，不把 matching registry 当 store。
- health 可增加 Connector 子状态，但不把 Device 离线视为整个 ADX 服务故障。

退出条件：

- API 测试覆盖 pairing 正常/过期/重复交换。
- WSS 可完成 hello、inventory、command、ack、event 闭环。
- 跨 owner 访问、非法 command 和 stale epoch 被拒绝。

### Phase 2：Go 本地 Connector MVP

目标：一个可运行的本地 CLI 能安全配对、发现 Runtime、建立 WSS 并执行有限命令。

#### 4.4 CLI

`connector/` 当前提供：

- `adx-connector pair`：Connector 主动 `POST /pairings`，把公开 `user_code` 和 verification URL 展示给用户，把私有 `device_code` 只保留在本地内存，然后轮询 `/pairings/exchange`；用户不向 CLI 输入 code。
- `adx-connector scan`：只读扫描并输出规范化 inventory；默认只发布 `runtime.probe` 与 `unverified_local_auth`。
- `adx-connector doctor`：检查本地凭据文件、Runtime 是否可发现及 `--version` probe；当前不检查 Gateway 网络、token 有效性或 WSS handshake。
- `adx-connector run`：启动前台 daemon/WSS loop；默认 detection-only。`--enable-codex-tasks` 在本机装载 Codex fixed driver，`--unsafe-enable-claude-tasks` 仅为认证来源未验证的隔离开发测试。
- 后续 `adx-connector logout`：撤销设备并删除本机凭据；当前 MVP 由 Web/API revoke，CLI 尚无该子命令。

MVP 可先以前台进程运行；系统 service、托盘 UI 和自动更新属于后续。

#### 4.5 本地存储

- 设备凭据优先存 OS credential store；若 MVP 使用文件，必须为当前用户独占并在文档中标明限制。
- outbox 保存未确认 event。
- inbox/command journal 保存已接受的 `command_id` 与结果摘要。
- 生产补充 session ownership registry，保存 Connector 创建的 PID、启动时间、driver、供应商 session id 和随机 ownership nonce。当前 MVP session registry 在内存中，Connector 重启后不 attach 或恢复任何已有进程。
- 不保存模型 API key、Claude OAuth token 或钱包私钥到普通 JSON state。

#### 4.6 Discovery

- 使用 `exec.LookPath` 与有限的 known locations。
- 每个 probe 使用 context timeout。
- 调用只读的 `--version`/health 命令。
- 输出 runtime kind/version/path fingerprint/capabilities/probe error。
- 默认 capabilities 只有 `runtime.probe`；本机 runtime-specific flag 才添加 session/task capability，Gateway 和 Connector driver registry 双层执行。
- inventory 未变化时不上报完整快照。

#### 4.7 Transport

- 单 Device 单 WSS，支持多 binding/session。
- 指数退避加 jitter，避免重连风暴。
- ping/heartbeat 与 application lease 分开。
- 严格 envelope decode、消息大小限制和 protocol version 检查。
- command 先持久化和校验，再 ack accepted。
- event 先写 outbox，收到 ack 后回收。

#### 4.8 Supervisor

- 只启动固定 driver 映射的 executable。
- cloud payload 不得提供任意 argv。
- `working_directory` canonicalize 后必须位于本地 `--allow-root`；默认 allow-root 是 Connector 启动 cwd。
- `environment_refs` 只接收变量名，并与 `--allow-env` 交集校验；值只从 Connector 本地环境读取且不上传。
- session ownership 必须可验证。
- cancel/stop 先走官方 driver，再对 owned child 做受控终止。
- Connector 重启后无法验证 ownership 的进程只标记 orphaned，不 attach。

退出条件：

- 本机无需开放入站端口即可完成配对与在线。
- `scan` 可检测 Claude/Codex。
- 重复 command 不重复启动 child。
- 手工启动的 Claude/Codex 进程不会被纳入 session 列表或被停止。
- WSS 断线重连后 event 可补发。

### Phase 3：Runtime Drivers

目标：从“发现 Runtime”进阶到“安全控制 Connector 所属 session”。

#### 4.9 Codex driver（生产目标：app-server）

当前 MVP 默认不装载 task driver；用户在本机显式 `--enable-codex-tasks` 后，才使用固定 `codex exec --json [resume <connector-captured-token>] -`，建立“一次 task、一个 owned child”的 runner。云端不能追加 executable、argv 或 resume token。该 runner **没有平台 approval request/response 闭环**：只继承 Codex 本地配置和原生权限行为，Connector 不代表用户批准高风险工具动作。

Discovery 默认只发布 `runtime.probe`，本机 flag 打开后才发布 task capability；Gateway 对 capability 强制校验，Connector 端没有装载 driver 时再次拒绝。该本机 opt-in 仍不是版本/permission 安全认证，生产必须保持 fixed argv task 关闭，直到契约测试驱动 capability probe。

生产完整 driver 再升级为 `codex app-server`：

1. Connector 启动 app-server 子进程并持有 stdio。
2. 完成 `initialize` / `initialized`。
3. `session.start` → `thread/start`。
4. `task.dispatch` → `turn/start`。
5. 规范化 `thread/*`、`turn/*`、`item/*` 事件。
6. `task.cancel` → `turn/interrupt`。
7. `session.resume` → `thread/resume`。
8. 收到 approval request 时暂停 turn，把请求交给本地用户或经授权的平台 policy，得到明确结果后再继续；任何高风险动作都不得自动放行。

需要对 Codex 版本进行兼容矩阵测试；未知版本默认降级为 detection-only。

#### 4.10 Claude driver（受认证门槛约束）

当前 MVP 的固定命令映射为：

```text
claude --print --output-format stream-json --verbose [--resume <connector-captured-token>]
```

Claude 默认 detection-only。只有本机显式 `--unsafe-enable-claude-tasks` 后，Prompt 才通过 stdin 进入 Connector-owned child；结构化 stdout/stderr 经脱敏后上报。当前同样**没有平台 approval 闭环**，且无法探测本地 Claude CLI 到底使用 API key、Bedrock/Vertex 还是消费订阅登录，因此这个 flag 只能用于隔离开发账号，不是生产开关。

后续可评估受支持 CLI/SDK 的持续 `--input-format stream-json`、官方 session resume 和 permission/tool control，但必须先完成版本契约与审批测试。

限制：

- Connector 不读取、上传或重放 Claude subscription OAuth token，但 child 会继承本地基础认证环境；这不能证明订阅身份未被 Runtime 使用。
- 生产仅允许完成 auth probe/gate 的 API key、Bedrock/Vertex 或其他明确受支持认证；当前只上报 `unverified_local_auth`。
- 未完成供应商/法务评审和技术认证探测前，生产 Claude task 恒为关闭。
- 若当前版本无法安全取消单个 turn，则 capability 明确降级，不伪装支持。
- fixed argv MVP 不得把“Runtime 自己可能弹出/拒绝审批”描述成平台已可观察、暂停或批准；完整平台审批等 app-server/官方 SDK driver 实现。

退出条件：

- driver capabilities 与真实版本行为一致。
- 结构化事件能映射到统一 event schema。
- cancel/stop/resume 的失败语义可观察。
- 认证凭据不进入 Gateway 或普通日志。

### Phase 4：前端接入与可观察性

目标：用户能在 Agent 页面完成连接，并理解 Agent 身份、Device 和 Runtime 的区别。

修改 `frontend/src/app/agents/page.tsx`：

- 增加“连接本地 Agent”和“创建平台 Agent”两个清晰入口。
- 生产 pairing UI 接收并确认 Connector 显示的公开 `user_code`，同时展示有效期、安装/启动命令和审批动作；不要求把 code 输入 CLI。
- 当前“Web 生成 demo pairing”会让 pairing 响应进入浏览器，只允许本地开发；生产 feature flag 必须关闭，私有 `device_code` 只能存在于发起 pairing 的 Connector。
- 设备卡展示在线状态、Connector 版本、最近心跳和撤销入口。
- Runtime 卡展示 kind/version/兼容状态/capabilities/auth status。
- Binding 流程让用户选择 ADX Agent + Device + Runtime。
- 当现有 Agent 数据源为空时，当前 UI 只允许显式标注的 `Connector-only identity (MVP)`；它是控制面占位引用，不会创建可撮合/交易的业务 Agent。生产必须接入真实 Agent 注册或强制选择现有 Agent。
- 当前事件面板展示 command 状态和已上传的脱敏 Runtime Event `data`；这不是 metadata-only。生产 UI 必须展示真实采集级别，并与 Connector 本地采集开关一致。
- 对 offline、auth required、incompatible、stale binding 提供明确修复建议。

前端不得：

- 显示或收集模型 API key、设备 token、OAuth token、钱包密钥。
- 把 Device online 显示为“Agent 已成交”。
- 宣称可以观察或控制用户手工打开的终端。
- 通过隐藏 UI 字段暗示后端没有采集；UI 隐藏不改变 Connector 上传和 Gateway retention。

退出条件：

- 首次 pairing 与回访重连路径可完成。
- UI 状态与 Gateway 状态机一致。
- 用户可以撤销 Device 和禁用 Binding。
- 空态、错误态和过期态均有可执行下一步。
- 生产前完成本地 `metadata_only/redacted_content/full_content` 采集开关、默认 metadata-only、服务端 retention/删除和用户可见说明。

### Phase 5：生产化

以下项目不应被 MVP 演示完成度掩盖：

#### 4.11 持久化与队列

- Postgres：Device、Runtime inventory、Binding、Command、Event metadata、Audit。
- Redis 或 durable broker：在线 connection registry、command delivery、lease。
- Object storage：大 artifact 与长日志。
- 唯一约束实现 command/event 幂等。
- 审计 append-only，并配置 retention。

#### 4.12 认证与授权

- 接入真实用户/tenant auth，移除 demo owner。
- 设备公私钥、短期 access token、refresh/rotation/revoke。
- WSS 只从 `Authorization: Device <token>` header 读取凭据，拒绝 query token。
- RBAC/ABAC：谁能绑定、启动、取消、查看内容和审批。
- 管理动作二次确认与风险分级。

#### 4.13 运维

- Gateway 水平扩展和 connection ownership。
- drain/redeploy 时连接迁移或平滑重连。
- SLO、告警和容量压测。
- Connector signed release、SBOM、校验和、回滚渠道。
- 跨平台安装器与 updater。

生产退出条件：

- Gateway 重启不丢 command/event。
- Device revoke 在目标时限内全局生效。
- 完成安全评审、渗透测试、隐私和供应商条款评审。
- 数据保留/删除、事件脱敏和租户隔离通过验证。

## 5. 测试矩阵

### 5.1 Gateway 单元测试

| 区域 | 用例 |
|---|---|
| Pairing | 正常、过期、未批准交换、重复交换、错误 code |
| Device auth | 正确 token、错误 token、撤销 token、token 不在响应/日志泄露 |
| Inventory | 正常更新、未知 kind、重复快照、超大 payload |
| Binding | 正常创建、跨 owner、Runtime 不存在、epoch 更新 |
| Command | allowlist、非法 payload、幂等、过期、stale epoch、非法状态转换 |
| Event | 正常写入、去重、乱序、跨 binding、大小限制、敏感字段过滤 |
| Lease | heartbeat、过期 offline、重连恢复 |

### 5.2 API/WebSocket 集成测试

- REST pairing → approve → exchange 全链路。
- 使用交换所得 token 建立 WSS。
- hello 协议版本协商。
- inventory 上报后 REST 可见。
- REST 创建 command 后 WSS 收到。
- Connector ack/event 后 REST 查询状态正确。
- 两个 Device 并发，命令不会串线。
- WSS 断开和重连，未确认 command 重发但不重复执行。
- malformed JSON、未知 message type、超大 frame 被关闭并记录结构化错误。

### 5.3 Go Connector 单元测试

- PATH/known locations discovery。
- probe timeout、非零退出、异常版本字符串。
- protocol encode/decode 与未知字段策略。
- command allowlist/payload validation。
- `session.start/task.dispatch/task.cancel/session.stop/session.resume` 的 binding、agent、runtime、epoch 归属负向测试。
- 云端 `conversation_id/resume_token` 注入、无本地捕获 token 的 resume、首次捕获 token 不可被覆盖。
- idempotency journal。
- 未终态 receipt 不淘汰、终态容量压缩，以及 lookup/claim/final-save 故障触发全局 persistence fail-closed。
- Runtime Event outbox、`sequence/through_sequence` 与重放。
- event/outbox stage/append/clear/ack 故障触发 fail-closed，不静默丢审计。
- supervisor ownership。
- Windows 与 Unix 的进程终止语义。
- credential/state file permission。

### 5.4 Driver 契约测试

使用 fake runtime process，避免依赖真实账号：

- init/start/dispatch/event/complete。
- cancel/stop/resume。
- 当前 fixed argv：验证不会构造/自动批准高风险 approval；无法保证本地审批的版本降级 detection-only。
- 未来 app-server/SDK：approval request、暂停、明确批准/拒绝和恢复。
- stdout partial line、invalid JSON、进程 crash。
- backpressure 和大输出截断。
- Runtime 版本不兼容时 capability 降级。

真实 Runtime smoke test 仅在明确配置凭据的隔离环境运行，不进入普通 CI。

### 5.5 端到端与混沌测试

- 首次用户 5 分钟内完成安装、配对和测试 task。
- Connector 运行中断网 30 秒再恢复。
- Gateway 在 task running 时重启。
- 同一 command 重放 3 次仍只执行一次。
- Device revoke 后活跃 WSS 被 `4403` 关闭，离线 Connector 用旧 token 重连同样收到 `4403` 并停止，相关 binding 停用且旧 epoch command 被拒绝。
- replacement 后旧 connection generation 的 ACK/event/inventory 均不能提交状态。
- Runtime 子进程 crash 后 UI 显示明确错误。
- 手工打开一个同类 Runtime，Connector 不 attach/stop。
- 向 command payload 注入 shell、路径穿越、环境变量和超长文本，全部被拒绝。

### 5.6 业务边界回归

- Connector Device offline 不删除 Agent、订单或 Arena 历史。
- Runtime task complete 不自动标记付款完成。
- ADX deal cancel 会产生业务动作和 typed cancel，但即使本地 cancel 失败，业务状态仍保留清晰的补偿路径。
- 钱包私钥不存在于 Connector models、API schema、event fixture 和日志。

## 6. 验证命令

根据当前仓库环境执行；命令缺失时必须如实记录，不能把静态检查写成已通过编译。

```powershell
# Python Gateway
python -m pytest tests/test_connector_gateway.py -q

# 全量 Python 回归
python -m pytest -q

# FastAPI 导入/路由 smoke
python -c "from web.api import create_app; app=create_app(); print([r.path for r in app.routes if '/api/connectors' in r.path])"

# Go Connector（需要 Go toolchain）
Set-Location connector
go test ./...
go vet ./...

# Frontend
Set-Location frontend
npm run lint
npm run build
```

### 6.1 当前验证快照

2026-07-23 本次实现已在 Go 1.26.5、Python 和本机 Chromium 环境完成：

- `go test -count=1 ./...`：通过，包括 OS singleton、4409 replacement/4403 revoke 终止、session ownership、未终态 receipt 保留、receipt/outbox persistence fail-closed、staged event、64-event 有界重放、Windows Job Object 与 task cancel 收敛。
- `go vet ./...`：通过。
- `go build ./...`、`go mod verify`：通过；Linux/amd64 `CGO_ENABLED=0` 交叉构建通过。
- `adx-connector scan` / `doctor`：实际发现 Claude Code 2.1.170 与 Codex 0.124.0；默认 inventory 仅发布 `runtime.probe` 与 `unverified_local_auth`。
- `python -m pytest -q`：9 个 Gateway/API/WSS 测试通过，包含默认关闭 unsafe Router、远程 peer 即使伪造 `X-Forwarded-For` 仍被 HTTP/WSS 拒绝、session ownership/resume token、revoked token 4403、multi socket revoke、single sender、connection handover/inbound generation、关闭码语义、迟到 ACK 和 Connector restart 状态投影。
- `python -m mypy connector_gateway --ignore-missing-imports`：通过。
- `tests/connector_go_e2e.py`：真实 Go binary → WSS online → 自动发现至少一个可用的 Claude Code 或 Codex Runtime → create binding → `runtime.probe` → command `succeeded`，通过；不触发模型。
- `tests/connector_ui_smoke.py`：fake WSS Connector + Chromium 中设备/Runtime 可见、选择现有 ADX Agent、binding 创建和 workspace 启动 guard 通过。
- 全量 `next build` 的编译阶段通过，Connector 相关 TypeScript 无新增错误。

验证边界：

- 未运行 `go test -race`，因为当前环境没有 C compiler/CGO；不能据此声称已完成 race detector 验证。
- 未执行会产生模型费用的真实 Claude/Codex task，只验证了 discovery、控制协议、fake/managed process 和单元测试路径。
- 真实供应商登录、权限审批、长任务中断和付费请求仍需在隔离测试账号上做 smoke test。
- 全量 `next build` 仍被本次 Connector 之外的既有错误 `frontend/src/components/BattleCard.tsx:77`（`Battle.quantity` 类型）阻断；不能把专项 TypeScript 通过描述为全量前端 build 通过。

在其他工作机若没有 Go toolchain，交付说明应明确：

- Go 源码和测试已写入；
- 未在该环境完成 `go test/go vet`；
- 需要在有 Go 的 CI 或开发机补充验证；
- 不得用 Python 测试通过替代 Go 编译结论。

## 7. 风险登记

| 风险 | 影响 | 缓解 | 上线门槛 |
|---|---|---|---|
| Claude 订阅 OAuth 不允许第三方路由 | 账号/产品合规风险 | Claude task 默认关闭；unsafe 开发 flag 明示认证未验证；生产需 auth probe + 受支持认证 + 供应商批准 gate | 法务与供应商书面确认 |
| Runtime CLI 协议随版本变化 | Driver 失效或误操作 | 版本矩阵、capability probe、unknown version detection-only | 每个支持版本有契约测试 |
| fixed argv 无平台 approval 闭环 | 高风险工具动作无法安全暂停/审批 | 默认 detection-only；本机 runtime-specific opt-in 只供可信开发；生产关闭 task feature flag；后续 app-server/SDK approval | 审批契约与负向测试通过 |
| Device header token 泄露 | Device 冒用 | 已禁止 URL token，使用 `Authorization: Device`、服务端摘要、日志脱敏与 revoke；后续增加轮换/短期票据 | 安全评审通过 |
| 内存 Gateway 重启丢状态 | 演示中断、无法审计 | 明确 MVP 限制；生产接 Postgres/queue | 持久化与恢复测试 |
| 未认证 Connector API 暴露 | 远程驱动本地 Agent、跨用户泄露 | 两个 app factory 默认不挂载；`ADX_CONNECTOR_UNSAFE_DEMO` 只限 loopback；生产接真实 login/tenant/object auth | 认证与授权集成测试 |
| 本地 receipt/outbox 写失败 | 重复副作用或审计空洞 | 锁存 persistence-degraded、拒绝新 command、退出并 shutdown owned task；保留诊断 | 磁盘/权限故障注入测试 |
| “平台控制本机”引发信任问题 | 用户不安装 | 清晰权限页、typed actions、本地 permission 边界、不安全版本 detection-only、可撤销 | UX 与安全说明评审 |
| 误接管手工 Runtime | 数据丢失/信任破坏 | ownership nonce/registry；无 attach | E2E 负向测试通过 |
| 猜测 session ID 跨 binding 控制 | 越权操作其他 Agent/Runtime | Connector 对 binding、agent、runtime、epoch 四元组逐项匹配；Gateway 先校验 binding 的当前 session | 全 lifecycle command 负向测试通过 |
| 云端伪造 provider resume token | 接管非 Connector-owned 会话 | command schema 禁止 `conversation_id/resume_token`；只接受 owned child 首次捕获的 token | Gateway/Connector 双层拒绝测试通过 |
| Linux/macOS 崩溃后残留 Runtime descendant | 本地孤儿工具进程、控制边界失真 | Windows 已用 Job Object；Linux/macOS 生产版使用 cgroup/systemd scope、supervisor shim 或等价 process-tree containment，完成前只承诺直接 child/显式 group cancel | 支持平台逐一通过 crash containment E2E |
| 事件携带源码/秘密 | 隐私和商业风险 | 当前 local redaction；生产增加采集开关、默认 metadata-only、retention/删除和字段/大小限制 | 隐私评审和泄密测试 |
| Runtime 输出伪造交付 | 交易欺诈 | 业务证据与 Runtime telemetry 分离 | 业务状态机回归 |
| 跨租户 Device/Binding 访问 | 严重数据泄露 | tenant-scope auth、对象级授权、审计 | 安全集成测试 |
| Connector 自动更新供应链 | 本地执行风险 | 签名、SBOM、checksum、分阶段 rollout | updater 安全方案完成 |

## 8. 上线与回滚

### 8.1 Feature flags

当前代码已实现的安全 gate：

- 服务端 `ADX_CONNECTOR_UNSAFE_DEMO`，默认 false；只控制未认证本地 demo Router。
- 本地 `--enable-codex-tasks` / `ADX_CONNECTOR_ENABLE_CODEX_TASKS`，默认 false。
- 本地 `--unsafe-enable-claude-tasks` / `ADX_CONNECTOR_UNSAFE_ENABLE_CLAUDE_TASKS`，默认 false，明确不具备认证合规保证。

以下是接入真实认证、持久化和发布系统后的平台侧目标 flags，当前尚未实现：

- `connector_pairing_enabled`
- `connector_wss_enabled`
- `connector_commands_enabled`
- `connector_codex_driver_enabled`
- `connector_claude_driver_enabled`
- `connector_content_observability_enabled`

先对内部 tenant 开启 pairing/inventory，再逐步开启 binding 和低风险控制 command。平台 flag 与本机 flag 必须同时允许才可执行；未完成本地 permission 契约验证前，生产 fixed argv task 保持关闭。

### 8.2 Rollout

1. 内部开发设备：pairing、inventory、doctor。
2. 测试 tenant：Codex detection 和 fake driver。
3. 完成本地 metadata-only 与 permission 契约后：受控用户的 Codex owned session。
4. 完成可验证 auth probe、API key 合规说明和供应商 gate 后：Claude API-key driver。
5. 持久化/设备密钥/审计上线后：扩大生产流量。

### 8.3 回滚

平台侧：

- 关闭 `connector_commands_enabled`，保留只读 heartbeat/inventory。
- 停止创建新 pairing/binding，不删除既有 ADX Agent 或业务历史。
- Gateway 版本回滚时继续接受前一协议小版本或返回明确 upgrade required。

本地侧：

- Connector 停止接收新 command。
- 尝试正常停止其 owned session；不触碰外部进程。
- 回滚到已签名的前一版本。
- 保留最小诊断和 outbox，待用户确认后清理。

业务侧：

- Connector 回滚不得回滚已成交/支付业务记录。
- running task 进入 `execution_unknown/degraded`，由业务补偿流程决定重试、改派或人工处理。
- 重试必须使用新的 command attempt 与相同业务 idempotency key，防止重复交付/付款。

## 9. Native A2A Endpoint 后续方案

Native A2A 适合已经运行在公网或可被平台代理访问、并原生暴露 A2A Agent Card/Endpoint 的 Agent。它是第二种接入 transport，不是本地 Connector 的“简化模式”。

### 9.1 可复用部分

- ADX Agent identity 与 ownership。
- Binding 概念，但 target 从 `device_id/runtime_id` 变为 `agent_endpoint_id`。
- task/correlation/idempotency/audit 模型。
- 规范化 capability 与 event projection。
- 与 matching、negotiation、payment 的业务边界。

### 9.2 不复用部分

- Device pairing 与本地 device credential。
- Runtime executable discovery。
- 本地 process supervisor/session ownership。
- Connector heartbeat lease 和本地 outbox。

### 9.3 后续模块建议

- `a2a_ingress/agent_cards.py`：获取、校验和缓存 Agent Card。
- `a2a_ingress/endpoints.py`：endpoint 注册、ownership challenge、health。
- `a2a_ingress/client.py`：A2A task/message transport。
- `a2a_ingress/security.py`：endpoint auth、SSRF 防护、allowlist、证书和签名。
- `a2a_ingress/events.py`：映射为 ADX 统一 execution projection。

### 9.4 Native A2A 风险

- SSRF 与恶意 Agent Card。
- 公网 endpoint 身份和 ownership 证明。
- 远端实现的协议版本/语义不一致。
- 远端 Agent 的审计粒度低于本地 Connector。
- task 已接受但远端状态不可见。

因此 Native A2A 可以在 Connector MVP 后并行设计，但不能为了赶进度共用 Device token 或假装具备本地 session 级控制。

## 10. 合并前核对清单

- [x] 重新扫描实际新增文件，更新本文“目标文件”的状态。
- [x] 文档中的 endpoint、枚举和字段名与代码一致。
- [x] `create_app()` 与 `create_app_with_db()` 均通过同一显式 unsafe demo gate；默认不挂载未认证 Router，启用后按直连 peer address 强制 loopback。
- [x] 测试结果按 Python、Go、Frontend 分开报告。
- [x] 未运行的编译/测试明确标为未验证。
- [x] README/前端文案不声称接管已打开终端。
- [x] 不存在可被协议接受的 `shell.exec` 或任意 argv 控制。
- [x] 不存在模型凭据或钱包密钥字段。
- [x] Claude task 默认关闭，unsafe 开发 flag 不被描述为已验证 subscription OAuth 合规。
- [x] 内存状态不被描述为生产持久化或不可篡改审计。
- [x] Connector Event 与 ADX Business Event 在代码和文档中区分。
- [x] Native A2A 保持后续独立入口，不混入本次本地 Device 接入。
