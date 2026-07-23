# SETTLE-001 · 人工步骤清单（领币 + 配钱包）

`check-env.ts` 只做只读探测。以下人工步骤准备好环境，然后跑脚本对照打钩。

## 1. 安装依赖

```bash
cd settlement
npm install
```

## 2. 生成三个测试钱包

任选一种。**测试专用，切勿用真实资产钱包。**

```bash
# 方式 A：用 openssl 生成 3 个私钥
for r in buyer seller facilitator; do echo "$r: 0x$(openssl rand -hex 32)"; done
```

或用 MetaMask 建 3 个账户导出私钥。把三个私钥填进 `.env`（从 `.env.example` 复制）。

## 3. 加 Injective EVM testnet 到钱包（可选，方便看余额）

| 字段 | 值 |
|------|----|
| Network Name | Injective EVM Testnet |
| RPC URL | `https://k8s.testnet.json-rpc.injective.network/` |
| Chain ID | `1439` |
| Currency | INJ |
| Explorer | `https://testnet.blockscout.injective.network/` |

## 4. 领币

- **facilitator 领 INJ**（付 gas）：`https://testnet.faucet.injective.network/`，粘贴 facilitator 地址。
- **买方领 USDC**：Circle testnet faucet（在 Injective docs "USDC on Injective" 页找 faucet 链接与 USDC 合约地址）。
  - 领到后把 **USDC 合约地址填入 `.env` 的 `USDC_ADDRESS`**。

## 5. 跑探测

```bash
npm run check-env
```

逐条看 AC1–AC5 是否打钩。全绿 → 更新 `specs/settle-001-env-verification.md` 验收表 + `deployments.json` 已生成 → 里程碑 M1 达成。

## 常见问题

- **AC1 失败**：RPC 可能限流/变更，去 docs `developers-evm/network-information` 核对最新 RPC。
- **AC4 失败**：Injective USDC 若无 EIP-3009 → 按 SETTLE-000 A2 记录证伪，SETTLE-003 切 Permit2。
- **领不到 USDC**：后备方案是自部署一个带 EIP-3009 的 mock USDC（后续 spec 补脚本）。
