# 被后端卡住登记 (BLOCKED)

> 遇到后端端点/字段缺失、形状不符时写这里，然后**继续做能做的**，别停等。
> 后端队友醒来会看这个文件。格式：`[时间] Agent X — 需要什么 — 期望形状 — 当前 workaround`

## 已知后端缺口（来自 SPRINT_CONSENSUS §2.3，无需重复登记）
- N 回合自动推进（只有单回合）
- 最终结算价 / settle table 生成
- 排行榜写库 + 读取 API（rankings 表空）
- 公开比赛运营 API（创建/开始/推进仅在 X-Arena-Dev-Token dev 端点）
- PaymentMandate、真实链上广播、Connector 入游戏

## 冲刺中新发现的阻塞
<!-- 在下面追加 -->
（暂无）
