# SETTLE-002.5 · 部署 Mock Stablecoin（ERC-20 + EIP-3009）

- **状态**: ✅ Approved（可执行）
- **负责人**: Felix
- **依赖**: SETTLE-001（EVM 通 + facilitator 有 gas）
- **解锁**: SETTLE-003（签名）、SETTLE-004（facilitator）、SETTLE-005（端到端）

## 背景

官方 testnet 无可用于 EVM 侧的 stablecoin（faucet 发的 peggy USDT 只存在于 Cosmos 侧，
EVM 无字节码，见 SETTLE-001 结论）。故自部署一个标准 ERC-20 + EIP-3009 的 mock stablecoin。

> mock USDC 与 mock USDT 无技术区别，仅 name/symbol 不同 → 合约参数化，可部署多个实例。

## 目标

1. 一个自包含合约 `MockStablecoin.sol`：标准 ERC-20 + EIP-3009 + 公开 faucet。
2. 部署到 Injective EVM testnet（chainId 1439），用 facilitator 钱包付 gas。
3. 部署后给买方钱包铸初始 USDC。
4. 把合约地址 + EIP-712 name/version 写回 `deployments.json`，供后续 spec 引用。

## 合约需求

| # | 需求 | 说明 |
|---|------|------|
| C1 | 标准 ERC-20 | name/symbol/decimals 构造函数可配（默认 mUSDC / 6 位）|
| C2 | **EIP-3009** | `transferWithAuthorization` + `receiveWithAuthorization` + `cancelAuthorization`；EIP-712 domain version="1" |
| C3 | **防重放** | `authorizationState[from][nonce]`；用过即标记，重复 revert（对应 D4）|
| C4 | 公开 `faucet(to)` | 任何人可领固定额度（如 1000 USDC），供 expo 现场领币按钮调用 |
| C5 | owner `mint` | 部署者可任意铸币（发种子资金、补给）|

## 验收标准

| # | 标准 | 验证 | 结果 |
|---|------|------|------|
| AC1 | 合约成功部署到 1439 | `0x06D223D12774386A96D33863D9106A800e52BDeD`，blockscout block 134436336 | ✅ |
| AC2 | **交易走 legacy** | legacy tx，gasPrice 0.48gwei（3×baseFee，见下坑）| ✅ |
| AC3 | 买方收到初始 USDC | `balanceOf(buyer)` = 10000 mUSDC | ✅ |
| AC4 | EIP-712 domain 正确 | DOMAIN_SEPARATOR `0xe00644…494f`，TRANSFER typehash = 标准 `0x7c7c6cdb…` ✓ | ✅ |
| AC5 | deployments.json 更新 | usdc.address 指向新合约，supportsEip3009=true | ✅ |

**✔️ 全部通过。合约地址 `0x06D223D12774386A96D33863D9106A800e52BDeD`。**

## 执行踩坑记录（后续 spec 必读）

1. **gasPrice 边界**：链 baseFee=0.16gwei，legacy gasPrice 必须 **严格 > baseFee**。
   给等于 baseFee 会被丢弃。方案：动态读 `getGasPrice()` × 3。
2. **RPC 回执延迟（重要）**：公共 RPC `k8s.testnet` 是负载均衡，写交易能进块，
   但读节点索引延迟，viem `waitForTransactionReceipt` 常超时误判"失败"。
   → **改用 blockscout API 轮询确认**（`scripts/lib-tx.ts` 的 `waitViaBlockscout`）。
   facilitator（SETTLE-004）发结算交易时必须复用这个模式。
3. **副作用**：因 #2 误判失败重发，链上有两个相同合约（`0xCAda…0cf1` 弃用，用 `0x06D2…BDeD`）。

## 交付物

- `settlement/contracts/contracts/MockStablecoin.sol`
- `settlement/contracts/hardhat.config.ts`（solc 编译配置）
- `settlement/contracts/scripts/deploy.ts`（viem 部署，**legacy tx**，部署后铸币 + 回写 deployments.json）
- `settlement/contracts/package.json`

## 关键约束（血泪坑）

- **D7**：Injective EVM 用 legacy 交易 → viem 部署时必须 `type:'legacy'` + `gasPrice:160000000n`。
- **decimals=6**：与真实 USDC 一致，避免后续金额换算混乱。
- **EIP-712 version="1"**：与主流 USDC 一致，x402 SDK 默认假设。
