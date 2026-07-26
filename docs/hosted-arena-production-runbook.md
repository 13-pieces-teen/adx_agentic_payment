# Arena 402 Hosted 上线部署与实现方案

> 状态：2026-07-25 批准的 Hosted testnet 上线目标。
>
> 当前仓库已经具备 Hosted Agent、持久化 AgentTask/Result、12 Agent 本地编排，
> 并已把生产 Current Game 硬上限配置为 100、Hosted Worker 配置为
> 4 副本 × 25 task slot、Facilitator 配置为 4 个独立 EOA shard。
> GitHub User 永久 testnet 钱包绑定、受限 PaymentMandate、x402 V2 HTTP 链路、
> 隔离的 PostgreSQL 密文 signer、自动提交编排、只读链上确认和确认后库存提交。
> 当前逐笔人工批准 bridge 只是开发验证工具，不是产品支付方案。自动链路已通过
> Fake E2E；100 Agent、四 shard live testnet、公共 Facilitator、新鲜 testnet
> 交易与完整生产 E2E 尚未验收。旧 2C4G 证据不能用于证明当前配置容量。

## 1. 上线目标

首发只交付一个清晰闭环：

```text
用户创建 Hosted Agent
  -> 加入 testnet Game，并一次性同意该局自动支付
  -> 最多 100 个 Hosted Agent 参加；4 个 Hosted Worker 各有 25 个 task slot
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

生产配置目标：

- Current Game 默认开赛阈值 10 个 Hosted Agent，硬上限 100；
- 固定展示 5 回合；代码继续支持 1–10 回合，但不作为首发容量承诺；
- Decide Task 同批创建，Hosted Worker 以 4 副本 × 25 task slot 起步；
- 同一组协商严格顺序执行，不同 pairing 可并发，但仍受 Provider 配额和数据库
  barrier 约束；
- Settlement Worker 以 4 个执行 slot 确定性路由到 4 个独立 EOA Facilitator
  shard；
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
Hosted Worker   Settlement Worker -> internal wallet-signer/facilitator
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
| Settlement Worker | claim Intent、Mandate reserve、调用 signer/facilitator、记录 tx hash | 修改冻结价格、在确认前提交库存 |
| Wallet signer | 通过专用函数读取单钱包密文，按 wallet id 与冻结 x402 requirement 签署 EIP-3009 | 直接读表、访问业务状态/Provider 或广播交易 |
| PostgreSQL | 唯一持久化队列和业务状态权威 | 保存原始模型 Key、钱包私钥、助记词或签名 |

API 的公开 x402 paid retry 只把签名 payload 转发到 bearer-authenticated 的内网
Settlement ingress；API 不持有 signer token、Facilitator authorization，也不直接
修改 reservation 或提交链上交易。

第一版通过 x402 V2 `/verify` 与 `/settle` 调用显式配置的 Facilitator shard；
生产 Compose 的 `testnet-facilitator` profile 启动四个不发布宿主机端口的内网
服务，也可替换为四个外部 HTTPS endpoint。任一必需 shard 未配置时 fail closed。
签名器同样只加入 data network。

自动支付新增的外部依赖只保留四个：

- PostgreSQL 中的 per-wallet AES-256-GCM 信封密文；
- 仓库外、宿主机 `0400` 的 32-byte wallet KEK，仅只读挂载给 signer；
- 四个批准的外部 HTTPS 或内网 x402 V2 Facilitator shard；
- 一个 Injective EVM RPC：提交、确认，并在结果未知时按 block/transaction
  扫描恢复。Blockscout 只作为运维查看工具，不是运行时依赖。

## 4. 自动支付模型

### 4.1 Guest wallet

首发 Hosted Agent 只使用 `sandbox_guest` 结算账户：

- 每个 GitHub 平台 User 首次读取钱包或入局时原子绑定一个预生成 testnet EVM
  地址；后续登录和 Game 继续使用同一钱包，不回收到空闲池；
- 每个 Game Participant 冻结该 User 已绑定的钱包快照；
- CSV 只由一次性 `wallet-admin` 导入任务读取；它逐项核对私钥派生地址后，在
  应用层生成随机 DEK 并写入密文、nonce 和 KEK version；
- PostgreSQL 不接收 KEK 或明文；长期 wallet-signer 通过
  `adx_wallet_signer_login` 只能调用单钱包密文读取函数；
- Hosted Worker、Arena Worker、API、Settlement Worker 和前端都不能读取 CSV、
  KEK 或私钥；
- signer 只允许配置的 testnet chain，并在每次签名前再次核对数据库地址、
  私钥派生地址和冻结 payer。

Game 启动前，平台为每个 guest wallet 自动准备冻结 Portfolio 的初始
`arena402-g` 现金，并完成双向转账所需的白名单登记。
EIP-3009 的 payer 不需要持有 gas；Facilitator relay account 负责 testnet
gas。首发使用一个 testnet-only owner/relay account 负责初始 `arena402-g` 铸造和
relay gas；它与每个 Participant 的 guest wallet 分离。首发不实现赛后自动 sweep，
过期 Game 的测试钱包由运维批次清理。

Wallet binding 使用 `wallet_inventory` 与 `user_wallets`：

```text
validate external CSV without printing keys
  -> envelope-encrypt each key and import public identity + ciphertext
  -> remove CSV from the runtime host path after verification
  -> first GitHub user access atomically binds one available row
  -> join freezes the same address into participant_settlement_accounts
```

绑定以 `user_id`、不可变 GitHub numeric subject、`wallet_id` 和
`(chain_id,address)` 唯一约束为幂等边界；并发登录使用 PostgreSQL row lock 与
`SKIP LOCKED`。CSV 不复制进仓库、镜像、长期容器或日志；数据库只保存 AES-GCM
密文。KEK 轮换只解包/重包每个 DEK，不改钱包地址或私钥密文，并用旧 version
条件更新避免并发覆盖。

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
owner 的权限与 relay gas 是否足以覆盖该局，不实现主网充值。

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
  -> build x402 V2 PaymentRequired from frozen EIP-712 domain
  -> derive deterministic EIP-3009 nonce from Intent hash
  -> internal signer returns x402 PaymentPayload
  -> HTTPS facilitator verifies and settles transferWithAuthorization
  -> persist tx hash and authorization nonce digest
  -> Arena Worker reads receipt and exact Transfer event
  -> mark chain_confirmed_uncommitted
  -> Arena confirms the chain payment and commits cash/holding
  -> Settlement Worker reconciles the reservation as consumed
  -> mark inventory_committed
  -> Round may close
```

`accept`、签名成功、交易提交和链上确认仍是不同状态。模型 Runtime 永远不参与签名，
自动支付循环和 signer 也不能修改价格、货物、数量或双方。

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

`018_arena_wallet_mandate_x402.sql` 新增：

1. `wallet_inventory` 与永久 `user_wallets`；
2. `payment_mandates` 与累计额度数据库 CHECK；
3. `payment_reservations`，以 SettlementIntent 唯一并记录
   `reserved | consumed | released`；
4. `x402_settlement_attempts`，记录 challenge、状态、安全错误码和 Worker lease；
5. SettlementIntent 的 token EIP-712 name/version 冻结字段；
6. `payment_mandate` approval source 与 API/Core 最小表权限。

`019_arena_wallet_encrypted_secret_vault.sql` 新增隔离
`wallet_secret_vault` schema、per-wallet 信封密文、地址外键，以及
`adx_wallet_signer` / `adx_wallet_importer` 的互斥函数权限：signer 可读取一个
钱包的完整密文但不能直接读表；importer 可导入、禁用和读取/更新 wrapped DEK，
不能读取私钥密文。

`reserve / consume / release / revoke` 由 repository 在显式 PostgreSQL 事务与 row
lock 中完成。自动循环使用 `x402_settlement_attempts` 的 `FOR UPDATE SKIP LOCKED`
和 lease 恢复重启。

### 5.2 Arena

修改 `arena_game/postgres.py`：

- Hosted Participant 加入时引用 GitHub User 永久绑定钱包并冻结 settlement account；
- 用户通过 Session + CSRF API 显式创建或撤销 Game-scoped Mandate；
- negotiation apply 校验冻结的 `max_trade_price_atomic` 和 buyer 当前 cash；
- accept 时在同一事务冻结 Intent；
- 不再生成 `human approval required` 状态；
- known settlement failure 关闭 pairing，但不移动库存；
- `inventory_committed` 后恢复双方 active 状态；
- 所有 pending settlement 完成后允许 Round close。

Arena 仍不签名、不读取 signer Secret、不提交交易。

### 5.3 Settlement Worker 与 signer

独立 Settlement Worker 的自动支付循环：

```text
claim Intent
  -> reserve
  -> call internal signer
  -> x402 verify/settle
  -> record tx hash
```

实现约束：

- 只处理 `sandbox_guest + single_eip3009`；
- 以 User wallet、Mandate、SettlementIntent 和 reservation 为幂等边界；
- 使用冻结 Intent hash 派生确定性 nonce；
- 崩溃后只从冻结的 domain、validity 与 nonce 重新生成同一 authorization；
- 提交前重新验证 token、chain、payer、payee 和 amount；
- signer 从专用 PostgreSQL 函数按 `wallet_id` 读取密文并用本地 KEK 解密，
  其他服务只持 bearer token；
- 仅记录 tx hash、payload digest、安全错误码和时间；
- 在日志和异常中禁止输出 private key、signature、raw nonce；
- `submitting` 后结果不明时保持 reservation，不释放、不生成第二笔支付。

Arena Worker 只负责调度与只读确认；Settlement Worker 使用独立
`adx_settlement` 数据库角色，私钥仍只存在 signer 容器。

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

`docker-compose.production.yml` 已接入 payment migration、API、独立 Settlement
Worker，以及可选 `wallet-signer` 和 `arena-facilitator`。后两者分别使用
`testnet-signer` / `testnet-facilitator` profile，只加入 data network、无宿主机
端口和只读文件系统；wallet-signer 只读挂载 `wallet-master.key`，不挂载 CSV。
一次性 `wallet-admin` profile 才挂载导入 CSV。自动支付默认关闭。Hosted profile 同时支持
单机 AES-GCM ciphertext vault，并在 master-key 文件、数据库角色与真实 Provider
配置验收后显式启用；腾讯 SSM 保留为可选高安全后端。

`ADX_ARENA_CORE_ENABLED=true` 同时挂载经过认证的 Pawnhouse participation
入口，并在 API 进程启动 Connector dispatcher。dispatcher 必须留在 API
进程，因为活跃 Connector WebSocket ownership 是进程内状态；独立的
`arena-worker` 继续负责 Arena coordination、deadline finalization、Game
推进和链上确认恢复，不能从 Connector ACK 推断游戏动作或支付结果。

云端启用前必须：

1. 在仓库外创建独立 wallet secret 目录；生成恰好 32 raw bytes 的
   `wallet-master.key`，设为容器 UID `10001` 可读、权限 `0400`，并保存加密离线备份；
2. 把 agent wallet CSV 放在仓库外固定绝对路径，设为 UID `10001` 可读且权限
   `0600`，配置 `ADX_WALLET_IMPORT_CSV_HOST_PATH`；
3. 先执行 dry-run，只校验 CSV，不写数据库：

   ```text
   docker compose --profile wallet-admin run --rm --build wallet-vault-admin
   ```

   再显式执行一次导入：

   ```text
   docker compose --profile wallet-admin run --rm --build wallet-vault-admin \
     npm run wallet:vault-import -- --apply
   ```

   验证数据库计数和 signer health 后，将 CSV 移出运行服务器或安全删除；系统不会
   自动删除源文件；
4. 在仓库外准备 Facilitator CSV，使用四个不同 EOA，`facilitator_index`
   1–4 各恰好一行，权限 `0600`，并配置
   `ADX_FACILITATOR_CSV_HOST_PATH`；四个内网 endpoint
   `http://arena-facilitator-{1..4}:4021`（或四个外部 HTTPS endpoint）必须各有
   唯一 facilitator id、钱包索引和至少 32 字符的独立 bearer token。部署脚本会
   在启动前校验索引、token/authorization 配对与 CSV 行；
5. 先保持 `ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED=false` 完成 API、数据库、
   signer health 与管理快照验收；
6. 第一笔真实 testnet 验收先把 `ADX_SETTLEMENT_INTENT_ID` 设置为已人工复核的
   单一不可变 SettlementIntent，再获得该笔交易的执行批准并开启自动路径；
   验收完成前不得清空这个 canary 限制。产品正式运行后，清空该变量，合法
   PaymentMandate 内的 A2A 交易不逐笔人工确认。

轮换 KEK 时先保留旧文件，准备新的 `0400` 32-byte 文件，配置 old/new filename
和 version，然后执行 dry-run：

```text
docker compose --profile wallet-admin run --rm --build wallet-vault-rotate
```

确认所有 active wallet 都使用预期旧 version 后，加 `--apply` 完成只重包 DEK：

```text
docker compose --profile wallet-admin run --rm --build wallet-vault-rotate \
  npm run wallet:vault-rotate -- --apply
```

随后把 signer 的文件和
`ADX_WALLET_MASTER_KEY_VERSION` 一起切到新 version 并重启。确认 signer health
和合成签名通过后才能销毁旧 KEK。

## 6. 100 Agent / 4 Facilitator 生产容量配置

当前配置基线：

```text
host: must be re-sized and load-tested; the old 2 vCPU / 4 GB host is rejected
active games: 1
max participants: 100
start threshold: 10
demo rounds: 5
hosted worker replicas: 4
hosted task concurrency per replica: 25
theoretical hosted task slots: 100
settlement execution concurrency: 4
facilitator shards: 4 independent EOA services
API processes: 1
API max concurrency: 64
PostgreSQL max_connections: 120
```

Compose 常驻容器内存上限：

| 服务 | 内存上限 |
|---|---:|
| PostgreSQL | 768 MB |
| API | 512 MB |
| Hosted Worker | 4 × 512 MB |
| Arena Worker | 320 MB |
| Settlement Worker | 256 MB |
| Wallet signer | 256 MB |
| Credential Controller | 192 MB |
| Facilitator | 4 × 256 MB |
| Caddy | 128 MB |

仅上述服务的配置上限已约 5.5 GB，尚未计入 Docker、宿主机、构建和瞬时内存，
所以旧 2C4G 主机不再是支持目标。生产主机必须按实际峰值留出充足 headroom，并在
100 Agent 验收前完成 CPU、内存、磁盘和网络重新定容；不能通过交换或关闭安全
边界硬撑。
Vercel 前端不占用这台服务器；`legacy-web` profile 不得在 MVP 生产机启动。

120 个数据库连接是当前起步值。所有服务必须继续满足
`sum(replica_count × pool_max_size) <= 84`，其余至少 30% 留给 migration、健康
检查、恢复和运维。增加 Worker 副本、API 进程或连接池前必须重新计算预算。

关键实现要求：

- 一轮全部 Decide Task 创建完成后，Hosted Worker 才开始 claim；
- Coordinator 使用一个事务批量创建本轮 Decide Task，并在提交末尾把 Runtime Run
  标记为 `dispatch_ready`；Hosted claim 函数只领取该状态的 Task；
- 比赛 Task 优先于 credential validation；
- 每个 Hosted Worker 最多同时执行 25 个 Task，4 副本提供 100 个理论 slot；
- 同一 pairing 的 negotiate 严格串行，不同 pairing 可并发，但不能绕过
  Provider 全局配额；
- 自动支付循环同时处理最多 4 个 Intent，并按冻结 Intent hash 固定到 4 个独立
  EOA shard；每个 shard 内部当前仍串行等待链上确认；
- 创建 Current Game 时拒绝超过 100 个参与者；
- 已有 active Game 时拒绝启动第二局；
- 不通过不可控排队来“支持”更多 Agent。

生产 `action_timeout_ms` 由真实 Provider 的 10/12/25/50/100 Agent wave 测试确定。
同一个冻结值必须同时满足：

```text
max_attempts = 2
effective_provider_concurrency =
  min(100, sum(provider quota and rate-limit slots))
decide budget >=
  ceil(participants * max_attempts / effective_provider_concurrency)
  * provider_p99 + margin
negotiate budget >=
  max_turns
  * ceil(pairings * max_attempts / effective_provider_concurrency)
                    * provider_p99 + margin
```

Settlement 另冻结 `settlement_timeout_ms = 600000`；100 Agent 的最坏 50 笔
成交在 4 shard 起步配置下至少需要 13 个 shard wave。每个 Intent 使用最近确认
区块的 timestamp 冻结
`authorization_valid_after`，并令 `authorization_valid_before =
authorization_valid_after + 420` 秒；剩余 180 秒用于等待过期区块再获得两个确认、
查询 authorization state 和完成最终 release/commit。

由于当前每个 Facilitator shard 仍在内存队列中等待确认，420/600 秒窗口能否覆盖
50 笔最坏场景必须由 live testnet 数据验证；未通过时应提高 shard 数或先实现广播/
确认解耦，不能把过期 Intent 当作容量成功。

600 秒是终态 deadline，不是“进入 submitted_unknown 即通过”。到 deadline 时：

- `inventory_committed | settlement_failed` 才是 Settlement 终态；
- `submitted_unknown` 仍不允许 Round close；
- RPC 无法给出安全证据时，Game 转为 `settlement_recovery_required`，停止推进和
  排名，并继续只读恢复；这局 MVP 验收失败，不无限显示为运行中。

不上线第二套通用 Round watchdog。

FCFS key 明确定义为数据库 Result Sink 写入的
`(result_received_at, pool_entry_id)`，不是 Task 创建顺序。100 个理论 task slot
不能消除 Provider 限流、进程调度或网络导致的 launch skew；在 100 Agent
launch-skew 验收前，不把该生产配置作为正式 Tournament 公平性证明。

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
  -> Wallet signer 与 facilitator（两个显式 testnet profile）
  -> enable Game creation
```

先部署但关闭自动结算：

```text
ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED=false
```

完成 wallet、Mandate、signer 权限和自动交易验收后，再设置为 `true` 并重新部署。
该 flag 关闭时不得启动需要链上结算的新 Game；不能退回逐笔人工批准。

## 8. 上线验收

### 8.1 必须通过

- 10/12 Agent 回归通过后，25/50/100 个 Hosted Agent 在重新定容的生产机完成
  5 回合；
- 浏览器关闭后 Game 继续；
- 同一轮 100 个 Decide Task 均在统一 deadline 内终态，并记录实际 Provider
  wave 与 launch skew；
- 单轮最多 50 组 pairing 不丢失，每组内部最多三轮严格串行；
- 受控场景产生最多 50 笔 accepted trade，并经 4 shard 全部进入安全支付终态；
- 监控证据中至少一次同时存在 4 笔、且分属 4 个 Facilitator shard 的非终态
  SettlementIntent；
- 600 秒内不得残留 `submitted_unknown`；`settlement_recovery_required` 计为
  MVP 失败，不计作安全完成；
- accepted trade 无人工操作自动产生 testnet 交易；
- 自动交易的 token、payer、payee、amount 与冻结 Intent 完全一致；
- 链上确认后现金和库存只更新一次；
- 至少一局包含多笔自动交易并完成最终排名；
- Settlement Worker 或 signer 重启不会生成第二笔支付；
- 任一 Facilitator shard 重启或不可用时，已持久化 route 不会静默换 shard 或
  生成第二笔支付；
- 超额或过期 Mandate 不广播交易；
- Provider Key、wallet key、signature 和 reasoning 不进入数据库、日志或 API；
- Hosted Worker 数据库 role 无法修改 Mandate、Settlement 和 Inventory；
- Arena Worker 无法读取 signer Secret；
- Arena Worker 数据库 role 不拥有 Mandate mutation 或 Facilitator 提交凭据；
- wallet-signer 无法访问或修改报价、配对和 Task Result。

### 8.2 容量通过标准

10/12 Agent 是历史回归基线，100 Agent 才是当前生产配置的容量门槛：

- 10、12、25、50、100 Agent 均完成 5 回合并生成最终排名；
- 同一轮 100 个 Decide Task 均在统一 action deadline 内终态；
- 最坏 50 组 pairing 完成且不丢 pairing；
- 4 笔 Settlement 可同时在途并落到 4 个不同 EOA shard；每个 EOA nonce 连续且
  无碰撞或无法解释的 gap；
- 无 Task 因 Worker claim 批次在开始执行前已经过期；
- 默认动作只来自真实 Provider/输出/deadline 失败，不来自 Worker 并发不足；
- PostgreSQL 无连接耗尽；
- API、Hosted Worker、Arena Worker、wallet-signer 无 OOM/restart loop；
- 宿主机峰值内存不超过重新定容后预算的 80%，无 swap thrash，磁盘剩余不低于
  15 GB；
- 完整 Game 可以从 PostgreSQL 状态恢复并继续。

## 9. 实施顺序

按以下五个提交边界实施：

1. **Mandate migration 与 repository**
   - 表、约束、reserve/consume/release、DB role、单元/真实 PostgreSQL 测试。
2. **自动支付与 signer**
   - signer port、x402 签名、facilitator 提交、tx hash 持久化、重启幂等测试。
3. **Arena 自动支付接线**
   - Join 创建 wallet/Mandate、accept 自动排队、失败关闭 pairing、Round 自动继续。
4. **Production API 与 Compose**
   - 正式 Game command、wallet-signer profile、环境变量、权限与健康检查。
5. **10/12/25/50/100 Agent E2E**
   - 真实 Provider、四 shard 自动 testnet 支付、多回合排名、发布证据和活动
     文档同步。

不并行实现 Local Connector、Native A2A、主网或多局调度。

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
