# Arena 402 钱包 / 代币导入 —— 进度与认知交接

> 负责人:Felix(settlement / 链上)。最后更新:2026-07-25。
> 用途:钱包端体验的当前状态、已定结论、关键认知与待办。**下一步依赖联系
> injpass 团队确认代币显示规范后继续。**

---

## 一句话状态

两个游戏代币已上 Injective EVM testnet(1439)并通过单测;**代币"上链"这一层完成**。
钱包端"用户看到并导入代币"的体验,**方向已定为 injpass + 社区代币列表批量导入**,
但卡在一个外部依赖:**injpass 如何显示自发 ERC-20 的确切规范,需向 injpass 团队确认**。

---

## 已完成(链上)

| 项 | 值 |
|----|----|
| arena402-m(纪念币) | `0xE6b9865a5fbbb45bF58b8235D02Ec40d97D58E8d` · decimals **0** · soulbound(mint-only,不可转) |
| arena402-g(游戏币) | `0xBF7B7268CE82d92BaC7a95a741F4003FE84e1884` · decimals **6** · 白名单受限转账 + EIP-3009 |
| 链 | Injective EVM testnet,chainId **1439** |
| owner(部署者) | facilitator `0x7fB8d49698E40656F727511Da6251D3e6dfFaD50`(⚠️主网前须重新考虑 owner) |
| 合约源码 | `../contracts/contracts/ArenaMemorial.sol`、`ArenaGameCoin.sol` |
| 单测 | `../contracts/test/tokens.test.mjs`(13 项全过:soulbound 全路径 revert、白名单双方校验、EIP-3009 经 facilitator 代付且受白名单约束) |
| 元数据 | `../deployments.json` 的 `memorial` / `gameCoin` 段 |

红线满足:-m 靠 soulbound、-g 靠白名单,均不可自由交易(符合 AGENTS.md「绝不发可自由交易的币」)。

---

## 关键认知(踩过 / 查证过,别重复走弯路)

### 1. 手机端"丝滑导入"不能靠 MetaMask
- iOS 版 MetaMask 因苹果政策,**普遍没有 DApp 浏览器入口**(实测本人 iOS 找不到)。
- `wallet_watchAsset` / `wallet_addEthereumChain` 一键能力**依赖 DApp 浏览器**,故
  **iOS MetaMask 上一键导入不可用**,用户只能手动加网络 + 贴地址,与"丝滑"相悖。
- 结论:**MetaMask 不是手机端最终方案**,只作为 PC/桌面参考路径。

### 2. injpass 才是手机端对的底座(已查证 @injpass/cli v2.7.0)
- 零依赖 <5KB,**iframe/浮窗嵌入自己的网页**(floating/modal/inline),用户在页面内
  用 **Passkey(WebAuthn)** 建钱包,popup 授权,私钥不离开 injpass 宿主。
- **iOS/Android 普通浏览器直接用**(Safari/Chrome),不需装 App、不需 DApp 浏览器
  → 正好绕开 MetaMask iOS 的死结。
- 提供 **EIP-1193 provider**(`connector.getEthereumProvider()` → `window.ethereum`),
  ethers/wagmi/viem 零改动;`signMessage` / `sendTransaction` / 读 RPC。
- README 示例**原生 chainId 1439 + 我们的 RPC**,为 Injective 而生,契合度极高。
- 需要 `embedUrl`(injpass 宿主服务)+ 把 dApp 注册进 injpass mini app 清单(origin 白名单)。
  → **接 injpass 不是纯本地能跑通,要和 injpass 平台对接**(去年冠军给了 500 测试钱包,应有渠道)。

### 3. injpass 代币显示 = 走社区代币列表批量导入(Felix 判断)
- injpass README **未提及 wallet_watchAsset** → 大概率不支持"网页一键弹窗加币"。
- **Felix 的方向**:走 Injective 生态通行的**代币列表(token list)批量导入**——
  injpass / 社区维护一份官方代币注册表,钱包从中拉取 address/symbol/decimals/**logoURI**
  自动显示;社区决定收录哪些代币。
- 含义:**图标不靠 watchAsset 传,而是随代币列表条目提交**;图标 URL 成为硬需求。

---

## 待办(阻塞在联系 injpass 团队)

### A. 必须向 injpass / Injective 社区确认(外部依赖,现在做不了)
1. 自发 ERC-20 如何进入 injpass 钱包资产显示?是社区代币列表还是别的机制?
2. 代币列表的**确切 schema**:字段名、是否 Uniswap tokenlist standard、logoURI 尺寸/格式要求、chainId 表示法。
3. **提交渠道**:GitHub 仓库 PR?后台表单?injpass mini app 注册?审核流程与周期?
4. testnet 代币要不要单独列表(上主网换地址时如何迁移)?
5. injpass 到底支不支持 watchAsset / 自定义图标的即时导入(确认前述判断)?

### B. 前端配合(不阻塞,可并行)
6. 出 arena402-m / -g 正式图标(规格见 `token-icons/README.md`:256×256 PNG、圆形安全区、<100KB)。
7. 上传图标到 CDN/IPFS 拿**公开 https URL**,替换 `deployments.json` 与 `token-list.draft.json` 的 logoURI 占位。

### C. 拿到规范后 Felix 做(等 A 完成)
8. 按社区确切 schema 把 `token-list.draft.json` 转成可提交条目并提交。
9. 若走 injpass 嵌入:把 dApp 集成 `@injpass/cli`(floating 浮窗 + connect + 暴露 window.ethereum),
   用 500 测试钱包实测代币显示与图标。

---

## 本目录文件说明

| 文件 | 用途 |
|----|----|
| `WALLET_INTEGRATION_STATUS.md` | 本文件,进度与认知交接 |
| `token-list.draft.json` | 代币列表提交草稿(格式无关,待规范确认转最终格式) |
| `token-icons/` | -m/-g 图标占位 PNG + 前端交付指引 |
| `import-tokens.html` | **MetaMask/桌面参考路径**(watchAsset 一键导入),非手机最终方案 |

## 相关(非本目录)
- 合约与单测:`../contracts/`(`npm run compile` / `npm run node` + `npm test` / `npm run deploy:tokens`)
- 代币元数据(权威):`../deployments.json`
- 项目状态:`~/Desktop/ADVX/agent-arena/PROJECT_STATE.md`(Felix 维护)
