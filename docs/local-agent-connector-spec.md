# Arena 402 本地 Agent Connector 产品与技术规格

> 文档状态：Self-hosted beta 规格，与当前仓库实现同步
> 最后更新：2026-07-25
> 适用范围：用户本地 Agent Runtime 接入 Arena 402 的出站 Connector
> 对应计划：[`local-agent-connector-implementation-plan.md`](./local-agent-connector-implementation-plan.md)
> 部署手册：[`self-hosted-connector-deployment.md`](./self-hosted-connector-deployment.md)
> 统一 Runtime 目标：[`hosted-arena-agent-spec.md`](./hosted-arena-agent-spec.md)
> 前端边界：产品 UI 已迁移到
> [`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)；
> 本仓库 Next.js 仅为 Compose 过渡壳，外部 Vercel/API 切换尚未验收

## 1. 核心结论

Arena 402 本地 Agent Connector 是独立的**设备与 Runtime 控制面**。它安装在用户电脑上，只发起 HTTPS/WSS 443 出站连接，发现受支持的本地 Agent Runtime，并仅控制由 Connector 自己启动和记录 ownership 的会话。

当前仓库已经具备一条不依赖 Supabase 的 self-hosted beta 路径：

- PostgreSQL 持久化 Connector 用户、邀请、会话、Pairing、Device、Runtime、Binding、Command、Event 与 Audit；
- 一次性 invite、注册、登录、会话恢复、退出和 CSRF 防护；
- 以认证用户为隔离键的 Device/Binding/Command/Event/Audit 对象级授权；
- 生产专用 Router、生产配置 fail-closed、公开认证与 Pairing 入口限流；
- Pairing 过期清理和待处理 Pairing 总量上限；
- Command 在任何 WSS 下发前先跨越 PostgreSQL durable pre-delivery barrier；
- `adx-connector connect --server https://...`、浏览器授权、凭据保存和系统自启动；
- Docker Compose + PostgreSQL + FastAPI + Next.js + Caddy 部署栈，支持域名 TLS 和短期 IPv4 TLS；
- Windows/Linux AMD64/ARM64 安装包、SHA-256 校验和与一行安装脚本。

这条路径当前只实现 Device/Runtime 控制面，尚未实现
[`game-design.md`](game-design.md) 定义的 `arena.decide`、`arena.negotiate`
业务适配，也没有把 dispatch ACK 与唯一 terminal `AgentTaskResult` 分开的 Arena
结果通道。Connector 的成功回执不能证明候选动作合法、Agent 已成交或链上支付已
确认。

这表示“控制面代码与单机部署路径已具备”，不表示某台真实服务器已经部署完成，也不表示已经通过外部网络端到端验收。

当前实现仍有五个明确边界：

1. Gateway beta 固定单个 Uvicorn worker；WSS 连接表、发送锁和限流桶仍在进程内，不支持水平扩展。
2. Arena 的 Game/Round/Pool/Negotiation/Inventory 已由 PostgreSQL 持久化；
   Connector 仍未接入该业务链，也无权直接修改这些状态。
3. Runtime task 默认 detection-only；fixed-argv Codex/Claude runner 不具备完整的平台审批闭环。
4. 安装包目前有 HTTPS 传输与 SHA-256 校验，但签名发布、SBOM、独立信任根和安全自动更新仍是后续。
5. Connector WSS 尚未实现版本化 `arena.agent-task.v1` /
   `arena.agent-result.v1` 映射、Arena Result Sink 与统一 Deadline Finalizer 接线。

## 2. 产品边界

### 2.1 Connector 负责

- 设备配对、设备凭据和出站 WSS；
- Claude Code、Codex 等 Runtime 的有限发现与版本探测；
- Device、Runtime、Binding 与 Connector-owned Session 的控制状态；
- typed command、命令确认、Runtime Event 和控制审计；
- 本地幂等 receipt、事件 outbox、重连和有限恢复；
- 撤销设备、关闭活跃连接并使旧 binding epoch 失效。

### 2.2 Connector 不负责

- 接管用户已经打开的 Claude Code、Codex 或其他终端窗口；
- 注入、抓取或复用任意外部进程的 stdin/stdout；
- 任意 shell、任意 argv、远程桌面或任意文件浏览；
- 撮合、定价、谈判、付款、托管、链上签名或争议解决；
- 保存或上传本地模型 API key、OAuth token、钱包私钥或助记词；Hosted Agent
  的模型 BYOK 是独立 write-only ingress + 外部 Secret Manager 路径，不经过
  Connector；
- 把 Runtime 输出当作成交、支付或交付的最终证明；
- 对 Connector 之外的本地 Agent 活动做“完整监控”。

MCP 可以作为某个 Runtime driver 的内部能力，但不能替代设备配对、WSS 生命周期、session ownership、命令幂等、断线恢复和跨 Runtime 审计语义。

Arena 级 routing 只引用 Connector-owned
`connector_binding_id + binding_epoch`。Arena 不复制或接管 Device、Runtime、
Binding、Command、Receipt、Session 与在线状态的权威；旧 epoch 的 ACK、Event
和 Result 均不得被当前 Game Agent 接受。

## 3. “一次安装、一次授权、自动上线”

### 3.1 首次使用

```text
管理员签发一次性 invite
  → 用户运行平台提供的一行安装命令
  → 安装器下载并校验 Connector
  → Connector 创建 Pairing 并自动打开 /connect?code=...
  → 用户用 invite 注册，或用已有账号登录
  → 用户核对设备并批准一次
  → Connector 交换设备凭据并建立 WSS
  → 自动上报 Runtime inventory
  → 安装器注册系统自启动
```

产品语义中的“一次授权”指首次安装后的浏览器设备批准。首次账户还需要一次 invite 注册；回访用户使用已有登录会话或重新登录，不需要再次输入 Device token。

安全约束：

- 浏览器只接触公开、短期的 `user_code`；
- 私有 `device_code` 只由 Connector 持有并用于轮询 exchange；
- Pairing 创建请求中的 owner 仅是展示提示，不能建立所有权；
- Device 所有权只在已认证用户批准 Pairing 时写入；
- bootstrap invite 只保存 SHA-256 摘要，明文只显示一次；
- 后续外部用户应使用独立、限时、一次性的 invite。

### 3.2 回访与自动上线

- Windows 安装器注册当前用户、Limited 权限的 Scheduled Task；
- Linux 安装器注册 systemd user service；
- Connector 从当前用户独占的 state 路径读取设备凭据；
- WSS 断开后指数退避重连；
- 收到 `4403`（撤销）或 `4409`（被新实例替换）时退出，避免无限重启或连接争抢；
- 未显式开启 task flag 时只上报发现结果和 `runtime.probe` 能力。

## 4. 系统架构

```text
User device
  adx-connector
    ├─ Pairing / local credential store
    ├─ Runtime discovery
    ├─ Connector-owned supervisor
    ├─ Durable receipt + event outbox
    └─ outbound HTTPS/WSS :443
                    |
                    v
Internet -> Caddy :80/:443
             ├─ Next.js 15.5.21 :3000
             ├─ FastAPI production router :8000
             ├─ /downloads (installer + binaries + SHA-256)
             └─ PostgreSQL 17 (internal Docker network only)
```

只有 Caddy 映射宿主机端口。Next.js、FastAPI 和 PostgreSQL 不直接暴露公网端口。Docker Compose 为 2 vCPU / 4 GB RAM 的单机 beta 配置了健康检查、资源上限、只读根文件系统（适用服务）和日志轮转。

目标 Arena 扩展尚未实现，其逻辑边界为：

```text
Arena Runtime Adapter
  -> immutable AgentTask through Connector Gateway
  -> outbound WSS Connector
  -> explicit terminal AgentTaskResult
  -> Arena Result Sink / Consumer / Finalizer
```

## 5. 身份、会话与对象授权

### 5.1 Self-hosted beta 身份

- `POST /api/auth/invite`：兼容的 invite 接受入口；
- `POST /api/auth/register`：使用一次性 invite 创建可恢复账号；
- `POST /api/auth/login`：用户名和密码登录；
- `GET /api/auth/session`：恢复当前浏览器会话；
- `POST /api/auth/logout`：撤销服务端会话并清理 Cookie。

密码使用 Argon2id。会话使用随机 token、服务端摘要记录和签名 Cookie；Session Cookie 为 `HttpOnly + Secure + SameSite=Lax`。CSRF 使用同源 double-submit Cookie 与 `X-CSRF-Token`，所有状态变更型浏览器请求都必须校验。

生产配置要求：

- PostgreSQL URL；
- 至少 32 字符的 session secret；
- HTTPS `ADX_PUBLIC_APP_URL`；
- Secure Cookie；
- bootstrap invite 的 64 位 SHA-256 十六进制摘要；
- 明确且不含 `*` 的 CORS origin。

缺少或使用不安全配置时，生产 app 在接受流量前启动失败。

### 5.2 对象级授权

生产 Router 不信任请求体中的 `owner_id`：

- Device 列表由登录用户过滤；
- Device get/revoke 要求当前用户拥有该 Device；
- Binding 列表、Command、Event 和 Audit 按拥有的 Device 过滤；
- 生产创建 Binding 时拒绝客户端指定 `agent_id`，先由控制面生成独立身份；在持久化业务 Agent ownership 服务接入前，UI 不允许把 Runtime 伪绑定到任意 Arena Agent；
- 跨用户对象返回 404，避免泄露对象存在性；
- Arena 的 Agent 注册、意图和谈判等 mutation 也要求 session、CSRF 和 Agent ownership。

当前 beta 以 `user_id` 作为 tenant-like 隔离键；组织级 tenant、成员关系、RBAC/ABAC 和管理员委托尚未实现，不能把当前模型描述为完整企业多租户。

目标 Arena identity 接线后，`connector_users` 暂时作为共享平台 User 的兼容表名。
Arena Agent 仍由 Arena identity store 拥有；Arena route 通过 Connector Binding
引用建立关联。入局事务必须保证一名 User 在同一 Game 中只有一个 Game Agent，
冻结当时的 `binding_id + binding_epoch`，且活动 Game 不允许中途切换 Runtime。

### 5.3 开发 demo 与生产 Router

- `create_app()` 在 `ADX_ENV=production` 或 `ADX_CONNECTOR_MODE=production` 时构建 production bundle，并挂载认证 Router；
- 生产 bundle 使用 PostgreSQL repository、Auth、Persistent Gateway 和 production Router；
- 已删除遗留 Supabase 开发工厂；`create_app()` 是唯一 HTTP 组合根；
- 未认证内存 demo 默认关闭，只能显式设置 `ADX_CONNECTOR_UNSAFE_DEMO=true`，并且按直连 peer 限制为 loopback；
- unsafe demo 不得位于公网反向代理之后。

## 6. 公开入口防滥用

当前单 worker beta 使用有界、进程内 sliding-window limiter：

- invite/register/login 共用认证入口限流；
- Pairing create/approve/exchange 使用独立限流；
- 超限返回 `429` 和 `Retry-After`；
- limiter 最多保存 4096 个 key，拒绝攻击者无限制造桶。

Pairing 状态同时受两层约束：

- 每次创建前清除已过期的 pending/approved Pairing；
- `ADX_CONNECTOR_MAX_PENDING_PAIRINGS` 限制未完成 Pairing 总量；
- PostgreSQL 状态事务写入后清理已到期的非终态 Pairing 行。

这些控制适合当前单 worker beta。多 worker/多实例部署前，必须把限流、连接 ownership 和 lease 移到共享基础设施。

## 7. 持久化与投递语义

### 7.1 PostgreSQL 范围

`002_connector_gateway.sql` 创建并约束：

- `connector_users`
- `connector_invites`
- `connector_sessions`
- `connector_pairings`
- `connector_devices`
- `connector_runtimes`
- `connector_bindings`
- `connector_commands`
- `connector_events`
- `connector_audit`

服务启动时恢复持久状态，所有 Device 被重新投影为 `offline` 或 `revoked`；此前仅标记为 `delivered`、尚未 ACK 的 Command 恢复为 `queued`。

### 7.2 Durable command pre-delivery barrier

Command 的正确顺序是：

```text
validate + create command/audit
  → persist mutable state + new audit delta to PostgreSQL
  → mark command durable-ready
  → only then write command frame to active WSS
  → persist delivery/ACK/result transition
```

任何命令都不能先通过 WSS 被 Connector 观察到、再补写数据库。重启后，已跨越 barrier 但未确认的命令可重新排队，并依赖 `binding_id + idempotency_key` 与 Connector 本地 receipt 避免重复副作用。

Runtime Event 在 Connector 本地先进入 durable outbox，通过 Device-scoped sequence 累计 ACK；Gateway 按 Device/sequence 去重并把 watermark 从 PostgreSQL 状态恢复。

### 7.3 当前持久化边界

`PersistentConnectorGateway` 当前对 Pairing、Device、Binding 和 Command 使用 snapshot/upsert，对 Event 与 Audit 使用增量 append，并在内存、启动加载和 PostgreSQL 中最多保留最近 10,000 条。累计 ACK watermark 与待补 gap 序列随 Device 状态持久化，因此裁剪旧 Event 不会破坏 Connector outbox 确认。这个实现适合单机 beta，不是高吞吐事件存储设计。WSS socket、per-device sender lock、connection generation 和 rate limiter 仍是进程内对象，因此：

- API 固定一个 Uvicorn worker；
- 不支持无协调的多副本；
- 不宣称零停机或高可用；
- 扩展前需要把其余 mutable snapshot 改为行级增量 repository，并增加共享 connection ownership/lease、分布式限流和容量测试。

## 8. Runtime 发现与控制

### 8.1 默认 detection-only

Runtime discovery 只搜索 `PATH` 和少量已知安装目录，执行只读、超时、有输出上限的版本探测，不遍历整块磁盘、不读取 shell history、不附着外部 PID。

默认 inventory 只发布：

- Runtime kind、显示名、版本和可用状态；
- `runtime.probe`；
- `unverified_local_auth`。

任务能力只有本机显式 opt-in 后才发布：

- Codex：`--enable-codex-tasks`；
- Claude：`--unsafe-enable-claude-tasks`，仅限隔离开发测试。

Gateway 仍会按 Runtime capabilities 拒绝未声明能力的 command。这个本地开关不等于生产审批或供应商认证通过。

### 8.2 当前 Typed command

允许：

- `runtime.probe`
- `session.start`
- `task.dispatch`
- `task.cancel`
- `session.stop`
- `session.resume`

禁止：

- `shell.exec`
- 任意 executable/argv；
- 任意环境变量值注入；
- 外部 PID attach/kill；
- 任意文件读取或上传；
- 云端提供 `conversation_id` / `resume_token`；
- 钱包导出、私钥读取或签名绕过。

`working_directory` 必须位于本机 `--allow-root`；`environment_refs` 只允许名称并受本机 `--allow-env` 二次约束。Session lifecycle command 必须匹配 `binding_id + agent_id + runtime_id + binding_epoch`。

### 8.3 目标 Arena typed task/result

Arena 接线保留 Connector 顶层 `task.dispatch` 兼容 action，但新增嵌入严格、
版本化 `arena.agent-task.v1` 的 payload variant，而不是把自由 `prompt` 当作
业务权威。外层 Connector transport envelope 继续使用现有兼容协议版本：

```json
{
  "session_id": "session-01",
  "task": {
    "taskId": "task-01",
    "kind": "arena.decide",
    "schemaVersion": "arena.agent-task.v1",
    "gameId": "game-01",
    "roundId": "round-03",
    "gameAgentId": "game-agent-01",
    "negotiationId": null,
    "deadlineAt": "2026-07-24T12:00:30Z",
    "idempotencyKey": "game-01:round-03:game-agent-01:decide",
    "inputHash": "sha256:...",
    "input": {}
  }
}
```

Connector 与 Runtime adapter 必须输出一条显式 terminal result：

```json
{
  "schemaVersion": "arena.agent-result.v1",
  "resultId": "result-01",
  "taskId": "task-01",
  "status": "succeeded",
  "action": {
    "action": "buy",
    "good": "ruby"
  }
}
```

业务 action 是严格 union：

- Decide：`action="buy" | "sell" | "pass"`；
- Negotiate：`action="propose" | "accept" | "reject"`；
- 价格使用定点字符串或最小单位整数，拒绝 extra fields；
- 公开 `message` 不超过 100 字，并在 Arena Result Sink 的任何持久化前经过统一
  PublicOutputPolicy。

状态必须分开：

```text
Command persisted/delivered ACK
  != Runtime terminal AgentTaskResult
  != Arena Result applied/rejected ACK
  != accepted trade
  != payment confirmed
```

- Gateway 不能从 stdout、最后一条 `runtime.message`、普通 Event 或 Command
  `succeeded` 推断业务动作；
- 相同 Task 的重连/重投使用原 `taskId + idempotencyKey`；每个 Task 最多一个
  terminal Result；
- Arena Result Sink 使用数据库时钟生成 `result_received_at`，Local Runtime
  自报时间不参与 FCFS；
- Result Sink/Consumer 通过唯一约束与 CAS 最多应用一次；late/duplicate 只追加
  不含候选原文的诊断 Event；
- Arena-owned Deadline Finalizer 在 Connector 不可用时也必须关闭过期 Task：
  Decide 为唯一 `pass`，Negotiate 为唯一 timeout；
- 同一 Game 的 Hosted、Local、rule 与未来 Native A2A Runtime 使用相同、
  经真实 P95/P99 与负载测试校准的 `action_timeout_ms`；
- 每个逻辑 AgentTask 最多两个 Attempt，只在错误可重试且剩余时间足够时重试，
  不切换 Runtime、Provider 或模型。

Connector 心跳丢失后的重连窗口是 30 秒与当前行动剩余时间中的较短者。窗口内使用
原 Task/key 恢复；超时后当前 Task 由 Finalizer 收敛，后续回合仍生成 explicit
default，不留下缺失行动，也不自动切换到 Hosted。

以上是目标契约，当前代码尚未实现；现有 `task.dispatch` v1
`{session_id, prompt, request_id?}` 继续作为兼容能力，不能被误报为 Arena typed
adapter 已完成。

### 8.4 Driver 边界

当前 Codex 和 Claude task driver 都是一次 task 一个 Connector-owned child 的固定 argv runner。它们可验证 owned-child、幂等 receipt、cancel/stop 和 resume token 来源，但没有“Runtime 暂停 → 平台展示 approval → 用户批准/拒绝 → Runtime 恢复”的完整闭环。

生产 Runtime task 继续保持关闭，直到：

- Codex app-server 或受支持 SDK/协议完成；
- Claude 认证来源可验证并通过供应商/法务 gate；
- 各支持版本的 permission/approval 契约测试通过；
- Linux/macOS descendant crash containment 达到发布要求。

## 9. API 契约

### 9.1 Browser auth

| 方法 | 路径 | 认证/CSRF |
|---|---|---|
| `POST` | `/api/auth/invite` | 公开、限流、一次性 invite |
| `POST` | `/api/auth/register` | 公开、限流、一次性 invite |
| `POST` | `/api/auth/login` | 公开、限流 |
| `GET` | `/api/auth/session` | Session |
| `POST` | `/api/auth/logout` | Session + CSRF |

### 9.2 Connector control plane

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/connectors/pairings` | Connector 创建一次性 Pairing；公开、限流 |
| `POST` | `/api/connectors/pairings/{user_code}/approve` | 登录用户批准；Session + CSRF + 限流 |
| `POST` | `/api/connectors/pairings/exchange` | Connector 交换设备凭据；公开、限流 |
| `GET` | `/api/connectors/devices` | 列出当前用户 Device |
| `GET` | `/api/connectors/devices/{device_id}` | 读取拥有的 Device 与 inventory |
| `POST` | `/api/connectors/devices/{device_id}/revoke` | 撤销拥有的 Device；CSRF |
| `POST` | `/api/connectors/devices/{device_id}/bindings` | 创建 Binding；CSRF |
| `GET` | `/api/connectors/bindings` | 仅列出拥有的 Device 下的 Binding |
| `POST` | `/api/connectors/bindings/{binding_id}/commands` | 创建 typed command；CSRF |
| `GET` | `/api/connectors/bindings/{binding_id}/commands` | 查询 Command |
| `GET` | `/api/connectors/bindings/{binding_id}/events` | 查询 Runtime Event |
| `GET` | `/api/connectors/audit` | 查询当前用户控制审计 |
| `WS` | `/api/connectors/ws?device_id=...` | `Authorization: Device <token>` |

设备 token 不得出现在 URL query、浏览器 storage、普通日志或 Runtime prompt 中。

## 10. 可观测性与业务边界

控制面提供三类信号：

1. Device/WSS/heartbeat/inventory；
2. Binding/Command/Session/Runtime Event；
3. 与 `agent_id/game_id/round_id/negotiation_id` 等业务对象的只读引用。

这些信号不是同一权威来源：

- Connector Gateway 是设备与 Runtime 控制状态的权威来源；
- Arena Game Core 与 PostgreSQL `arena402` schema 是游戏业务权威；
- 支付、托管与链上服务才可决定资金最终性；
- Runtime 文本或工具事件只是可观察证据，不能证明支付或交付完成。

Hosted/rule Runtime 已使用以下由 Arena 拥有的业务结果链；Local Connector
Adapter 仍需接入：

```text
AgentTask created
  -> dispatch ACK
  -> terminal Result submitted
  -> Result applied/rejected/defaulted
```

公开协商时间线只展示经过 Arena 校验和过滤的
`propose/accept/reject`、价格、服务端时间和 settlement 状态；Connector Event、
usage、latency 与安全错误只进入 owner/operator 可见投影。任何路径都不采集
private chain-of-thought。

当前 Runtime Event 会进行本地凭据模式和 secret-key redaction，但尚未完成默认 `metadata_only`、按级别 retention/删除和用户自助数据治理。不得把当前 event store 描述为不可篡改金融审计。

## 11. 部署形态

仓库提供：

- `docker-compose.production.yml`；
- PostgreSQL 17、migration job、单 worker FastAPI、Next.js 15.5.21、Caddy 2；
- 域名模式的 Caddy 自动 TLS；
- 裸 IPv4 的 HTTP challenge bootstrap、Certbot short-lived IP certificate 和 systemd 续期 timer；
- 只暴露 80/443，PostgreSQL、API 和 Web 仅在 Docker 网络；
- 数据库备份/恢复、升级/回滚、SSH/UFW hardening 脚本；
- Windows/Linux AMD64/ARM64 Connector artifacts 与 installer。

完整服务器操作、TLS 选择和外部验收步骤见 [`self-hosted-connector-deployment.md`](./self-hosted-connector-deployment.md)。

当前状态是“部署资产已实现并完成静态/本地构建验证”；真实腾讯云主机部署、真实公网 IP TLS、外部电脑安装和 WSS E2E 仍必须在目标环境执行后才能标记完成。

## 12. 当前实现矩阵

| 范围 | 主要路径 | 当前状态 |
|---|---|---|
| Self-hosted Auth | `connector_gateway/auth.py`、`repository.py`、`postgres_repository.py` | 已实现 invite/register/login/session/logout、Argon2id、签名 Session Cookie、CSRF、session revoke |
| Production config | `connector_gateway/config.py`、`production.py`、`web/api.py` | 已实现 fail-closed；遗留 Supabase 工厂已删除 |
| Tenant-like object auth | `connector_gateway/api.py`、`web/api.py` | 已实现 user-owner scope 和跨用户对象隐藏；组织 tenant/RBAC 未实现 |
| PostgreSQL persistence | `db/migrations/002_connector_gateway.sql`、`persistent_service.py` | 已实现 beta 持久化与重启恢复；Event/Audit 增量且有界，其余 mutable state snapshot/upsert、单 worker |
| Command delivery | `connector_gateway/service.py`、`persistent_service.py` | 已实现 durable pre-delivery barrier、重排与幂等 |
| Public ingress protection | `rate_limit.py`、`service.py` | 已实现 auth/Pairing 限流、key 上限、Pairing 过期清理与容量上限 |
| Local Connector | `connector/` | 已实现 discovery、pair/connect/run、WSS、outbox/receipt、owned child；默认 detection-only |
| Onboarding | `frontend/src/app/connect/page.tsx`、`deploy/install/` | 已实现浏览器授权、Windows Scheduled Task、Linux systemd user service |
| Frontend | 外部 `sunruize93-cmyk/arena402` + 本地 `frontend/` 过渡壳 | 产品 UI 已迁移但 Vercel/API 切换未验收；当前 Compose 仍使用固定 Next.js 15.5.21 壳 |
| Self-hosted deployment | `docker-compose.production.yml`、`deploy/` | 已实现单机部署资产、域名/IP TLS 和安装包托管；真实服务器 E2E 待执行 |
| Arena business durability | `arena_game/`、`arena_core/`、`db/migrations/006_*`–`012_*` | Game/Round/Pool/Pairing/Negotiation/Inventory/Ranking 已持久化；Connector 仍不可直接写入 |
| Runtime approval | `connector/internal/driver/` | 未实现完整审批闭环；生产 task 必须保持 detection-only |
| Signed release / SBOM | `deploy/artifacts/` | 未实现；当前仅 HTTPS + SHA-256 |
| Native A2A Endpoint | 未接入 | 第二方案，后续独立实现 |
| Arena typed task/result | `arena_agent_contracts/`、`arena_core/` | 统一 schema、Result Sink/Consumer/Finalizer 已实现；Connector WSS 映射仍未实现 |

## 13. 验收门槛

### 13.1 已由自动化测试覆盖的代码契约

- [x] 生产配置缺失或不安全时 fail-closed。
- [x] bootstrap invite 一次性、密码 Argon2id、Cookie 安全属性与 session revoke。
- [x] Session + CSRF、对象 ownership、跨用户 Device/Binding/Command/Event/Audit 隔离。
- [x] 公开认证与 Pairing 入口限流。
- [x] Pairing 总量上限和过期清理。
- [x] PostgreSQL 风格 repository 重启恢复、Device revoke 和旧 token 失效。
- [x] Command 在 WSS 发送前持久化，重试保持幂等。
- [x] 任意 shell/argv、跨 binding session、云端 resume token 和 stale epoch 被拒绝。
- [x] Connector event outbox、receipt fail-closed、重连和 replacement/revoke 退出语义。
- [x] 安装器拒绝远程 HTTP、拒绝降级重定向并校验 SHA-256。

### 13.2 部署前必须重新运行

```powershell
python -m pytest -q
python -m mypy --ignore-missing-imports connector_gateway

Set-Location connector
go test -count=1 ./...
go vet ./...
go build ./...

Set-Location ../frontend
npm ci
npm run build
```

还应在有 Docker daemon 的环境执行 Compose config、镜像构建、migration 和容器 health check。

### 13.3 尚未完成的真实环境验收

- [ ] 在目标服务器完成 Docker/Caddy/PostgreSQL 部署。
- [ ] 从公网验证 HTTPS/WSS；若使用裸 IP，验证 short-lived IP certificate 与续期 timer。
- [ ] 从服务器之外的 Windows/Linux 电脑运行一行安装。
- [ ] 完成 invite 注册/登录、浏览器批准、Device online 和 Runtime inventory。
- [ ] 重启 API/PostgreSQL 后验证状态恢复与未确认 Command 重排。
- [ ] 撤销 Device 后验证活跃 WSS 关闭、旧 token 无法重连。
- [ ] 验证主机只开放预期的 22/80/443，3000/5432/8000 不可公网访问。

在这些项目完成前，文档和发布说明不得声称“已部署到腾讯云”或“真实外部用户 E2E 已通过”。

### 13.4 Arena Runtime 接线验收

- [ ] Arena route 只引用当前 `connector_binding_id + binding_epoch`；
- [ ] Hosted/Local/rule Agent 接收相同 `arena.agent-task.v1`；
- [ ] Decide 只返回 `action=buy|sell|pass`；
- [ ] Negotiate 只返回 `action=propose|accept|reject`；
- [ ] dispatch ACK、terminal Result 和 Arena apply ACK 可独立恢复；
- [ ] stdout、`runtime.message` 和普通 Event 不会被解析为业务结果；
- [ ] FCFS 只使用 Result Sink 数据库时间；
- [ ] late/duplicate Result 不重复入池或写协商；
- [ ] Connector 断线超过重连窗口后由 Finalizer 产生明确 default；
- [ ] 同局各 Runtime 使用相同、经测试校准的 deadline；
- [ ] 一名 User 每局只有一个 Game Agent，活动 Game 不切换 binding；
- [ ] Connector 不接收 Hosted BYOK、钱包私钥或 PaymentMandate signer 密钥。

## 14. 后续优先级

1. 完成目标服务器部署和外部 E2E，收集安装到 online 的耗时与失败点。
2. 为 Connector artifacts 增加签名、SBOM、独立信任根和安全升级/回滚渠道。
3. 将 Runtime Event 默认收敛为 metadata-only，补 retention、删除和隐私说明。
4. 将 Connector Binding 与真实、持久化的 Arena 402 Agent registry 对接，只通过
   binding id + epoch 路由。
5. 实现版本化 Arena typed Task、显式 terminal Result、Result Sink/Consumer 和
   Deadline Finalizer 接线。
6. 将 Game/Round/Pool/Pairing/Negotiation/Inventory 从进程内状态迁移到独立业务
   持久层，并保持与支付最终性解耦。
7. 实现共享限流、WSS connection ownership、mutable state 行级增量 repository
   和多实例 drain 后再扩展 worker。
8. 完成 Codex app-server/受支持 Claude SDK 的 approval 闭环与认证兼容矩阵。
9. 以独立入口设计 Native A2A Endpoint，复用业务身份、任务关联和审计模型，但不
   复用 Device pairing 或本地 process supervisor。
