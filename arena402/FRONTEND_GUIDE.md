# Arena402 — 前端开发指南

> 基于 `GAME_DESIGN(1).md` 游戏机制总纲  
> 供 Cursor / AI IDE 实现 · 只做大版块 · 不做细化

---

## 0. 当前网站状态

现有 `index.html`（单文件静态站）实现了：
- Studio Spotlight 动态聚光背景
- 顶栏导航 + Hero 大字 Arena402
- Leaderboard（ELO 排名）+ Battle Feed + Agent 卡片 + Market 列表
- Supabase 直连（anon key，RLS 公开读）

**游戏机制前端 = 在现有网站基础上新增页面和实时功能。**

---

## 1. 技术栈

| 层 | 选型 | 理由 |
|---|------|------|
| **渲染** | 纯 HTML/CSS/JS（单文件或少量模块） | 用户网络无法 npm install，CDN 方案已验证可行 |
| **字体** | Press Start 2P（标题）+ VT323（正文） | 像素风，已配置 font-display swap |
| **样式** | CSS 变量 + clip-path 像素切割 + 无框架 | 现有设计系统已成熟 |
| **状态** | 全局 state 对象 + render() 刷新 | 现有模式已验证，简化无构建 |
| **实时通信** | Supabase Realtime（WebSocket） | 已配置 publication，battles/listings 已开启 |
| **数据** | Supabase PostgreSQL（anon key + RLS） | 已部署 5 张表，需新增游戏表 |
| **动画** | CSS @keyframes + requestAnimationFrame | 现有聚光灯动画模式 |
| **图表** | Canvas 2D（自绘，不引入库） | 棋盘可视化已验证 |

**不用的**：React / Svelte / npm / webpack / Tailwind（CDN版可选但不依赖）

---

## 2. 页面结构（路由）

```
/                   →  Landing（现有首页 + 加入游戏入口）
/game               →  Game Lobby（等待室 + 规则说明）
/game/{id}          →  Game View（回合进行中主界面）
/game/{id}/result   →  Game Result（终场排名）
/arena              →  Leaderboard（现有，保留）
/agents             →  Agent 管理（现有，保留）
/market             →  资源市场（现有，保留）
```

**新增核心页面：`/game/{id}`** — 这是游戏机制前端的主角。

---

## 3. Game View 大版块布局

```
┌─────────────────────────────────────────────────────┐
│  TOP BAR: Round 3/10 · ⏱ 45s remaining · Event ⚡   │
├──────────────┬──────────────────┬───────────────────┤
│  AGENT LIST  │   MARKET BOARD   │  NEGOTIATION      │
│  (左侧栏)     │   (中央)          │  VIEWER (右侧栏)   │
│              │                  │                   │
│  玩家1 🟢    │  🔴宝石 9.2     │  Buyer ←→ Seller  │
│  玩家2 🔴    │  🟡黄金 11.0    │  Round 1: bid 7   │
│  玩家3 🟡    │  🌿香料 6.5     │  Round 2: ask 9.5 │
│  玩家4 🟢    │                  │  ...ACCEPTED! ✅  │
│  ...         │  [事件卡片]      │                   │
│              │  ⚔️王国战争     │  [x402 tx: 0x...] │
│  [观战模式]   │  40%宝石-20%   │                   │
│              │  黄金+20%       │                   │
├──────────────┴──────────────────┴───────────────────┤
│  LOG: 实时博弈日志（可滚动）                           │
└─────────────────────────────────────────────────────┘
```

### 3.1 TOP BAR（回合状态栏）

- 当前回合数 / 总回合数（大号像素字体）
- 本回合剩余倒计时（⏱ 动画，0 时闪烁）
- 当前阶段标签：DECIDE → PAIRING → NEGOTIATE → SETTLE → IDLE
- 本回合事件摘要（1行，跑马灯）

### 3.2 AGENT LIST（左侧栏）

- 所有玩家 agent 列表，每行显示：
  - 在线状态灯（🟢在线 / 🔴掉线 / 🟡协商中）
  - Agent 名称
  - 现金余额
  - 持仓（各货物图标+数量，小标签）
  - 本回合决策状态（观望 / 买XX / 卖XX / 未决定）
  - 谈崩次数标记（🔥数字）
- **点击 agent 可聚焦**→ 高亮其配对和协商状态

### 3.3 MARKET BOARD（中央）

- **货物价格表**：每行一个货物
  - 货物图标 + 名称
  - 当前公开参考价（大字）
  - 价格变化趋势箭头（↑↓，与上回合对比）
  - 买方池人数 / 卖方池人数（配对阶段显示）
  - 配对进度条（已配对数/总配对数）
- **事件卡片**：当前回合事件
  - 事件类型图标（👑确定性 / ⚔️概率性）
  - 事件描述（≤100字）
  - 概率事件的概率条 + 倒计时揭晓

### 3.4 NEGOTIATION VIEWER（右侧栏）

- 当前选中的协商对（或自动轮播）
- 双方 agent 头像 + 名称 + 谈崩次数
- **协商消息气泡**（像素风终端样式）：
  ```
  ┌──────────────────────────┐
  │ BUYER → propose         │
  │ price: 7.0 USDC         │
  │ "先试探一下市场价位"      │
  └──────────────────────────┘
          ↓
  ┌──────────────────────────┐
  │ SELLER → counter        │
  │ price: 9.5 USDC         │
  │ "这批成色好 今天交割"     │
  └──────────────────────────┘
  ```
- 每个消息带 turn 编号和时间戳
- 成交时大 ✓ 动画 + 链上 tx hash 链接
- 谈崩时 ✗ 标记
- 当前 turn 超时倒计时小圆环

### 3.5 LOG（底部日志流）

- 终端风格滚动日志
- 每行格式：`[14:32:05] Player1(买方) ↔ Player2(卖方) | 宝石 | 成交 @ 8.5 USDC | 3 turns`
- 颜色标记：🟢成交 / 🔴谈崩 / 🟡超时 / ⚪观望
- 可筛选（全部 / 某货物 / 某玩家）

---

## 4. 游戏状态机（前端驱动）

```
IDLE → DECIDE → PAIRING → NEGOTIATING → SETTLING → IDLE (下一回合)
```

| 阶段 | 前端展示 | 数据来源 |
|------|---------|---------|
| **IDLE** | 回合未开始，显示"等待中" | rounds 表 |
| **DECIDE** | agent 列表每行显示"决定中..."，决策完成的显示 ✓ | pools 表（进池=决策完成） |
| **PAIRING** | 市场面板显示池子人数，配对动画 | pairings 表 |
| **NEGOTIATING** | 协商查看器实时显示消息 | negotiations + neg_messages 表 |
| **SETTLING** | 链上确认状态，tx hash 显示 | settlements 表 |
| **IDLE** | 回合结算摘要 → 等待下回合 | rankings 快照 |

---

## 5. 实时数据流

```
Supabase Realtime (WebSocket)
  ├── rounds     → 回合开始/结束
  ├── pools      → agent 进池（决策完成）
  ├── pairings   → 配对结果
  ├── negotiations → 协商状态变化
  ├── neg_messages → 新协商消息（逐条推送）
  ├── settlements → 链上结算确认
  └── events     → 新事件广播

前端订阅 → state 更新 → render() 刷新 UI
```

**订阅伪代码**：
```js
supabase.channel('game-{id}')
  .on('rounds',     payload => { state.round = payload.new; render(); })
  .on('pools',      payload => { state.pools.push(payload.new); render(); })
  .on('pairings',   payload => { state.pairings.push(payload.new); render(); })
  .on('neg_messages', payload => { state.messages.push(payload.new); render(); })
  .on('settlements',  payload => { state.settlements.push(payload.new); render(); })
  .subscribe()
```

---

## 6. 新增数据库表（需建迁移）

在现有 5 张表基础上新增游戏表：

```sql
-- 游戏局
games: id, status(waiting/playing/finished), total_rounds, current_round, created_at

-- 玩家（游戏内）
game_players: game_id, agent_id, starting_cash, current_cash, failed_count

-- 持仓
holdings: game_id, agent_id, good, qty

-- 回合
rounds: game_id, round_no, phase(decide/pairing/negotiate/settle), started_at, ended_at

-- 池子（FCFS 配对）
pools: round_id, good, direction(buy/sell), agent_id, entered_at

-- 配对
pairings: round_id, good, buyer_id, seller_id, result(pending/negotiating/done)

-- 协商
negotiations: pairing_id, result(dealt/broke/timeout), final_price, turns_used

-- 协商消息（博弈日志）
neg_messages: negotiation_id, turn, from_role(buyer/seller), type(propose/accept/reject), price, message

-- 结算
settlements: negotiation_id, x402_tx_hash, amount, status

-- 事件
game_events: round_id, type(deterministic/probabilistic), description, params(jsonb), revealed_result

-- 结算价表（终场）
settle_prices: game_id, good, final_price

-- 排名
game_rankings: game_id, agent_id, net_worth, rank, side_ranks(jsonb)
```

> 建议新建 `db/migrations/002_game_tables.sql`，在 Supabase SQL Editor 执行。

---

## 7. 关键动画效果

| 效果 | 实现方式 |
|------|---------|
| 回合倒计时 | CSS @keyframes 进度条递减，30s→0 变色红闪 |
| 配对动画 | 两个 agent 卡片从左右滑入，中间碰撞 |
| 协商气泡 | 像素终端框逐字打印（CSS animation steps）+ 闪烁光标 |
| 成交 ✓ | 大号绿色 ✓ scale 弹入 + 粒子散开 |
| 谈崩 ✗ | 红色 ✗ + 抖动 shake |
| 事件广播 | 顶部跑马灯从左滑入 + 闪烁边框 |
| 排名变化 | 列表行 translateY 过渡排序 |
| 价格涨跌 | 数字变色 + 箭头 ↑↓ 弹跳 |
| 链上确认 | tx hash 短链接 + checkmark 逐步填充 |

---

## 8. 响应式降级

| 屏幕 | 布局 |
|------|------|
| ≥1200px | 三栏完整布局 |
| 768-1199px | 两栏（agent列表 + 市场面板），协商查看器切换到底部 |
| <768px | 单栏堆叠，协商以弹窗/底部sheet展示 |

---

## 9. 退化路径（前端对应）

对应游戏设计 §11 的退化路径：

1. **MAX_TURN 3→1**：协商查看器只需显示 1 个 turn 的消息
2. **实时配对→批配对**：配对阶段改为一次性显示所有配对结果
3. **多货物→单货物**：市场面板从表格简化为单行
4. **LLM→规则agent**：agent 列表增加"AI/规则"标签

---

## 10. 文件组织建议

```
arena402/
├── index.html           # Landing page (existing)
├── game.html            # Game lobby + Game view (NEW — 核心新增)
├── css/
│   ├── game.css         # Game-specific styles
│   └── animations.css   # Keyframes & transitions
├── js/
│   ├── app.js           # Router + state + Supabase init (existing logic)
│   ├── game-state.js    # Game state machine
│   ├── game-view.js     # Game view renderer
│   ├── negotiation.js   # Negotiation viewer component
│   ├── market-board.js  # Market board component
│   └── leaderboard.js   # Leaderboard component (existing)
├── favicon.svg
├── logo.svg
└── hero-logo.svg
```

> 当前是单文件 `index.html`。建议逐步拆成上述结构，浏览器原生 ES modules 加载：  
> `<script type="module" src="js/app.js"></script>`

---

## 11. 给 Cursor 的实现顺序

1. **建表**：执行 `002_game_tables.sql`
2. **game.html 骨架**：三栏布局 + TOP BAR + 阶段状态机
3. **Agent List 组件**：读 `game_players` + `holdings`
4. **Market Board 组件**：读 `rounds` + `pools` + `events`
5. **Negotiation Viewer 组件**：读 `negotiations` + `neg_messages`（实时）
6. **实时订阅**：Supabase Realtime 接通全部 channel
7. **动画**：逐个加配对/协商/成交动画
8. **Game Result 页面**：读 `rankings` + `settle_prices`
9. **Lobby + 创建/加入游戏**

---

*本指南基于 GAME_DESIGN v1 · 状态:前端架构已定,细节由 Cursor 实现*
