# Arena 402 游戏结算集成契约

> 状态：目标集成契约；当前仓库尚未完成端到端接线。
>
> 本文取代已归档的 RFQ/数字交付结算方案，只定义“协商被接受”到“链上确认后
> 转移游戏货物”的边界。Settlement 模块当前能力和验证证据仍以
> [`../agent-arena/settlement/README.md`](../agent-arena/settlement/README.md)
> 为准。

## 当前能力与目标能力

当前已实现：

- EIP-3009/EIP-712 买方离线授权；
- 项目自建 Facilitator 的验证与提交；
- Injective EVM testnet 上 mUSDC 的点对点转账；
- nonce 重放保护；
- mock/real SettlementSDK 适配器。

当前验证限制：

- Facilitator `/verify` 不恢复 EIP-712 签名；无效签名由 token contract 在
  `/settle` 时拒绝，仍可能消耗 relay gas；
- `RealSettlement.lockFunds()` 只做本地 signer recovery，尚未把授权完整绑定
  到 buyer、seller、amount、expiry、token、chain 和 `negotiationId`；
- 游戏集成层必须在提交前补齐这些冻结参数校验。

当前未实现：

- Arena 游戏协商到 SettlementSDK 的持久化适配器；
- 游戏级幂等、崩溃恢复和数据库原子更新；
- 标准 HTTP `402 Payment Required` challenge/retry/header 流程；
- 标准公共 x402 Facilitator 兼容；
- 链上 escrow、退款、争议或生产手续费。

因此，现有代码应称为 **EIP-3009 direct-relay settlement prototype**，
不能称为完整标准 x402 HTTP 实现。

## 权威状态

| 状态类型 | 权威来源 |
|----------|----------|
| 协商是否接受、价格、货物和双方 | Arena 数据库 |
| 授权 preflight 与交易是否提交 | Settlement service 记录 |
| 支付是否最终成功 | Injective EVM 链上交易结果 |
| 货物和现金余额 | Arena 数据库，仅在链上确认后更新 |

前端、Connector acknowledgement、Facilitator HTTP `200` 或本地 SDK 状态都
不能单独证明付款成功。

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

## 状态机

```text
accepted_pending_settlement
  -> authorization_requested
  -> authorized
  -> submitted
  -> chain_confirmed_uncommitted
  -> inventory_committed
```

失败终态：

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
3. 自带 Agent 的 Buyer Runtime，或游客 Agent 对应的受限 guest signer，只对
   该 intent 的 chain、token、payee、amount、有效期和 nonce 生成 EIP-3009
   授权。
4. Settlement adapter 校验授权和冻结快照一致。
5. Facilitator 提交交易并返回交易哈希。
6. Settlement worker 等待链上成功确认，并核对 token 转账事件；此时进入
   `chain_confirmed_uncommitted`。
7. Arena 在一个数据库事务中：
   - 保存链上确认事实；
   - 扣减 seller 货物并增加 buyer 货物；
   - 更新双方现金投影；
   - 写入 `inventoryCommittedAt` 并转为 `inventory_committed`；
   - 写入不可变审计事件。
8. 前端展示交易哈希和已确认成交。

链上确认前不得转移货物。数据库提交失败时保持
`chain_confirmed_uncommitted`，不得重新付款；恢复任务应根据已确认交易完成
幂等的库存提交。玩家端只有在 `inventory_committed` 后显示成交完成。

## 幂等与约束

- `negotiationId` 只能关联一个冻结成交快照；
- `idempotencyKey` 在 Arena 与 Settlement 两侧唯一；
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

## 演示验收

- 一个被接受的交易产生唯一 `SettlementIntent`；
- 修改价格、payee、token 或 chain 会使授权校验失败；
- 过期或重复 nonce 不产生第二笔转账；
- 链上失败不会转移货物；
- 链上成功后只更新一次货物和现金；
- 重启后能从持久化状态和链上结果恢复；
- UI 展示可核验的 testnet 交易哈希；
- 文档和演示明确区分 direct EIP-3009 relay 与标准 HTTP x402。
