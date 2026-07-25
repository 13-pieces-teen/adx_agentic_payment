# Arena 402 Token 图标 —— 前端交付指引

> 这里放 arena402-m / arena402-g 两个 token 的钱包图标。
> **前端最终把设计好的 PNG 放到本目录,替换掉占位文件即可**,链上/导入逻辑会自动引用。

## 需要交付的文件(替换同名占位文件)

| 文件名 | 对应 Token | 用途 |
|---|---|---|
| `arena402-m.png` | arena402-m(纪念币,soulbound) | MetaMask/injpass 钱包导入时显示的币图标 |
| `arena402-g.png` | arena402-g(游戏币,白名单) | 同上 |

当前这两个 `.png` 是 **占位文件(0 字节 / 纯色方块)**,请用最终设计稿覆盖。

## 图标规格(钱包导入用,请严格遵守)

- **格式**:PNG(带透明通道优先)。
- **尺寸**:正方形。推荐 **256×256**(MetaMask `wallet_watchAsset` 与多数钱包都接受;会自动缩放到列表里的小图标)。最低不小于 64×64。
- **文件体积**:建议 < 100 KB。钱包导入走 `image` URL,过大影响加载。
- **视觉**:圆形裁切安全 —— 多数钱包会把方图裁成圆形显示,重要元素放中心圆内,四角留白。
- **区分度**:-m 与 -g 要在小尺寸(约 24×24 显示)下一眼可分(建议主色不同 / 图形符号不同)。

## 交付后怎么被使用(前端无需改这里,仅说明)

1. 图标最终需要一个 **公开可访问的 https URL**(钱包 `wallet_watchAsset` 的 `image` 字段只吃 URL,不吃本地路径)。
   - 部署时会把本目录的 PNG 上传到 CDN / 对象存储 / IPFS,拿到 URL 后写进导入配置。
   - 托管 URL 最终会回写到 `../deployments.json` 的 `memorial.image` / `gameCoin.image` 字段(见下)。
2. 前端"一键导入代币"按钮调用 `wallet_watchAsset`,`image` 用该 URL,`decimals`/`symbol`/`address` 从 `deployments.json` 读。

## 命名 / decimals / symbol 约定(冻结,勿改)

| Token | symbol | decimals | soulbound/白名单 |
|---|---|---|---|
| 纪念币 | `arena402-m` | `0`(纪念币按枚计,无小数) | soulbound,mint-only |
| 游戏币 | `arena402-g` | `6`(与结算金额 GOLD_SCALE 对齐) | 白名单转账,mint 不限 |

> decimals 一旦部署即固定,前端导入时务必传对(-m 传 0,-g 传 6),否则钱包显示数量会错位。

## 相关文件

- 合约:`../contracts/contracts/ArenaMemorial.sol`(-m)、`ArenaGameCoin.sol`(-g)
- 部署后地址与图标 URL:`../deployments.json`(`memorial` / `gameCoin` 段,部署脚本回写)
- 钱包导入前端逻辑:待前端实现,读 `deployments.json` + 本目录图标 URL
