# Arena 402 单一当前游戏：后端 PRD

> 版本：v0.1
> 日期：2026-07-25
> 状态：需求草案，待评审
> 负责人：Backend / Arena / Settlement
> 前端配套：[prd-current-game-frontend.md](prd-current-game-frontend.md)

## 1. 背景

Arena 402 面向普通玩家只提供一个“当前游戏”入口。玩家不创建房间、不选择房间，
平台按以下循环持续运行：

```text
WAITING -> RUNNING -> COMPLETED -> 创建下一场 WAITING
```

这里的“满 10 人”不是 10 个登录用户，而是 10 个已经完成 Agent、Runtime、钱包和
本局 PaymentMandate 校验的 Ready Game Participant。

现有仓库已经具备：

- 持久化 Game、Game Participant、Hosted Runtime Binding 和 Game Core；
- 单用户单局唯一参与、Runtime Ready 校验和 Runtime/config 快照冻结；
- `maxParticipants` 数据库级并发上限；
- 回合编排、FCFS 配对、有限轮协商、排名与公开 Timeline；
- 永久 testnet 钱包绑定、受限可撤销 PaymentMandate、幂等
  `reserve / consume / release`、x402 V2 HTTP envelope 和 unattended
  Settlement Worker 的 Fake E2E 基础；
- EIP-3009 direct-relay 链上原型，以及 `submitted`、
  `chain_confirmed_uncommitted`、`inventory_committed` 等恢复状态。

本 PRD 新增“单一当前游戏”产品投影、Ready 语义、Join/Withdraw、自动启动、下一局
创建和面向前端的精简交易接口。现有支付基础可以称为 x402 V2 实现，但标准公共
Facilitator 兼容和当前部署服务上的新鲜 testnet E2E 尚未验收，不能写成生产认证。

## 2. 目标与非目标

### 2.1 目标

1. 任意时刻最多存在一个可加入的当前 Game。
2. 用户使用自己拥有且 Ready 的 Hosted Agent 加入当前 Game。
3. 只有支付、钱包、Runtime 和 Game Participant 全部 Ready 才进入 Ready 计数。
4. 达到 `startThreshold` 后，服务端原子启动 Game，并停止加入和退出。
5. Game 完成后，服务端幂等创建下一场 WAITING Game。
6. 为前端提供稳定的大厅、参与者、实时阶段、配对、交易和结果投影。
7. 保留结算恢复所需的详细内部状态，同时只向普通用户暴露安全、可理解的状态。

### 2.2 非目标

- 普通用户创建、选择、筛选或手动启动房间；
- 同时开放多个 Waiting Game；
- 在本需求中完成 Local Connector Game Adapter；
- 把 Provider 成功、Connector ACK 或协商 `accept` 当作成交；
- 在本需求中重写 Game Core、FCFS 或排名规则；
- 删除结算的 submitted/unknown/confirmed-uncommitted 恢复状态；
- 向前端返回 API Key、钱包私钥、签名原文、策略全文或 chain-of-thought；
- 宣称当前结算已经通过标准公共 Facilitator 或生产 testnet 验收。

## 3. 名词与状态

### 3.1 对外状态

| 状态 | 含义 | 是否可 Join / Withdraw |
|---|---|---|
| `WAITING` | 当前大厅正在等待 Ready Participant | 是 / 是 |
| `RUNNING` | Game 已原子启动，正在运行或生成最终排名 | 否 / 否 |
| `COMPLETED` | 排名与最终结果已持久化 | 否 / 否 |

### 3.2 内部状态映射

| 内部 Game 状态 | 对外状态 |
|---|---|
| `registration`、`portfolio_setup`、`portfolio_locked` | `WAITING` |
| `running`、`final_valuation`，以及各 Round Phase | `RUNNING` |
| `completed` | `COMPLETED` |
| `cancelled` | 不作为当前可玩 Game；只出现在管理员记录中 |

对外状态只能由 Arena 权威状态生成，不能由 Runtime、Connector 或前端推断。
`final_valuation` 在排名事务完成前仍为 `RUNNING`，避免客户端提前显示最终结果。

### 3.3 Ready Participant

一个 Participant 计入 `readyCount` 必须同时满足：

1. Game 仍处于可加入的 WAITING 内部阶段；
2. Agent 属于当前用户且状态为 active；
3. Runtime kind 为 Hosted，Binding 与 Hosted Config 均为 ready；
4. 本局 Runtime/config 快照已冻结；
5. 初始 Portfolio 已校验并锁定；
6. Settlement account 的 chain、token 和地址满足本局配置；
7. 本局 PaymentMandate 已确认、未撤销、未过期，且额度满足本局最坏情况预留；
8. Participant 状态未被取消。

Join 预检失败时不得增加 `readyCount`。不能使用登录人数、前端成功提示、Provider
探活结果或 Connector ACK 替代 Ready 判定。

## 4. 核心业务规则

### 4.1 单一当前游戏

- 数据库必须保证最多一个非终态 Current Game。
- “Current”是服务端投影或显式指针，不由“最新创建时间”临时猜测。
- Current Game 完成后，在同一编排事务或可恢复的幂等 Outbox 流程中创建下一局。
- 下一局创建失败不得回滚已完成 Game；Worker 必须可重试，且不能创建重复 Game。
- `GET /current` 在短暂恢复窗口可返回最后一场 `COMPLETED` 和
  `nextGamePending=true`，但正常目标是直接返回下一场 WAITING。

### 4.2 容量与启动

配置在 Game 创建时冻结：

| 配置 | 测试环境建议 | 正式环境建议 |
|---|---:|---:|
| `startThreshold` | 2，或管理员手动启动 | 10 |
| `maxParticipants` | 12 | 100，默认开赛阈值仍为 10 |

约束：

```text
2 <= startThreshold <= maxParticipants <= 100
```

底层 Game Core 可继续支持更宽的开发范围，但单一大厅产品接口必须应用上述产品上限。
数据库硬上限不能因测试环境或管理员手动启动而关闭。

当 `readyCount >= startThreshold` 时：

1. 锁定 Current Game；
2. 重新校验所有被计数 Participant 的 Ready 条件；
3. 将 Game 从可加入状态切换到不可加入状态；
4. 锁定 Portfolio 和参与者快照；
5. 启动 Game 与首回合；
6. 写入唯一 `game.started` 事件；
7. 提交后由 Worker 开始执行 AgentTask。

以上步骤必须原子或具有等价的幂等恢复语义。并发的第 10 个 Join 只能触发一次启动。

### 4.3 Join

- MVP 只允许 Hosted Agent；Local/Native A2A 在适配器完成前返回不可用。
- 一个 User 在同一 Game 最多一个 Participant。
- 一个 Agent 在同一 Game 最多参与一次，但可参加后续 Game。
- Join 必须携带 Idempotency-Key。
- Join 成功响应只代表 Participant 已进入本局 Ready 集合，不代表 Game 已开始。
- 若本次 Join 达到阈值，响应可以直接返回 `gameStatus=RUNNING`。

### 4.4 Withdraw

- 只允许 Participant 所有者在 WAITING 阶段退出。
- Withdraw 必须幂等；重复退出返回同一最终状态。
- Withdraw 必须在一个事务中取消 Participant、释放未消费的 Mandate reservation，
  并更新 Ready 计数。
- 已 `submitted` 的链上支付不能被 Withdraw 当作从未发生；正常情况下 WAITING 阶段
  尚不应存在交易提交。
- RUNNING 后返回 `409 game_already_started`。

### 4.5 管理员操作

管理员创建、取消、归档和测试环境手动启动属于最后一个实施阶段：

- 不出现在普通用户 API 和前端；
- 手动启动仍必须满足 `minParticipants >= 2`、Ready 校验和硬上限；
- 所有操作写审计事件；
- 取消不得破坏已 submitted 支付的恢复。

## 5. PaymentMandate 基础与单大厅适配

当前代码已经实现：

- GitHub User 永久 testnet 钱包绑定；
- Game-scoped PaymentMandate；
- 单笔 / 累计额度、有效期、撤销和显式 allowed payees；
- 并发安全、幂等的 `reserve / consume / release`；
- revoke 阻止新 reserve，已 submitted 记录继续恢复；
- x402 V2 challenge/retry/header、隔离 signer 和 unattended Worker；
- Fake 全链路测试。

单大厅上线仍需解决一个 P0 契约差异：当前 Mandate 创建要求 Participant 已存在，且
`allowedPayees` 是创建时的显式地址列表；单大厅中的 Participant 会逐个加入，早期
Mandate 不能天然覆盖后加入者。

目标契约必须选择并验证一种安全方案：

1. **推荐：同局动态 payee 规则。** Mandate 冻结
   `allowedPayeeRule=SAME_GAME_SETTLEMENT_ACCOUNT`，Settlement 每次 reserve 时
   校验 payee 是同一 Game 的已冻结 Participant settlement account；
2. 或实现用户明确确认的、只增不减且有审计的 payee allowlist 扩展；
3. 不允许由前端自行提交任意 payee，也不允许因后加入者缺失而在运行中回退为逐笔
   人工确认。

无论采用哪种方案，PaymentMandate 都必须：

- 绑定 User、Game、payer、chain、token 和受限 payee 范围；
- 在 Ready 前验证单笔额度、累计额度、有效期和撤销状态；
- 在 Settlement 提交前再次校验并幂等 reserve；
- 保证 revoke 不抹掉已 submitted 的支付；
- 不让原始密钥、Secret 或未过滤签名材料进入业务数据库、日志或前端。

本 PRD 中的 `paymentMandateId` 始终引用 Settlement 权威记录，额度与状态不由前端
保存或推断。

单大厅接入所需的现有接口差异：

| 当前实现 | 单大厅目标 |
|---|---|
| 创建 Mandate 前要求 `game_participation_required` | 预检生成短期、owner/game/agent 绑定的 `joinAuthorizationId`；Mandate 可凭此在最终 Participant 创建前确认 |
| 客户端提交显式 `allowedPayees` | 后端生成 `SAME_GAME_SETTLEMENT_ACCOUNT` 规则或等价安全范围 |
| Mandate atomic 请求字段使用 JSON number | 产品 v2 使用十进制整数字符串，避免浏览器精度丢失 |
| Join 只校验 Agent/Runtime | Join v2 同时校验 `paymentMandateId` 和 `joinAuthorizationId` |

`joinAuthorizationId` 不是 Participant，不进入 Ready 计数，并在短时间后过期。最终
Join 仍必须重新校验全部条件，不能把预检当作容量预留或启动凭据。

## 6. API 契约

以下为目标产品接口。已有开发接口可作为实现基础，但不构成最终前端契约。所有响应
包含 `schemaVersion`，所有金额使用 atomic 整数字符串及显式 decimals。

### 6.1 获取当前游戏

```http
GET /api/v1/games/current
```

公开、可缓存、无需登录。

```json
{
  "game": {
    "gameId": "game-20260725-001",
    "status": "WAITING",
    "readyCount": 7,
    "startThreshold": 10,
    "maxParticipants": 100,
    "roundCount": 5,
    "currentRound": 0,
    "roundPhase": null,
    "joinedByMe": false,
    "participants": [
      {
        "participantId": "gagent-public-id",
        "agentId": "agent-public-id",
        "displayName": "Merchant Fox",
        "runtimeKind": "hosted",
        "readiness": "READY",
        "joinedAt": "2026-07-25T10:00:00Z"
      }
    ],
    "createdAt": "2026-07-25T09:00:00Z",
    "startedAt": null,
    "completedAt": null
  },
  "nextGamePending": false,
  "schemaVersion": "arena.current-game.v1"
}
```

匿名请求的 `joinedByMe` 为 `false`；登录请求由服务端按 principal 计算。不得返回
`userId`、Runtime credential、策略说明或钱包地址。

### 6.2 最近完成游戏

```http
GET /api/v1/games/recent?limit=5&cursor=<opaque>
```

- 默认 `limit=5`，最大 20；
- 只返回 `COMPLETED`；
- 游标必须不透明且排序稳定；
- 返回 gameId、完成时间、冠军 Agent、参与数和结果页链接所需字段。

### 6.3 Join 预检

```http
POST /api/v1/games/{gameId}/join-preflight
Authorization: session
Idempotency-Key: <unique>
X-CSRF-Token: ...
Content-Type: application/json

{
  "agentId": "agent-123"
}
```

响应：

```json
{
  "eligible": true,
  "readyToJoin": false,
  "joinAuthorizationId": "join-auth-opaque-id",
  "joinAuthorizationExpiresAt": "2026-07-25T10:10:00Z",
  "checks": {
    "agent": "READY",
    "runtime": "READY",
    "settlementAccount": "READY",
    "paymentMandate": "ACTION_REQUIRED"
  },
  "mandateRequirements": {
    "chainId": 1439,
    "tokenSymbol": "mockUSDC",
    "tokenDecimals": 6,
    "maxPerPaymentAtomic": "10000000",
    "maxCumulativeAtomic": "50000000",
    "allowedPayeeRule": "SAME_GAME_SETTLEMENT_ACCOUNT",
    "expiresAt": "2026-07-25T12:00:00Z"
  },
  "portfolioRequirements": {
    "initialNetWorthAtomic": "20000000",
    "goldDecimals": 6,
    "initialPricesAtomic": {
      "grain": "2000000",
      "iron": "5000000",
      "warhorse": "8000000",
      "gems": "3000000"
    },
    "allowedGoods": ["grain", "iron", "warhorse", "gems"],
    "defaultPortfolio": {
      "cashAtomic": "20000000",
      "holdings": {
        "grain": 0,
        "iron": 0,
        "warhorse": 0,
        "gems": 0
      }
    }
  },
  "safeErrorCode": null,
  "schemaVersion": "arena.game-join-preflight.v1"
}
```

预检是提示，不是锁；`joinAuthorizationId` 不占用 Participant 名额，Join 时必须
重新校验。

### 6.4 获取钱包与当前 Mandate

复用现有 owner-scoped API：

```http
GET /api/v1/me/wallet
GET /api/v1/me/payment-mandates/{gameId}
```

钱包响应可返回 testnet 地址用于用户确认，但不得返回 `secretRef`、密文、KEK、
私钥或签名材料。Mandate 响应返回额度、已预留、已消费、有效期和撤销时间。

### 6.5 创建或确认本局 PaymentMandate

```http
POST /api/v1/me/payment-mandates
Authorization: session
Idempotency-Key: <unique>
X-CSRF-Token: ...

{
  "mandateId": "mandate-opaque-id",
  "gameId": "game-20260725-001",
  "joinAuthorizationId": "join-auth-opaque-id",
  "chainId": 1439,
  "tokenAddress": "0x...",
  "maxPerPaymentAtomic": "10000000",
  "maxCumulativeAtomic": "50000000",
  "allowedPayeeRule": "SAME_GAME_SETTLEMENT_ACCOUNT",
  "validFrom": "2026-07-25T09:55:00Z",
  "expiresAt": "2026-07-25T12:00:00Z"
}
```

所有 chain、token、额度下限和 payee 规则由后端根据 Game 配置校验，客户端不能
扩大范围。响应只返回安全投影，不返回 Secret：

```json
{
  "mandate": {
    "mandateId": "mandate-opaque-id",
    "gameId": "game-20260725-001",
    "status": "ACTIVE",
    "maxPerPaymentAtomic": "10000000",
    "maxCumulativeAtomic": "50000000",
    "reservedAtomic": "0",
    "consumedAtomic": "0",
    "expiresAt": "2026-07-25T12:00:00Z"
  },
  "schemaVersion": "arena.payment-mandate.v1"
}
```

现有显式 `allowedPayees` 请求字段可保留为兼容路径，但单大厅产品模式必须由服务端
生成或使用上述动态规则。Mandate 的签名、reservation 和恢复流程仍由 Settlement
契约拥有。

撤销复用：

```http
POST /api/v1/me/payment-mandates/{mandateId}/revoke
```

### 6.6 加入当前游戏

复用并版本化现有参与语义：

```http
POST /api/v1/games/{gameId}/participants
Authorization: session
Idempotency-Key: <unique>
X-CSRF-Token: ...

{
  "agentId": "agent-123",
  "paymentMandateId": "mandate-opaque-id",
  "joinAuthorizationId": "join-auth-opaque-id",
  "portfolio": {
    "cashAtomic": "2000000",
    "holdings": {
      "grain": 1,
      "iron": 1,
      "warhorse": 1,
      "gems": 1
    }
  }
}
```

`portfolio` 必须按预检返回的初始价满足：

```text
cashAtomic
  + grain*2000000
  + iron*5000000
  + warhorse*8000000
  + gems*3000000
= 20000000
```

`cashAtomic` 使用十进制整数字符串，持仓数量使用非负整数。服务端校验并在 Join
事务中锁定组合，开赛时不得再次随机分配或覆盖。为兼容尚未升级的客户端，
`portfolio` 暂时可省略并回退为 20 金全现金；产品前端应始终显式提交用户确认的
组合。

```json
{
  "participant": {
    "participantId": "gagent-123",
    "gameId": "game-20260725-001",
    "agentId": "agent-123",
    "runtimeKind": "hosted",
    "readiness": "READY",
    "joinedAt": "2026-07-25T10:00:00Z"
  },
  "gameStatus": "WAITING",
  "readyCount": 8,
  "startThreshold": 10,
  "schemaVersion": "arena.game-participation.v2"
}
```

主要错误：

| HTTP | code | 含义 |
|---:|---|---|
| 404 | `game_not_found` / `agent_not_found` | 资源不存在或不属于当前用户 |
| 409 | `game_not_current` | 请求指向旧 Game |
| 409 | `game_already_started` | Game 已开始 |
| 409 | `participant_limit_reached` | 达到硬上限 |
| 409 | `user_already_joined` | 用户已用另一 Agent 加入 |
| 409 | `runtime_not_ready` | Runtime 未 Ready |
| 409 | `mandate_not_ready` | Mandate 未确认、过期、撤销或额度不足 |
| 422 | `invalid_portfolio` | 初始组合包含未知货物、非法数量或总值不等于 20 金 |
| 409 | `idempotency_conflict` | Key 被不同请求复用 |

### 6.7 退出等待游戏

```http
DELETE /api/v1/games/{gameId}/participants/{participantId}
Authorization: session
Idempotency-Key: <unique>
X-CSRF-Token: ...
```

```json
{
  "participantId": "gagent-123",
  "status": "WITHDRAWN",
  "gameStatus": "WAITING",
  "readyCount": 7,
  "schemaVersion": "arena.game-participation-withdrawal.v1"
}
```

### 6.8 游戏公开快照

```http
GET /api/v1/games/{gameId}
```

返回：

- 对外 Game 状态、内部阶段的安全显示值；
- 当前回合 / 总回合；
- Participant 公共身份和状态；
- 当前公开世界事件和参考价格；
- 当前配对和精简协商状态；
- 完成后的最终价格和排名；
- `snapshotSequence`，供客户端与 Timeline 对齐。

现有 `/api/v1/pawnhouse/games/{gameId}` 可作为实现基础，但产品响应不得泄露内部
恢复细节或不稳定数据库字段。

### 6.9 Timeline 增量

```http
GET /api/v1/games/{gameId}/timeline?after=<sequence>&limit=100
```

MVP 可轮询；后续可在不改变事件语义的前提下增加 SSE。公开事件至少包括：

- `game.started`
- `round.started`
- `pairing.created`
- `negotiation.updated`
- `settlement.updated`
- `round.closed`
- `game.completed`

事件必须来自持久化 Outbox/Timeline。客户端断线重连后以 sequence 补齐，不以进程内
广播作为唯一事实来源。

### 6.10 交易列表

```http
GET /api/v1/games/{gameId}/trades?limit=20&cursor=<opaque>&status=<optional>
```

- 默认 20，最大 50；
- 结果页使用不透明游标完整分页；
- 只包含已接受协商及其结算状态；
- `rejected`、`timeout` 和未配对记录不进入交易列表。

```json
{
  "trades": [
    {
      "tradeId": "trade-opaque-id",
      "round": 3,
      "buyer": {"agentId": "agent-a", "displayName": "A"},
      "seller": {"agentId": "agent-b", "displayName": "B"},
      "asset": {"id": "iron", "displayName": "精铁", "quantity": 1},
      "price": {
        "amountAtomic": "5500000",
        "decimals": 6,
        "symbol": "mockUSDC"
      },
      "status": "Settling",
      "transaction": {
        "chainId": 1439,
        "txHash": "0x...",
        "explorerUrl": "https://...",
        "confirmationCount": 0
      },
      "acceptedAt": "2026-07-25T10:10:00Z",
      "confirmedAt": null,
      "settlementFlow": {
        "protocol": "x402_v2_eip3009_direct_relay",
        "stage": "submitted"
      }
    }
  ],
  "nextCursor": null,
  "schemaVersion": "arena.game-trades.v1"
}
```

公开状态映射：

| 内部事实 | 对外状态 |
|---|---|
| `accepted_pending_settlement`、reserved、approved、`submitted`、`submitted_unknown`、`chain_confirmed_uncommitted` | `Settling` |
| `inventory_committed` 且配对 settled | `Confirmed` |
| 已接受后明确终止且确认未完成库存提交 | `Failed` |

`txHash` 只有实际提交后才返回。`explorerUrl` 由后端按受信 chain allowlist 构造。
详情中的流程名称必须如实标识 x402 V2 envelope 下的 EIP-3009 direct relay。在
公共 Facilitator 真实 testnet 验收前，前端不得显示“公共 x402 生产认证”。

### 6.11 排名结果

```http
GET /api/v1/games/{gameId}/result
```

只在 `COMPLETED` 后返回 200，包含：

- 最终排名和 atomic 净资产；
- 最终结算价格；
- Game / event schedule commitment 与公开 seed；
- 交易数量摘要；
- 交易分页入口。

未完成时返回 `409 game_not_completed`，不得生成临时最终排名。

## 7. 数据与并发要求

建议新增或扩展：

- Current Game 指针或数据库唯一非终态约束；
- `start_threshold` 与产品配置版本；
- Participant readiness 状态及逐项安全检查结果；
- `payment_mandate_id` 引用和 reservation 状态；
- Withdraw 时间、原因和幂等记录；
- 可分页的 public trade projection；
- 下一局创建 Outbox / orchestration job。

必须满足：

1. Join、Withdraw、自动启动均使用数据库锁和唯一约束；
2. Ready 计数来自权威行查询，不依赖可漂移缓存；
3. 所有写操作幂等；
4. Runtime/config、Game 配置和 Participant view 在 Join/Task 创建时冻结；
5. 库存只在链上确认后幂等提交；
6. `chain_confirmed_uncommitted` 只重试库存提交，不重新付款；
7. 游戏结束与下一局创建可在 Worker 重启后恢复。

## 8. 安全、权限与隐私

- Join/Withdraw/Mandate 需要登录、CSRF 和 owner 校验；
- 管理员接口与普通用户接口物理或权限隔离；
- Public Participant 只返回 Agent 公共显示信息；
- 公共谈判消息先经过现有 PublicOutputPolicy；
- 不返回 Prompt、strategy instructions、Provider 原始响应或 chain-of-thought；
- 不记录或返回原始 API Key、钱包私钥、seed phrase 和 Connector credential；
- Explorer URL 仅允许受信 chain 域名；
- 所有 safe error 使用枚举，不将 Provider/数据库内部异常原文暴露给前端。

## 9. 可观测性

最低指标：

- Current Game 状态和状态停留时长；
- `readyCount`、Join/Withdraw 成功率及错误码；
- 达阈值到 `game.started` 的延迟；
- 自动启动重复触发次数，目标为 0；
- 下一局创建延迟和失败重试次数；
- Runtime task P50/P95/P99；
- Settling 数量与各内部恢复状态停留时长；
- `chain_confirmed_uncommitted` 恢复成功率；
- 每局交易、确认、失败和库存提交数量。

审计记录只包含结构化状态、时间、主体 ID、幂等键摘要和支付证据，不包含 Secret。

## 10. 验收标准

1. 并发 10 个合格 Join 最终只有 10 个唯一 Ready Participant，并只启动一次。
2. 不合格 Agent、Runtime、钱包或 Mandate 不增加 Ready 计数。
3. 第 10 个 Ready Join 与 Game 启动之间没有可被第 11 个请求绕过的竞态。
4. 测试配置可在 2 人自动或管理员手动启动，但始终执行硬上限。
5. WAITING 可幂等 Withdraw，RUNNING 不可 Withdraw。
6. Game 完成后自动、幂等创建下一场 WAITING Game。
7. 前端断线后可用快照和 Timeline sequence 恢复当前展示。
8. `accept` 后显示 Settling；链上确认但库存未提交仍显示 Settling。
9. 只有库存幂等提交后显示 Confirmed。
10. rejected/timeout 协商不出现在交易列表。
11. 结果页排名与 Game Core 权威排名完全一致。
12. API 响应和日志不包含 Secret、私钥、原始策略或 chain-of-thought。

## 11. 实施顺序

1. Current Game 权威指针与公共投影；
2. Join preflight、单大厅 payee 规则适配、PaymentMandate 引用、Ready 投影；
3. Join / Withdraw 与数据库并发约束；
4. Ready 阈值和原子自动启动；
5. Public Timeline、配对与精简交易投影；
6. 完成后下一局自动创建；
7. 结果接口与完整交易分页；
8. 管理员创建、取消、手动启动和归档。

## 12. 发布门槛

生产自动启动前必须完成：

- 单大厅动态 Participant 下的 Mandate payee 覆盖验证；
- Hosted Agent、Settlement 和数据库并发压测；
- 10 Agent 完整 Game 运行验证；
- submitted/unknown/chain-confirmed-uncommitted 重启恢复演练；
- 外部 Vercel 前端、API 域名、CORS 和鉴权 E2E；
- 当前部署服务上的新鲜 Injective EVM testnet 端到端验证；
- 标准公共 Facilitator 兼容验收，或在发布说明中明确只支持项目 Facilitator。

没有上述证据时只能标记为测试环境或受控演示，不得宣称生产 E2E 已完成。
