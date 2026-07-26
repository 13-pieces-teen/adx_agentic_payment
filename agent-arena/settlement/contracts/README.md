# Settlement Contracts

Injective EVM testnet 上的合约集合：

1. **MockStablecoin (mUSDC)** — 结算用 mock 稳定币：标准 ERC-20 + EIP-3009 +
   公开 faucet。供 Arena 402 的 EIP-3009 direct-relay 结算原型使用。
2. **ArenaMemorialNFT (arena402)** — 新纪念 NFT，限量 402 枚，token ID 为
   `0..401`，实现 ERC-721 Metadata + ERC-5192，**soulbound / mint-only**。
   `transferFrom`、两个 `safeTransferFrom`、`approve`、`setApprovalForAll`
   全部 revert，无 EIP-3009。
3. **ArenaMemorial (arena402-m，deprecated)** — 已部署的旧 ERC-20
   纪念币，仅保留历史源码；新发放不得再使用。
4. **ArenaGameCoin (arena402-g)** — 游戏币，**白名单受限转账** ERC-20 + EIP-3009：
   `_transfer` 要求 from 与 to 都在白名单，只有登记的参赛钱包之间可转；DEX 池地址
   进不了白名单 → 建不了池 → 无法外部炒作。白名单检查覆盖 EIP-3009 全路径。

> 红线（见 `AGENTS.md`）：绝不发可自由交易的纪念 NFT。arena402 依靠
> ERC-5192 + 全转移路径 revert，arena402-g 仍依靠白名单。纪念 NFT 与
> 游戏币、支付授权和结算链路完全分离。

它们本身不构成完整 HTTP x402 闭环。

## 单测（不上链，本地 hardhat node）

```bash
npm install
npm run compile
npm run node        # 终端 A：起本地 EVM（内存链，固定测试私钥）
npm test            # 终端 B：跑 arena402 NFT、旧 -m 和 -g 红线单测
```

覆盖：arena402 的顺序编号、批量 mint、402 上限、ERC-5192 和 soulbound
全路径；旧 -m 的历史回归；-g 白名单双方校验、未登记（含 DEX 池）拒转、
EIP-3009 经 facilitator 代付成功且同样受白名单约束。

## 纪念 NFT 部署（人工确认）

部署脚本默认只做 dry run。部署是 testnet 状态变更，检查环境后必须显式添加
`--apply`：

```bash
npm run compile
npm run deploy:memorial-nft
npm run deploy:memorial-nft -- --apply
```

环境变量：

- `FACILITATOR_PRIVATE_KEY`：部署者，同时成为合约 owner；
- `MEMORIAL_BASE_URI`：每枚 NFT 的 URI 前缀；
- `INJECTIVE_EVM_RPC` / `INJECTIVE_CHAIN_ID`：默认 testnet 1439。

脚本继续使用 Injective testnet 的 legacy transaction、动态 gas price ×3 和
Blockscout 轮询。确认成功后只更新 `../deployments.json` 的 `memorial` 段。

## 按注册资格发放

钱包由离线 CSV 预先生成一次。后端 migration `035` 只导入公开地址，并按持久化
GitHub 用户的注册顺序锁定前 402 名；它不接入 arena402-g、PaymentMandate、
Facilitator API 或游戏结算。

`deploy/scripts/prepare_memorial_mint_batch.py` 从已锁定资格生成一个最多 40 个地址、
token ID 连续的公开 manifest。人工核对后，合约脚本默认只验证合约
`nextTokenId`、地址摘要和收款顺序，不发送交易：

```bash
npm run issue:memorial-nft -- \
  --manifest /absolute/path/to/memorial-batch-000.json
```

只有使用同一 manifest 显式添加 `--apply` 才会调用 `mintBatch`。Blockscout
确认后脚本原子更新 manifest，再由
`deploy/scripts/record_memorial_mint_batch.py --apply` 回写业务库：

```bash
npm run issue:memorial-nft -- \
  --manifest /absolute/path/to/memorial-batch-000.json --apply
```

公开 manifest 不含 user id、助记词或私钥；已确认的批次可以安全重跑。

### 明文钱包 CSV（仅离线交付）

只有在明确需要把钱包交付给人工保管时，才生成包含助记词和私钥的 CSV。输出路径
必须在仓库外，文件已存在时脚本会拒绝覆盖：

```bash
npm run generate:memorial-wallet-csv -- 402 /secure/arena402-memorial-wallets.csv
```

每个钱包使用独立的 12 词助记词和
`m/44'/60'/0'/0/0` 派生路径。CSV 包含地址、公钥、私钥、助记词、token ID
和 chain ID。它是完整控制凭证，不得提交 Git、发送到聊天或放入运行容器；生成后
应立即收紧文件 ACL，并制作加密离线备份。

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
