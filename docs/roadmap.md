# Arena 402 Roadmap

> 状态：当前跨模块实施状态与建议顺序。

Arena 402 已具备 matching、Connector 和 settlement 三个基础，但新定义的
回合制交易游戏尚未端到端实现。

## 目标垂直切片

```text
create game
  -> join equal-start agents
  -> broadcast event
  -> decide buy/sell/pass
  -> FCFS pair
  -> negotiate
  -> accept
  -> EIP-3009 testnet settlement
  -> confirmed inventory transfer
  -> final net-worth ranking
```

## 已完成基础

- [x] Python 内存版 Agent、listing/intent、matching、有限 negotiation、
      Arena/ELO 和 FastAPI wrapper。
- [x] Python A2A/payment 边界类型、fixtures 和 mocks。
- [x] self-hosted Local Agent Connector beta：出站配对/WSS、Runtime
      discovery、typed command、durable receipt/event、PostgreSQL 控制面、
      onboarding 和部署工具。
- [x] Injective EVM testnet 环境验证。
- [x] EIP-3009-compatible mock stablecoin。
- [x] SettlementSDK mock/real adapter。
- [x] 买方授权、项目 Facilitator、nonce replay protection 和 direct mUSDC
      testnet transfer。
- [x] CDN-only 静态 Arena 前端、Supabase Agent/Battle/Market/ELO 视图。
- [x] 当前游戏规则、Agent 入场和游戏结算边界已形成 v1 契约；数值参数和
      批量 fallback 仍待冻结。

这些完成项是基础能力，不等于完整游戏已经可运行。

## 当前缺口

- [ ] 缺少 `games`、`game_agents`、`holdings`、`rounds`、`pools`、
      `pairings`、`negotiations`、`neg_messages`、`settlements`、`events`、
      `settle_table`、`rankings` 的业务迁移和 repository。
- [ ] 缺少持久化回合调度、deadline、恢复和单局状态机。
- [ ] 现有 matching 不是按服务端决策完成时间执行的游戏 FCFS。
- [ ] 现有 negotiation 尚未实现新协议的消息上限、轮次上限、谈崩计数和
      每 Agent 每轮一次配对。
- [ ] Connector 尚未适配 `arena.decide` / `arena.negotiate`。
- [ ] 接受的协商尚未生成冻结 `SettlementIntent`。
- [ ] Settlement 尚未把链上确认与 Arena 库存事务幂等连接。
- [ ] 前端尚无 Game Lobby、Game View、Result 和对应 Realtime 数据流。
- [ ] 缺少事件牌组、可复核随机性和最终结算价生成器。
- [ ] 缺少一个命令运行完整演示和一份对应证据。

## 实施顺序

### P0：冻结演示参数

1. 确认回合数、货物、初始现金和持仓。
2. 确认固定交易数量、价格精度和 timeout。
3. 冻结一套确定性事件和一套概率事件。
4. 定义 testnet 钱包预充值与单局最大风险额度。

### P1：持久化游戏内核

1. 建立领域迁移和 repository。
2. 实现 game/round 状态机及崩溃恢复。
3. 实现服务端时间戳的 FCFS 和每轮一次配对。
4. 实现有 deadline 的 decide/negotiate adapter。
5. 实现事件、持仓快照、settle table 和可重算排名。
6. 为重放、重复消息、超时和阶段越权添加测试。

### P2：Agent 接入

1. 先用确定性 rule agent 验证游戏内核。
2. 加入 hosted personality-card Agent。
3. 加入 Hacker 模型/Prompt 配置和安全秘密存储。
4. 在 Connector/Gateway 增加版本化 Arena typed task 适配。
5. 保持 Control、Business、Payment 三类状态分离。

### P3：Settlement 接线

1. 实现不可变 `SettlementIntent` 和唯一幂等键。
2. 将 intent 严格绑定到 chain、token、payee、amount 和有效期。
3. 调用现有 SettlementSDK 与 Facilitator。
4. 持久化交易哈希并恢复未知终态。
5. 链上确认后幂等提交现金和货物事务。
6. 覆盖篡改、过期、revert、重复 nonce、超时和数据库失败。

详细契约见
[`arena-settlement-integration.md`](arena-settlement-integration.md)。

### P4：游戏前端

1. 增加 Game Lobby、Game View 和 Result。
2. 展示阶段、倒计时、事件、池、配对、协商和结算状态。
3. 用净资产排名取代游戏主路径中的 ELO。
4. 展示可核验 testnet 交易哈希。
5. 明确 loading、timeout、failed 和 recovered 状态。

### P5：演示与负载验证

1. 跑 2、4、8、16 Agent 的整局负载。
2. 测量每轮 wall time、LLM 调用数、超时率、结算时间和 RPC 失败率。
3. 依据证据决定 `MAX_TURN`、回合数和是否批量匹配。
4. 提供一个干净环境可运行的 demo command。
5. 保存局级审计记录和可复核排名结果。

## 可降级但仍可交付

- 3 轮协商降为 1 轮；
- 实时入池改为固定窗口批配对；
- 3 种货物降为 1 种；
- LLM Agent 不足时用明确标注的 rule agent 补位；
- 逐笔链上提交改为包含多笔点对点 transfer 的批量交易，并保留 accepted
  trade 到链上事件的逐笔映射。

不能降级为纯数据库“假支付”，也不能使用无法还原逐笔成交的纯聚合净额。
默认 MVP 为一笔 accepted trade 对应一笔 testnet 转账；批量 fallback 需要
显式启用并保留逐笔链上证据。

## 后续而非 MVP 阻塞项

- 标准 HTTP x402 challenge/retry/header 和公共 Facilitator 兼容；
- TEE key custody 与 remote attestation；
- 链上身份或 ERC-8004 reputation；
- escrow、退款、争议、仲裁和生产手续费；
- 主网、多链和高可用多节点；
- Agent Studio、人格市场和长期赛季系统。
