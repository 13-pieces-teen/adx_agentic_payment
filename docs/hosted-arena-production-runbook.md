# Arena 402 Hosted 上线部署与实现方案

> 状态：2026-07-25 批准的 Hosted testnet 上线目标。
>
> 当前仓库已经具备 Hosted Agent、持久化 AgentTask/Result、12 Agent 本地
> 编排、单笔 EIP-3009 SettlementIntent、只读链上确认和确认后库存提交。
> 当前逐笔人工批准 bridge 只是开发阶段验证工具，不是上线支付方案。
> 本文中的自动 Mandate、guest wallet 和 Settlement Worker 尚待实现；完成本文
> 验收前，不得宣称 Hosted Agent 已能无人值守完成整局支付。

## 1. 上线目标

首发只交付一个清晰闭环：

```text
用户创建 Hosted Agent
  -> 加入 testnet Game，并一次性同意该局自动支付
  -> 10 个 Hosted Agent 参加；Hosted Worker 最多 5 个调用同时在途
  -> 每种货物按数据库时间 FCFS 配对
  -> 最多三轮公开协商
  -> accept 后自动 reserve 支付额度
  -> 平台 testnet guest wallet 自动签署 EIP-3009
  -> 自动提交 Injective EVM testnet 转账
  -> 链上确认后自动更新现金和货物
  -> 自动进入下一回合并生成最终排名
```

用户不需要逐笔点击确认。浏览器和用户电脑离线后，Hosted Agent 仍能完成决策、
协商和 testnet 支付。

MVP 上线容量目标：

- 展示局默认 10 个 Hosted Agent，硬上限 12；
- 固定展示 5 回合；代码继续支持 1–10 回合，但不作为首发容量承诺；
- Decide Task 同批创建，Hosted Provider 调用最多 5 个同时在途；
- 最多 5 组协商并发，同一组内严格顺序执行；它与 Hosted Provider 全局并发 5
  共用同一上限；
- 最多 2 笔自动 Settlement 同时在途；
- 首发部署同一时间只运行一局 active Game。

## 2. 首发范围

### 2.1 包含

- Hosted Agent；
- DeepSeek/OpenAI-compatible allowlisted Provider；
- 平台托管、隔离、testnet-only 的 guest wallet；
- 加入 Game 时一次性创建的受限 PaymentMandate；
- EIP-3009 direct settlement；
- PostgreSQL 持久化任务、额度、结算和游戏状态；
- 现有 Vercel 前端与腾讯云后端；
- 自动链上确认、库存提交、回合推进和排名。

### 2.2 不包含

- Local Connector 游戏 Adapter；
- Native A2A Endpoint；
- 用户自带钱包的无人值守签名；
- 主网或真实资金；
- 标准 HTTP x402 challenge/retry/header；
- escrow、退款、争议和手续费；
- Redis、Kafka、独立消息队列；
- Kubernetes、多地域、自动扩缩容和多副本高可用；
- 同一部署并行运行多局比赛。

这些能力不进入首发关键路径。

## 3. 最小生产拓扑

```text
Vercel frontend
      |
      v
Caddy -> FastAPI API
             |
             v
         PostgreSQL
          /   |   \
         /    |    \
Hosted Worker |  Settlement Worker
               |
          Arena Worker
```

只新增一个长期运行进程：`settlement-worker`。

| 进程 | 职责 | 明确禁止 |
|---|---|---|
| API | 登录、Hosted Agent、Game/参赛命令、只写 Secret ingress | 读取模型 Key、读取钱包 Key、签名、提交交易 |
| Hosted Worker | 读取单个模型 Secret、执行 AgentTask、提交候选 Result | 修改 pool、negotiation、inventory、mandate、settlement |
| Credential Controller | 撤销 Hosted 模型 Secret | 读取模型 Key、访问钱包 Key |
| Arena Worker | 回合编排、Result apply、Finalizer、只读查链、库存提交 | 读取钱包 Key、签名、广播交易 |
| Settlement Worker | Mandate reserve、guest wallet 签名、EIP-3009 提交、记录 tx hash | 调用 Provider、修改游戏报价、直接提交库存 |
| PostgreSQL | 唯一持久化队列和业务状态权威 | 保存原始模型 Key、钱包私钥、助记词或签名 |

第一版不新增独立 HTTP Facilitator 服务。复用现有 settlement 模块的校验和提交逻辑，
作为 `settlement-worker` 内部 library 调用，直接通过 viem 向 Injective EVM
testnet 提交。这样避免额外的内部 HTTP 跳转和第二套运行状态。

自动支付新增的外部依赖只保留两个：

- 批准的 Secret Manager：模型 Secret、guest signer Secret、treasury/relay Secret
  使用不同前缀与最小读取策略；
- 一个 Injective EVM RPC：提交、确认，并在结果未知时按 block/transaction
  扫描恢复。Blockscout 只作为运维查看工具，不是运行时依赖。

## 4. 自动支付模型

### 4.1 Guest wallet

首发 Hosted Agent 只使用 `sandbox_guest` 结算账户：

- 每个 Game Participant 拥有一个独立 testnet EVM 地址；
- 私钥只由 Settlement Worker 的 signer port 使用；
- 数据库只保存地址和不可逆/不透明的 `signer_key_id`；
- signer 根密钥或派生材料只存在于批准的外部 Secret Manager；
- Hosted Worker、Arena Worker、API 和前端都不能读取 signer Secret；
- Settlement Worker 的链上账户只持有测试资产，不承载主网或真实资金。

Game 启动前，平台为每个 guest wallet 自动准备该局最大初始现金对应的 mUSDC。
EIP-3009 的 payer 不需要持有 gas；Settlement Worker 的 relay account 负责 testnet
gas。首发使用一个 testnet-only treasury/relay account 同时负责初始 mUSDC 分发和
relay gas；它与每个 Participant 的 guest wallet 分离。首发不实现赛后自动 sweep，
过期 Game 的测试钱包由运维批次清理。

Wallet provisioning 直接复用 `participant_settlement_accounts`，不新增独立 job
表：

```text
provisioning
  -> Settlement Worker creates one scoped Secret
  -> stores address + signer_key_id
  -> funds initial mUSDC
  -> ready
```

Settlement Worker 直接 claim `provisioning` account row；Game start 只接受全部账户
为 `ready` 的 Participant。

Provisioning 以 `game_participant_id` 为幂等键：同一 Participant 只能对应一个
确定的 Secret 名称和一个链上地址。Settlement Worker 使用专用的
guest-signer Secret Creator + exact-key Reader 身份生成私钥并写入 Secret Manager；
数据库只记录地址和 `signer_key_id`。API、Hosted Worker、Arena Worker 和
Credential Controller 均无这组权限。

Secret 名称由 `game_participant_id` 确定。create 成功或返回 AlreadyExists 后，
Worker 只读取这个确切 Secret、派生地址并写数据库；create 结果未知时等待后按同名
Get/Create 恢复，不换名字、不生成第二个钱包。数据库提交失败留下的同名 Secret
会在下一次 claim 时被复用。

Funding 与正常 Settlement 复用同一个 `submit_relay_transaction()`：它先锁定唯一
`relay_accounts` row，为 treasury/relay EOA 分配 nonce，并在广播前持久化
relay nonce、目标、金额、起始 block 和重建交易所需的非秘密参数。广播后再记录
tx hash。若发送响应丢失，Worker 先用 RPC 按 relay address + nonce 扫描；未找到时
只能用相同 relay nonce、guest 地址和金额重发替换交易。一个 EOA nonce 最终只能
确认一笔交易，因此不会重复发放。

Allocator 只有在前一个 nonce 已获得明确的 RPC send 结果或已恢复链上交易后，才
分配下一个 nonce；遇到发送结果未知时，全局广播短暂停在该 nonce，先恢复或重发，
不跳号。这个短临界区只串行广播，不串行 Provider 调用、授权签名或链上确认。

Game start 只接受 funding receipt 已达到两个确认、且 token `Transfer` event 完全
匹配的 `ready` account。结果未知时不创建第二个钱包，也不分配新的 relay nonce。
首发不做在线 signer 轮换；signer 不可用时禁止启动新 Game。启动前只检查
treasury 的 mUSDC 与 relay gas 是否足以覆盖该局，不实现自动充值。

### 4.2 一次性 Mandate

用户加入 Game 时，一次性同意该局自动支付。API 在参赛事务中创建：

```text
PaymentMandate
  game_id
  game_participant_id
  chain_id
  token_address
  max_per_trade_atomic
  max_total_atomic
  expires_at
  status = active | revoked | expired
```

首发不实现任意 payee allowlist。合法 payee 被限定为同一 Game 中、由 Arena 配对出的
seller settlement address。单笔额度不能超过当前 Game cash，也不能超过
`max_per_trade_atomic`；累计额度不能超过 `max_total_atomic`。

Game 创建时必须冻结 `max_trade_price_atomic`。首发计算：

```text
max_per_trade_atomic = max_trade_price_atomic
max_total_atomic = round_count * max_trade_price_atomic
expires_at = game_expires_at
```

因为每个 Agent 每回合最多购买一次，这个上界覆盖整局最大累计买入次数。每次
`propose` 和 `accept` 都必须先通过 `price <= max_trade_price_atomic` 与当前 buyer
cash 校验，不能把超额报价推迟到 Settlement 阶段处理。

`reserve_payment_mandate(intent_id)` 必须在一个 PostgreSQL 事务里依次锁定
Intent、Mandate、buyer Participant cash 和该 Mandate 的 reservation rows，然后
校验：

- Intent 仍可执行，Mandate 为 active 且未过期；
- buyer 当前 cash 不小于本笔 amount；
- amount 不超过 `max_per_trade_atomic`；
- 已有 `reserved | consumed` reservation 之和加本笔 amount 不超过
  `max_total_atomic`；
- 同一 Intent 不存在第二条 reservation；
- 同一 buyer 在同一 Round 不存在另一笔 settlement。

数据库对 `(round_id, buyer_participant_id)` 增加唯一约束。配合“每个 Agent 每回合
最多成交一次”以及“本轮 Settlement 终态后才进入下一轮”，这同时关闭并发超卖和
跨轮占款问题。额度统计直接从 reservation rows 求和；不维护可漂移的聚合计数字段。

Mandate 可由其所属用户通过鉴权 API 撤销。撤销后不再创建新 reservation；该
Participant 后续的 `buy` 结果无法通过 Arena apply 并按规则收敛为 `pass`，但
`sell | pass` 仍可执行。已经 reserve 或已经提交的 Intent 继续完成，首发不实现
链上取消或中途退款。

`revoke_payment_mandate` 与 `reserve_payment_mandate` 必须锁定同一 Mandate row：
revoke 先提交，则后续 reserve 失败；reserve 先提交，则该 Intent 继续完成，而
revoke 只阻止下一笔。该顺序就是首发唯一的撤销竞态规则。

Mandate 是自动支付所需的最小授权边界，不是额外支付流程。它只实现三个额度动作：

```text
reserve(settlement_intent_id)
consume(settlement_intent_id)
release(settlement_intent_id)
```

每个动作使用 SettlementIntent ID 做唯一键，并在 PostgreSQL 行锁/CAS 中完成。

### 4.3 自动结算主链路

```text
Arena validates accept
  -> freeze immutable SettlementIntent
  -> Settlement Worker claims Intent
  -> validate Game/participant/token/payee/amount/expiry
  -> reserve Mandate amount
  -> freeze EIP-712 domain, validAfter, validBefore
  -> derive deterministic EIP-3009 nonce from Intent hash
  -> sign with buyer guest wallet
  -> allocate relay EOA nonce and submit transferWithAuthorization
  -> persist tx hash, relay nonce and authorization nonce digest
  -> Arena Worker reads receipt and exact Transfer event
  -> mark chain_confirmed_uncommitted
  -> atomically consume reservation and commit cash/holding
  -> mark inventory_committed
  -> Round may close
```

`accept`、签名成功、交易提交和链上确认仍是不同状态。模型 Runtime 永远不参与签名，
Settlement Worker 也不能修改价格、货物、数量或双方。

Game 创建时冻结 `required_confirmations = 2`。Arena Worker 只有在满足确认深度后，
才能把 Intent 标记为 `chain_confirmed_uncommitted`，并且必须校验：

- receipt status 为 success，且再次读取时 block hash 不变；
- receipt 的 chain、token contract 与 Intent 一致；
- `transferWithAuthorization` calldata 中的 payer、payee、amount、nonce 与冻结
  Intent 一致；
- receipt 中存在完全一致的 token `Transfer` event。

首发使用同一个 Injective RPC 完成正常确认与恢复，不做多 RPC quorum。结果未知时
从 `broadcast_started_block` 起按 relay address + relay nonce 扫描交易，并解码
token calldata；Blockscout 不能替代 RPC receipt 与 event 校验。

### 4.4 最小失败语义

只保留支付正确性必需的两类失败：

1. **提交前确定失败**
   - 例如 Mandate 超额、过期、wallet 未就绪；
   - 不广播交易；
   - release 已 reserve 额度；
   - pairing 标记 `settlement_failed`，本轮不成交并继续。
2. **提交后结果未知**
   - 已经获得 tx hash 或请求可能已发送；
   - 保持 `submitted_unknown`；
   - 有 tx hash 时查询同一交易；
   - 无 tx hash 时先查询 token 的 `authorizationState(authorizer, nonce)`；
   - authorization 尚未使用时，只重发完全相同的 EIP-3009 authorization；
   - authorization 已使用时，从 RPC block/transaction 按 relay EOA nonce 与 calldata
     authorization nonce 恢复原 tx hash；
   - 可以用同一 relay EOA nonce 替换未确认交易，但禁止分配新 relay nonce、生成新
     authorization nonce、创建第二个授权或重新定价。

链上明确 revert 时，受限数据库函数原子 release reservation 并把 pairing 标记为
`settlement_failed`。链上成功后，`commit_confirmed_inventory` 在同一数据库事务中
consume reservation、更新现金/持仓并标记完成。数据库提交失败时保持
`chain_confirmed_uncommitted`，只重试库存事务，不重新付款。

`submitted_unknown` 不无限等待：Worker 按固定间隔执行上述同授权恢复。到链上某个
block 的 timestamp 已超过 authorization `validBefore`、且该 block 又获得两个确认
后，如果 token authorization 仍未使用且不存在成功 receipt，才原子 release 并标记
`settlement_failed`。恢复过程中 EIP-3009 authorization 输入保持不变，relay 交易
保持同一 EOA nonce，因此网络超时不会产生第二笔付款。

不增加通用补偿工作流、死信队列或多级 fallback。

## 5. 代码实现

### 5.1 PostgreSQL migration

新增一个连续 migration，完成：

1. `payment_mandates`；
2. `payment_reservations`；
3. `participant_settlement_accounts.status`、`signer_key_id`、`funding_tx_hash` 与
   funding relay nonce/start block、provisioning lease，并对 `game_participant_id`
   唯一；
4. `settlement_intents.mandate_id`、`reservation_id`、EIP-712 domain、
   `authorization_valid_after`、`authorization_valid_before`、authorization nonce
   digest、relay nonce、broadcast start block、tx hash 与 Worker lease 字段；
5. 单行 `relay_accounts` nonce allocator；funding 与 Settlement 共用；
6. `games.max_trade_price_atomic`、`game_expires_at`、`required_confirmations`；
7. `payment_reservations(round_id, buyer_participant_id)` 唯一约束；
8. 单 active Game 的数据库约束；
9. `reserve_payment_mandate(intent_id)`；
10. `consume_payment_mandate(intent_id)`；
11. `release_payment_mandate(intent_id)`；
12. `revoke_payment_mandate(mandate_id, user_id)`；
13. Settlement Worker 专用数据库 role 和最小函数授权。

不新增独立 `settlement_jobs` 表。Settlement Worker 直接 claim
`settlement_intents` 中可执行的状态，使用 `FOR UPDATE SKIP LOCKED`、`leased_by`
和 `lease_expires_at` 恢复重启。

### 5.2 Arena

修改 `arena_game/postgres.py`：

- Hosted Participant 加入时创建 guest settlement account 和 Mandate；
- Game 启动前检查全部 Hosted Participant 的 wallet/mandate 为 ready；
- negotiation apply 校验冻结的 `max_trade_price_atomic` 和 buyer 当前 cash；
- accept 时在同一事务冻结 Intent；
- 不再生成 `human approval required` 状态；
- known settlement failure 关闭 pairing，但不移动库存；
- `inventory_committed` 后恢复双方 active 状态；
- 所有 pending settlement 完成后允许 Round close。

Arena 仍不签名、不读取 signer Secret、不提交交易。

### 5.3 Settlement Worker

在 `agent-arena/settlement/` 增加一个无公网端口的 Worker：

```text
claim Intent
  -> reserve
  -> sign
  -> submit
  -> record tx hash
```

Worker 必须：

- 只处理 `sandbox_guest + single_eip3009`；
- claim `provisioning` account、创建 signer Secret、记录地址并自动分发初始 mUSDC；
- 以 Participant 和 Intent 为幂等边界，恢复 wallet funding 与交易提交；
- 使用一个数据库化 relay EOA nonce allocator；授权准备可并发，广播分配 nonce
  短暂串行，已广播交易可同时等待确认；
- 使用冻结 Intent hash 派生确定性 nonce；
- 崩溃后只从冻结的 domain、validity 与 nonce 重新生成同一 authorization；
- 提交前重新验证 token、chain、payer、payee 和 amount；
- 从 Secret Manager 读取当前 `signer_key_id` 对应材料；
- 仅记录 tx hash、nonce digest、安全错误码和时间；
- 在日志和异常中禁止输出 private key、signature、raw nonce；
- 每个 Intent 最多存在一个链上支付。

现有 Arena Worker 继续负责 read-only confirmation 与库存提交，不把两种权限合并。

### 5.4 API 与前端

生产 API 增加正式 Game command：

```text
POST /api/games
POST /api/games/{game_id}/participants
POST /api/games/{game_id}/start
POST /api/games/{game_id}/participants/{participant_id}/payment-mandate/revoke
```

Hosted Agent 加入时：

- 前端只展示一次 testnet 自动支付说明和该局最大额度；
- 用户点击 Join 即确认该局 Mandate；
- 不要求用户输入钱包地址；
- 不在每笔交易前弹确认框；
- wallet/mandate 未 ready 时不能启动 Game。

revoke command 只能由 Participant 所属用户发出，必须使用现有 Session/CSRF
保护，并返回 Mandate 状态以及仍在完成中的 Intent。它不是逐笔审批入口，也不能
撤回已经 reserve 或提交的链上付款。

公开时间线显示：

- settlement intent created；
- payment submitted；
- chain confirmed；
- inventory committed；
- settlement failed。

不显示私钥、签名、原始 nonce、模型 Key 或 private reasoning。

### 5.5 Production Compose

当前 `docker-compose.production.yml` 只包含已有 API、Hosted Worker、Credential
Controller 和 Arena Worker；Arena Worker 默认启用，Hosted profile 使用单机
AES-GCM ciphertext vault，并在 master-key 文件、数据库角色与真实 Provider
配置验收后显式启用。腾讯 SSM 保留为可选高安全后端。它还不能启动自动支付。
本文已先把现有服务默认值压到 2C4G 基线，但以下 Settlement Worker 接线是上线前的
代码阻塞项，不能用文档配置替代。

`ADX_ARENA_CORE_ENABLED=true` 同时挂载经过认证的 Pawnhouse participation
入口，并在 API 进程启动 Connector dispatcher。dispatcher 必须留在 API
进程，因为活跃 Connector WebSocket ownership 是进程内状态；独立的
`arena-worker` 继续负责 Arena coordination、deadline finalization、Game
推进和链上确认恢复，不能从 Connector ACK 推断游戏动作或支付结果。

在现有 production Compose 中增加：

- `settlement-worker` profile/service；
- `cpus: 0.20`、`mem_limit: 256m`；
- Settlement Worker 专用数据库登录；
- guest signer Secret Creator + exact-key Reader 凭据；
- relay account Secret Reader 凭据；
- `ADX_SETTLEMENT_WORKER_CONCURRENCY=2`；
- `ADX_AUTOMATIC_TESTNET_SETTLEMENT_ENABLED`。

保持单个 PostgreSQL、单个 API、单个 Hosted Worker、单个 Arena Worker、单个
Settlement Worker。所有 Worker 无公网端口。只有 Worker entrypoint、migration、
DB role 和 Secret IAM 测试通过后，才把该 service 接入 `deploy.sh` 的 MVP profile。

## 6. 2C4G/70GB MVP 容量配置

首发基线：

```text
host: 2 vCPU / 4 GB RAM / 70 GB disk
active games: 1
max participants: 12
default participants: 10
demo rounds: 5
hosted task concurrency: 5
negotiation concurrency: 5
settlement concurrency: 2
API processes: 1
API max concurrency: 64
PostgreSQL max_connections: 40
```

常驻容器内存上限：

| 服务 | 内存上限 |
|---|---:|
| PostgreSQL | 768 MB |
| API | 512 MB |
| Hosted Worker | 512 MB |
| Arena Worker | 320 MB |
| Settlement Worker（待实现） | 256 MB |
| Credential Controller | 192 MB |
| Caddy | 128 MB |

常驻容器合计约 2.7 GB，给宿主机、Docker、页缓存和短时 migration 留约 1.3 GB。
Vercel 前端不占用这台服务器；`legacy-web` profile 不得在 MVP 生产机启动。

40 个数据库连接按当前连接池上界预留：API 最多 20、Hosted Worker 5、Arena
Worker 5、Credential Controller 2、Settlement Worker 2，剩余 6 个留给
migration、健康检查和运维。不得再增加进程或连接池而不重新计算该预算。

关键实现要求：

- 一轮全部 Decide Task 创建完成后，Hosted Worker 才开始 claim；
- Coordinator 使用一个事务批量创建本轮 Decide Task，并在提交末尾把 Runtime Run
  标记为 `dispatch_ready`；Hosted claim 函数只领取该状态的 Task；
- 比赛 Task 优先于 credential validation；
- Worker 一次最多 claim 5 个 Task；10 Agent 为两个 wave，12 Agent 为三个 wave；
- 同一 pairing 的 negotiate 严格串行，不同 pairing 最多 5 组并发；
- Settlement Worker 可以同时处理最多 2 个 Intent；relay nonce 分配/广播短暂串行，
  链上等待与确认保持 2 笔并发；
- 创建 Game 时拒绝超过 12 个参与者；
- 已有 active Game 时拒绝启动第二局；
- 不通过不可控排队来“支持”更多 Agent。

生产 `action_timeout_ms` 由真实 Provider 的 5 并发、10/12 Agent wave 测试确定。
同一个冻结值必须同时满足：

```text
max_attempts = 2
decide budget >= ceil(participants * max_attempts / 5) * provider_p99 + margin
negotiate budget >= max_turns * ceil(pairings * max_attempts / 5)
                    * provider_p99 + margin
```

Settlement 另冻结 `settlement_timeout_ms = 600000`；10 Agent 的最坏 5 笔成交按
2 + 2 + 1 三个 wave 提交。每个 Intent 使用最近确认区块的 timestamp 冻结
`authorization_valid_after`，并令 `authorization_valid_before =
authorization_valid_after + 420` 秒；剩余 180 秒用于等待过期区块再获得两个确认、
查询 authorization state 和完成最终 release/commit。

600 秒是终态 deadline，不是“进入 submitted_unknown 即通过”。到 deadline 时：

- `inventory_committed | settlement_failed` 才是 Settlement 终态；
- `submitted_unknown` 仍不允许 Round close；
- RPC 无法给出安全证据时，Game 转为 `settlement_recovery_required`，停止推进和
  排名，并继续只读恢复；这局 MVP 验收失败，不无限显示为运行中。

不上线第二套通用 Round watchdog。

MVP 的 FCFS key 明确定义为数据库 Result Sink 写入的
`(result_received_at, pool_entry_id)`，不是 Task 创建顺序。5 + 5 wave 会把平台调度
延迟计入 FCFS，因此这只是资源受限展示语义，不作为正式 Tournament 公平性证明；
扩容到同轮全部 Decide 并发前不开放竞技性排名承诺。

磁盘边界：

- PostgreSQL、备份和部署产物总预算 40 GB；
- Docker JSON log 继续使用每容器 `10 MB × 3`；
- 部署和开局前检查剩余空间；低于 15 GB 告警，低于 10 GB 时运维关闭 Game
  creation flag，不新增后台磁盘守护进程；
- 首发不保存模型原始响应、签名或重复链上 payload；
- 每次发布前检查镜像、build cache、PostgreSQL volume 和备份占用，不做后台自动
  清理策略。

## 7. 部署顺序

代码完成后沿用当前已验证的部署入口：

```sh
sh deploy/scripts/generate-env.sh \
  --app-url https://www.arena402.com \
  api.arena402.com
chmod 600 deploy/.env
sh deploy/scripts/deploy.sh
```

部署顺序固定为：

```text
PostgreSQL
  -> migrations
  -> database roles
  -> API
  -> Hosted Worker / Credential Controller
  -> Arena Worker
  -> Settlement Worker
  -> enable Game creation
```

先部署但关闭自动结算：

```text
ADX_AUTOMATIC_TESTNET_SETTLEMENT_ENABLED=false
```

完成 wallet、Mandate、signer 权限和自动交易验收后，再设置为 `true` 并重新部署。
该 flag 关闭时不得启动需要链上结算的新 Game；不能退回逐笔人工批准。

## 8. 上线验收

### 8.1 必须通过

- 10 个 Hosted Agent 在 2C4G 生产机完成 5 回合；
- 浏览器关闭后 Game 继续；
- 同一轮 10 个 Decide Task 经两个 wave 后均在统一 deadline 内终态；
- 单轮 5 组 pairing 可以同时进入协商，每组内部最多三轮严格串行；
- 受控场景产生 5 笔 accepted trade，并按 2 + 2 + 1 wave 全部进入支付终态；
- 监控证据中至少一次同时存在 2 笔非终态 SettlementIntent；
- 600 秒内不得残留 `submitted_unknown`；`settlement_recovery_required` 计为
  MVP 失败，不计作安全完成；
- accepted trade 无人工操作自动产生 testnet 交易；
- 自动交易的 token、payer、payee、amount 与冻结 Intent 完全一致；
- 链上确认后现金和库存只更新一次；
- 至少一局包含多笔自动交易并完成最终排名；
- Settlement Worker 重启不会生成第二笔支付；
- 超额或过期 Mandate 不广播交易；
- Provider Key、wallet key、signature 和 reasoning 不进入数据库、日志或 API；
- Hosted Worker 数据库 role 无法修改 Mandate、Settlement 和 Inventory；
- Arena Worker 无法读取 signer Secret；
- Settlement Worker 无法修改报价、配对和 Task Result。

### 8.2 容量通过标准

以 10 Agent 为 MVP 必须通过、12 Agent 为非阻塞容量验证：

- 12 Agent 能完成 5 回合并生成最终排名；若未通过，不阻塞 10 Agent MVP；
- 同一轮 12 个 Decide Task 经三个 wave 后均在统一 action deadline 内终态；
- 最坏 6 组 pairing 按 5 + 1 wave 完成，不丢 pairing；
- 2 笔 Settlement 可同时在途，relay EOA nonce 连续且无碰撞或 gap；
- 无 Task 因 Worker claim 批次在开始执行前已经过期；
- 默认动作只来自真实 Provider/输出/deadline 失败，不来自 Worker 并发不足；
- PostgreSQL 无连接耗尽；
- API、Hosted Worker、Arena Worker、Settlement Worker 无 OOM/restart loop；
- 宿主机峰值内存不超过 3.2 GB，无 swap thrash，磁盘剩余不低于 15 GB；
- 完整 Game 可以从 PostgreSQL 状态恢复并继续。

## 9. 实施顺序

按以下五个提交边界实施：

1. **Mandate migration 与 repository**
   - 表、约束、reserve/consume/release、DB role、单元/真实 PostgreSQL 测试。
2. **Settlement Worker**
   - signer port、自动签名、直接提交、tx hash 持久化、重启幂等测试。
3. **Arena 自动支付接线**
   - Join 创建 wallet/Mandate、accept 自动排队、失败关闭 pairing、Round 自动继续。
4. **Production API 与 Compose**
   - 正式 Game command、Settlement Worker service、环境变量、权限与健康检查。
5. **10/12 Agent E2E**
   - 真实 Provider、自动 testnet 支付、多回合排名、发布证据和活动文档同步。

不并行实现 Local Connector、Native A2A、标准 x402、主网或多局调度。

## 10. 上线完成定义

只有以下完整路径在部署环境中通过，才算 Hosted 上线完成：

```text
login
  -> create 10 Hosted Agents
  -> credentials ready
  -> join one Game and create automatic Mandates
  -> start
  -> multi-round Decide/Pair/Negotiate
  -> automatic EIP-3009 payments
  -> confirmed inventory commits
  -> final ranking
```

其中任何一笔 accepted trade 都不能停在等待人工确认，也不能在没有链上确认时改变
库存。
