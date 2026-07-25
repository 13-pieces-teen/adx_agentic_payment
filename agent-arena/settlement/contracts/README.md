# Settlement Contracts

Injective EVM testnet 上的合约集合：

1. **MockStablecoin (mUSDC)** — 结算用 mock 稳定币：标准 ERC-20 + EIP-3009 +
   公开 faucet。供 Arena 402 的 EIP-3009 direct-relay 结算原型使用。
2. **ArenaMemorial (arena402-m)** — 纪念币，**soulbound / mint-only**：除 owner
   铸造外任何账户都不可转（transfer/transferFrom/approve 全 revert，无 EIP-3009）。
3. **ArenaGameCoin (arena402-g)** — 游戏币，**白名单受限转账** ERC-20 + EIP-3009：
   `_transfer` 要求 from 与 to 都在白名单，只有登记的参赛钱包之间可转；DEX 池地址
   进不了白名单 → 建不了池 → 无法外部炒作。白名单检查覆盖 EIP-3009 全路径。

> 红线（见 `AGENTS.md`）：绝不发可自由交易的币。-m 靠 soulbound、-g 靠白名单
> 双重满足该红线。两个合约均为单文件、无外部依赖，便于审计。

它们本身不构成完整 HTTP x402 闭环。

## 单测（不上链，本地 hardhat node）

```bash
npm install
npm run compile
npm run node        # 终端 A：起本地 EVM（内存链，固定测试私钥）
npm test            # 终端 B：跑 arena402-m/-g 红线单测（13 项）
```

覆盖：-m soulbound 全路径 revert、owner-only mint；-g 白名单双方校验、
未登记（含 DEX 池）拒转、EIP-3009 经 facilitator 代付成功且同样受白名单约束。

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
