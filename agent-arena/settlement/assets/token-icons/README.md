# Arena 402 链上资产图标

> 最后更新：2026-08-09。当前唯一待完成的 ERC-20 图标交付是
> `arena402-g.png`；Founding 402 纪念品已经迁移到 ERC-721 `arena402`，使用 NFT
> metadata 中的图片。`arena402-m.png` 只保留为已弃用资产的兼容文件。

## 当前文件

| 文件 | 状态 | 用途 |
|---|---|---|
| `arena402-g.png` | 待最终设计和公开托管 | `arena402-g` 游戏币在第三方钱包中的图标 |
| `arena402-m.png` | Deprecated | 旧纪念 ERC-20，不再用于 Founding 402 |

## `arena402-g.png` 规格

- PNG，优先透明背景；
- 正方形，推荐 256×256，最低 64×64；
- 建议小于 100 KB；
- 重要图形放在中心安全区，兼容钱包圆形裁切；
- 在约 24×24 的显示尺寸下仍能识别。

完成设计后：

1. 上传到稳定的公开 HTTPS/CDN 路径；
2. 把 URL 写入 [`../../deployments.json`](../../deployments.json) 的
   `gameCoin.image`；
3. 验证地址、symbol `arena402-g` 和 decimals `6`；
4. 在桌面和移动钱包中检查缩放、缓存和圆形裁切。

Founding 402 NFT 当前从
`https://www.arena402.com/api/memorial/metadata/<tokenId>` 获取 metadata，图片 URL
由合约 `baseURI` 对应的 API 管理，不从本目录读取。
