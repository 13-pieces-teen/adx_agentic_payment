# Arena 402 钱包与链上资产状态

> 最后更新：2026-08-09。本文记录当前网站钱包体验、测试网资产和仍需完成的展示
> 工作；合约地址与元数据以 [`../deployments.json`](../deployments.json) 为准。

## 当前资产

| 资产 | 当前用途 | Injective EVM testnet |
|---|---|---|
| `arena402` | Founding 402 纪念 NFT，ERC-721 + ERC-5192，soulbound | `0x17D9Cd66b9BC7b6D12FB3c60cbf81b2f814c2364` |
| `arena402-g` | Current Game 游戏币，6 decimals、白名单转账、EIP-3009 | `0xBF7B7268CE82d92BaC7a95a741F4003FE84e1884` |
| `mUSDC` | SDK/合约兼容和隔离 canary | `0x06D223D12774386A96D33863D9106A800e52BDeD` |
| `arena402-m` | 已弃用的旧纪念 ERC-20 | 仅保留历史源码和兼容记录 |

纪念 NFT 与游戏币是两套独立资产：纪念 NFT 不参与支付；`arena402-g` 只在已登记
参赛钱包之间转移，链上确认后才改变 Arena 现金和库存。

## 网站钱包体验

当前网站有两类钱包关系：

1. **平台测试钱包**：注册用户按内部 `user_id` 懒分配一个稳定的
   `sandbox_guest` 钱包，用于 Current Game 的 GameCoin、PaymentMandate 和自动
   testnet 结算。API 只返回公开地址和余额，不返回 signer secret。
2. **用户控制的钱包绑定**：用户通过 EIP-191 challenge 证明一个外部 Injective
   EVM testnet 地址的所有权。它用于身份展示和后续扩展，不会自动获得平台钱包的
   签名或支付权限。

玩家在 [Treasury](https://www.arena402.com/wallet) 查看平台钱包，在 Founding 402
页面领取纪念 NFT。资格和已铸数量属于生产数据库运营状态，不写死在本文。

## GameCoin 准备

加入 Current Game 后，Participant 必须完成钱包白名单和 `arena402-g` 初始金额
准备，链上确认后才能进入 `READY`。GameCoin Provisioner 当前：

- 最多保持 16 笔 owner 交易在途；
- 并发执行 whitelist/balance/gas 只读准备和已提交交易确认；
- 按连续 nonce 串行签名、持久化和广播；
- 始终先持久化签名交易，再广播，失败后按原 nonce 恢复。

2026-08-09 的隔离 100 钱包批次为 100/100 confirmed、0 failed，整批从
`692.500s` 降至 `162.430s`，Ready P95 为 `159.019s`。该测试只运行 Provisioner，
没有创建或开启比赛。

## 钱包展示与图标

- Founding 402 NFT 的 metadata 和图片已由 `www.arena402.com` 提供；
- `arena402-g` 的合约元数据已冻结，但 `deployments.json` 中的公开图标 URL 仍是
  TODO；
- [`token-icons/arena402-g.png`](token-icons/arena402-g.png) 是待替换/上传的源文件；
- `arena402-m.png` 只保留为旧资产兼容文件，不再是新纪念品交付物。

当前网页体验不依赖 MetaMask `wallet_watchAsset` 或 injpass 才能参赛。若后续要让
用户在第三方钱包中直接查看测试资产，再单独接入公开 token/NFT metadata、钱包
连接器和图标 CDN。

## 下一步

- 完成 `arena402-g` 正式图标并写回 HTTPS URL；
- 在移动端验证外部钱包绑定和 NFT 展示；
- 保持平台 signer、Facilitator、Hosted Credential 和用户外部钱包四个权限域分离；
- 主网资产、真实资金托管和可交易纪念品不属于当前项目范围。
