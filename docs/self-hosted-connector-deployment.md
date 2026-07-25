# Arena 402 Connector 云端控制面部署与运维

> 前端迁移说明（2026-07-25）：产品前端权威已迁移到
> [`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，目标由
> Vercel 部署。本文的 Next.js/Caddy web 拓扑仍描述当前可运行的 Compose 过渡栈；
> 外部前端与 API/CORS 切换验收完成后需要删除该 web service 并更新本文。

本文档描述当前仓库的单机生产拓扑，目标是让外部用户完成“一次安装、一次授权、自动上线”。部署不依赖 Supabase：Connector 身份、配对、设备、Runtime、命令、事件和审计数据均落在自托管 PostgreSQL。

这套部署只覆盖 Connector/Gateway 控制面，不会部署完整 Arena 402 游戏。
Game/Round/Pairing/Negotiation/Inventory 业务库、游戏前端和 Injective
Settlement 需要按 [`roadmap.md`](roadmap.md) 单独集成。Device online 或
Command succeeded 也不能作为成交或链上支付确认。

## 1. 目标拓扑

```text
Internet
  |
  | TCP 80/443, UDP 443
  v
Caddy
  |----------------------|
  v                      v
Next.js :3000        FastAPI :8000
                         |
                         v
                   PostgreSQL :5432
```

- 只有 Caddy 映射主机端口。Next.js、FastAPI、PostgreSQL 仅存在于 Docker 网络。
- `edge` 网络连接 Caddy、Web 和 API；`data` 是 `internal: true` 的数据库网络。
- API 固定为一个 Uvicorn worker，因为在线 WebSocket 注册表当前仍在进程内；业务状态由 PostgreSQL 持久化。
- Event/Audit 按增量写入并仅保留最近 10,000 条；这是一套单机 beta 留存策略，不是不可篡改金融审计仓库。
- 容器配置了健康检查、内存/CPU 上限、只读根文件系统（适用服务）以及 Docker 日志轮转。
- Connector 安装包由 Caddy 从只读目录提供：`/downloads/adx-connector-{windows|linux}-{amd64|arm64}[.exe]`，每个文件同时提供 `.sha256`。

2 vCPU、4 GB RAM、70 GB 磁盘可承载当前单机 MVP。它不是高可用拓扑；生产增长后应拆分数据库、对象存储和 WebSocket 连接层。

## 2. TLS 模式

### 2.1 域名（推荐）

`ADX_TLS_MODE=domain` 时，Caddy 自动申请和续期证书。将域名 A 记录指向主机公网 IP，然后生成环境文件：

```bash
sh deploy/scripts/generate-env.sh arena.example.com ops@example.com
```

中国大陆地域用域名对公网提供 Web 服务前，应先确认备案和接入要求。腾讯云说明见 [轻量应用服务器网站备案](https://cloud.tencent.com/document/product/1207/44376/)。

### 2.2 裸 IPv4

`ADX_TLS_MODE=ip` 时，部署脚本按以下顺序执行：

1. 用 `Caddyfile.ip-bootstrap` 在 HTTP 80 只提供 Webroot challenge；其他路径一律返回 `503`，不会以明文代理 API、安装包或网页。
2. 通过 Certbot 5.7 请求 `shortlived` profile 的 IP 地址证书。
3. 切换到 `Caddyfile.ip`，显式加载 `fullchain.pem` 和 `privkey.pem`。
4. 安装每 6 小时运行一次的 systemd 续期检查；续期后热加载 Caddy。

Let’s Encrypt 的 IP 证书已正式可用，只能使用短期证书，当前有效期为 160 小时。Certbot 的 Webroot IP 支持要求 5.4 或更新版本。参考 [Let’s Encrypt GA 公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability.html) 和 [Certbot IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)。

```bash
sh deploy/scripts/generate-env.sh 203.0.113.10 ops@example.com
```

仓库和示例中不得写入真实 IP、邮箱、密码或会话密钥。

### 2.3 仅 HTTP 烟雾测试

```bash
sh deploy/scripts/generate-env.sh --http 203.0.113.10
```

这会把 `ADX_ENV` 设为 `development` 并关闭 Secure Cookie，仅用于浏览器/IP 连通性检查。外部 Connector 拒绝非回环 HTTP/WS，因此该模式不能作为用户接入环境。

## 3. 主机准备

腾讯云安全组/防火墙只开放：

- SSH：仅允许管理员固定源 IP；
- TCP 80；
- TCP 443；
- UDP 443（HTTP/3，可选；若不开放，HTTPS 仍可走 TCP）。

不要开放 3000、5432 或 8000。腾讯云操作说明见 [轻量应用服务器防火墙](https://cloud.tencent.com/document/product/1207/44577/)。

Ubuntu 首次安装 Docker：

```bash
sudo sh deploy/scripts/bootstrap-host.sh
```

脚本使用 Docker 官方 Ubuntu APT 源并安装 Engine、Buildx、Compose plugin。若当前用户刚加入 `docker` 组，请退出并重新登录。

## 4. 首次部署

```bash
# 1. 在仓库根目录生成 chmod 600 的服务器专用环境文件。
sh deploy/scripts/generate-env.sh <domain-or-ip> [acme-email]

# 2. 检查模式和公开地址；不要把完整文件贴到工单或聊天。
grep -E '^(ADX_TLS_MODE|ADX_PUBLIC_HOST|ADX_PUBLIC_APP_URL)=' deploy/.env

# 3. 构建四个平台 Connector 安装包、迁移数据库并收敛所有服务。
sh deploy/scripts/deploy.sh
```

`generate-env.sh` 会生成 URL-safe 的 PostgreSQL 密码、会话签名密钥和一次性 bootstrap invite。环境文件只保存 invite 的 SHA-256，不保存明文；明文只在终端显示一次，应立即存入密码管理器。若 `deploy/.env` 已存在，脚本拒绝覆盖。

自动化部署不应把 invite 打进任务日志，可改为：

```bash
sh deploy/scripts/generate-env.sh \
  --invite-output-file "$HOME/.adx-bootstrap-invite" <domain-or-ip> [acme-email]
```

目标文件必须事先不存在，脚本以 `0600` 创建；管理员读取并安全保存后应删除该临时文件。

部署脚本是幂等的：

- 先执行 `docker compose config --quiet`；
- 构建或校验 Connector 安装包与 SHA-256；
- 等待 PostgreSQL 健康；
- 使用 advisory lock 和迁移校验和执行 `002_connector_gateway.sql`；
- 仅在迁移成功后启动单 worker API、Web 和 Caddy；
- IP 模式自动处理证书与续期 timer。

如由 CI 上传已签名的安装包，将 `ADX_BUILD_CONNECTOR_ARTIFACTS=false`，并把四个二进制、四个对应的 `.sha256`、`install.sh` 和 `install.ps1` 放到 `deploy/artifacts/`。脚本会重新计算并校验每个二进制的 SHA-256，而不只检查文件是否存在。

### 4.1 外部用户的一行安装

Linux：

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://<domain-or-ip>/downloads/install.sh \
  | sh -s -- --server https://<domain-or-ip>
```

Windows PowerShell：

```powershell
$u = "https://<domain-or-ip>/downloads/install.ps1"; `
$s = (iwr -UseBasicParsing -MaximumRedirection 0 $u).Content; `
& ([scriptblock]::Create($s)) -Server https://<domain-or-ip>
```

安装器会校验下载地址、拒绝降级重定向、核对二进制 SHA-256，并打开同源 `/connect` 页面。首次使用 bootstrap invite 时，用户同时创建可再次登录的用户名和至少 12 位密码；批准一次设备后，Connector 保存最小设备凭据并注册系统自启动。默认仅检测 Runtime，不开放 Codex 任务执行；需要远程任务能力时必须显式传入 `--enable-codex-tasks` / `-EnableCodexTasks`。

浏览器会话默认有效期为 7 天（`ADX_CONNECTOR_SESSION_TTL_SECONDS=604800`）；会话过期不影响已授权 Connector 的设备凭据，用户使用首次兑换 invite 时创建的用户名和密码重新登录即可。

### 4.2 为其他外部用户签发邀请

Bootstrap invite 只用于创建首个运营账号。之后每位外部用户使用独立、限时、一次性的 invite：

```bash
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  exec api python -m connector_gateway.invite_cli --persist --ttl-hours 24
```

命令从容器环境读取数据库 URL，不把数据库凭据放进参数或 shell 历史；明文 invite 只显示在当前管理员终端。通过密码管理器单独交付给目标用户，不要贴到部署日志、群聊或工单。invite 被使用一次或超过有效期后失效。

## 5. 验收

```bash
docker compose --env-file deploy/.env -f docker-compose.production.yml ps
curl -fsS "$(sed -n 's/^ADX_PUBLIC_APP_URL=//p' deploy/.env)/api/health"
```

安装包检查：

```bash
base_url="$(sed -n 's/^ADX_PUBLIC_APP_URL=//p' deploy/.env)"
curl -fLO "${base_url}/downloads/adx-connector-linux-amd64"
curl -fLO "${base_url}/downloads/adx-connector-linux-amd64.sha256"
sha256sum -c adx-connector-linux-amd64.sha256
```

IP 证书模式还应检查：

```bash
systemctl status adx-ip-cert-renew.timer --no-pager
systemctl list-timers adx-ip-cert-renew.timer --no-pager
docker compose --env-file deploy/.env -f docker-compose.production.yml \
  run --rm --no-deps certbot certificates
```

最终 E2E 验收应从服务器之外的一台电脑执行：

1. 下载与系统架构匹配的 Connector 和 `.sha256`，先校验摘要。
2. 运行一次安装/连接命令。
3. 浏览器自动打开同源授权页，使用仅显示一次的 bootstrap invite 创建首个账户。
4. 批准设备后，Connector 获得设备凭证并建立 WSS。
5. Arena 能看到设备、Runtime inventory、心跳、命令事件与审计记录。
6. 撤销设备后，现有 WSS 被拒绝且旧设备 token 不能再次上线。

## 6. SSH 与主机防火墙硬化

先安装本机公钥，并在第二个终端确认“只使用密钥”能够新建 SSH 会话。保留原会话，再运行：

```bash
sudo sh deploy/scripts/harden-host-access.sh \
  --cloud-firewall-verified \
  --i-have-tested-key-login
```

脚本会：

- 验证非 root 用户确有 `authorized_keys`；
- 写入 sshd drop-in，禁用 Password 和 Keyboard Interactive；
- 先运行 `sshd -t`，失败则恢复旧配置；
- reload 而不是 restart sshd，保留当前会话；
- 配置 UFW 默认拒绝入站，只新增 OpenSSH、80/TCP、443/TCP、443/UDP。

默认保留已有 UFW 规则并在末尾展示，便于人工发现多余端口。确认这是全新规则集且需要清空旧规则时，额外传 `--reset-ufw`；该选项会删除原 UFW 规则，必须先核对云防火墙和 SSH key。

完成后再次打开新的 key-only SSH 会话，再修改/轮换旧登录密码。任何密码都不应进入命令行参数、环境文件、仓库、日志或聊天。

## 7. 升级与回滚

升级前先备份：

```bash
sh deploy/scripts/backup.sh
git fetch origin
git switch main
git pull --ff-only
sh deploy/scripts/deploy.sh
```

部署脚本会复用卷和未变化镜像层。Migration 文件一旦应用，其 SHA-256 不得修改；应新增更高编号 migration。

应用回滚可以切回旧 Git revision 后重新运行 `deploy.sh`。数据库 migration 默认只向前，应用回滚前必须确认旧代码兼容新 schema，不能把 Git 回滚等同于数据库回滚。

## 8. 备份与恢复

手工备份：

```bash
sudo sh deploy/scripts/backup.sh
```

默认写到 `/var/backups/adx`，权限为 700/600，保留 14 天。备份包含用户、设备、事件和审计敏感数据，应复制到加密的异地主机/对象存储；Docker volume 本身不是备份。

恢复会覆盖数据库，应先停止 API/Web、保留当前备份并在维护窗口操作：

```bash
docker compose --env-file deploy/.env -f docker-compose.production.yml stop api web
gunzip -c /var/backups/adx/<backup>.sql.gz | \
  docker compose --env-file deploy/.env -f docker-compose.production.yml \
  exec -T postgres sh -eu -c \
  'pg_restore --clean --if-exists --no-owner --no-privileges \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
sh deploy/scripts/deploy.sh
```

至少每月做一次隔离环境恢复演练；只检查备份文件存在并不能证明可恢复。

## 9. 常用诊断

```bash
docker compose --env-file deploy/.env -f docker-compose.production.yml ps
docker compose --env-file deploy/.env -f docker-compose.production.yml logs --tail=200 caddy
docker compose --env-file deploy/.env -f docker-compose.production.yml logs --tail=200 api
docker compose --env-file deploy/.env -f docker-compose.production.yml logs --tail=200 postgres
docker stats --no-stream
df -h
```

安全边界：

- 不得把 `ADX_CONNECTOR_UNSAFE_DEMO` 改成 `true`；
- 不得把 PostgreSQL/API/Web 端口映射到公网；
- 除 `generate-env.sh` 首次生成时的一次性提示外，不得在日志中打印 session secret、bootstrap invite、设备 token 或用户输入的 Agent 凭证；
- `deploy/.env`、数据库备份和 Certbot 私钥必须按密钥材料管理；
- 证书、容器和 `/api/health` 正常并不等于完整 E2E 通过，仍需从外部机器完成安装、授权、WSS 上线、命令和撤销测试。
