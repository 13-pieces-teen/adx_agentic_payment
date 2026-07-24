# Arena 402 Product Contract

> Status: current product scope for the hackathon MVP.

## 产品定位

**Arena 402** 是面向 Agent 的受限 RFQ 交易与可验证交付竞技场，用于购买和
交付可机器验证的数字商品或服务结果。

买方不是浏览通用 marketplace，而是把预算、期限、交付要求和授权边界写入
`Mandate`，再发出结构化 `RFQ`。卖方 Agent 返回带价格、有效期和交付承诺的
签名 `Offer`。双方只进行受限协商，形成不可变的 `Deal`，随后通过支付解锁
可验证交付物。

Injective 是 MVP 的具体 testnet 结算层。标准 x402 HTTP 支付流程是目标接口，但
Injective、x402 或底层支付设施本身不负责 Arena 402 的 RFQ、报价、协商和
交付验证。

## MVP 交易对象

首个演示只支持一种能够端到端机器验证的数字交付物。它必须具备：

- 可明确判断的规格、价格、截止时间和商业条款；
- 支付前可提交的 `DeliveryCommitment`；
- 可通过内容哈希或等价证明验证的最终交付物；
- 与成交条款、支付凭证和交付结果关联的 `Receipt`。

物理商品、主观质量无法自动判断的服务，以及需要人工仲裁才能确认结果的交易，
不进入首个 MVP。

## 目标流程

1. Buyer Agent 根据用户授权创建 `Mandate` 和 `RFQ`。
2. Seller Agent 返回签名 `Offer`；协议最多允许一次 `CounterOffer`。
3. 接受后的条款固化为 `Deal`，不再由支付层重新定价。
4. Seller 提交绑定交付物哈希、条款或许可、有效期的
   `DeliveryCommitment`。
5. 交付端点按 x402 协议返回 `402 Payment Required` 和 payment requirements。
6. Buyer 完成 Injective testnet 支付；支付失败或无效时不得解锁交付物。
7. Seller 返回交付物或解锁材料，以及可关联 Deal、payment 和 artifact 的
   `Receipt`。
8. Buyer 验证支付凭证、条款、许可和交付物哈希。

## MVP 验收标准

以下是目标验收条件，不代表当前仓库已经全部实现：

- 一次演示完整经过
  `Mandate -> RFQ -> Offer -> Deal -> Payment -> Delivery -> Receipt`。
- RFQ 之外的报价、过期 Offer 或被修改的 Deal 不得触发交付。
- 支付结果可通过 Injective testnet 交易哈希或等价凭证验证。
- Buyer 可以独立验证最终交付物与 `DeliveryCommitment` 一致。
- 重试不会产生重复 Deal、重复付款或重复解锁。
- 无效或失败支付不会释放交付物，并返回明确终态。
- 仓库、日志和交付凭证不包含私钥、助记词、访问令牌或其他真实秘密。

## 当前实现边界

- `matching/` 和 `web/api.py` 已实现内存版 Agent 注册、listing/intent、
  matching、受规则约束的 negotiation 和 Arena/ELO 记分/会话原型；它不是
  完整的 Arena 402 RFQ 应用层。
- `agent-arena/settlement/` 已实现 Injective EVM testnet 上的 EIP-3009
  授权与直接 mUSDC 结算原型。
- 上述两部分尚未接成 Arena 402 的 `RFQ -> Deal` 领域协议。
- x402 HTTP challenge、带支付重试、交付解锁和最终 `Receipt` 尚未形成
  端到端闭环。
- TEE、链上 escrow、争议处理、真实退款、生产手续费和信誉系统尚未作为产品
  能力实现。

## 非目标

首个 MVP 不实现：

- 通用多品类 marketplace 或完整链上订单簿；
- 多轮砍价、开放式 Agent 博弈或最优价格发现；
- 对每次推理、消息或谈判轮次单独收费；
- 主网真实资金交易；
- 依赖 TEE、链上身份、信誉或争议系统才能成立的可信承诺；
- 跨链结算、流动性服务和生产级运营体系。

## 待冻结问题

- [ ] 首个机器可验证数字交付物的具体类型与验收器是什么？
- [ ] 现有 Python `Intent` / `ResourceListing` 如何映射到
      `Mandate` / `RFQ` / `Offer` / `Deal`？
- [ ] 标准 x402 HTTP 流程的 seller resource endpoint、client retry 和
      payment response 采用什么接口？
- [ ] `DeliveryCommitment` 和 `Receipt` 的最小字段、签名者与持久化方式是什么？
- [ ] 演示继续使用 mUSDC，还是切换到团队验证过的其他 Injective testnet 资产？
- [ ] 哪些步骤使用真实 testnet，哪些可以使用明确标注的 mock？
