# Arena 402 前端冲刺共识 (SPRINT_CONSENSUS)

> 面向 4 个并行 Claude Code 的唯一事实来源。开工前**必须完整读完本文**。
> 截稿：距提交约 12 小时（本文写于 2026-07-25 凌晨）。
> 本文基于对后端 `origin/dev-ly`/`origin/main` 真实代码的逐文件分析，不是推测。

---

## 0. 一句话目标

在**队友已经连好 API 的 React (Next.js 15) 前端**基础上，做出一个
**评委现场能看到"真实数据流动"的可跑通 Demo 闭环**：
`创建 Hosted Agent → 开一局单回合王城典当行 → 看到决策/撮合/协商 → 冻结结算意图`。
视觉统一到用户现有的**中世纪 + 未来科技风**，杜绝"多次修改痕迹"。

---

## 1. 铁律（违反即返工，先读三遍）

1. **只改 `frontend/` 目录**。后端代码（`arena_game/`、`web/`、`arena_*`、`db/` 等）一律**只读**，不许改。要后端配合就写进 `frontend/BLOCKED.md`。
2. **每个 agent 开自己的 feature 分支**（命名见 §5），只在自己的分支干活，最后合并到 `fe/sprint-integration`。不要直接推 `main`/`dev`。
3. **不要新增 Supabase 依赖**。Supabase 正在被后端队友迁移下线。所有数据走 `NEXT_PUBLIC_API_URL`（即 `api.arena402.com`）的 REST。现有 `src/lib/supabase.ts` 其实是"披着 supabase 名字的 REST 轮询"，不是真 Supabase SDK——可以用，但别再引入真 Supabase。
4. **视觉基准 = 用户现有风格**（见 §3），不是 React 版当前的 `arena-*` 深色 dashboard 主题。像素风(pixel/CRT)是旧方案，**全部舍弃**。
5. **money 是定点整数字符串**，永远不要用 JS float 处理金额（见 §4.3）。
6. 改完自己的板块**必须 `npm run build` 通过**再合并。

---

## 2. 事实基线：现在有什么、缺什么

### 2.1 前端基座（`frontend/`，Next.js 15.5 + React 18 + Tailwind + framer-motion）
已存在页面：`app/page.tsx`(首页) `app/arena/page.tsx`(排行榜+战报) `app/listings/page.tsx`(市场) `app/agents/page.tsx`(Agent 管理) `app/connect/page.tsx`(设备配对/登录)。
已存在组件：`BattleCard` `ConnectorConsole`(~640行,很完整) `HostedAgentCreator` `LiveCounter` `TierBadge`。
已连好的 API client：`src/lib/connector-api.ts` `src/lib/hosted-agent-api.ts` `src/lib/supabase.ts`(REST轮询)。

### 2.2 后端真能端到端跑的（对齐这些）
- Hosted Agent 创建全链路（登录→存凭据→创建 Agent→Worker 执行）。
- **单回合**王城典当行：FCFS 撮合 → 协商 propose/accept → 冻结 SettlementIntent。
- 只读的游戏状态/时间线/结算意图查询端点（`/api/v1/pawnhouse/...`）。

### 2.3 后端**尚未实现**（做了也没真数据，别投入）
- ❌ N 回合自动推进（只有单回合）
- ❌ 最终结算价 / settle table 生成
- ❌ **排行榜写库**（`arena402.rankings` 表存在但没有任何代码写入，也没有读取 API）
- ❌ PaymentMandate（多笔额度/撤销）
- ❌ 真实链上广播（停在人工确认门前）
- ❌ Connector 本地 Agent 接入游戏契约
- ❌ 公开的比赛运营 API（创建/开始/推进比赛目前只在 `X-Arena-Dev-Token` dev 端点后面）

> **推论**：排行榜页、N 回合动画、最终名次页——现在做都是**空壳**，后端喂不出数据。优先级往后放（§6）。

---

## 3. 视觉基准（所有 agent 统一，禁止各自发挥）

来源：用户现有前端 `~/arena402/css/style.css`。**中世纪（羊皮纸报刊 + 衬线）+ 未来科技（终端等宽）**。

### 3.1 字体
- 衬线（标题/数字/强调）：`Instrument Serif`, 'Times New Roman', serif
- 等宽（正文/数据/标签）：`IBM Plex Mono`, 'Courier New', monospace

### 3.2 配色（CSS 变量，照抄）
```css
--ink:        #0a0a0b;   /* 主墨黑 */
--ink-deep:   #060607;   /* 更深背景 */
--paper:      #f4f2ec;   /* 羊皮纸白（主文字/高亮） */
--paper-dim:  rgba(244,242,236,0.62);
--grey:       #8f8f94;
--grey-dark:  #55555a;
--line:       rgba(244,242,236,0.16);
--line-strong:rgba(244,242,236,0.42);
```

### 3.3 间距/布局系统
```css
--frame: 10px;
--edge: clamp(20px, 5vw, 84px);   /* 页面水平内边距 */
--content-max: 1440px;
```

### 3.4 落地方式
由**风格负责人（Agent A）**先把这套 token 注入 `tailwind.config.ts` + `globals.css`，覆盖现有 `arena-*` 主题；其余 agent 一律用这套变量/token，不要硬编码颜色和字体。改动前先看 Agent A 是否已提交风格基座（§5 依赖关系）。

---

## 4. API 契约（对齐后端真实代码，字段名以此为准）

**Base URL**：`process.env.NEXT_PUBLIC_API_URL`（本地 `http://localhost:8000`，生产 `https://api.arena402.com`）。
**跨域**：后端 CORS `allow_credentials=true`，所有请求带 `credentials: 'include'`。允许方法仅 `GET/POST/PATCH`（无 PUT/DELETE）。

### 4.1 ⚠️ 两套命名规范并存（最容易踩的坑）
- **hosted-agent 接口**：请求/响应全 **camelCase**（`displayName` `providerId` `thinkingEnabled`）。
- **connector / arena / pawnhouse 接口**：全 **snake_case**（`device_id` `agent_id` `asset_class`）。
- **例外**：`GET /api/v1/pawnhouse/games/{id}` 顶层 camelCase，但 `participants[]`/`rounds[]` 内部是 snake_case（后端已知不一致）。
> TS 类型必须逐字段照抄，别自作主张统一驼峰。

### 4.2 鉴权机制（后端自带，非 Supabase）
- **仅在后端以生产模式挂载 Connector 时启用**（`ADX_ENV=production`）。invite/密码 + 签名 session cookie。
- 登录：`POST /api/auth/invite`（邀请码建号）| `POST /api/auth/login`（用户名密码）| `GET /api/auth/session`（查会话）| `POST /api/auth/logout`。
- Cookie：`adx_session`（HttpOnly）+ `adx_csrf`（**非** HttpOnly，JS 要读它）。
- **所有 POST（除登录）必须带 `X-CSRF-Token` header**，值 = `adx_csrf` cookie 的值。
- hosted-agent 的 POST 还要带 `Idempotency-Key` header + `Content-Type: application/json`（严格，多一个字符都 415）。
- `connector-api.ts` 里已实现自动附加 CSRF 的 `apiRequest<T>`，**复用它，别重写**。

### 4.3 金额（money）—— 硬规则
- 全部是**定点整数 atomic**，`GOLD_SCALE = 1_000_000`（即 `7 gold = "7000000" atomic`）。
- 大整数在 JSON 里是**字符串**（`unitPriceAtomic: "7000000"`），避免 JS float 精度丢失。
- 协商报价等 wire 值是**定点小数字符串**如 `"7.000000"`，正则 `^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$`。
- 前端显示：`Number(atomic) / 1_000_000`，但**参与计算/传回后端时保持字符串**。永远不要 `parseFloat` 后再回传。

### 4.4 关键端点速查（Demo 闭环需要的）

**Hosted Agent（camelCase，需登录+CSRF+Idempotency-Key）**
```
GET  /api/hosted-agents/capabilities   → {creationEnabled, models[], schemaVersion}  (无需鉴权)
GET  /api/hosted-agents?scope=mine     → {agents[], total}
GET  /api/hosted-agents/{id}           → HostedAgentDetail
POST /api/model-credentials            body:{providerId, apiKey}      → CredentialMetadata
POST /api/hosted-agents                body:{displayName, credentialId, providerId, modelId, thinkingEnabled, strategyInstructions} → HostedAgentDetail
```
hosted agent 状态：`provisioning | ready | degraded | disabled`。
凭据状态：`pending_write | stored | pending_validation | valid | invalid | revoking | revoked`。

**游戏只读投影（snake_case 混合，无需鉴权，Demo 展示主力）**
```
GET /api/v1/pawnhouse/games/{game_id}                  → {gameId, phase, roundCount, currentRound, participants[], rounds[], schemaVersion}
GET /api/v1/pawnhouse/games/{game_id}/timeline?after=N → {gameId, events[{sequence, roundId, type, data, createdAt}], nextAfter}  (轮询这个做实时)
GET /api/v1/pawnhouse/games/{game_id}/runtime-run      → {status, stage, errorCode, ...}
GET /api/v1/pawnhouse/games/{game_id}/settlement-intents → {settlementIntents[], total}
```
> 比赛的创建/开始/推进在 `POST /api/dev/pawnhouse/...`，需 `X-Arena-Dev-Token`，是 **dev 端点**。Demo 期间由脚本/后端触发推进，前端主要**读 timeline 轮询展示**。

**Arena 排行/战报（snake_case，多数无需鉴权）**
```
GET /api/agents                    → {total, agents[]}
GET /api/arena/leaderboard         → {total, leaderboard[]}   ⚠️见 §2.3：无真数据
GET /api/arena/battles?limit=      → {total, battles[]}
GET /api/arena/stats               → 汇总统计
GET /api/health                    → {status, version, connector_gateway, ...}
```

### 4.5 游戏枚举（前端渲染直接用这些字面量，禁止臆造）
| 概念 | 值 |
|---|---|
| 货物 good | `grain`(粮草🌾) `iron`(精铁⚔️) `warhorse`(战马🐎) `gems`(宝石💎) |
| decide 动作 | `buy` `sell` `pass` |
| negotiate 动作 | `propose` `accept` `reject` |
| 协商角色 | `buyer` `seller` |
| 游戏 phase | `registration` `portfolio_setup` `portfolio_locked` `running` `final_valuation` `completed` `cancelled` |
| 回合 phase (DB) | `event_reveal` `decide` `matching` `negotiate` `settling` `completed` `cancelled` |
| 协商 status | `active` `accepted_pending_settlement` `rejected` `timeout` |
| 结算意图 status | `authorization_requested` `submitted` `chain_confirmed_uncommitted` `inventory_committed` `authorization_failed` `submission_failed` `expired` `reverted` |
| 名次 tier（中文，DB 强制） | `公爵` `御用商人` `王城行商` `流浪商贩` |
| timeline event_type | `game.created` `participant.joined` `world.event_revealed` `decision.applied` `pairing.created` `negotiation.message` `settlement.intent_frozen` `settlement.submitted` `settlement.chain_confirmed` `settlement.inventory_committed` `runtime.run_queued/completed/failed` 等 |

> ⚠️ `chain_confirmed_uncommitted` = 链上已确认但事务未提交的**可恢复中间态**，**绝不能**在 UI 显示为"已完成交易"。只有 `inventory_committed` 才是成交。
> 初始净资产固定 20 gold；单笔交易固定数量 1。

---

## 5. 4 个 Agent 分工（按页面/模块，互不重叠）

分支命名：`fe/<板块>`。都从 `origin/main` 拉，改完合入 `fe/sprint-integration`。

### Agent A —— 风格基座 + 首页（`fe/style-home`）**先行，其他人依赖**
- 把 §3 的字体/配色/间距 token 注入 `tailwind.config.ts` + `src/app/globals.css`，覆盖 `arena-*` 主题。
- 提供统一基础组件：`Button` `Card` `Badge` `PageFrame`（用 §3 变量）。
- 改造 `src/app/page.tsx` 首页 + `src/app/layout.tsx` 导航为中世纪/科技风。
- **交付里程碑（越快越好，其他 agent 等它）**：token 注入完成 + 一个示范组件，提交后在 `frontend/HANDOFF.md` 写"风格基座 ready"。

### Agent B —— Hosted Agent 创建流（`fe/agent-create`）**对齐后端真能跑的**
- 打磨 `src/app/agents/page.tsx` + `HostedAgentCreator.tsx`：capabilities 拉取 → 选 Provider/Model/thinking → 存凭据 → 创建 Agent → 轮询 `provisioning→ready`。
- 严格用 §4.2 鉴权 + §4.1 camelCase + `hosted-agent-api.ts` 现有 client。
- 用 §4.5 状态做清晰的状态机 UI（provisioning 转圈、degraded 警示、ready 可入场）。

### Agent C —— 对局展示页（`fe/game-view`）**Demo 的 WOW 主场**
- 新建 `src/app/games/[gameId]/page.tsx`：轮询 `GET /api/v1/pawnhouse/games/{id}` + `/timeline?after=N`。
- 渲染单回合闭环：事件揭晓 → 各 Agent decide(buy/sell/pass) → FCFS 撮合 → 协商 propose/accept 逐条上屏 → 冻结 SettlementIntent。
- 用 §4.5 event_type 把 timeline 事件转成"公共战报流"。金额按 §4.3 处理。
- 新建 `src/lib/pawnhouse-api.ts`（snake_case 混合类型，照 §4.4）。

### Agent D —— 结算意图 + Agent 详情（`fe/settlement-detail`）
- 结算面板：读 `GET /api/v1/pawnhouse/games/{id}/settlement-intents`，展示冻结的意图（链/token/金额/双方/status）。
- 严格区分 `accepted_pending_settlement` / `chain_confirmed_uncommitted`（未成交）vs `inventory_committed`（成交）。
- Agent 详情页 `src/app/agents/[id]/page.tsx`（`getAgent(id)` 已有 client）。
- 结算意图金额全用字符串处理（§4.3）。

> **排行榜/N 回合动画/最终名次：暂不做**（§2.3 后端无数据）。若有余力，Agent A/C 在冲刺后期用 `/api/arena/leaderboard` 做**只读展示**并标注"beta"。

---

## 6. 优先级（12 小时，从上到下）

**P0（必须，Demo 闭环）**
1. Agent A 风格基座（阻塞项，最先）
2. Agent B Hosted Agent 创建流跑通（登录→凭据→创建→ready）
3. Agent C 对局展示页 + timeline 轮询（能看到 decide/撮合/协商流动）
4. Agent D 结算意图展示（看到 SettlementIntent 冻结）

**P1（有时间就做）**
5. Agent A 首页叙事/视觉冲击（评委第一眼）
6. Agent D Agent 详情页
7. 空/加载/错误态统一（无骨架的页面补上）

**P2（余力，标 beta）**
8. 排行榜只读展示（后端无真数据，标注）
9. 移动端适配打磨

**不做**：N 回合自动动画、最终名次页、PaymentMandate UI、真实链上广播 UI、Connector 本地 Agent 入游戏。

---

## 7. 协作规程

1. **开工前**：`git fetch origin && git checkout -b fe/<你的板块> origin/main`。只碰自己 §5 的文件。
2. **跨板块共享**：改公共文件（`globals.css`/`tailwind.config.ts`/`layout.tsx`）只有 Agent A 能动；其他人需要就写进 `frontend/HANDOFF.md` 请 A 加。
3. **被后端卡住**：写进 `frontend/BLOCKED.md`（哪个端点/字段缺、期望形状），继续做能做的，别停等。
4. **合并**：`npm run build` 通过 → 合入 `fe/sprint-integration` → 在 `HANDOFF.md` 记一行"X 板块 done"。
5. **冲突**：优先 rebase 到 `fe/sprint-integration` 最新，冲突只解自己板块的。
6. **本地起服务**：`cd frontend && npm install && NEXT_PUBLIC_API_URL=https://api.arena402.com npm run dev`（或本地 `http://localhost:8000` 连后端 dev）。
7. **禁止**：改后端目录、引入 Supabase、直连数据库、硬编码密钥、用像素/CRT 旧风格。

---

## 8. 快速自检清单（每个 agent 合并前过一遍）
- [ ] 只改了 `frontend/` 下的文件？
- [ ] 用了 §3 的字体/配色 token，没硬编码颜色？
- [ ] 字段名对齐 §4（camelCase vs snake_case 没搞混）？
- [ ] 金额用字符串/atomic，没 parseFloat 回传？
- [ ] POST 带了 CSRF（和 hosted 的 Idempotency-Key）？
- [ ] `chain_confirmed_uncommitted` 没显示成"成交"？
- [ ] `npm run build` 通过？
- [ ] 在 `HANDOFF.md` 记录了进度？

---
*本文由主控 Claude 基于 origin/dev-ly 真实代码分析生成，2026-07-25。契约细节如与后端代码冲突，以后端代码为准，并在 BLOCKED.md 反馈。*
