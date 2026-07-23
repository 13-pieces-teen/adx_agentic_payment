# SETTLE-003/004/005 · x402 点到点结算闭环

- **状态**: ✔️ Done（M3 达成 2026-07-23）
- **负责人**: Felix
- **依赖**: SETTLE-002.5（mUSDC 已部署，买方有币）
- **实现方式**: D9 — viem 手写 EIP-3009 + 自建 Express facilitator

## ✔️ 验收结果（真实上链）

| AC | 结果 | 证据 |
|----|------|------|
| 003-1/2/3 | ✅ | 本地恢复==买方；SDK domain==链上 DOMAIN_SEPARATOR；nonce 每次不同 |
| 004-1/2/3 | ✅ | /settle 返回真 tx，卖方 +5 mUSDC，仅 facilitator 付 gas |
| 005-1 | ✅ | 卖方 +5，买方 -5（精确）|
| 005-2 | ✅ | 重放被拒 "nonce already used"（EIP-3009 链上防重放）|
| 005-3 | ✅ | 买卖方零 INJ 消耗，仅 facilitator 付 gas |

结算 tx: `0x2458782ea387e981fde73b50bb00880736a7ec5953d50679096be72d0f9cef55`（block 134438902）

## 目标闭环

```
买方(TEE将来替换) ──① 离线EIP-712签名 TransferWithAuthorization──▶ payload
payload ──② HTTP POST /settle──▶ facilitator
facilitator ──③ 用自己私钥调 mUSDC.transferWithAuthorization(payload, legacy tx)──▶ 链上
链上 ──④ mUSDC 从买方转给卖方, facilitator 付 gas──▶ 完成
```

---

## SETTLE-003 · 买方 EIP-3009 签名封装

**交付**: `sdk/src/x402.ts` — `signTransferAuthorization(params)`

- 从 `deployments.json` 读 EIP-712 domain（name/version/chainId/verifyingContract）。
- 构造 EIP-712 typedData：`TransferWithAuthorization(from,to,value,validAfter,validBefore,nonce)`。
- 用买方私钥 `signTypedData` → 拆成 `v,r,s`。
- **nonce**: 32字节随机（禁止写死，D4）；**validBefore**: now+10min；**validAfter**: 0。
- 返回完整 `PaymentAuthorization` 对象（含 v,r,s）。

**验收**:
| AC | 标准 |
|----|------|
| 003-1 | 本地 `recoverTypedDataAddress` 恢复出的地址 == 买方地址 |
| 003-2 | domain 与链上 `DOMAIN_SEPARATOR()` 一致（否则签名链上验不过）|
| 003-3 | 每次调用 nonce 不同 |

---

## SETTLE-004 · 自建 facilitator

**交付**: `facilitator/src/index.ts`（Express）+ `facilitator/src/settle.ts`（结算逻辑）

**端点**:
| 路由 | 功能 |
|------|------|
| `POST /verify` | 只读校验：模拟调用 or 本地 recover 校签 + 查买方余额是否够 |
| `POST /settle` | facilitator 私钥调 `transferWithAuthorization`，legacy tx，blockscout 确认 |
| `POST /faucet` | 调合约 `faucet(to)` 给地址发 1000 mUSDC（expo 用）|
| `GET /health` | 存活 + facilitator INJ 余额 |

**约束**:
- legacy tx + gasPrice×3（D7）；用 `waitViaBlockscout`（SETTLE-002.5 坑2）。
- **nonce 顺序**：facilitator 单账户发交易，串行处理 /settle（加锁/队列），避免 nonce 冲突。

**验收**:
| AC | 标准 |
|----|------|
| 004-1 | /settle 返回真实 tx hash，blockscout 显示 success |
| 004-2 | 结算后卖方 mUSDC 余额增加 == value |
| 004-3 | facilitator 付了 gas（其 INJ 减少），买卖方 INJ 不变 |

---

## SETTLE-005 · 端到端闭环

**交付**: `sdk/scripts/e2e.ts`

**流程**:
1. 打印买卖方 mUSDC 初始余额
2. 买方签名（SETTLE-003）转 X mUSDC 给卖方
3. POST /settle（SETTLE-004）
4. 打印最终余额，断言：卖方 +X，买方 -X
5. **防重放测试**：同一 payload 再 POST 一次 → 预期 facilitator 返回失败（nonce 已用）

**验收**:
| AC | 标准 |
|----|------|
| 005-1 | 卖方余额恰好增加 X，买方减少 X |
| 005-2 | 重复提交被拒（nonce 防重放，D4 链上验证）|
| 005-3 | 全程买方/卖方零 INJ 消耗，仅 facilitator 付 gas |

**达成 = M3 里程碑：x402 agentic payment 点到点跑通。**

---

## 数据结构（供 TEE/arena 对接）

```typescript
interface PaymentAuthorization {
  from: string; to: string;
  value: string;          // atomic units (decimals=6)
  validAfter: string; validBefore: string;
  nonce: string;          // 0x + 64 hex
  v: number; r: string; s: string;
  token: string;          // mUSDC 合约地址
  chainId: number;
}
```
TEE 的 `GeneratePayment`（spec Interface A）将来产出这个对象；arena 的成交事件提供 from/to/value。
