# ADX 本地 Agent Connector 产品与技术规格

> 文档状态：MVP 规格，随实现同步
> 最后更新：2026-07-23
> 适用范围：用户本地 Agent Runtime 接入 ADX Arena 的出站 Connector
> 相关计划：[`local-agent-connector-implementation-plan.md`](./local-agent-connector-implementation-plan.md)

## 1. 核心结论

ADX 本地 Agent Connector 是一层独立的**设备与 Runtime 控制面**。它安装在用户电脑上，主动通过 HTTPS/WSS 443 端口连接 ADX，发现受支持的本地 Agent Runtime，并只对由 Connector 自己启动的会话执行有限、可审计的操作。

它不属于 ADX 的撮合、谈判、结算或钱包层，也不把用户电脑变成一台可由云端任意操作的远程主机。

本方案的关键约束如下：

- 不接管用户已经打开的 Claude Code、Codex 或其他终端窗口。
- 不注入、不抓取、不复用任意外部进程的 stdin/stdout。
- 只管理由 Connector 启动并记录 ownership 的 session/子进程。
- 云端只能下发协议中预定义的 typed command；不存在 arbitrary cloud shell。
- Connector 不进行撮合、定价、交易签名或结算。
- 钱包私钥、助记词和支付签名密钥不进入 Connector，也不进入 Connector 事件流。
- Runtime 凭据优先留在本机；云端不得回传或持久化原始模型 API key。
- Claude 第三方订阅 OAuth 存在明确合规门槛。当前 Connector 无法识别本地 Claude CLI 实际认证来源，因此 Claude task 默认关闭；只有本机显式 `--unsafe-enable-claude-tasks` 的隔离开发测试才能打开，不能据此宣称已满足生产认证合规。

## 2. 背景与问题

ADX 希望让用户以最少步骤把已有 Agent 能力带入 Arena，同时保留用户对 Runtime、模型、skills 和本地工作环境的控制权。平台内模板 Agent 可以提供低门槛试用，但不能覆盖以下需求：

- 用户已经在 Claude Code、Codex 或其他 Runtime 中形成了自己的配置和工作流。
- 金融交易相关场景要求运行过程可观察、操作有边界、关键动作可审计。
- 平台需要知道一个 Agent 当前是否可用、运行在哪个设备、具备哪些能力，以及某条平台指令是否被接收和执行。
- 用户不应为了接入而开放本地入站端口、配置公网 IP 或把机器权限交给平台。

单独提供 MCP 不能完整解决这些问题。MCP 更适合向 Agent 暴露工具和上下文；它本身不定义设备配对、Runtime 生命周期、session ownership、心跳租约、命令确认、断线重放和跨 Runtime 的统一审计语义。因此，MCP 可以成为某个 Runtime driver 的内部能力，但不是本地接入控制面的替代品。

## 3. 目标与非目标

### 3.1 MVP 目标

- 用户登录 ADX 后，在一次设备配对流程中连接本机 Connector。
- Connector 自动发现本机受支持 Runtime，至少识别 Claude Code 与 Codex 的可执行文件、版本和基本能力。
- 用户能够将一个 ADX Agent 绑定到一个本地 Runtime。
- 平台能够看到设备在线状态、Runtime inventory、binding 状态和规范化 session/event 流。
- 平台能够通过有限指令启动、派发、取消、停止或恢复 Connector 所属 session。
- 所有控制指令具有身份、租户、时效、幂等和审计信息。
- 断线后可安全重连；重复消息不会重复执行高影响操作。
- 本地 Agent 接入与现有 `matching/`、Arena、A2A/x402 业务逻辑保持解耦。

### 3.2 非目标

- 接管或镜像已经打开的终端窗口。
- 远程桌面、任意命令执行、任意文件浏览或通用运维代理。
- 自动扫描用户整块磁盘、读取 shell history、抓取其他进程内存或凭据。
- 在 Connector 内完成交易策略、撮合、谈判、支付、托管或争议解决。
- 在 Connector 内保存钱包私钥、助记词或链上签名权。
- 宣称对用户整台电脑或所有 Agent 行为实现“完整审计”。审计边界只覆盖 Connector 控制和 Connector 所属 session 可提供的事件。
- MVP 阶段动态加载不受信任的第三方 driver。
- MVP 阶段支持所有 OS、所有 Agent Runtime 和生产级静默自动更新。
- 用 Claude.ai Free/Pro/Max 等订阅 OAuth 凭据替第三方流量计费。

## 4. 术语与边界

| 术语 | 定义 |
|---|---|
| Connector | 安装在用户电脑上的本地常驻进程/CLI，负责配对、发现、出站连接、session 管理和事件上报 |
| Connector Gateway | ADX 平台侧的设备配对、WSS 连接、命令与事件接入服务 |
| Device | 一次已配对的 Connector 安装实例，而不是用户账号本身 |
| Runtime | Claude Code、Codex 等本地 Agent 执行载体 |
| Inventory | Connector 探测到的 Runtime 及其版本、路径指纹和能力 |
| ADX Agent | Arena 中的业务身份，包含 owner、策略、ELO、可交易资产等 |
| Binding | 将一个 ADX Agent 绑定到一个 Device 上的 Runtime 的关系 |
| Session | Connector 为某个 Binding 启动并拥有的 Runtime 会话 |
| Command | 云端下发给 Connector 的有限、类型化控制指令 |
| Runtime Event | Connector 从所属 session 上报的运行事件；当前主要是脱敏 passthrough + wire 映射，高级语义规范化是后续 |
| Business Event | 撮合、报价、成交、支付等 ADX 业务事件 |

### 4.1 两个平面的明确分工

```text
Local Device / Runtime Control Plane             ADX / A2A Business Plane
┌──────────────────────────────┐                 ┌─────────────────────────────┐
│ Detect runtime/version       │                 │ Agent identity & Agent Card │
│ Pair device                  │                 │ Listing / intent / matching │
│ Start owned session          │  binding/ref    │ Negotiation / Arena         │
│ Send/cancel typed task       │◀───────────────▶│ Deal / payment orchestration│
│ Redact/map runtime events    │                 │ Business audit & settlement │
│ Heartbeat / reconnect        │                 │ Wallet policy               │
└──────────────────────────────┘                 └─────────────────────────────┘
```

控制面可以携带 `agent_id`、`deal_id`、`task_id` 等业务引用，但不能自行改变这些业务对象的权威状态。业务面可以根据可信的命令确认或结果事件推进流程，但不得把“Connector 收到命令”误当作“交易已经完成”。

权威来源约定：

- 设备、Runtime、Binding、Command、Connector session 状态：Connector Gateway。
- Agent 注册、ELO、订单、撮合和谈判状态：ADX 业务服务。
- 支付、托管和链上最终性：支付/链上服务。
- Connector 上报的文本或工具事件：可观察证据，不等同于支付或交付的最终证明。

## 5. 用户流程

### 5.1 首次接入

1. 用户登录 ADX，选择“连接本地 Agent”，页面给出安装和 `pair` 启动指引。
2. 用户在本机启动 Connector。Connector 主动创建 pairing，向用户显示公开的一次性 `user_code` 和 verification URL，把私有 `device_code` 只保留在本地内存；用户不向 CLI 输入 code。创建请求中的 owner 只能作为 UI hint，不能建立设备所有权。
3. 用户把 Connector 显示的 code 输入已登录的 ADX 页面，并核对设备信息。
4. 用户确认 pairing；此时才将 Device 所有权绑定到该登录用户/tenant。
5. Connector 使用本地私有 `device_code` 轮询一次性交换接口，获得设备凭据，随后建立出站 WSS。
6. Connector 扫描受支持 Runtime，并上报 inventory。
7. Web 端展示“已发现 Claude Code/Codex”、版本、兼容状态和可用能力。
8. 用户选择 Runtime，创建或选择 ADX Agent，并确认 binding。
9. 平台完成一次安全的 probe 或新建测试 session。
10. 成功后进入 Arena；ADX 业务任务通过 binding 派发到本地 Runtime。

产品目标是：已安装 Connector 的回访用户，从登录到 Agent online 不超过三次显式操作；首次用户除安装外，不要求配置端口、证书或公网网络。

当前 UI 可把 Runtime 绑定到页面已经加载的现有 ADX Agent；当业务 Agent 数据源不可用时，还提供明确标注的 `Connector-only identity (MVP)`，只生成控制面 `agent_id`，不会偷偷注册一个可撮合或可交易的业务 Agent。生产 onboarding 必须把“创建新 ADX Agent”接到业务注册服务，或要求选择一个真实 Agent，之后才能进入 Arena。

当前前端还提供“由 Web 生成 demo pairing”的开发演示路径，用于没有可执行 Connector 时测试 API；该响应会进入浏览器并带有 `device_code`，因此只能用于本地 demo。整个未认证 Connector Router 默认不挂载，只有服务端显式设置 `ADX_CONNECTOR_UNSAFE_DEMO=true` 才能启用；启用后 ASGI middleware 会依据直连 peer address 强制只允许 loopback，远程 HTTP 返回 403、远程 WSS 在握手阶段被拒绝，且不信任 `X-Forwarded-For`。该模式不得放在反向代理之后，也只能用于单用户开发环境。生产必须接入真实登录与 tenant/object 授权，并以 Connector 创建 pairing、登录用户批准为唯一 onboarding 主路径。

### 5.2 日常重连

1. Connector 启动并读取本机安全存储中的设备凭据。
2. 建立 WSS，发送 `hello`、最后确认序号和当前 inventory digest。
3. Gateway 校验设备、租户和 binding epoch，恢复未完成命令。
4. Connector 重放尚未确认的事件，平台去重。
5. 心跳恢复后设备状态从 `reconnecting` 变为 `online`。

### 5.3 解绑与撤销

- 用户可在 Web 端禁用 binding，不必删除 ADX Agent。
- 用户可撤销整个 Device；撤销后旧 token 立即失效，活跃 WSS 以 `4403` 被关闭，Connector 停止重连并 shutdown 自己的 task，相关 binding 停用并递增 epoch，使在途旧命令失效。
- 生产 CLI 应提供 logout/revoke，删除设备凭据并停止平台所属 session。当前 MVP CLI 提供 `scan|doctor|pair|run|version`，设备 revoke 由 Web/API 完成，尚无独立 `logout` 子命令。
- 删除 Device 不删除独立的 Arena 历史、交易记录或支付审计。

## 6. 系统架构

```text
┌──────────────────── User Device ─────────────────────┐
│                                                     │
│  ADX Connector (Go CLI / per-user daemon)           │
│  ├─ Pairing & local credential store                │
│  ├─ Runtime discovery                               │
│  ├─ Runtime drivers                                 │
│  ├─ Session supervisor + ownership registry         │
│  ├─ Event mapper / redactor                         │
│  ├─ Durable event outbox + sequence watermark       │
│  └─ WSS client ─────────────────────────────────┐   │
│                                                │   │
│  ┌───────────────┐     ┌───────────────────┐    │   │
│  │ Claude Code   │     │ Codex app-server  │    │   │
│  │ owned child   │     │ owned child       │    │   │
│  └───────────────┘     └───────────────────┘    │   │
└─────────────────────────────────────────────────│───┘
                                                  │ outbound TLS/WSS :443
┌──────────────────── ADX Platform ───────────────│───────────────┐
│  Connector Gateway                             ▼               │
│  ├─ Pairing / device auth                                      │
│  ├─ Connection & heartbeat registry                            │
│  ├─ Runtime inventory                                          │
│  ├─ Binding registry                                           │
│  ├─ Typed command queue                                        │
│  └─ Runtime event / audit intake                               │
│                                                               │
│  Existing ADX services                                        │
│  ├─ matching/agent.py                                          │
│  ├─ matching/engine.py                                         │
│  ├─ matching/negotiation.py                                    │
│  ├─ matching/arena.py                                          │
│  └─ payment / chain boundary                                   │
└───────────────────────────────────────────────────────────────┘
```

### 6.1 连接模型

- 每个 Device 默认只保持一条 WSS，复用多个 binding/session。
- `pair`/`run` 对同一 state 文件持有跨进程 OS singleton lock；第二实例 fail-fast。若 Gateway 仍发生替换，旧连接收到 `4409` 后必须退出，不能与新实例持续重连抢占。
- 仅由 Connector 发起出站连接；本机无需监听公网端口。
- WSS 用于控制消息和小型结构化事件。
- 大文件、产物和长日志使用预签名 HTTPS 上传，不通过 WSS 长时间传输。
- 协议 v1 使用 JSON envelope；后续可在兼容层内演进为 protobuf，而不改变业务 API。
- 目标语义为 at-least-once delivery，依赖 `message_id`、`command_id`、`idempotency_key` 和单调序号去重。当前 app-scoped in-memory Gateway 已实现 command 重投与幂等键，但尚未实现跨进程重启的 durable command delivery；Runtime Event 只在 Connector 端具备 durable outbox。
- 当前 Connector 用 crash-recoverable staged event 把 `sequence` 分配和 outbox 落盘串成可恢复步骤；append 或进程崩溃后会幂等完成同一序号，不会留下永久阻塞累计 ACK 的空洞。
- Gateway 为每次 WSS handover 分配 connection generation，并把连接切换、撤销、所有 outbound frame 和 command delivery 串行化；只有发送前后的 generation 都一致才提交 `delivered`，否则命令保持/恢复为 queued 并交给新连接。每个 inbound sequence/hello/heartbeat/inventory/command ACK/runtime event 也在持有状态锁时复核 generation，旧 socket 在 replacement 后不能再提交状态。
- 同一连接最多保持 64 个已发送但未 ACK 的在途 Runtime Event；本地 outbox backlog 可以更大，并以 8 条为一个发送 burst。重放期间持续消费 command、ACK 和 `event.ack`，避免大 backlog 阻塞控制面。
- Connector 启动时把尚停留在 `accepted` 的 durable command receipt 转为 `failed / connector_restarted` 后重放，不把一次崩溃后的未知执行结果伪装成仍在运行。所有未终态 receipt 都保留；终态 receipt 只保留最新 512 条，因此生产持久化的 retention/TTL 必须覆盖平台最大重投窗口。
- command receipt 的 lookup/claim/final save，或 staged event/outbox 的 stage、append、clear、load/ACK 持久化失败会锁存 `persistence-degraded`，best-effort 返回明确失败、拒绝新 command 并退出；`run` 随后 shutdown owned task，不允许 Runtime 在审计链路已断裂时继续接收或保留新任务。
- Windows owned child 在 suspended 状态加入 `KILL_ON_JOB_CLOSE` Job Object 后才恢复，能够覆盖整个 Job process tree；Containment 建立失败即拒绝启动。Linux 使用独立 process group 与 `Pdeathsig=SIGKILL`，可在 Connector 崩溃时清理直接 Runtime child、在显式 cancel/stop 时清理 process group，但尚不能保证崩溃时清理 Runtime 自行派生的所有 descendant；其他 Unix 也只保证显式 process-group cancel。

## 7. 信任边界与威胁模型

### 7.1 信任主体

- 浏览器用户：已通过 ADX 身份认证，但仍需通过资源级授权。
- Connector Device：持有设备凭据，只能访问所属 tenant/device/binding。
- Runtime 子进程：不默认信任其输出；事件需验证大小、类型和敏感字段。
- ADX 控制面：可下发有限命令，但无权任意执行 shell。
- ADX 业务面：拥有业务对象，不直接操作用户操作系统。
- 第三方模型供应商：有独立认证、计费和数据处理条款。

### 7.2 主要威胁与控制

| 威胁 | 必须控制 |
|---|---|
| pairing code 被截获 | 短 TTL、一次性使用、用户在已登录页面确认设备指纹 |
| device token 泄露 | 仅存摘要于服务端；本地使用 OS credential store；可撤销和轮换 |
| 跨租户访问 | 所有 Device/Binding/Command/Event 查询强制 tenant scope |
| 命令重放 | `command_id`、`idempotency_key`、过期时间、binding epoch |
| 旧 binding 接收新任务 | 绑定更新递增 `binding_epoch`；Connector 拒绝旧 epoch |
| 跨 binding/session 控制 | 每个 session lifecycle command 都必须与 session registry 中的 `binding_id`、`agent_id`、`runtime_id`、`binding_epoch` 完全一致 |
| 云端注入供应商会话 | `conversation_id`/`resume_token` 不属于云端 command schema；只使用 Connector-owned child 输出中首次捕获的 provider token |
| 云端获得 shell | 协议不定义 shell command；driver 只接受白名单动作和结构化参数 |
| Connector 接管外部进程 | supervisor 只承认自己创建并写入 ownership registry 的 session |
| 事件泄密 | 元数据优先、字段分级、redaction、大小上限、用户可见的采集模式 |
| Runtime 输出伪造成交 | 业务面不以自然语言输出作为支付/交付最终性 |
| 钱包密钥泄露 | Connector schema 禁止钱包私钥字段；签名在独立钱包/支付边界完成 |

## 8. Runtime 自动发现

### 8.1 探测原则

Runtime discovery 是**能力探测**，不是进程接管。

- 搜索 `PATH` 和少量已知安装目录，不遍历整块磁盘。
- 对候选可执行文件做 canonical path、文件信息和版本探测。
- 版本命令必须只读、有超时、输出大小上限且无交互。
- 不读取 shell history，不解析其他终端的输入输出，不注入运行中的进程。
- 不因“发现了某个 PID”就宣称该 Runtime 可受控。
- 发现结果包含 probe 时间和原因，前端区分“已安装”和“可被 Connector 管理”。

### 8.2 规范化 inventory

每个 Runtime 建议包含：

```json
{
  "runtime_id": "rt_...",
  "kind": "codex",
  "display_name": "Codex CLI",
  "executable_path": "C:\\...\\codex.exe",
  "version": "x.y.z",
  "available": true,
  "detected_at": "2026-07-23T12:00:00Z",
  "capabilities": [
    "session.start",
    "task.dispatch",
    "task.cancel",
    "session.resume",
    "events.structured"
  ],
  "auth_modes": ["openai_api_key", "chatgpt_managed_login"]
}
```

`executable_path` 属于敏感设备元数据。生产 Web UI 应只显示 basename 与模糊目录，完整路径不进入业务日志。当前 MVP UI 仍显示 Connector 上报的完整路径，这是上线前必须修复的数据最小化缺口。

### 8.3 探测状态

```text
not_found
   │ candidate found
   ▼
detected ── version/probe failure ──▶ incompatible
   │ successful capability probe
   ▼
compatible ── user disables ────────▶ disabled
```

启动时执行一次扫描；用户可手动刷新。后台周期扫描应有退避，且 inventory 未变化时只上报 digest/heartbeat。

当前 Go discovery 内部使用 `ready|degraded`，Gateway/UI 主要投影为 `available: true|false`。新 Connector 默认只发布 `runtime.probe`，认证标记为 `unverified_local_auth`；用户必须在本机使用 `--enable-codex-tasks` 或开发专用的 `--unsafe-enable-claude-tasks` 才会发布对应 session/task capability，Gateway 也拒绝未发布 capability 的 command。这个本地 gate 不能代替逐版本 permission/approval 兼容矩阵，未知或未验证版本在生产仍应保持 detection-only。

## 9. Claude Code 与 Codex 的接入差异

| 维度 | Claude Code | Codex |
|---|---|---|
| 生产优先控制接口 | CLI/Agent SDK 的 `stream-json` 输入输出，或供应商批准的 SDK 路径 | `codex app-server` 双向 JSON-RPC |
| 新建会话 | Connector 启动新的非交互会话 | Connector 启动 app-server，再调用 `thread/start` |
| 恢复 | 使用官方 session id 与 `--resume` 等受支持参数 | `thread/resume` |
| 发送任务 | stream-json stdin 或受支持 SDK | `turn/start` |
| 取消 | 优先使用官方取消机制；否则对 Connector 所属子进程做受控终止 | `turn/interrupt` |
| 事件 | init/message/result、hooks/SDK 可见事件，需规范化 | `thread/*`、`turn/*`、`item/*` 通知，结构化程度更高 |
| 当前审批 | 默认 detection-only；显式开发 flag 后 fixed argv runner 仍没有平台审批闭环，只继承 Claude 本地 permission 行为 | 默认 detection-only；显式本地 flag 后 fixed argv runner 仍没有平台审批闭环，只继承 Codex 本地权限行为 |
| 生产审批目标 | 官方 SDK/CLI 能暂停时转发 approval，不绕过本地边界 | app-server 暂停 turn，转发 approval 并等待明确结果 |
| 认证 | 当前只报告 `unverified_local_auth`，不能区分 API key、Bedrock/Vertex 或消费订阅登录；生产只允许通过合规 gate 的认证 | 当前只报告 `unverified_local_auth`；生产优先由本地 Codex/app-server 管理受支持认证 |

当前 Claude driver 的唯一命令形状是 `claude --print --output-format stream-json --verbose [--resume <connector-captured-token>]`，prompt 通过 stdin 输入。当前 Codex driver 的唯一命令形状是 `codex exec --json [resume <connector-captured-token>] -`。两者都是一次 task 一个 owned child，不接受云端追加 executable/argv 或 resume token。

### 9.1 Claude 认证合规门槛

Anthropic 当前公开说明中，Claude 订阅 OAuth 面向 Claude Code 与 Anthropic 原生应用的正常使用；第三方开发者不得向用户提供 Claude.ai 登录，也不得代表用户把 Free/Pro/Max 订阅额度路由给第三方产品。因此：

- Connector 不读取、复制、上传或重放 Claude Code 的订阅 OAuth token，但 fixed CLI child 可能继承本地认证环境；因此“没有上传 token”不等于“已证明没有使用订阅身份”。
- 本机已登录 Claude Code 不自动等价为 ADX 可使用该订阅额度；Claude task 默认关闭。
- `--unsafe-enable-claude-tasks` 只用于隔离开发账号，并明确表示认证来源尚未验证，不是生产 feature flag。
- 生产采用用户自备 Anthropic API key、Bedrock/Vertex 等供应商支持的企业认证时，仍需先实现可验证的 auth probe/gate。
- 若未来希望支持 Claude subscription login，必须先完成法务/供应商批准和技术认证评审；未获批准前不得上线。
- 凭据由本机 Runtime 或 OS secret store 使用，Gateway 只接收 `auth_modes`/能力等非秘密提示，不接收原始凭据；真实 `auth_status` 探测属于后续 driver 能力。

参考：[Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)、[Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)。

### 9.2 Codex 接入

Codex 官方 app-server 提供双向 JSON-RPC，包含 initialize、thread、turn、item event、interrupt 和 approval 等接口，适合作为生产完整 driver 的首选路径。Connector 应启动自己的 app-server 子进程并持有 stdio，不尝试附着到其他 Codex 窗口。

本地 Codex/app-server 负责其官方认证流程和 token 生命周期。Connector 只观察认证状态并转发用户可理解的登录动作，默认不把 token 上传到 ADX。参考：[Codex app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)。

当前 Go MVP driver 使用固定的 `codex exec --json [resume <connector-captured-token>] -` 运行 Connector 所属子进程，prompt 通过 stdin 输入，尚未接入 app-server 的完整 thread/turn/approval surface。因此当前可观察性来自 JSONL 子进程输出，不能宣称已具备 app-server 级 Runtime 控制或平台审批闭环。

## 10. Binding、Session 与状态机

### 10.1 Device 状态

```text
pending_pairing → approved → online ⇄ reconnecting
                               │
                               ├─ lease expired → offline
                               └─ revoke        → revoked
```

- `online` 必须由有效 WSS 和新鲜 heartbeat 共同决定。
- WSS 断开不立即删除 Device；先进入 `reconnecting/offline`。
- `revoked` 为终态；旧凭据不能恢复该 Device。

这是生产目标状态机。当前 MVP 对外状态为 `online|offline|revoked`，其中 heartbeat lease 过期投影为 `offline`，尚未单独暴露 `reconnecting`。

### 10.2 Binding 状态

```text
available → starting → running ⇄ degraded
    │           │         │
    └───────────┴─────────┴─ user/revoke/stop → stopped
```

每次更换 device/runtime 或重新启用都递增 `binding_epoch`。命令必须携带期望 epoch，防止任务落到旧 Runtime。

Session registry 同时固化 `binding_id + agent_id + runtime_id + binding_epoch`。`session.start` 对已存在 session ID 的检查，以及 `task.dispatch`、`task.cancel`、`session.stop`、`session.resume` 的每次操作，都必须匹配这四项；只匹配 `session_id` 或 epoch 不足以证明归属。

`probing/ready/disabled/unavailable` 可在生产版细化；当前 MVP 代码使用 `available|starting|running|degraded|stopped`。

### 10.3 Session 状态

```text
created → starting → running → completed
                  │     │  ├─ cancel → cancelling → cancelled
                  │     │  ├─ stop   → stopping   → stopped
                  │     │  └─ crash  → failed
                  └─────┴─ timeout   → failed
```

`resume` 创建新的运行尝试，但保留 `session_id` 与供应商 session/thread 引用。供应商引用只能由 Connector 从自己启动的 Runtime child 结构化输出中捕获，云端不能创建或覆盖；未捕获 token 时 `session.resume` 必须拒绝。是否可恢复由 driver capability 决定。

### 10.4 Command 状态

```text
queued → delivered → accepted → running → succeeded
   │         │          │          ├──────→ failed
   │         │          └──────────→ rejected
   │         └─ timeout ───────────→ expired
   └─ expires before delivery ─────→ expired
```

“WSS 写入成功”只代表 `delivered`，不能标为 `succeeded`。Connector 在持久化 command 和通过本地校验后返回 `accepted`；driver 完成后返回最终状态。当前 Command 枚举没有 `cancelled`；`task.cancel` 本身是一条独立 command，原 task 的最终结果通过其 dispatch command/event 收敛。

## 11. 控制协议

### 11.1 Envelope

生产目标 envelope 如下：

```json
{
  "protocol_version": "1.0",
  "message_id": "msg_...",
  "type": "command",
  "device_id": "dev_...",
  "sequence": 42,
  "sent_at": "2026-07-23T12:00:00Z",
  "payload": {
    "command_id": "cmd_...",
    "binding_id": "bind_...",
    "runtime_id": "rt_...",
    "binding_epoch": 3,
    "action": "session.start",
    "idempotency_key": "start-...",
    "expires_at": "2026-07-23T12:05:00Z",
    "payload": {
      "working_directory": "E:\\workspace"
    }
  }
}
```

要求：

- 发送方维护单调 `sequence`。
- Runtime Event 接收方返回累计 `through_sequence`；Connector 只删除已累计确认的本地 outbox。
- 同一 `message_id` 重放必须无副作用。
- 单条消息、事件批次和文本字段均设置大小上限。
- 未知 `protocol_version`、消息类型或非法状态转换应明确拒绝，不做宽松执行。

当前处于 v1 wire 兼容窗口，Gateway 允许早期客户端省略部分 outer metadata：

| Outer field | 当前 Gateway 入站 | 当前 Go Connector | Production strict |
|---|---|---|---|
| `type` | 必填 | 必填 | 必填 |
| `payload` | 可省略并按 `{}` 处理 | 必填 | 必填 |
| `protocol_version` | 可选；提供时校验为 `1.0` | 必填 | 必填 |
| `device_id` | 可选；提供时必须匹配 credential | 必填 | 必填 |
| `message_id` | 可选；缺失时无 message ack | 必填 | 必填 |
| `sent_at` | 可选 | 必填 | 必填 |
| `sequence` | 可选 | Runtime Event 必填，0 值消息可省略 | Runtime Event 和 command 必填，其他消息按类型定义 |

`hello.payload.protocol_version` 当前必须为 `1.0`。新客户端必须按 Go Connector 的完整 envelope 发送，不能依赖兼容省略；Production strict 再由 Gateway 强制全部对应字段并持久化 sequence。当前 Gateway 只在 app memory 中跟踪序号和 command queue，跨 Gateway 进程重启仍不是 durable delivery。

Runtime Event 的字段也分发送与存储两层：

- Connector wire payload：`event_id/binding_id/session_id/task_id/event_type/sequence/occurred_at/level?/data`。
- Gateway 存储/响应补充：新的服务端 `event_id`、`source_event_id`、`device_id`、`received_at`；event ack 单独返回 `through_sequence`。

当前 `runtime.message` 是 Runtime JSON line 经本地 secret redaction 后的 passthrough，不是 ADX 已完成语义归一化的事件。跨 Claude/Codex 的高级事件规范化属于后续。

### 11.2 允许的 MVP Command

| Command | 作用 | 关键参数 |
|---|---|---|
| `runtime.probe` | 重新探测 Runtime inventory | `{}` |
| `session.start` | 新建 Connector 所属 session | `{working_directory, initial_prompt?, environment_refs?}` |
| `task.dispatch` | 向已存在的所属 session 发送结构化任务 | `{session_id, prompt, request_id?}`；省略时 Gateway 生成 |
| `task.cancel` | 取消当前 task | `{session_id, request_id}` |
| `session.stop` | 正常停止所属 session | `{session_id, reason?}` |
| `session.resume` | 使用 Connector 已从该 owned session 捕获的供应商 token 恢复；云端不能提交或替换 token | `{session_id}` |

本地参数解析契约：

- `environment_refs` 只能包含环境变量**名称**，不能包含 `NAME=value` 或任何 secret value。Production strict Gateway 必须只允许平台配置中预先批准的名称；当前 Gateway 只做字符串、数量和长度校验，因此在补上服务端名称 allowlist 前不得为生产打开该字段。本地 Connector 无论如何都会再用 `--allow-env NAME` 做最终强制 allowlist。
- Connector 只从自己的本地进程环境解析获准变量，把值传给 owned child；值不上传到 Gateway、不进入 ack/event，也不写普通日志。名称非法、未被 `--allow-env` 批准或本地不存在时拒绝 session；未配置 `--allow-env` 时，没有任何 secret-bearing variable 可被 `environment_refs` 引用。
- `working_directory` 必须由云端显式提供为非空字符串；Gateway 与 Connector 都拒绝缺失、空串或纯空白值，不会自动回退到第一个 allow-root。Connector 随后 canonicalize 绝对路径并解析 symlink，再检查它是否位于本地 `--allow-root` 下；未显式传 `--allow-root` 时，唯一允许的 root 是 Connector 启动 cwd。
- `task.dispatch.request_id` 可由调用方提供；省略时 Gateway 在入队前生成，并将生成后的值纳入幂等 fingerprint 与下发 payload。
- `conversation_id` 与 `resume_token` 在 Gateway 和 Connector 两侧都被拒绝。Connector 只保存 owned child 首次报告的 `session_id/thread_id`，且不会把该 token 暴露给云端。

明确禁止：

- `shell.exec`
- 任意 executable path、任意 argv 或任意环境变量注入
- 针对未知 PID 的 kill/attach
- 任意文件读取/上传
- 钱包导出、私钥读取、签名绕过

driver 可以在本地把 typed command 映射为固定的 CLI 参数或 JSON-RPC 方法，但云端不能直接提供未经校验的命令行。

## 12. 平台 API

本次 MVP 的平台路由前缀为 `/api/connectors`。

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/pairings` | 创建一次性 pairing |
| `POST` | `/pairings/{user_code}/approve` | 已登录用户确认 pairing |
| `POST` | `/pairings/exchange` | Connector 交换设备凭据 |
| `GET` | `/devices` | 列出当前用户的设备 |
| `GET` | `/devices/{device_id}` | 查询设备、连接状态与 inventory |
| `POST` | `/devices/{device_id}/revoke` | 撤销设备、关闭连接并使相关 binding 失效 |
| `POST` | `/devices/{device_id}/bindings` | 将 ADX Agent 绑定到 Runtime |
| `GET` | `/bindings` | 列出 binding |
| `POST` | `/bindings/{binding_id}/commands` | 创建 typed command |
| `GET` | `/bindings/{binding_id}/commands` | 查询 command 状态和结果 |
| `GET` | `/bindings/{binding_id}/events` | 查询规范化运行事件 |
| `GET` | `/audit` | 查询 MVP app-scoped 控制审计视图 |
| `WS` | `/ws?device_id=...` | Connector 出站长连接；设备凭据放在 `Authorization: Device <token>` header |

生产版本必须由登录态推导 owner/tenant，不能信任客户端传入的 `owner_id`。Pairing 创建阶段的 owner 只能作为提示；真正所有权必须在已登录用户批准 `user_code` 时写入。MVP revoke body 若暂时携带 `owner_id`，它只是开发环境认证替身，禁止直接用于生产。

当前代码尚未接入真实登录中间件：部分 list/get/binding/audit 路由是 app-scoped demo view，前端审批和 revoke 使用 `demo-user`。因此 `create_app()` 与 `create_app_with_db()` 默认都不挂载 Connector Router；只有显式 `ADX_CONNECTOR_UNSAFE_DEMO=true` 才会挂载。该模式由 ASGI middleware 按直连 peer address 限制为 loopback，不能放在反向代理之后，也不能部署到共享网络或多租户生产环境。

### 12.1 核心数据模型

以下先列当前 MVP wire/store 字段；标为“生产目标”的字段尚未实现。

#### Pairing

- `pairing_id`
- `user_code`：给人输入的短码
- `device_code_hash`：服务端只存摘要
- `requested_owner_id`：可选展示提示，不授予所有权
- `status`：`pending|approved|consumed|expired`
- `owner_id`：当前由批准请求绑定；生产应从登录态同时绑定 tenant
- `device_name`
- `expires_at`、`approved_at`、`consumed_at`
- `device_id`

#### Device

- `device_id`
- `owner_id`（当前 demo；生产由登录态附带 tenant）
- `name`、`hostname`、`platform`、`connector_version`、`protocol_version`
- `token_hash`（仅服务端内部，永不返回）
- `status`
- `connected_at`、`last_seen_at`
- `runtimes[]`
- `binding_epoch`
- `outbound_sequence`、`last_inbound_sequence`
- `revoked_at`

`credential_version`、显式 `lease_expires_at` 和设备公钥是生产目标。

#### Binding

- `binding_id`
- `agent_id`
- `device_id`
- `runtime_id`、`runtime_kind`
- `display_name`
- `binding_epoch`
- `status`
- `last_session_id`、`last_task_id`
- `created_at`、`updated_at`

Capabilities 当前来自 Device 的 Runtime inventory，不复制到 Binding。

#### Command

- `command_id`
- `binding_id`、`device_id`、`runtime_id`、`agent_id`
- `binding_epoch`、`session_id`
- `action`
- `payload`
- `idempotency_key`
- `status`
- `created_at`、`expires_at`、`delivered_at`、`updated_at`
- `delivery_attempts`
- `result/error`

`request_fingerprint` 只在服务内部防止同一幂等键被用于不同 payload，公共响应会隐藏它。`created_by` 和独立 accepted/completed 时间是生产审计补充。

#### Runtime Event

- `event_id`
- `source_event_id`
- `device_id`
- `binding_id`、`session_id`、`task_id`
- `event_type`
- `sequence`
- `level`
- `occurred_at`、`received_at`
- `data`
- `visibility/redaction_level`（生产目标；当前 MVP wire 尚未提供）
- `correlation`：生产目标，可选 `deal_id`、`negotiation_id` 等业务引用

## 13. 可观测性

### 13.1 三类信号

1. **连接运行指标**：在线设备、WSS 重连、heartbeat 延迟、命令队列深度、ack 延迟。
2. **Runtime 生命周期事件**：session start/stop、turn/task 状态、tool request/result 摘要、错误和用量。
3. **业务关联事件**：某个 `task_id` 与 `deal_id` 的关联，但交易权威状态仍在业务面。

### 13.2 目标规范化事件类型（当前仅部分实现）

- `connector.connected`
- `connector.disconnected`
- `inventory.snapshot`
- `runtime.auth_required`
- `runtime.session.started`
- `runtime.session.stopped`
- `runtime.task.started`
- `runtime.output.delta`
- `runtime.tool.requested`
- `runtime.tool.completed`
- `runtime.approval.requested`
- `runtime.task.completed`
- `runtime.error`
- `command.acknowledged`

当前 fixed argv runner 主要产生 `session.*`、`process.*`、`runtime.message`、`runtime.stdout/stderr` 等执行事件；不会产生可交互的 `runtime.approval.requested` 闭环。上表其余统一类型是 app-server/SDK driver 的目标 schema。

### 13.3 生产目标隐私等级（当前未实现）

- `metadata_only`（生产默认目标）：状态、时间、类型、耗时、token/cost 等供应商可见统计，不含完整 prompt/response。
- `redacted_content`：经过本地规则脱敏的输入输出摘要。
- `full_content`：仅在明确用户授权、合规允许和业务确有必要时启用。

平台不收集 hidden chain-of-thought。Runtime 若产生 reasoning 字段，应按供应商协议处理并默认不进入业务展示。所有 UI 都要标注数据采集级别和保留期。

上述是目标，不是当前采集事实。当前 MVP 会在本地对 stdout/stderr 做凭据模式和 secret-key redaction，再将内容作为 `runtime.message` 或文本事件上传；前端可以展示 event `data`。即使 UI 隐藏某段内容，也不代表 Connector 没有采集或 Gateway 没有接收。当前尚无 `metadata_only/redacted_content/full_content` 采集开关、按级别 retention 或删除策略，只能用于知情的开发测试数据。生产前必须先实现本地采集开关、服务端 retention/删除和可验证的默认 metadata-only。

### 13.4 审计与业务事件分离

- 控制审计记录“谁在何时对哪个 binding 下发了什么 typed command、Connector 如何确认”。
- Runtime telemetry 记录 session 的可见运行信号。
- Business audit 记录报价、接受、成交、支付和交付证明。

三者通过 correlation id 关联，但不应写进同一个模糊日志表，更不能用 Runtime 自述替代链上或支付证据。

## 14. 操作控制与审批

- `session.start` 必须明确 Runtime、工作目录 scope 和权限策略。
- Connector 根据本机 policy 再做一次校验；云端允许不代表本地必须执行。
- 当前 fixed argv runner 没有“暂停 Runtime → 向平台发 approval request → 等待批准 → 恢复”的闭环，也不会替用户自动批准工具动作。
- MVP 只继承 Runtime 本地配置和原生 permission 行为。若某 Runtime/版本在非交互模式下无法安全保留本地审批边界，Connector 必须把对应 task/tool capability 降为 detection-only 或直接禁用，不能以“默认 require user approval”掩盖未知行为。
- Web 上批准 pairing 或创建 command 只表示设备/业务授权，不等于批准 Runtime 的高风险工具动作。
- 完整审批要等 Codex app-server 或供应商支持的 Claude SDK/CLI 能暂停执行并暴露 approval request 后实现；任何平台策略都不能绕过 Runtime/OS 的权限边界。
- 当前 fixed argv runner 的 cancel/stop 只能终止 Connector 自己持有的 task child；app-server/官方 SDK driver 落地后应优先使用 Runtime 官方 cancel，再以 owned-child 终止作为超时兜底。
- 所有控制动作必须可在 Web 中看到 actor、时间、目标、结果和失败原因。
- 设备 offline 时可以排队低风险命令，但超过 `expires_at` 不再交付。

当前 discovery 默认只声明 `runtime.probe`；session/task capability 只有本机 runtime-specific flag 打开后才会上报，Gateway 也强制检查 capability，Connector 端缺少对应 driver 时再次拒绝。该 gate 只证明“用户在本机显式打开”，不证明安装版本的审批语义安全；在完成版本/permission 契约测试前，生产仍必须保持 fixed argv task flags 关闭。

## 15. 安全与审计要求

### 15.1 设备认证

MVP 可使用随机高熵 device token，并在服务端仅存 SHA-256 摘要；生产应升级为设备公私钥、sender-constrained token 或 mTLS。无论实现阶段：

- pairing code 一次性且短 TTL。
- token 不写入日志、URL analytics 或 Runtime prompt。
- WebSocket 设备凭据必须使用 `Authorization: Device <token>` header，不得放入 URL query、代理日志或 analytics。后续可以进一步升级为一次性连接票据或 sender-constrained token。
- 支持 revoke、rotate 和 credential version。

### 15.2 本地权限

- Connector 以当前用户身份运行，不要求管理员/root。
- work directory 必须来自用户选择或预先授权的 allowlist。
- Runtime driver 使用最小环境变量集，不继承无关敏感变量。
- 本地 outbox 和 session registry 设置当前用户独占权限。
- updater 必须验签；MVP 未实现生产 updater 时不得承诺自动更新安全性。

### 15.3 数据最小化

- 不上传模型 API key、OAuth token、cookie、钱包私钥和助记词。
- 默认不上传完整 filesystem path、环境变量和 shell 输出。
- prompt/result 内容按用户选择和业务必要性采集。
- artifact 单独走受限、短期、可审计的上传通道。
- retention 和删除策略按数据类型分开配置。

### 15.4 审计完整性

生产版审计记录应追加写、不可静默覆盖，包含：

- `audit_id`
- actor（user/service/device）
- tenant、device、binding、session、command
- action、decision、policy version
- request/result digest
- timestamp、correlation id

MVP 内存存储只用于演示，不能宣称具备生产级不可篡改审计。

## 16. 异常恢复

| 异常 | 预期行为 |
|---|---|
| WSS 短暂断开 | 指数退避重连；保留 session；Runtime Event 以 `sequence/through_sequence` 补发 |
| Gateway 重启 | Connector 重连并发送最后 ack；未确认消息重放 |
| 重复 command | 在本地 receipt retention 窗口内按 binding/idempotency key 返回原结果，不重复启动 session；当前未终态全保留、终态保留最新 512 条，生产 durable store 的 TTL 必须覆盖最大重投窗口 |
| Connector 重启 | Windows Job Object 清理 owned process tree；Linux 清理直接 Runtime child，但 descendant crash containment 仍是生产缺口。未终态 receipt 在下次启动投影为 `failed / connector_restarted`；Gateway 发现 `hello.started_at` 改变后清空旧 session/task 投影并标记 binding degraded；当前 MVP 不恢复进程内 session，也不会 attach 外部进程 |
| Runtime crash | 上报 `runtime.error` 与 session `failed`；是否重试由 command policy 决定 |
| Runtime 升级后不兼容 | inventory 标记 `incompatible/degraded`，暂停新任务，不删除历史 binding |
| pairing 过期 | exchange 返回明确错误；用户重新创建 pairing |
| Device 被撤销 | Gateway 以 `4403` 关闭 WSS；Connector 不再重连，`run` defer shutdown 终止其 owned task；旧 token、后续 exchange/reconnect 和命令均被拒绝 |
| binding epoch 不匹配 | Connector 拒绝命令并上报 `stale_binding` |
| 事件过大/非法 | 本地截断或拒绝，并上报结构化 error，不阻塞整个连接 |
| receipt 或 event/outbox 持久化失败 | 锁存 `persistence-degraded`，拒绝新 command、退出并 shutdown owned task；不得只记录日志后继续运行 |

恢复策略必须避免“自动恢复”扩大权限。例如 Connector 重启后看到同名外部进程，也不能把它认作自己的 session。

## 17. 产品形态要求

Agent 页面应区分三件事：

1. **ADX Agent 身份**：名称、策略、ELO、资产能力。
2. **本地连接状态**：Device、Runtime、版本、在线状态、最近心跳。
3. **执行状态**：Binding、session、当前 task、最近事件与可用控制。

避免使用“Deploy Agent”同时表示创建业务 Agent 和连接本地 Runtime。推荐入口：

- `连接本地 Agent`：pairing → inventory → binding。
- `创建平台 Agent`：模板 Agent 试用，不依赖 Connector。
- `管理设备`：撤销、重命名、重新探测、查看诊断。

错误信息必须给出下一步，例如“检测到 Codex，但版本不兼容”“Claude 需要 API key；订阅 OAuth 不能用于此第三方接入”“设备在线，但 binding epoch 已失效”。

## 18. MVP 验收标准

以下复选框是 release gate，不等同于“有对应源码”。只有端到端验证及安全边界均通过后才能勾选；当前实现与已知缺口见第 19 节和 implementation plan。

### 18.1 功能验收

- [ ] 未开放本地入站端口时，Connector 可通过 WSS 连接 Gateway。
- [ ] pairing code 过期、复用或未批准时无法换取设备凭据。
- [ ] 生产 pairing 由 Connector 发起，浏览器只接触公开 `user_code`，不接收私有 `device_code`。
- [ ] 平台可列出 Device、inventory、Binding、Command 和 Event。
- [ ] 自动发现能正确区分 `not_found`、`compatible` 与 `incompatible`。
- [ ] Codex 与 Claude Code 至少完成无副作用的检测与版本探测。
- [ ] 一个 ADX Agent 可绑定到指定 Device/Runtime，且 binding epoch 生效。
- [ ] 只允许协议列出的 typed command；任意 shell 字段被拒绝。
- [ ] Connector 只启动和管理自己的 session，不附着到已打开终端。
- [ ] 命令 ack 与最终结果可区分；重复 command 不产生重复副作用。
- [ ] 断线重连后未确认事件可补发，服务端可去重。
- [ ] Web 端能显示连接、Runtime、Binding 和最近运行事件。

### 18.2 安全验收

- [ ] 服务端不存明文 device token。
- [ ] API 查询按 owner/tenant 隔离。
- [ ] 未接入真实登录/对象授权时，Connector Router 默认不挂载；unsafe demo 仅允许 loopback。
- [ ] 日志与 API 响应不包含模型 API key、OAuth token 或钱包私钥。
- [ ] Claude subscription OAuth 路径默认关闭。
- [ ] Runtime task capability 默认 detection-only，只有本机显式 runtime-specific opt-in 才能开启。
- [ ] Device revoke 后旧 token 无法重连。
- [ ] Device revoke 会关闭活跃 socket、停用相关 binding，并通过 epoch 使旧命令失效。
- [ ] stale binding epoch、过期 command 和超大消息被拒绝。
- [ ] 所有 session lifecycle command 对 `binding_id/agent_id/runtime_id/binding_epoch` 做完整归属校验；跨 binding session id 被拒绝。
- [ ] 云端不能提交 `conversation_id/resume_token`；没有 Connector-captured token 的 session 不能 resume。
- [ ] 工作目录和 Runtime executable 必须经过本地 allowlist/探测结果校验。

### 18.3 可观察性验收

- [ ] 每个 command 可追踪到 actor、binding、session、ack 和最终状态。
- [ ] 设备在线状态由 heartbeat lease 计算，而非仅靠数据库布尔值。
- [ ] Runtime Event 与 ADX Business Event 在模型和存储上可区分。
- [ ] 生产默认 `metadata_only` 在本地就不采集/上传完整 prompt/response，而不只是 UI 隐藏。
- [ ] UI 不宣称可以观察 Connector 之外的本地 Agent 活动。

### 18.4 MVP 不达标条件

出现以下任一情况不能标记为可生产：

- 需要用户开放入站端口。
- 云端可构造任意 shell/argv。
- 可以附着到任意已有 Claude/Codex 进程。
- token、模型密钥或钱包密钥进入普通日志。
- 内存事件队列被描述为持久化、不可篡改审计。
- 未经批准使用 Claude 消费订阅 OAuth 为 ADX 流量计费。
- 在共享/远程服务上启用 `ADX_CONNECTOR_UNSAFE_DEMO`，或把 `--unsafe-enable-claude-tasks` 当作生产认证 gate。

## 19. 当前仓库实现快照

本节只用于说明代码状态，不替代上述目标规格。实现状态应以仓库和测试结果为准。

| 范围 | 路径 | 状态说明 |
|---|---|---|
| ADX Agent / matching / Arena | `matching/`、`web/api.py` | 变更前已存在的业务面 |
| Connector Gateway 模型与服务 | `connector_gateway/` | 本次 MVP 新增；当前为 app-scoped in-memory 状态、`demo-user` 和未完成对象级授权 |
| Connector Gateway 路由 | `connector_gateway/api.py`、`web/api.py` | 本次 MVP 新增；默认不挂载，只有 `ADX_CONNECTOR_UNSAFE_DEMO=true` 才启用未认证本地 demo |
| Gateway 测试 | `tests/test_connector_gateway.py` | 本次 MVP 新增；Router 默认关闭/远程 peer 拒绝、pairing、WSS、幂等、redaction、session ownership/resume token、multi-socket revoke、single sender、connection handover、关闭码语义、迟到 ACK 和 Connector restart 投影共 9 个测试 |
| 本地 Connector | `connector/` | 本次 MVP 新增 Go 模块；默认 detection-only，本机 runtime-specific flag 才装载 fixed argv driver；owned-child runner、fail-closed outbox/receipt 和 OS process containment 已实现，session registry 仍只在进程内，app-server 完整接入属于后续 |
| 跨语言/UI smoke | `tests/connector_go_e2e.py`、`tests/connector_ui_smoke.py` | 本次 MVP 新增；分别验证真实 Go binary 无模型控制链路和 Chromium 入口流程 |
| Agent 连接入口 | `frontend/src/app/agents/page.tsx`、`frontend/src/components/ConnectorConsole.tsx`、`frontend/src/lib/connector-api.ts` | 本次 MVP 新增/更新；支持选择现有 ADX Agent，空数据时仅创建明确标记的 control-plane placeholder |
| Pairing/API 环境配置 | `.env.example`、`frontend/.env.example` | 新增 `ADX_PUBLIC_APP_URL`、`NEXT_PUBLIC_API_URL` 与默认关闭的 `ADX_CONNECTOR_UNSAFE_DEMO` |
| 持久化 command/event/audit、跨 Gateway 重启 delivery | 未接入 | 后续阶段；当前不得作为生产可靠队列或不可篡改审计使用 |
| 生产设备公钥/mTLS | 未接入 | 后续安全强化 |
| Native A2A Endpoint | 未接入 | 第二方案，见 implementation plan |

在合并前应再次运行文件扫描与测试，若某个路径未实际落地，应将对应状态改为“计划中”，不能在发布说明中声称已实现。
