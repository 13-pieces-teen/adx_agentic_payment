# Arena 402 单一当前游戏：前端 PRD

> 版本：v0.1
> 日期：2026-07-25
> 状态：需求草案，待评审
> 负责人：Frontend / Product Design
> 后端配套：[prd-current-game-backend.md](prd-current-game-backend.md)

## 1. 交付边界

本 PRD 定义 Arena 402 “单一当前大厅 + 游戏匹配展示 + 结果页”的前端体验。

正式产品前端的代码源和部署源是独立的 `sunruize93-cmyk/arena402` 仓库，并通过
Vercel 部署。本仓库的 `frontend/` 仍是临时 Compose 集成壳，不在其中新增产品 UI。
只有外部前端完成 API/CORS/E2E 切换且本仓库 Compose 不再依赖该壳后，才另行移除它。

## 2. 产品目标

1. 用户进入 `/game` 后不需要理解房间、赛季或服务器概念。
2. 一屏看清当前状态、Ready 人数、参与 Agent 和自己的可执行操作。
3. Join 流程只包含选择 Hosted Agent、确认本局 PaymentMandate、查看结果。
4. 达到启动阈值后自动进入观战，不要求用户刷新或再次点击。
5. RUNNING 页面清楚展示回合、买卖配对、协商状态和已接受交易的结算进度。
6. COMPLETED 页面清楚展示排名和可分页交易记录。
7. 展示语义严格遵循 Arena 权威状态，不把 `accept` 或链上提交误称为成交。

## 3. 非目标

- Waiting Games 列表、房间搜索、筛选和分页；
- 普通用户 Create Game、Start Game、Cancel Game；
- 运行中更换 Agent、Runtime 或 PaymentMandate；
- 在页面公开 Prompt、策略全文、Provider 原始输出或私有推理；
- 为尚未完成的 Local Connector Game Adapter 提供可用入口；
- 把当前 x402 V2 + EIP-3009 direct relay 显示为已通过公共 Facilitator 生产认证；
- 在本仓库临时 `frontend/` 中实现正式产品页面。

## 4. 用户与前置条件

### 4.1 访客

- 可查看当前大厅、参与 Agent、实时 Game 和历史结果；
- 点击 Join 时引导登录；
- 不显示钱包地址、User ID 或内部 Runtime 状态。

### 4.2 已登录用户

- 至少拥有一个 active 且 Runtime ready 的 Hosted Agent；
- 钱包 / testnet settlement account 可用于本局；
- 可完成本局 PaymentMandate 确认；
- 同一 Game 只可加入一个 Agent。

若无可用 Hosted Agent，Join 弹窗显示明确的创建/修复 Agent CTA，但不把未 Ready
Agent 伪装成可加入选项。

## 5. 信息架构与路由

| 路由 | WAITING | RUNNING | COMPLETED |
|---|---|---|---|
| `/game` | 当前大厅 | 当前 Game 摘要并自动进入观战 | 显示下一场大厅和最近完成记录 |
| `/game/:gameId` | 等待室 | 实时游戏 / 配对展示 | 跳转结果页 |
| `/game/:gameId/result` | 提示尚未完成 | 提示游戏中并提供观战入口 | 排名与完整交易记录 |

不新增 `/games` 房间列表，也不向普通用户展示管理路由。

## 6. 对外状态与显示文案

| API 状态 | 中文文案 | 主视觉 |
|---|---|---|
| `WAITING` | 等待大厅 | `7 / 10 READY` |
| `RUNNING` | 游戏中 | `第 3 / 5 回合` + 当前阶段 |
| `COMPLETED` | 已完成 | 最终排名 |

内部阶段只作为辅助标签：

| 内部阶段 | 前端显示 |
|---|---|
| Event reveal | 事件公布 |
| Decide | Agent 决策 |
| Pair / Match | 买卖配对 |
| Negotiate | 协商中 |
| Settle | 结算中 |
| Round close | 回合结算 |
| Final valuation | 最终估值 |

前端不得根据倒计时自行切换权威状态。倒计时归零后显示“等待服务端确认”，直到收到
新快照或 Timeline 事件。

## 7. 页面需求

### 7.1 `/game` 当前大厅

最小首屏：

```text
Current Game
WAITING · 7/10 READY

[ Join with my Agent ]

Participants
...

Recent completed games
...
```

页面模块：

1. **Current Game Header**
   - 状态；
   - Ready / Threshold；
   - 已登录用户是否已加入；
   - 测试环境手动启动不面向普通用户展示。
2. **Primary Action**
   - 未登录：`登录并加入`；
   - 已登录未加入：`使用我的 Agent 加入`；
   - 已加入：`已 Ready` + `退出本局`；
   - RUNNING：`进入观战`；
   - 当前 Game 恢复中：禁用按钮并显示可重试提示。
3. **Participants**
   - Agent 头像、显示名、Runtime 类型、Ready 标记和加入顺序；
   - 不显示 User ID、钱包地址、模型 Key 或策略说明；
   - 人数较多时保持单列/网格可扫读，不做房间筛选。
4. **Recent Completed Games**
   - 最近 3–5 局；
   - 完成时间、冠军 Agent、参与数；
   - 点击进入结果页。

### 7.2 `/game/:gameId` 等待室

WAITING 时与 `/game` 使用同一大厅组件，但锁定 `gameId`：

- 如果该 Game 已结束，不偷偷替换成新 Game，而是跳转该局结果页；
- 如果该 Game 不存在，显示 404 和“返回当前游戏”；
- 已加入用户可 Withdraw；
- 非当前旧 WAITING 状态视为异常，只提供返回当前游戏。

### 7.3 `/game/:gameId` 实时游戏

页面结构：

1. **Game Status Bar**
   - 回合、总回合、公开阶段；
   - 当前公开事件；
   - 数据更新时间和重连状态。
2. **Participant Board**
   - Agent 公共身份；
   - 当前公开现金、库存或净值仅按后端允许的投影展示；
   - 不从前端自行计算最终排名。
3. **Matching Board**
   - 按资产分组展示本轮 Buyer / Seller 配对；
   - 卡片格式：Buyer Agent ↔ Seller Agent、Asset、配对序号；
   - 状态：`Negotiating`、`Settling`、`Confirmed`、`Failed`；
   - 未配对 Agent 可显示“本轮未匹配”，但不算失败协商。
4. **Public Negotiation**
   - 只展示经过后端清洗的公开消息；
   - 展示 `propose / accept / reject` 的人类可读标签；
   - 不展示原始 Provider 输出、Prompt 或推理。
5. **Recent Trades**
   - 默认最近 20 条，最大 50 条；
   - 仅展示已接受协商；
   - 可展开精简结算流程和区块浏览器链接。
6. **Timeline**
   - 事件公布、配对创建、协商结果、结算更新和回合结束；
   - 使用服务端 sequence 去重和断线补齐。

### 7.4 `/game/:gameId/result`

模块：

1. 最终排名：名次、Agent、净资产、Tier；
2. 最终资产价格；
3. Game 摘要：回合数、参与数、开始/完成时间；
4. 完整交易表：游标分页、状态筛选；
5. Event seed / schedule commitment 的公开验证信息；
6. 返回当前游戏入口。

结果页只读取后端最终排名。Game 未完成时不显示预测冠军或临时最终榜。

## 8. Join 交互

### 8.1 流程

```text
点击 Join
  -> 登录检查
  -> 选择一个 Ready Hosted Agent
  -> Join preflight
  -> 查看并确认本局 PaymentMandate
  -> 创建或确认 Game-scoped Mandate
  -> 提交 Join
  -> READY 成功页
  -> 返回大厅并等待自动开始
```

### 8.2 Agent 选择

每个 Agent 显示：

- 公共名称和头像；
- Hosted 标记；
- Runtime 状态；
- 不可选原因，例如 `Runtime 未 Ready` 或 `已被用于本局`。

MVP 不显示 Local Agent 为可选。后端返回不支持时，前端不得通过隐藏校验强行提交。

### 8.3 PaymentMandate 确认

确认页必须清楚展示：

- Testnet 网络和 token；
- 单笔上限；
- 本局累计上限；
- 失效时间；
- 可撤销说明；
- 这是支付授权，不是立即付款；
- 当前使用的是测试资产。

用户取消确认时保留在大厅，不创建 Ready Participant。前端先通过
`GET /api/v1/me/wallet` 获取安全的 testnet 钱包投影，再通过
`GET/POST /api/v1/me/payment-mandates` 查询或创建本局 Mandate。前端只持有
`paymentMandateId` 和安全状态，不存储 Secret、私钥、seed phrase 或 signer
材料。

单大厅中的 payee 范围必须由后端以同局 Settlement Account 规则或等价安全方式
生成。前端不得收集、拼接或提交任意钱包地址列表。

### 8.4 Join 成功

- 显示 `READY` 和最新 `readyCount / startThreshold`；
- 如果本次 Join 触发启动，直接进入 `/game/:gameId`；
- 如果响应仍为 WAITING，回到大厅继续订阅；
- 对 Idempotency replay 显示同一个成功结果，不创建重复卡片。

### 8.5 Join 错误

| code | 用户文案与动作 |
|---|---|
| `game_not_current` | 本局已切换，刷新并进入当前游戏 |
| `game_already_started` | 游戏刚刚开始，进入观战 |
| `participant_limit_reached` | 本局已满，等待下一局 |
| `user_already_joined` | 显示已加入的 Agent |
| `runtime_not_ready` | 前往 Agent 页面修复 Runtime |
| `mandate_not_ready` | 重新确认 PaymentMandate |
| `idempotency_conflict` | 生成新请求键后重试，不重复沿用冲突键 |

错误文案不展示 Provider、数据库或签名内部细节。

## 9. Withdraw 交互

- 仅在 WAITING 且当前用户已加入时显示；
- 点击后弹出一次确认，说明退出会释放本局名额和未消费预留；
- 提交期间禁用 Join/Withdraw；
- 成功后移除自己的 Participant 卡片并刷新计数；
- 若服务端返回 `game_already_started`，关闭弹窗并自动进入观战；
- 重复请求按幂等成功处理。

## 10. 自动进入观战

在 `/game` 或等待室收到以下任一权威变化时跳转：

- Current Game 快照从 WAITING 变为 RUNNING；
- Timeline 收到 `game.started`；
- Join 响应直接返回 RUNNING。

跳转目标为 `/game/:gameId`。同一个 `game.started` 只触发一次导航，浏览器返回时不得
形成跳转循环。

未加入用户也可以观战；加入用户的 Agent 不要求浏览器保持在线。

## 11. 匹配与交易展示

### 11.1 Pairing

`pairing.created` 只表示 Buyer 与 Seller 已按 FCFS 配对，不表示交易成立。展示字段：

- Round；
- Buyer / Seller Agent；
- Asset；
- Pairing 顺序；
- 当前协商状态。

### 11.2 Trade

每条交易行：

| 字段 | 显示 |
|---|---|
| Round | 第几回合 |
| Buyer / Seller | 双方 Agent |
| Asset | 货物与固定数量 |
| Price | 按 decimals 格式化，保留 atomic 原值供详情使用 |
| Status | Settling / Confirmed / Failed |
| Transaction | 截断 hash + 外部链接 |
| Time | 接受时间；确认后补充确认时间 |

严格语义：

- `accept` 后：`Settling`；
- `submitted`、`submitted_unknown`：`Settling`；
- `chain_confirmed_uncommitted`：仍为 `Settling`；
- `inventory_committed` 后：`Confirmed`；
- rejected/timeout/未配对：不进入交易表；
- 已接受但最终确认失败：`Failed`，并显示后端安全错误类别。

### 11.3 展开详情

可展示：

```text
Offer accepted
-> PaymentMandate validated / reserved
-> x402 V2 request
-> EIP-3009 direct relay submitted
-> Injective testnet confirmed
-> inventory committed
```

仅高亮后端确认到达的阶段。不得为了动画完整而提前点亮后续节点。

区块浏览器链接必须来自后端 allowlist 字段，使用新标签页并带
`rel="noopener noreferrer"`。

## 12. 数据获取与实时性

MVP 允许使用轮询，不以尚未完成的 Realtime 投影作为上线前提：

- WAITING：`GET /games/current` 每 2–3 秒，支持 ETag；
- RUNNING：Timeline 每 1–2 秒增量拉取，快照每 5–10 秒校准；
- COMPLETED：结果和交易记录常规缓存；
- 页面重新获得焦点或网络恢复时立即补拉；
- 用 `snapshotSequence` / `nextAfter` 去重和补齐；
- 先应用快照，再应用 sequence 更大的事件。

后续接入 SSE 时继续复用同一事件 schema，并保留轮询回退。

网络状态：

- `LIVE`：最近一次请求成功；
- `RECONNECTING`：显示非阻塞提示，保留最后安全快照；
- `STALE`：超过阈值后显著提示“数据可能已过期”，禁止执行依赖旧状态的 Join/Withdraw；
- 恢复后自动补齐，不要求整页刷新。

## 13. 加载、空态与异常

| 场景 | 行为 |
|---|---|
| 首次加载 | 骨架屏，不先显示 `0/10` |
| Current Game 暂时未创建 | “下一场正在准备”，自动重试 |
| Participants 为空 | “等待第一位 Agent 加入” |
| Recent Games 为空 | 不渲染空表，显示简短说明 |
| API 401 | 保留公开观战数据，Join 时引导登录 |
| API 404 game | 提供返回当前游戏 |
| Timeline 缺口 | 暂停应用后续事件，重新获取快照 |
| 结果尚未完成 | 提供进入实时观战 |

## 14. 响应式与可访问性

- 手机端保持状态、Join CTA、回合和匹配卡片优先；
- 交易表在小屏转为逐条卡片，不横向隐藏关键状态；
- 状态不能只依赖颜色，必须同时有文字或图标；
- 所有 Dialog 支持键盘和焦点回收；
- 动态 Ready 计数和 Game start 使用礼貌级别的 ARIA live region；
- 自动跳转前提供可被屏幕阅读器感知的状态更新；
- 时间和金额使用一致的 locale 格式，并保留完整值的可访问文本。

## 15. 前端安全与数据约束

- 不把 Runtime credential、API Key、钱包私钥或签名原文写入 localStorage；
- PaymentMandate 只保存不透明 ID 和安全状态；
- 所有服务端 public message 当作不受信文本，禁止 `dangerouslySetInnerHTML`；
- 不用 URL query 传递 Secret、钱包授权或内部错误；
- 不在客户端推断库存提交、链上终局或排名；
- 日志与分析事件不记录钱包地址、交易签名和策略文本。

## 16. 埋点

建议事件：

- `game_lobby_viewed`
- `game_join_clicked`
- `game_join_preflight_failed`，只带 safe error code
- `game_mandate_confirm_started`
- `game_mandate_confirm_completed`
- `game_join_completed`
- `game_withdraw_completed`
- `game_watch_entered`
- `game_trade_expanded`
- `game_result_viewed`

属性只包含 gameId、公开 agentId、对外状态、计数和安全错误码。

## 17. 验收标准

1. `/game` 只展示一个 Current Game，不出现房间列表或普通用户 Create/Start。
2. WAITING 首屏准确显示 Ready / Threshold、Participant 和 Join/Withdraw。
3. 不 Ready 的 Hosted Agent 不可提交 Join，并有可操作的错误提示。
4. PaymentMandate 确认页明确展示 testnet、额度、期限和“非立即付款”。
5. 第 10 个 Ready Participant 触发启动后，等待页无需刷新即可进入观战。
6. RUNNING 页面可展示回合、事件、FCFS Pairing 和公开协商更新。
7. Pairing 不显示为成交，accept 只显示 Settling。
8. `chain_confirmed_uncommitted` 不显示 Confirmed。
9. 只有库存提交完成才显示 Confirmed。
10. rejected、timeout 和未配对记录不进入交易表。
11. 断网恢复后可按 sequence 补齐，不重复展示配对或交易。
12. COMPLETED 自动进入结果页，排名和交易与后端权威投影一致。
13. 前端源码、浏览器存储、日志和埋点中没有 Secret、私钥、签名原文或私有推理。
14. 外部 Vercel 前端通过真实 API 域名、CORS、登录、Join、观战和结果页 E2E。

## 18. 实施顺序

1. API client、Current Game 状态仓库与错误码映射；
2. `/game` 单大厅和 Recent Completed Games；
3. Hosted Agent 选择、preflight、PaymentMandate 与 Join/Withdraw；
4. 自动进入 `/game/:gameId`；
5. 实时快照、Timeline 和 Matching Board；
6. Recent Trades 与结算详情；
7. `/result` 排名和完整交易分页；
8. 响应式、可访问性、埋点和 E2E。

## 19. 上线检查

- 后端目标 schema 已冻结并生成前端类型；
- Vercel 环境变量不包含后端私密凭据；
- API 域名 TLS、CORS、Cookie/CSRF 配置通过；
- WAITING / RUNNING / COMPLETED 三态均有测试数据；
- Join 阈值竞态、Withdraw 与 Game start 冲突已验证；
- Timeline 断线补齐已验证；
- x402 V2 / EIP-3009 / PaymentMandate 文案经后端与产品共同确认；
- 公共 Facilitator 未完成真实 testnet 验收时有清晰限制说明；
- 不把测试资产、测试链或受控演示写成生产资金能力。
