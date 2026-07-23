# Facilitator — 自建 x402 结算服务 (SETTLE-004)

拿买方离线签名的 EIP-3009 授权，用 facilitator 私钥代付 gas，在 Injective EVM 上
调 `mUSDC.transferWithAuthorization` 完成结算。买方/卖方零 gas。

## 启动

```bash
npm install
npm start          # 默认 :4021，读 ../.env 和 ../deployments.json
```

## 端点

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/health` | 存活 + facilitator INJ 余额 |
| POST | `/verify` | 预检（不上链）：nonce 未用 + 余额足 + 未过期 |
| POST | `/settle` | 代付 gas 上链结算，返回 tx hash |
| POST | `/faucet` | `{to}` → 发 1000 mUSDC（expo 现场领币）|

`/settle` 和 `/faucet` body = SETTLE-003 产出的 `PaymentAuthorization`（settle）或 `{to}`（faucet）。

## 设计要点

- **串行队列**：facilitator 单账户发交易，`/settle` 串行执行避免 nonce 冲突。
- **legacy tx + gasPrice×3**（D7）；**blockscout 轮询确认**（不用 viem 回执，见 SETTLE-002.5 坑2）。
- `/settle` 前先 `/verify` 预检，明知失败不烧 gas。

## 验证

见 `../sdk/scripts/e2e.ts`（SETTLE-005）：一条命令跑通 签名→结算→防重放。
