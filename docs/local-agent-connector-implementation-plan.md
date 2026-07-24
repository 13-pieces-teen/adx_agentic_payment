# Arena 402 本地 Agent Connector Implementation Plan

> 文档状态：Self-hosted beta 实施状态与后续计划
> 最后更新：2026-07-24
> 对应规格：[`local-agent-connector-spec.md`](./local-agent-connector-spec.md)
> 部署手册：[`self-hosted-connector-deployment.md`](./self-hosted-connector-deployment.md)
> 统一 Runtime 计划：[`hosted-arena-agent-implementation-plan.md`](./hosted-arena-agent-implementation-plan.md)

## 1. 交付策略

优先交付出站 Connector，把“设备如何接入、Runtime 如何发现、平台如何有限控制并观察 Connector-owned session”做成独立控制面；Native A2A Endpoint 作为后续补充入口。

当前里程碑不是继续制作未认证内存 demo，而是把已实现的 self-hosted beta 安全地部署到单台云主机，并完成服务器外部的“一次安装、一次授权、自动上线”验收。

### 1.1 当前 beta 成功定义

```text
Deploy Caddy + Next.js + FastAPI + PostgreSQL
  → Admin creates one-use invite
  → User runs one installer command
  → Connector creates pairing and opens /connect
  → User registers/logs in and approves the device once
  → Connector stores device credential and installs user autostart
  → Outbound WSS online
  → Claude/Codex inventory visible
  → User creates Connector binding
  → Detection-only runtime.probe is auditable
```

成功不包含：

- 把 Connector-only `agent_id` 当作已经注册、可交易的 Arena 402 Agent；
- 把进程内 Arena 状态当作金融生产数据；
- 默认开启 Codex/Claude task execution；
- 把 Connector Command 成功当作游戏成交或支付确认；
- 把现有自由 prompt `task.dispatch` 当作已经完成版本化 Arena Task/Result 接线；
- 宣称安装包已经签名或具有 SBOM；
- 在没有真实公网验证时宣称云主机部署和外部 E2E 已完成。

### 1.2 实施原则

- 安全默认：未定义 command 一律拒绝；无 ownership 的 session 一律不接管。
- 数据先持久化：Command 必须先通过 PostgreSQL durable barrier，才能通过 WSS 下发。
- 身份来自 Session：生产路由不信任客户端 `owner_id`。
- 本地凭据本地使用：Connector 路径不上传原始模型凭据或钱包密钥。Hosted BYOK
  是独立 write-only ingress + 外部 Secret Manager 路径，不经过 Connector。
- 状态分层：Connector control state、Arena business state、payment finality 分开。
- 单 worker 明示：不通过增加 Uvicorn worker 伪装水平扩展。
- 能力声明：Runtime task 默认 detection-only，平台按 inventory capability 执行。
- 证据分级：源码、自动化测试、Docker 构建、真实服务器 E2E 分开报告。

## 2. 当前实现范围

### 2.1 生产控制面

| 文件/目录 | 当前责任 |
|---|---|
| `connector_gateway/config.py` | 校验 PostgreSQL、HTTPS public URL、session secret、Secure Cookie、bootstrap invite hash、限流和 Pairing 容量；生产 fail-closed |
| `connector_gateway/auth.py` | invite/register/login/session/logout、Argon2id、签名 Session Cookie、CSRF |
| `connector_gateway/repository.py` | repository 契约与测试用内存实现 |
| `connector_gateway/postgres_repository.py` | PostgreSQL 用户、会话和 Gateway 状态持久化 |
| `connector_gateway/persistent_service.py` | 重启恢复、状态持久化、Command pre-delivery barrier |
| `connector_gateway/rate_limit.py` | 单 worker 有界 sliding-window limiter |
| `connector_gateway/service.py` | Pairing、Device、inventory、Binding、Command/Event、WSS 状态机 |
| `connector_gateway/api.py` | production auth Router 与 `/api/connectors` REST/WSS |
| `connector_gateway/production.py` | production composition root |
| `db/migrations/002_connector_gateway.sql` | self-hosted Connector 表、索引和唯一约束 |
| `web/api.py` | 挂载 production bundle；保护 Arena mutations；阻止遗留 DB factory 在生产启动 |

### 2.2 本地 Connector 与 onboarding

| 文件/目录 | 当前责任 |
|---|---|
| `connector/cmd/adx-connector/` | `scan`、`doctor`、`pair`、`connect`、`run`、`version` |
| `connector/internal/enrollment/` | HTTPS Pairing、浏览器 verification URL 和 exchange |
| `connector/internal/discovery/` | Claude Code/Codex 有界只读探测 |
| `connector/internal/transport/` | WSS、Device header、heartbeat、重连、Command/Event |
| `connector/internal/supervisor/` | allow-root、owned child、session ownership 和有限控制 |
| `connector/internal/store/` | 凭据、receipt、staged event、durable outbox 和 singleton lock |
| `connector/internal/driver/` | 固定 argv Claude/Codex 开发 runner；默认不启用 |
| `connector/README.md` | Connector 使用与安全边界 |
| `frontend/src/app/connect/page.tsx` | invite/register/login 和 Pairing 批准页面 |
| `frontend/src/lib/connector-api.ts` | 同源 Auth/Connector API client |
| `deploy/install/` | Windows/Linux 安装、凭据目录权限和系统自启动 |

### 2.3 部署栈

| 文件/目录 | 当前责任 |
|---|---|
| `docker-compose.production.yml` | PostgreSQL、migration、单 worker API、Next.js、Caddy、Certbot |
| `deploy/docker/` | Python 3.12 API 和 Node 22 Web 镜像 |
| `deploy/caddy/` | 域名 TLS、IP challenge bootstrap、IP certificate 配置 |
| `deploy/scripts/` | env 生成、migration、部署、artifact、IP 证书续期、备份、主机硬化 |
| `deploy/artifacts/` | 平台托管的 installer、Connector 二进制和 `.sha256` |
| `requirements/production.txt` | 带 hash 的 Python production lock |
| `frontend/package-lock.json` | Next.js 15.5.21 前端依赖锁 |

详细运行步骤不在本计划重复维护，以 [`self-hosted-connector-deployment.md`](./self-hosted-connector-deployment.md) 为准。

## 3. 已冻结契约

### 3.1 Browser Auth API

- `POST /api/auth/invite`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/session`
- `POST /api/auth/logout`

状态变更请求使用 Session Cookie + `X-CSRF-Token`。公开凭据入口按客户端 IP 限流；invite 一次性并仅以 SHA-256 摘要存储。

### 3.2 Connector REST/WSS API

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
- `WS /api/connectors/ws?device_id=...`

WSS Device token 只允许放在 `Authorization: Device <token>`，不进入 query。

### 3.3 Typed command allowlist

| Action | v1 payload |
|---|---|
| `runtime.probe` | `{}` |
| `session.start` | `{working_directory, initial_prompt?, environment_refs?}` |
| `task.dispatch` | `{session_id, prompt, request_id?}` |
| `task.cancel` | `{session_id, request_id}` |
| `session.stop` | `{session_id, reason?}` |
| `session.resume` | `{session_id}` |

以下永不进入 v1：

- `shell.exec`
- 任意 executable/argv；
- 外部 PID attach/kill；
- 云端提供 `conversation_id`/`resume_token`；
- 任意 secret value、模型凭据或钱包密钥；
- 任意文件读取/上传。

上表是当前已实现的 v1 控制协议。Arena 接线新增同一个顶层
`task.dispatch` 的严格 payload variant：

```text
task.dispatch Arena branch
  -> embedded arena.agent-task.v1
  -> explicit terminal arena.agent-result.v1
```

统一业务 action：

- Decide：`action=buy|sell|pass`；
- Negotiate：`action=propose|accept|reject`。

实现时必须把 Command persisted/delivered ACK、terminal Result 和 Arena
applied/rejected ACK 建模为三个可独立恢复的状态。不得从 stdout、
`runtime.message`、普通 Event 或 Command `succeeded` 推断 Arena action。完整
payload 见 [`local-agent-connector-spec.md`](./local-agent-connector-spec.md)；
当前代码尚未实现该 variant。

### 3.4 投递与幂等

- Pairing exchange 一次性；
- Pairing 创建不授予 owner，登录用户批准时才绑定所有权；
- `binding_id + idempotency_key` 防止重复 Command 副作用；
- Binding 变化递增 `binding_epoch`；
- Command 在任何 WSS frame 前持久化完整 Command 与 Audit；
- 重启时未确认的 `delivered` Command 变回 `queued`；
- Runtime Event 以 Device sequence 去重并累计 ACK；
- Connector 本地 receipt/outbox 写入失败时锁存 persistence-degraded，拒绝新任务并退出；
- Gateway replacement/revoke 后旧连接不能继续提交 ACK、Event 或 Inventory。
- Arena route 只引用 `connector_binding_id + binding_epoch`；旧 epoch 的 terminal
  Result 也不得被接受。
- 相同 Arena Task 的重连与重投复用原 `taskId + idempotencyKey`。
- Arena Result Sink 使用数据库 `result_received_at`，Local Runtime 自报时间不参与
  FCFS。
- Connector offline 的重连窗口为 30 秒与行动剩余时间中的较短者；超时由
  Arena Finalizer 收敛为 `pass` 或 negotiation timeout。

## 4. 分阶段实施状态

| Phase | 状态 | 当前结论 |
|---|---|---|
| Phase 0：规格与边界 | 已完成 | control/business/payment 边界、typed command 和威胁模型已固化 |
| Phase 1：Gateway MVP | 已完成 | Pairing、Device、Binding、Command/Event、WSS 与专项测试已实现 |
| Phase 2：Go Connector | 已完成 | discovery、pair/connect/run、outbox/receipt、owned child 和重连已实现 |
| Phase 3：Runtime driver | 部分完成 | fixed argv runner 可用于受控开发；生产默认 detection-only，原生 approval 待实现 |
| Phase 4：Web onboarding | 已完成 beta | `/connect`、invite/register/login、批准、状态页面和 API client 已实现 |
| Phase 5A：Self-hosted persistence/auth | 已完成 beta | PostgreSQL、Auth、CSRF、owner scope、rate limit、bounded Pairing 和 durable barrier 已实现 |
| Phase 5B：Single-host deployment assets | 已完成代码 | Docker/Caddy、domain/IP TLS、artifact 与运维脚本已实现；真实服务器部署待验收 |
| Phase 5C：GA/HA hardening | 未完成 | signed release、SBOM、共享限流、multi-worker ownership、隐私治理和容量测试待实现 |
| Phase 6：Arena typed Runtime Adapter | 未开始 | 统一 Task/Result、binding epoch、Result Sink/Finalizer |
| Phase 7：Native A2A Endpoint | 未开始 | 保持独立入口，不混入本地 Device 模型 |

## 5. 已完成的安全加固

### 5.1 生产启动 fail-closed

`create_app()` 在生产模式先构建 `ProductionConnectorBundle`，所有安全配置在服务接受流量前校验。以下条件任一不满足均拒绝启动：

- PostgreSQL URL 缺失或不是 PostgreSQL scheme；
- session secret 少于 32 字符；
- public URL 非绝对 HTTPS URL；
- 生产关闭 Secure Cookie；
- bootstrap invite hash 缺失或格式错误；
- CORS origin 使用 `*`。

`create_app_with_db()` 是遗留 Supabase/开发工厂。在 `ADX_ENV=production` 或 Connector production mode 下显式抛错，不能成为绕过 self-hosted Auth 的替代入口。

### 5.2 Auth 与 CSRF

- invite 一次性消费；
- 用户名规范化和唯一性；
- 密码 Argon2id；
- 未知用户走固定成本 dummy hash，降低 timing enumeration；
- hash 工作放在线程执行，避免阻塞 event loop；
- 随机 session token 只以摘要持久化；
- 签名、`HttpOnly + Secure + SameSite=Lax` Session Cookie；
- double-submit CSRF Cookie + header + server-side hash；
- logout、session expiry、session revoke；
- bootstrap invite 环境变量只保存 SHA-256，不保存明文。

### 5.3 对象授权

- owner 从认证 Session 推导，不信任请求 body/query；
- Device get/revoke、Binding、Command、Event 按当前用户拥有的 Device 校验；
- 跨用户对象返回 404；
- Audit 按 owner 过滤；
- Arena mutation 需要 Session + CSRF，涉及 Agent 时验证 Agent owner。

当前 `user_id` 是 beta 的 tenant-like isolation key。组织 tenant、团队成员、角色和委托授权不在当前范围。

### 5.4 公开入口有界化

- Auth 与 Pairing 分别配置 sliding-window limiter；
- limiter key 总数有 4096 上限；
- Pairing 每次创建前清理过期项；
- pending Pairing 有配置上限；
- PostgreSQL 清理已过期的 pending/approved/expired Pairing。
- Event/Audit 只追加本次增量，并在内存、启动加载和数据库中保留最近 10,000 条；ACK watermark 独立随 Device 状态持久化。

限流器是进程内实现，因此 API 只能按当前单 worker 拓扑部署。扩展到多实例前必须迁移到共享 limiter。

### 5.5 Durable pre-delivery barrier

`PersistentConnectorGateway.queue_command()` 先把 Command 放入状态机并持久化；`deliver_pending()` 在发送前再次调用 `_prepare_command_delivery()`。只有完整 Command、幂等键和 Audit 已提交 PostgreSQL，WSS 才能观察到 Command。

需要继续保持的回归测试：

- repository 写入失败时不发送 frame；
- 发送后断线时 Command 不被错误标为成功；
- 重启恢复后未确认 Command 可重排；
- 同一幂等键重试不创建第二个副作用；
- Device replacement/revoke 时旧 socket 的迟到 ACK 不改变状态。

## 6. 当前单机部署计划

### 6.1 拓扑

```text
Public 80/443
  → Caddy
      → Next.js 15.5.21
      → FastAPI / WSS, one Uvicorn worker
      → /downloads
  → PostgreSQL 17 on internal Docker network
```

目标主机 2 vCPU、4 GB RAM、70 GB 磁盘可承载当前 beta。只开放 80/443；SSH 仅允许管理员固定源地址。不得开放 3000、8000 或 5432。

### 6.2 TLS

- 优先使用域名，由 Caddy自动签发与续期；
- 没有域名时可使用 Certbot 5.7 的 short-lived IPv4 certificate；
- IP 模式先启动仅提供 ACME webroot 的 HTTP bootstrap，再切换为 Caddy 显式证书；
- systemd timer 每 6 小时检查续期；
- 纯 HTTP 只能用于 loopback/连通性 smoke，外部 Connector 会拒绝。

### 6.3 部署顺序

1. 先确认 SSH key 登录，之后再关闭密码登录。
2. 只开放必要防火墙端口。
3. 在服务器生成 `deploy/.env`；不得把 session secret、数据库密码或明文 invite 写入日志。
4. 构建或校验四个平台 Connector artifacts 和 `.sha256`。
5. 启动 PostgreSQL，执行带 advisory lock 与 checksum 的 migration。
6. 启动单 worker API、Next.js 和 Caddy。
7. 检查 health、证书、下载与 checksum。
8. 从服务器之外的电脑执行安装和授权 E2E。
9. 验证 Device revoke、服务重启和数据库恢复。

命令与回滚细节见 [`self-hosted-connector-deployment.md`](./self-hosted-connector-deployment.md)。

## 7. 验证矩阵

### 7.1 代码级自动化覆盖

| 区域 | 已覆盖契约 |
|---|---|
| Production config | 缺失 DB/secret/HTTPS/invite hash 时 fail-closed |
| Auth | invite 一次性、Argon2id、登录、session expiry/revoke、Cookie hardening、CSRF |
| Object auth | 跨用户 Device/Binding/Command/Event/Audit 隐藏，伪造 owner 无效 |
| Arena auth | mutation 需要 Session + CSRF，Agent ownership 校验 |
| Pairing ingress | rate limit、容量上限、过期清理、一次性交换 |
| PostgreSQL adapter | 状态恢复、Device revoke、Command/Event/Audit 持久化模型 |
| Durable delivery | WSS 前持久化、重试幂等、restart requeue |
| Gateway WSS | hello/inventory/command/ack/event、single sender、handover、迟到 ACK |
| Go Connector | HTTPS/WSS 限制、discovery detection-only、receipt/outbox、ownership、reconnect |
| Installer | URL 校验、拒绝降级、SHA-256、Windows/Linux 自启动 |
| Frontend | `/connect` auth/pairing、Connector console 和生产 API client |

“已覆盖”表示仓库存在并曾执行相应自动化测试，不替代提交前重新运行，也不等同于真实服务器 E2E。

Arena typed adapter 落地后追加：

| 区域 | 必须覆盖 |
|---|---|
| Contract | strict `action` union、extra fields、定点金额、schema version |
| Binding | route 只接受当前 binding epoch，撤销/替换后 stale Result 无效 |
| Delivery | duplicate dispatch 复用 Task/key；ACK 不产生业务动作 |
| Result | 显式 terminal Result、late/duplicate、Result Sink/Consumer crash recovery |
| Deadline | 同局统一可校准时间窗；Connector offline 后 Finalizer 唯一 default |
| FCFS | 只使用 Result Sink 数据库 `result_received_at` |
| Participation | 并发 join 仍只有一个 `(game_id, user_id)` Game Agent |
| Privacy | stdout/Event/CoT/本地凭据不进入 Arena Result 或公开时间线 |

### 7.2 提交前验证命令

```powershell
# Python
python -m pytest -q
python -m mypy --ignore-missing-imports connector_gateway

# Go
Set-Location connector
go test -count=1 ./...
go vet ./...
go build ./...

# Frontend
Set-Location ../frontend
npm ci
npm run build
```

Docker daemon 可用时追加：

```bash
docker compose --env-file deploy/.env -f docker-compose.production.yml config --quiet
docker compose --env-file deploy/.env -f docker-compose.production.yml build
sh deploy/scripts/deploy.sh
docker compose --env-file deploy/.env -f docker-compose.production.yml ps
```

`deploy/.env` 必须由生成脚本创建且不得提交。

### 7.3 当前验证清单

#### 仓库级

- [x] Python production Auth、object authorization、rate limit、bounded Pairing 和 durable delivery 有专项测试。
- [x] `create_app_with_db()` 生产 fail-closed 有回归测试。
- [x] Go Connector discovery/transport/store/supervisor 有单元测试。
- [x] Next.js 已固定为 15.5.21，前端使用 lockfile。
- [x] Docker Compose、Dockerfile、Caddy、migration、installer 和运维脚本已进入仓库。
- [x] 文档不再把 production Gateway 描述为“仅内存、无认证、Phase 5 未实现”。

#### 目标服务器与外部 E2E

- [ ] Docker daemon 上完成所有 production image build。
- [ ] 目标服务器完成 Compose deployment 和 health check。
- [ ] 域名或裸 IP TLS 从公网验证通过。
- [ ] IP TLS 模式的续期 timer 验证通过。
- [ ] 外部 Windows 安装、浏览器授权、Scheduled Task 自启动通过。
- [ ] 外部 Linux 安装、浏览器授权、systemd user 自启动通过。
- [ ] Device online、Runtime inventory、Binding 和 detection-only probe 通过。
- [ ] API 重启后 Device/Binding/Command/Event 状态恢复通过。
- [ ] Device revoke 后活跃 WSS 关闭、旧 token 拒绝通过。
- [ ] 端口扫描确认 3000/5432/8000 未公网暴露。

在上述目标服务器项目完成前，不得将部署状态写成已完成。

## 8. 尚未完成与优先级

### P0：部署与真实 E2E

- 完成 SSH key、主机防火墙和 Docker 安装；
- 执行单机部署；
- 从服务器之外完成安装、授权、WSS 和重启恢复；
- 记录从安装开始到 Device online 的耗时、失败率和人工步骤。

### P1：发布供应链

- 为四个平台二进制签名；
- 生成并发布 SBOM；
- 使用独立签名信任根，不只依赖同源 `.sha256`；
- 设计安全 updater、分阶段 rollout 和回滚；
- 增加发布 provenance。

### P1：业务持久化边界

- 将真实 Arena 402 Agent registry 与 Connector Binding 对接；
- Arena route 只引用 Connector-owned `connector_binding_id + binding_epoch`，
  不复制 Device/Runtime/Session 权威；
- 数据库原子保证一名 User 每局只有一个 Game Agent，并在入局时冻结 binding epoch；
- 持久化 Game、Game Agent、Round、AgentTask/Result/Attempt、Pool、Pairing、
  Negotiation、Inventory 与业务 Audit；
- 保持 Runtime telemetry、Business Event 和 Payment finality 三类权威来源分离；
- 实现版本化 `arena.decide` / `arena.negotiate` payload 和统一 `action` union；
- 增加显式 terminal AgentTaskResult；Connector 只负责有界投递和结构化结果，
  不解释游戏规则；
- 接入 Arena Result Sink/Consumer：过滤公开 message、数据库生成接收时间、
  唯一 Result 与最多一次业务 apply；
- 接入独立 Deadline Finalizer；Connector 整体离线时 Decide 收敛为唯一 `pass`，
  Negotiate 收敛为唯一 timeout；
- 同一 Game 的 Hosted/Local/rule Runtime 使用同一、经真实 P95/P99 和负载测试
  校准的 `action_timeout_ms`；每个逻辑 AgentTask 最多一次重试；
- `accept` 只进入 pending settlement；PaymentMandate/链上确认与库存提交仍由
  Settlement/Arena 负责；
- 未完成前禁止真实资金或不可逆金融动作。

### P1：隐私与审计

- 默认在本地只采集 `metadata_only`；
- 为 redacted/full content 增加用户授权；
- 按类型实现 retention、删除与导出；
- 在现有增量 append-only 写入基础上补 tamper evidence；
- 增加敏感数据泄漏测试。

### P2：高可用与扩展

- 将 Pairing/Device/Binding/Command 的 mutable snapshot 改为行级增量事务；
- 使用共享 WSS connection ownership/lease；
- 使用共享 rate limiter；
- 支持 drain/redeploy；
- 完成容量、长连接和故障注入测试后再增加 worker/replica；
- 评估 Redis/broker，但不在没有需求证据时先引入。

### P2：Runtime task

- Codex app-server thread/turn/item/approval；
- Claude 受支持 SDK/CLI 和可验证 auth probe；
- Runtime 版本/permission 兼容矩阵；
- Linux/macOS descendant crash containment；
- 平台 approval 与本地 permission 双层 gate。

在这些项目完成前，production installer 和 service 必须默认 detection-only。

## 9. Rollout 与回滚

### 9.1 Rollout

1. 内部运营账号：登录、Pairing、inventory、revoke。
2. 内部设备：一行安装和自启动，仅 detection-only。
3. 小范围邀请用户：观测安装成功率、WSS 稳定性和支持成本。
4. 完成签名/SBOM后扩大安装范围。
5. 完成业务持久化和金融安全评审后，再讨论交易链路。
6. Arena typed task/result 必须先以 rule/fake Runtime 验证，再灰度本地 Runtime。
7. Runtime task 必须独立灰度，不随 Connector online 自动开启。

### 9.2 回滚

平台侧：

- 停止签发 invite 和创建新 Pairing；
- 保持 heartbeat/inventory 只读，停止新 Command；
- 回滚 API/Web 镜像但不回滚已完成交易或支付记录；
- migration 只前滚；需要恢复时使用数据库备份并记录恢复点。

本地侧：

- Device revoke 使旧 token 失效并关闭 WSS；
- Connector 收到 revoke/replacement 后退出，停止自有 task；
- 使用已签名的前一版本回滚是目标能力；当前只有 checksum，不应承诺安全自动回滚；
- 卸载本地服务不自动删除平台业务历史。

业务侧：

- Connector 不可用只应把 execution 标记为 unknown/degraded；
- Arena-owned Finalizer 仍必须为每个到期逻辑行动写出确定性 default；
- 不得自动把支付、成交或交付状态回滚；
- 重试必须保持业务 idempotency key，避免重复交付或付款。

## 10. Native A2A Endpoint 后续方案

Native A2A Endpoint 适合已经运行在公网或可被平台代理访问、并原生暴露 A2A Agent Card/Endpoint 的 Agent。它是第二种 transport，不是本地 Connector 的简化模式。

可复用：

- Arena 402 Agent identity 与 ownership；
- versioned AgentTask/Result、correlation/idempotency/Result Sink/audit；
- capability 与 event projection；
- matching/negotiation/payment 的业务边界。

不可复用：

- Device Pairing 和本地 Device credential；
- Runtime executable discovery；
- 本地 process supervisor/session ownership；
- Connector heartbeat lease 和本地 outbox。

后续模块建议：

- `a2a_ingress/agent_cards.py`
- `a2a_ingress/endpoints.py`
- `a2a_ingress/client.py`
- `a2a_ingress/security.py`
- `a2a_ingress/events.py`

Native A2A 的主要风险是 SSRF、Endpoint ownership、Agent Card 恶意内容、协议版本语义差异和远端审计粒度不足。它不得复用 Device token，也不得假装拥有本地 session 级控制。

Native A2A 仍由 Arena Gateway 调用远端 Endpoint，不能允许 Agent 绕过 Arena 直接
通信。远端 Task success 只是一条候选 Result，不是合法交易或支付证明。
