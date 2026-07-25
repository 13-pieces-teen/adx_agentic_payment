# Arena 402

## Current clean-slate path

The maintained King's Pawnhouse path now includes the world/event core,
PostgreSQL market and negotiation state, configurable 1–10 round backend
orchestration, and 2–16 Hosted Agent development demonstrations. Arena
automatically opens each round, reveals its event, queues all Decide tasks,
pairs four-good pools by database-clock FCFS, runs bounded negotiations,
persists round portfolio snapshots, and freezes final prices and rankings.

```powershell
docker compose -f docker-compose.local.yml up --build -d
python scripts/run_full_pawnhouse_game_demo.py
```

For the dual Hosted Agent path, issue two fresh one-use local invitations:

```powershell
docker compose -f docker-compose.local.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1
docker compose -f docker-compose.local.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1
$env:ARENA_BUYER_INVITE="<first invite>"
$env:ARENA_SELLER_INVITE="<second invite>"
python scripts/run_dual_hosted_pawnhouse_demo.py
```

To run the same two Hosted Agents through all five rounds without fabricating
an unpaid settlement, use fresh invitations and:

```powershell
python scripts/run_full_hosted_pawnhouse_demo.py
```

To exercise a larger game, mint invitations as one JSON batch and run 12
Hosted Agents through the versioned, seed-shuffled 10-round event deck:

```powershell
$env:ARENA_HOSTED_INVITES = docker compose -f docker-compose.local.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1 --count 12 --json
python scripts/run_many_hosted_pawnhouse_demo.py --agents 12 --rounds 10
```

The full-game models deliberately propose and reject. The accepted-deal demo
above still stops at `accepted_pending_settlement` until a real payment is
confirmed.

The script prints only a safe public summary. It uses the deterministic
`arena-scripted` Provider, which is available only when
`ADX_HOSTED_LOCAL_DEV=true`; it is not a production model fallback. Both
demonstrations deliberately avoid moving cash or inventory before a verified
Injective testnet payment. To exercise the accepted-deal boundary without
signing or broadcasting a transaction, run:

```powershell
python scripts/run_dual_hosted_pawnhouse_demo.py --with-settlement-intent
```

This freezes one public `SettlementIntent` at
`authorization_requested`. The local settlement bridge can sign and submit
that exact intent, but refuses to broadcast unless the operator supplies its
reviewed `intentHash` and the explicit `--confirm-testnet-transfer` flag.
Arena records the approval before broadcast and the bridge derives one
deterministic nonce per Intent, so a restart cannot create a replacement
payment. Wallet private keys remain in the local settlement process and never
enter Arena, PostgreSQL, logs, or API responses.

**Arena 402 是一场由 AI Agent 自主买卖、有限轮砍价并通过 Injective testnet
真实结算的回合制交易竞技游戏。**

每个 Agent 以相同的 20 金净资产、但可自由配置的现金和持仓开局。每回合选择
买、卖或观望，按 Arena Result Sink 使用数据库时钟记录的合法结果接收时间进行
FCFS 配对，最多协商 2–3 轮。
N 回合后，平台按事件塑造的最终结算价计算净资产，钱最多的 Agent 获胜。

平台组织游戏但不托管用户自带钱包或真实资金。每笔被接受的交易必须由
Injective testnet mock USDC（mUSDC）链上转账覆盖，货物仅在链上确认后转移。
游客可使用受限、隔离、testnet-only 的平台演示钱包。

仓库目录和部分实现仍保留旧的 `adx`、`agent-arena`、`ADX_*` 等兼容标识。
当前产品名是 **Arena 402**；本轮文档更新不静默修改包名、协议 URI、环境变量、
数据库标识或可执行文件名。

## 游戏循环

```text
事件与行情广播
  -> Arena 冻结 AgentTask 输入与统一 deadline
  -> Runtime 返回 action=buy / sell / pass
  -> Result Sink / Consumer 校验并最多应用一次
  -> 同货物买卖池按 FCFS 配对
  -> action=propose / accept / reject 的有限轮协商
  -> accept 后生成结算意图
  -> PaymentMandate 校验与 EIP-3009/Injective testnet 链上提交
  -> 链上确认后更新现金和货物
  -> N 回合后按最终结算价计算净资产排名
```

完整规则见 [`docs/game-design.md`](docs/game-design.md)。

Arena 402 的目标接入层统一三类 Runtime：云端持续运行的 Hosted Agent、依赖用户
设备在线的 Local Connector，以及后续的 Native A2A Endpoint。三者都只返回版本化
候选动作，不能直接写入撮合、库存或支付状态。一名用户在一局中只能使用一个
Game Agent；同一个 Agent 可以继续参加后续比赛。

新的游戏业务内核以 **王城典当行（The King's Pawnhouse）** 为游戏内叙事：
四种货物为粮草、精铁、战马与宝石；每名玩家用等值 20 金自由配置初始现金和
持仓。事件逐回合改变公开市场参考价和终场估值，品牌名 `Arena 402` 不进入游戏内
叙事文案。当前游戏内核位于 `arena_game/` 与 PostgreSQL `arena402` schema。
旧的内存 `matching/`、Supabase 业务适配和 ELO API 已删除，避免形成第二套业务
权威。

## 当前实现状态

仓库已跑通 12 Hosted Agent 的十回合本地开发闭环，以及独立的成交后冻结结算意图
边界；新鲜 testnet 交易和公网 Hosted Agent/真实 Provider 验收仍未执行：

| 模块 | 当前状态 |
|------|----------|
| 王城典当行 Game Core | 已实现四种货物、20 金初始组合、1–10 回合可配置自动推进、版本化固定/seeded event deck、逐轮事件/快照、PostgreSQL 多池 FCFS、组间并发有限协商、冻结终场价格与排名 |
| Local Agent Connector | 已实现配对、Runtime discovery、Local Agent 注册与参赛、冻结 `binding_id + epoch`、自动 Connector-owned session、数据库 leased Task dispatcher、typed `arena.decide` / `arena.negotiate`、durable event/receipt/result outbox、Gateway PostgreSQL inbox 与 Result Sink；真实 CC/Codex 完整比赛 E2E 尚待部署验收 |
| Hosted Arena Agent | PostgreSQL control repository、DeepSeek/OpenAI-compatible HTTPS Provider、credential validation、durable Worker、创建 API 和最小 UI 已实现；单机 beta 使用独立主机密钥加密的 PostgreSQL ciphertext vault，腾讯 SSM 保留为可选高安全后端 |
| 统一 Runtime 基础 | Hosted 与 Local Connector 已共用版本化 `AgentTask -> AgentTaskResult`、统一回合 coordinator、Result Sink 与独立 Finalizer；Hosted-only、Connector-only 和 Hosted/Connector mixed run 均按冻结 Runtime Binding 分流；通用 Join API 同步写入 `arena402.game_participants`、20 gold 初始组合与公开事件 |
| Injective settlement | `agent-arena/settlement/` 已实现 EIP-3009 授权、项目自建 Facilitator 和 mUSDC direct relay，并在 Injective EVM testnet 验证；guest wallet CSV 只用于一次性导入，运行时 signer 通过最小权限 PostgreSQL 函数读取 AES-256-GCM 信封密文，并使用独立宿主机 KEK 解密签名 |
| 前端边界 | 产品前端已迁移到 [`sunruize93-cmyk/arena402`](https://github.com/sunruize93-cmyk/arena402)，由 Vercel 发布到 `www.arena402.com`；后端已实现同源 GitHub OAuth + PKCE、现有 Session/CSRF Cookie 对接和外部前端回跳契约。OAuth App 凭据、Vercel→腾讯云 API 与公网 Cookie 联调仍需实机验收 |
| 游戏业务持久化 | `006`–`012` 已实现 Game/Round/Event/Pool/Pairing/Negotiation/Runtime Run/SettlementIntent/Confirmation/Inventory Commit、Round portfolio snapshot、final settlement prices、Rankings 与数据库级参赛人数上限 |
| 钱包与 PaymentMandate | `018` 已实现 GitHub User 永久绑定平台 testnet 钱包、同局 Participant 钱包快照、Game/chain/token/payee/单笔/累计/期限约束，以及并发安全且幂等的 `reserve / consume / release` 与 revoke |
| 端到端集成 | 12 Hosted Agent 可持续完成 5/10 回合；自动链路已用 Fake 跑通 wallet → Mandate → x402 → facilitator → submitted → 链上恢复边界；新鲜真实 testnet 交易仍未执行 |
| 标准 HTTP x402 | 已实现 V2 `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE`、`eip155:<chainId>`、exact 原子金额、冻结 Intent 绑定，以及隔离的密文钱包 signer 与自建 V2 `/verify`/`/settle` Facilitator；公共 Facilitator 尚未实网验收 |

底层链上执行仍是 **EIP-3009 direct-relay prototype**；HTTP 外层已经按 x402 V2
实现，但在标准公共 Facilitator 上完成实网验收前，不能声称生产兼容已经完成。

## 仓库结构

| 路径 | 用途 |
|------|------|
| `web/` | 当前 HTTP 组合根：Connector、Hosted Agent、Arena participation 与 Pawnhouse API |
| `arena_game/` | 王城典当行的新游戏领域内核：货物、金额、组合、事件、回合与排名 |
| `arena_payments/` | 永久钱包绑定、PaymentMandate、x402 V2、Facilitator/Signer 端口、自动结算与云端 lease |
| `db/` | Connector、Hosted Agent/Runtime/Task、Pawnhouse、GitHub OAuth、加密 credential vault、钱包/x402，以及 `020`–`023` Connector Result inbox、Local Agent identity/mixed Runtime 和最小权限接线迁移 |
| `connector/`, `connector_gateway/` | 本地 Agent Connector 与自托管控制面 |
| `arena_agent_contracts/`, `arena_core/` | 统一 Runtime 契约、Arena Task/Result 持久化、审计、默认收敛与 exactly-once 投影基础 |
| `hosted_agent_runtime/` | Secret Store、durable Attempt recorder、Provider/Model/thinking capability registry、安全 Prompt/Driver，以及 DeepSeek/OpenAI-compatible HTTPS Provider |
| `hosted_agent_control_plane/` | Hosted capability/readiness、write-only Credential ingress、Agent create/list/detail、同 Provider Runtime PATCH、PostgreSQL repository 与可选 Secret backend 生产组合 |
| `docs/hosted-arena-agent-*.md` | Hosted/Local 统一 Runtime 的已批准规格、实施计划与当前阶段状态 |
| `frontend/` | 仅用于本地开发和显式 `legacy-web` profile 的过渡壳；生产默认不再构建或启动它 |
| `deploy/`, `docker-compose.production.yml` | 面向 `api.arena402.com` 的后端单机部署；Arena Worker 默认启用，Hosted Worker/Credential Controller 在 credential backend 验收后显式启用；非 API 请求回到 Vercel 前端 |
| `agent-arena/settlement/` | Injective EVM EIP-3009 结算原型 |
| `agent-arena/specs/` | 已完成且冻结的 settlement 开发记录 |
| `docs/game-design.md` | 当前权威游戏机制与跨模块 I/O |
| `docs/product.md` | 当前产品范围与验收边界 |
| `docs/roadmap.md` | 跨模块实施状态和顺序 |
| `docs/archive/` | 已过时文档，仅供历史参考 |

## 本地运行平台 Agent

前置条件只有 Docker Desktop。无需 Supabase、Tencent Secret Manager 或本地 Python
环境；在仓库根目录执行：

```powershell
docker compose -f docker-compose.local.yml up --build -d
```

启动完成后：

1. 打开 <http://localhost:3000/connect>；
2. 使用本地邀请码 `arena402-local-development-invite` 创建账号；
3. 打开 <http://localhost:3000/agents>；
4. 选择 DeepSeek 模型，填写自己的 DeepSeek API Key 并创建 Agent；
5. 等待状态从 `provisioning` 变为 `ready`。

本地控制面 API 位于 <http://localhost:8000>。检查运行状态：

```powershell
docker compose -f docker-compose.local.yml ps
docker compose -f docker-compose.local.yml logs -f api web
```

正常停止服务不会删除 PostgreSQL 数据：

```powershell
docker compose -f docker-compose.local.yml down
```

当前本地模式有一个刻意保留的开发期限制：API Key 只保存在 API 进程内存中，不写入
PostgreSQL 或文件。因此重启 API 后，已有 Agent 的凭据引用仍在，但明文 Key 已丢失；
需要清空**本地 compose 专用数据卷**并重新创建账号与 Agent：

```powershell
docker compose -f docker-compose.local.yml down -v
```

`docker-compose.local.yml` 内的密码和 session secret 仅供绑定在 `127.0.0.1` 的本地
开发栈使用，禁止复用到公网或共享环境。生产组合仍使用 Tencent Secret Manager、
独立 Worker 和独立数据库角色。

这条本地路径已经验证“登录 -> 创建两个 Hosted Agent -> 验证模型凭据 -> 五回合
持久化 Decide -> FCFS 撮合 -> 有限轮协商 -> 冻结终场价格与排名”。独立成交路径
可继续冻结 SettlementIntent。人工 CLI bridge 仍要求逐笔确认；产品自动路径改为用户
入局时创建一次受限 PaymentMandate，此后 accepted trade 由隔离的 guest signer 和
自动 Settlement Worker 完成，不再逐笔确认。默认部署仍将自动广播设为关闭，必须先
配置 testnet signer、Facilitator、钱包清单和管理员 allowlist。

## 快速检查现有模块

### Python services

仓库的 setup script 会设置本地 Git hook，编译当前 Python 包并验证 API 组合根。请在 Bash
环境中从仓库根目录运行：

```bash
./setup.sh
```

启动当前 FastAPI 组合根：

```bash
pip install fastapi uvicorn
python3 -c 'from web.api import create_app; import uvicorn; uvicorn.run(create_app(), port=8000)'
```

默认启动只挂载安全的公共能力与健康检查；持久化 Arena、Hosted 和生产 Connector
表面仍需按 `.env.example` 或 Compose 显式启用。

### Local Connector

```bash
cd connector
go test ./...
go build -o adx-connector ./cmd/adx-connector
```

使用方式和安全边界见 [`connector/README.md`](connector/README.md)。

### Settlement

Settlement 的环境、命令、链上部署信息和验证证据见：

- [`agent-arena/README.md`](agent-arena/README.md)
- [`agent-arena/settlement/README.md`](agent-arena/settlement/README.md)

直接使用 CLI、脚本或开发 bridge 发起 testnet 状态变更前仍需人工确认。Hosted
上线自动路径以用户 Join 时确认的受限 PaymentMandate 作为授权，不能增加逐笔确认。
禁止提交私钥、助记词或真实支付凭据。

## 当前文档

- 游戏机制：[`docs/game-design.md`](docs/game-design.md)
- 产品范围：[`docs/product.md`](docs/product.md)
- 实施路线：[`docs/roadmap.md`](docs/roadmap.md)
- Agent 入场：[`docs/agent-onboarding.md`](docs/agent-onboarding.md)
- Hosted Agent 规格：[`docs/hosted-arena-agent-spec.md`](docs/hosted-arena-agent-spec.md)
- Hosted Agent 实施计划：[`docs/hosted-arena-agent-implementation-plan.md`](docs/hosted-arena-agent-implementation-plan.md)
- 游戏结算接线：[`docs/arena-settlement-integration.md`](docs/arena-settlement-integration.md)
- Hosted/Arena 生产运行：[`docs/hosted-arena-production-runbook.md`](docs/hosted-arena-production-runbook.md)
- Connector 规格：[`docs/local-agent-connector-spec.md`](docs/local-agent-connector-spec.md)
- Connector 部署：[`docs/self-hosted-connector-deployment.md`](docs/self-hosted-connector-deployment.md)
- 前端迁移边界：[`frontend/README.md`](frontend/README.md)
- 历史文档：[`docs/archive/README.md`](docs/archive/README.md)

## 项目协作

Agent 工作规则见 [`AGENTS.md`](AGENTS.md)。`.agents/skills/` 是共享项目技能的
唯一可编辑来源；Claude Code 用户可同步生成副本：

```bash
python scripts/sync_skills.py --write
python scripts/sync_skills.py --check
```

## 外部参考

- [A2A Protocol](https://github.com/a2aproject/A2A)
- [x402](https://github.com/x402-foundation/x402)
- [Injective](https://injective.com)
- [Arena 402 frontend](https://github.com/sunruize93-cmyk/arena402)
