# Arena 402

## Current clean-slate path

The maintained King's Pawnhouse path now includes the world/event core,
PostgreSQL market and negotiation state, complete five-round backend
orchestration, and dual Hosted Agent development demonstrations. Arena
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
叙事文案。新内核位于 `arena_game/` 与 PostgreSQL `arena402` schema；旧
`matching/` 只保留为尚未删除的历史原型。

## 当前实现状态

仓库已跑通双 Hosted Agent 的五回合本地开发闭环，以及独立的成交后冻结结算意图
边界；新鲜 testnet 交易和生产 Tencent SSM/真实 Provider 验收仍未执行：

| 模块 | 当前状态 |
|------|----------|
| 王城典当行 Game Core | 已实现四种货物、20 金初始组合、五回合自动推进、逐轮事件/快照、PostgreSQL 多池 FCFS、组间并发有限协商、冻结终场价格与排名 |
| Python matching/Arena | `matching/` 与 `web/api.py` 提供内存版 Agent、listing/intent、matching、有限 negotiation 和 ELO/Arena 原型；它不是新游戏的持久化回合引擎 |
| Local Agent Connector | `connector/` 与 `connector_gateway/` 已实现配对、Runtime discovery、typed command、durable event/receipt 和 PostgreSQL 控制面；尚未接入 `arena.decide` / `arena.negotiate` |
| Hosted Arena Agent | PostgreSQL control repository、DeepSeek/OpenAI-compatible HTTPS Provider、credential validation、durable Worker、`005` 迁移、创建 API 和最小 UI 已实现；本地开发模式可直接创建并持续运行，生产模式使用 Tencent SSM 且仍需部署环境完成真实凭据验收 |
| 统一 Runtime 基础 | Hosted Agent 已通过版本化 `AgentTask -> AgentTaskResult`、Result Sink/Consumer 与独立 Finalizer 接入 Game Core；Local Connector 游戏适配仍待实现 |
| Injective settlement | `agent-arena/settlement/` 已实现 EIP-3009 授权、项目自建 Facilitator 和 mUSDC direct relay，并在 Injective EVM testnet 验证 |
| 静态 Arena 前端 | `arena402/index.html` 已有 Supabase 驱动的 Agent、Battle、Market 和 ELO 页面；新的 `/game` 回合视图、数据表和实时状态机尚未实现 |
| 游戏业务持久化 | `006`–`010` 已实现 Game/Round/Event/Pool/Pairing/Negotiation/Runtime Run/SettlementIntent/Confirmation/Inventory Commit、Round portfolio snapshot、final settlement prices 与 Rankings |
| 端到端集成 | 双 Hosted Agent 可持续完成五回合；独立成交演示可冻结单笔 EIP-3009 意图；只读链上恢复与确认后现金/货物幂等提交已实现；通用 PaymentMandate 和新鲜交易验收尚未完成 |
| 标准 HTTP x402 | 尚未实现 `402 Payment Required` challenge、支付 header、paid retry 或标准公共 Facilitator 兼容 |

现有 settlement 是 **EIP-3009 direct-relay prototype**，不能描述为完整标准
x402 HTTP 实现。Arena 402 的产品红线是“真实链上结算”，而不是声称已经完成
尚不存在的协议兼容。

## 仓库结构

| 路径 | 用途 |
|------|------|
| `matching/`, `web/` | 现有内存 matching、negotiation 和 Arena/ELO 原型 |
| `arena_game/` | 王城典当行的新游戏领域内核：货物、金额、组合、事件、回合与排名 |
| `db/` | Connector、Hosted Agent/Runtime/Task，以及 `006`–`010` Pawnhouse 市场、Hosted orchestration、确认后结算和完整游戏迁移 |
| `arena402/` | 根 Vercel 部署使用的 CDN-only 静态前端 |
| `connector/`, `connector_gateway/` | 本地 Agent Connector 与自托管控制面 |
| `arena_agent_contracts/`, `arena_core/` | 统一 Runtime 契约、Arena Task/Result 持久化、审计、默认收敛与 exactly-once 投影基础 |
| `hosted_agent_runtime/` | Secret Store、durable Attempt recorder、Provider/Model/thinking capability registry、安全 Prompt/Driver，以及 DeepSeek/OpenAI-compatible HTTPS Provider |
| `hosted_agent_control_plane/` | Hosted capability/readiness、write-only Credential ingress、Agent create/list/detail、PostgreSQL repository、Tencent SSM 生产组合与显式 local-development 组合 |
| `docs/hosted-arena-agent-*.md` | Hosted/Local 统一 Runtime 的已批准规格、实施计划与当前阶段状态 |
| `frontend/` | Connector onboarding、控制台，以及默认受 readiness 关闭的最小 Hosted Agent 创建壳 |
| `deploy/`, `docker-compose.production.yml` | Connector 控制面，以及可选 Hosted Worker、Credential Controller、Arena Worker 的 fail-closed 单机部署 |
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
可继续冻结 SettlementIntent。新鲜 Injective testnet 支付仍需要单独的人类确认；
通用 PaymentMandate 尚未完成。

## 快速检查现有模块

### Python prototype

仓库的 setup script 会设置本地 Git hook 并运行内存 smoke check。请在 Bash
环境中从仓库根目录运行：

```bash
./setup.sh
```

启动现有 FastAPI wrapper：

```bash
pip install fastapi uvicorn
python3 -c 'from web.api import create_app; import uvicorn; uvicorn.run(create_app(), port=8000)'
```

这些命令只启动旧的 Python prototype，不会启动完整 Arena 402 游戏。

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

涉及 testnet 状态变更前仍需人工确认，禁止提交私钥、助记词或真实支付凭据。

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
- 前端目标：[`arena402/FRONTEND_GUIDE.md`](arena402/FRONTEND_GUIDE.md)
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
- [x402](https://github.com/coinbase/x402)
- [Injective](https://injective.com)
