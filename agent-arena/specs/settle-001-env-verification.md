# SETTLE-001 · Injective EVM 环境验证 + USDC EIP-3009 探测

- **状态**: ✅ Approved（可执行）
- **负责人**: Felix
- **依赖**: 无 · **解锁**: 全部后续 spec 的地基假设

## 为什么先做这个

整个方案（SETTLE-000）建立在两个假设上：Injective EVM 可用（A1）、USDC 支持 EIP-3009（A2）。
**在写任何签名/facilitator 代码前，必须先用最小成本证伪或证实这两点。** 若 A2 不成立，
SETTLE-003 要改走 Permit2，越早知道越好。

## 验收标准（Acceptance Criteria）

| # | 标准 | 如何验证 | 结果 |
|---|------|---------|------|
| AC1 | 能连上 Injective EVM testnet RPC | 脚本读到最新区块号 + chainId == 1439 | ⬜ |
| AC2 | facilitator 钱包有 INJ（付 gas） | 领 faucet 后脚本查余额 > 0 | ⬜ |
| AC3 | 买方钱包有 USDC | 领 Circle faucet 后脚本查 USDC 余额 > 0 | ⬜ |
| AC4 | **确认 USDC 是否支持 EIP-3009** | 脚本探测 USDC 合约有无 `transferWithAuthorization` + 读 `name()`/`version()` | ⬜ |
| AC5 | 记录所有链上参数 | 产出 `settlement/.env.example` + `deployments.json`（USDC 地址/decimals/EIP712 name/version）| ⬜ |

## 关键参数（已知）

| 项 | 值 |
|----|----|
| CAIP-2 / Chain ID | `eip155:1439` / `1439` |
| JSON-RPC | `https://k8s.testnet.json-rpc.injective.network/` |
| Explorer | `https://testnet.blockscout.injective.network/` |
| INJ faucet | `https://testnet.faucet.injective.network/` |
| USDC | Circle testnet faucet；合约地址由本 spec 探测确认 |

## 交付物

1. `settlement/scripts/check-env.ts` — 一键探测脚本（viem，只读，不花钱不上链）
2. `settlement/scripts/setup-env.md` — 人工步骤清单（领币、配私钥）
3. `settlement/.env.example` — 环境变量模板
4. `settlement/deployments.json` — 探测确认的链上参数（供后续 spec 引用）

## 执行步骤

1. `npm install`（settlement/，装 viem + dotenv）
2. 人工：创建 3 个测试钱包（买方/卖方/facilitator），填入 `.env`
3. 人工：facilitator 领 INJ、买方领 USDC
4. `npx tsx scripts/check-env.ts` → 逐条打钩 AC1–AC5
5. 把探测到的 USDC 地址/decimals/name/version 写入 `deployments.json`

## 执行结果（2026-07-23）

| AC | 结果 | 说明 |
|----|------|------|
| AC1 | ✅ | chainId 1439，RPC 连通（加重试后稳定），区块 ~1.344 亿 |
| AC2 | ✅ | facilitator `0x7fB8…aD50` 有 1 INJ，gas 就绪 |
| AC3 | ❌→重定义 | faucet 发的是 peggy USDT，见下 |
| AC4 | ❌→重定义 | 同下 |
| AC5 | ✅ | deployments.json 已生成 |

### 关键发现（决定性）

faucet 发放的是 **peggy USDT**（Cosmos denom `peggy0x87aB3B4C8661e07D6372361211B96ed4Dc36B1B5`）。
探测显示该地址 **在 EVM 侧无合约字节码** → 它不是可直接调用的标准 ERC-20，
无法用于 EVM 侧的 EIP-3009 / x402 流程。

**结论**：印证 SETTLE-000 D8 —— **自部署 mock USDC（标准 ERC-20 + EIP-3009）为确定主路径**。
M1 里程碑达成（EVM 可用 + gas 就绪 + token 方案确定）。后续 USDC 由 SETTLE-002.5 部署。

## 风险与分支（已解决）

- ~~AC4 走 Permit2~~ → 不需要，自部署合约原生带 EIP-3009。
- 后备的"自部署 mock USDC"已升为主路径。
