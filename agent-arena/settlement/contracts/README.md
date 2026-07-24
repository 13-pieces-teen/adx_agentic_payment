# Settlement Contracts — Mock Stablecoin (SETTLE-002.5)

Injective EVM testnet 上的 mock 稳定币：标准 ERC-20 + EIP-3009 + 公开 faucet。
供 x402 结算闭环使用。

## 已部署（testnet 1439）

| 项 | 值 |
|----|----|
| mUSDC 合约 | `0x06D223D12774386A96D33863D9106A800e52BDeD` |
| decimals | 6 · EIP-712 name "Mock USD Coin" version "1" |
| Explorer | https://testnet.blockscout.injective.network/address/0x06D223D12774386A96D33863D9106A800e52BDeD |

（参数以 `../deployments.json` 为准，代码从那里读，勿硬编码。）

## 命令

```bash
npm install
npm run compile                 # 编译合约（Hardhat/solc）
npm run deploy                  # 部署 mUSDC（默认）
TOKEN=USDT npm run deploy       # 部署 mUSDT（同合约，改 name/symbol）
```

## Injective EVM 两个坑（见 SETTLE-002.5 spec）

1. **legacy tx + gasPrice > baseFee**：viem 默认 EIP-1559 发不出；须 `type:'legacy'` + 动态 gasPrice×3。
2. **回执延迟**：公共 RPC 读节点索引慢，别用 `waitForTransactionReceipt`；用 `scripts/lib-tx.ts` 的
   `waitViaBlockscout()` 轮询 blockscout 确认。

## faucet（供 expo 现场领币）

合约有公开 `faucet(address to)`，任何人可领 1000 mUSDC。前端领币按钮直接调它。
