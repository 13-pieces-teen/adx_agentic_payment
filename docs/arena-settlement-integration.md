# Arena 402 游戏结算集成契约

## 2026-07-25 implementation update

The following single-payment foundation is now implemented:

- an accepted negotiation can freeze exactly one immutable
  `arena402.settlement-intent.v1` when the Game explicitly selects
  `authorizationMode="single_eip3009"`;
- chain, token, token decimals, payer, payee, amount, expiry, participants,
  pairing, round, and negotiation are frozen and hash-bound;
- the local TypeScript bridge checks the frozen intent, signs locally, calls
  the project Facilitator, and refuses to broadcast without the operator's
  explicit `--confirm-testnet-transfer` flag and matching
  `--approved-intent-hash`;
- Arena records that hash-bound approval before broadcast, and the bridge
  derives one deterministic EIP-3009 nonce from the immutable Intent hash;
- Arena persists only the transaction hash and a nonce digest, never a wallet
  private key, raw signature, or raw nonce;
- read-only recovery verifies chain ID, successful receipt, confirmation depth,
  and the exact ERC-20 `Transfer` before recording a confirmation;
- cash and holdings move in one idempotent PostgreSQL transaction only after
  that persisted confirmation.

The deterministic nonce closes the duplicate-payment retry hazard: a bridge
restart cannot generate a second authorization for the same Intent. If the
bridge loses the Facilitator response before recording the transaction hash,
the operator must reconcile the public hash and resume with
`--record-existing-tx-hash`; it must never authorize a replacement payment.
`arena:submit-only` plus `arena:verify-restart` provides the explicit
submitted-state/Worker-restart/inventory-replay acceptance drill.

The no-broadcast dual Hosted Agent demonstration reaches
`authorization_requested`. A rollback-only integration verifier has exercised
the confirmation and inventory-commit transaction, and a historical Injective
testnet transfer has been matched through the read-only recovery path. No new
state-changing transaction was broadcast as part of this milestone.

The following remain outside the implemented foundation:

- a bounded, revocable multi-payment `PaymentMandate` with
  `reserve / consume / release`;
- an unattended isolated guest signer and its production IAM boundary;
- standard HTTP x402 challenge, headers, paid retry, or public Facilitator
  compatibility;
- a newly approved live testnet transaction proving the complete path against
  the current deployed services.

> 状态：单笔 EIP-3009 游戏接线已实现；PaymentMandate、无人值守 signer 与新鲜
> live testnet 端到端验收尚未完成。
>
> 本文取代已归档的 RFQ/数字交付结算方案，只定义“协商被接受”到“链上确认后
> 转移游戏货物”的边界。Settlement 模块当前能力和验证证据仍以
> [`../agent-arena/settlement/README.md`](../agent-arena/settlement/README.md)
> 为准。Hosted/Local Runtime 的上游 Task 契约见
> [`hosted-arena-agent-spec.md`](hosted-arena-agent-spec.md)。

## Approved Hosted launch target

The per-Intent human confirmation bridge remains a development verifier, not
the Hosted production payment path. The approved testnet launch target is:

- each Hosted Game Participant receives an isolated platform-managed
  `sandbox_guest` wallet;
- joining the Game once confirms a bounded Game-scoped PaymentMandate;
- every accepted trade is reserved, signed, submitted, confirmed, and committed
  automatically without a per-trade user action;
- a dedicated non-public Settlement Worker owns Mandate mutation, guest signing,
  and EIP-3009 submission;
- the Hosted Worker has no signer, Mandate, settlement, or wallet access;
- the Arena Worker remains non-signing and owns read-only confirmation plus the
  idempotent inventory commit;
- reservation is serialized by Mandate and buyer balance locks plus a unique
  buyer-per-Round constraint; mandate usage is derived from reservation rows
  rather than duplicated aggregate counters;
- an unknown submission is recovered or resent only with the same deterministic
  EIP-3009 authorization, and inventory waits for two confirmations plus exact
  calldata and `Transfer` event verification;
- the MVP freezes a 420-second authorization validity inside a 600-second
  settlement deadline; unknown is not terminal, and unresolved safe recovery
  moves the Game to `settlement_recovery_required` without ranking;
- wallet funding and Settlement share one database-backed relay EOA nonce
  allocator; preparation and confirmation may be concurrent while nonce
  allocation and broadcast are briefly serialized;
- the 2 vCPU / 4 GB / 70 GB MVP target is one active Game with 10 Hosted Agents,
  a hard cap of 12, bounded Worker waves, and platform testnet wallets only.

This target deliberately does not add a user-wallet unattended-signing path,
standard HTTP x402, escrow, Redis, Kafka, or multi-Game scheduling. The
implementation and deployment sequence is maintained in
[`hosted-arena-production-runbook.md`](hosted-arena-production-runbook.md).
Until that plan passes its live acceptance, the implementation status below
remains authoritative.

## 当前能力与目标能力

当前已实现：

- EIP-3009/EIP-712 买方离线授权；
- 项目自建 Facilitator 的验证与提交；
- Injective EVM testnet 上 mUSDC 的点对点转账；
- nonce 重放保护；
- mock/real SettlementSDK 适配器；
- accepted negotiation 到不可变 SettlementIntent 的持久化适配；
- hash-bound 人工批准、提交记录、只读链上恢复和确认后幂等库存提交。

当前验证限制：

- Facilitator `/verify` 不恢复 EIP-712 签名；无效签名由 token contract 在
  `/settle` 时拒绝，仍可能消耗 relay gas；
- `RealSettlement.lockFunds()` 单独使用时只做本地 signer recovery；Arena 的本地
  bridge 必须继续在提交前校验完整冻结快照，不能绕过该集成层；
- 当前证据包括 rollback-only 提交验证和历史交易只读恢复，不等于新鲜 live
  交易已经验收。

当前未实现：

- 可覆盖一局多笔交易的 PaymentMandate，以及额度
  `reserve / consume / release`；
- PaymentMandate revoke 与 reserved/submitted 的竞态及完整 reorg 策略；
- 与 Hosted Worker 完全隔离的 guest signer service 权限接线；
- 当前部署服务上的一笔新鲜、经批准 testnet 端到端交易；
- 标准 HTTP `402 Payment Required` challenge/retry/header 流程；
- 标准公共 x402 Facilitator 兼容；
- 链上 escrow、退款、争议或生产手续费。

因此，现有代码应称为 **EIP-3009 direct-relay settlement prototype**，
不能称为完整标准 x402 HTTP 实现。

## 权威状态

| 状态类型 | 权威来源 |
|----------|----------|
| 协商是否接受、价格、货物和双方 | Arena 数据库 |
| PaymentMandate 范围、额度预留/消费/释放和撤销 | Settlement 数据库 |
| 授权 preflight 与交易是否提交 | Settlement service 记录 |
| 支付是否最终成功 | Injective EVM 链上交易结果 |
| 货物和现金余额 | Arena 数据库，仅在链上确认后更新 |

前端、Connector acknowledgement、Facilitator HTTP `200` 或本地 SDK 状态都
不能单独证明付款成功。Hosted Provider success、AgentTaskResult
`action="accept"` 或 Arena `accepted_pending_settlement` 也不能证明支付完成。

## PaymentMandate 目标

为了让 Hosted Agent 在用户关闭网页和电脑后仍能完成整局 testnet 交易，支付主体
必须在入局前明确确认一份受限、可撤销、可审计的 PaymentMandate。最小范围包括：

- `gameId`、network/chain id、token 与 settlement contract；
- 允许的 payee 或 payee 规则；
- 单笔最大金额与本局累计最大金额；
- 生效时间、到期时间和撤销状态；
- 防重放 nonce/sequence 与签名域；
- 并发 Deal 的 `reserve / consume / release` 状态。

建议状态机：

```text
active
  -> reserve(deal) -> reserved
       -> submit -> submitted
            -> confirm -> consumed
            -> known failure before transfer -> released
  -> revoked / expired
```

额度预留必须在冻结 SettlementIntent 后、链上提交前完成。`reserved` 与
`submitted` 不能因普通 revoke 被当作从未存在：revoke 阻止新 reserve，而在途
记录必须按已冻结政策完成查询、确认或安全释放。`confirmation_timeout`、RPC 故障
或数据库重启时不得盲目再次消费额度或重新付款。

Wallet-backed User 与 Sandbox Guest 的实现可以不同：

- Wallet-backed User 需要用户控制钱包对明确 Mandate 或单笔 intent 授权；
- Sandbox Guest 由独立、限额、testnet-only signer service 执行；
- Hosted Worker 无 signer IAM、钱包密钥、Mandate consume 权限或任意签名接口；
- Local Connector 也不得把钱包私钥上传给平台。

当前 EIP-3009 direct relay 只支持单笔授权原型，不天然实现上述多笔 Mandate。
在签名域和额度状态机落地前，可以完成 Decide/Negotiate 与手动单笔 testnet
结算，但不能宣称“Hosted Agent 在用户完全离线后可自动完成全部支付”。

## 冻结的成交快照

Arena 在 `accept` 后生成不可变的 `SettlementIntent`：

```json
{
  "schemaVersion": "arena402.settlement-intent.v1",
  "gameId": "game-1",
  "roundId": "round-3",
  "negotiationId": "neg-42",
  "buyerAgentId": "agent-a",
  "sellerAgentId": "agent-b",
  "buyerWallet": "0x...",
  "sellerWallet": "0x...",
  "good": "ruby",
  "quantity": 1,
  "unitPrice": "9.500000",
  "amount": "9.500000",
  "chainId": 1439,
  "token": "0x...",
  "validBefore": 1784900000,
  "idempotencyKey": "game-1:round-3:neg-42"
}
```

金额必须通过 token 最小单位或定点十进制转换，禁止二进制浮点。`amount` 必须
等于 `unitPrice * quantity`，payee 必须是冻结的 seller wallet。

SettlementIntent 还必须引用验证时使用的 `paymentMandateId` 或明确标记
`authorizationMode="single_eip3009"`。Settlement 不得在 Agent 接受后重新定价、
更换 payee、token、network 或 quantity。

## 状态机

```text
accepted_pending_settlement
  -> mandate_reserved
  -> authorization_requested
  -> authorized
  -> submitted
  -> chain_confirmed_uncommitted
  -> inventory_committed
```

`mandate_reserved` 只适用于 PaymentMandate 模式；明确标记的单笔 EIP-3009 模式从
`accepted_pending_settlement` 直接进入 `authorization_requested`。

失败终态：

- `mandate_rejected`
- `authorization_failed`
- `submission_failed`
- `expired`
- `reverted`

可恢复状态或条件：

- `confirmation_timeout`：链上结果未知；
- `chain_confirmed_uncommitted`：链上成功，库存尚未提交；
- `inventory_commit_failed`：最近一次库存事务失败，逻辑状态仍是
  `chain_confirmed_uncommitted`。

`confirmation_timeout` 不能直接当作失败重付。恢复任务必须先按交易哈希和授权
nonce 查链，再决定是否重新提交。唯一成功终态是 `inventory_committed`。

## 成功路径

1. Arena 校验双方仍有资格交易，seller 持仓充足，buyer 预算充足。
2. Arena 原子冻结 `SettlementIntent`，协商不再允许改价。
3. Settlement 校验 Mandate 的 Game/network/token/payee、期限、单笔/累计额度和
   revoke 状态，并原子 reserve 该 Deal 的额度；使用单笔模式时则明确等待对应
   EIP-3009 授权。
4. 用户控制的钱包，或隔离的 guest signer service，只对该冻结 intent/Mandate
   范围生成授权；模型 Runtime 不参与签名。
5. Settlement adapter 校验授权和冻结快照一致。
6. Facilitator 提交交易并返回交易哈希；若使用 Mandate，其 reservation 进入
   submitted，不能被重复消费。
7. Settlement worker 等待链上成功确认，并核对 token 转账事件；此时进入
   `chain_confirmed_uncommitted`。
8. 若使用 Mandate，Settlement 将相应 reservation 幂等标记为 consumed；Arena 在
   一个数据库事务中：
   - 保存链上确认事实；
   - 扣减 seller 货物并增加 buyer 货物；
   - 更新双方现金投影；
   - 写入 `inventoryCommittedAt` 并转为 `inventory_committed`；
   - 写入不可变审计事件。
9. 前端展示交易哈希和已确认成交。

链上确认前不得转移货物。数据库提交失败时保持
`chain_confirmed_uncommitted`，不得重新付款；恢复任务应根据已确认交易完成
幂等的库存提交。玩家端只有在 `inventory_committed` 后显示成交完成。

## 幂等与约束

- `negotiationId` 只能关联一个冻结成交快照；
- `idempotencyKey` 在 Arena 与 Settlement 两侧唯一；
- 一个 SettlementIntent 只能拥有一个 Mandate reservation；
- Mandate reservation 的 reserve/submit/consume/release 使用唯一 Deal key 和
  单向 CAS；
- revoke 后不能创建新 reservation；对已 reserved/submitted 的处理必须遵循冻结
  策略并可审计；
- 同一 EIP-3009 nonce 不得被第二次使用；
- 已有交易哈希时，重试先查询而不是重新授权；
- `submitted`、`chain_confirmed_uncommitted` 和 `inventory_committed` 只能
  单向推进；
- 任何参数变更都必须创建新的 intent，旧 intent 明确作废；
- Facilitator 必须限制 chain、token、payee、amount、有效期和业务 ID；
- 日志不得包含私钥、签名密钥、API Key 或完整设备凭据。

## Connector 边界

Connector 可向受控 Runtime 投递“为这个 intent 生成授权”的 typed command，
并返回执行结果，但：

- Connector receipt 只证明本地命令执行状态；
- Connector 不决定协商是否接受；
- Connector 不直接写游戏持仓；
- Connector 不将链上提交成功投影为最终确认；
- 支付私钥不得由平台通过命令参数下发。

该命令仅适用于本地钱包在用户设备上的明确授权流程，不得让 Hosted Worker 经
Connector 代理获取签名权。Connector ACK 与 terminal Runtime Result 仍必须分离，
且二者都不能替代 Settlement 对 Mandate、授权和链上状态的校验。

## 演示验收

- 一个被接受的交易产生唯一 `SettlementIntent`；
- 超出 Mandate 的 Game、network、token、payee、单笔/累计额度或期限时无法提交；
- revoke 后不能创建新 reserve，reserved/submitted 与 revoke 的竞态可恢复；
- 同一 Deal 的 `reserve / consume / release` 重试不重复占用或释放额度；
- 修改价格、payee、token 或 chain 会使授权校验失败；
- 过期或重复 nonce 不产生第二笔转账；
- 链上失败不会转移货物；
- 链上成功后只更新一次货物和现金；
- 重启后能从持久化状态和链上结果恢复；
- chain unknown/reorg 不触发盲目重付，Sandbox Signer 与 Hosted Worker 权限隔离；
- UI 展示可核验的 testnet 交易哈希；
- 文档和演示明确区分 direct EIP-3009 relay 与标准 HTTP x402。
