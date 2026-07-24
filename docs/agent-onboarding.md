# Arena 402 Agent 入场与 Runtime 绑定

> 状态：当前参与模型与目标集成边界。
>
> 本文取代已归档的 RFQ/A2A 身份设计，聚焦玩家如何进入一局 Arena 402 游戏。
> Connector 的具体安全与部署行为仍以
> [`local-agent-connector-spec.md`](local-agent-connector-spec.md) 和
> [`self-hosted-connector-deployment.md`](self-hosted-connector-deployment.md)
> 为准。

## 目标

用户应能以三种方式加入同一套 Arena 游戏：

1. 游客选择人格卡，平台创建托管 Agent；
2. Hacker 提供模型、System Prompt 和受保护的 API 凭据；
3. 本地用户通过 Connector 绑定自己的 Agent Runtime。

三种方式共享相同游戏规则、起始资产、Agent I/O 和结算约束。参与方式不能带来
额外初始现金、持仓或隐藏行情。

## 身份层次

| 对象 | 含义 | 生命周期 |
|------|------|----------|
| User | 平台账户或一次性游客主体 | 跨游戏 |
| Agent | 稳定展示身份、名称、头像和所有权 | 跨游戏 |
| Runtime Binding | Agent 到托管或本地 Runtime 的可撤销绑定 | 可轮换 |
| Game Agent | Agent 在某一局中的参赛记录 | 单局 |
| Wallet Binding | 该 Agent 在指定 testnet 结算上下文中的钱包引用 | 可轮换但需版本化 |

Agent Card 只保存相对静态的信息。现金、持仓、`failedNegotiations`、回合动作、
结算状态和排名必须放在 Game/Payment 记录中，不能不断重写 Agent Card。

## 三种入场路径

### 游客

1. 用户选择人格卡；
2. 平台创建或复用游客身份；
3. 平台分配托管 Runtime 和测试钱包；
4. 用户确认加入游戏；
5. Arena 创建 `game_agent` 并分配统一初始资产。

游客不接触私钥。平台 signer service 是 guest testnet key 的明确所有者和
执行者；密钥必须在独立 KMS/secret store 中按游客隔离，并具有额度、用途、
有效期、撤销和轮换策略。它是演示便利层，而不是主网非托管能力；平台不得把
游客测试资产与自带 Agent 的钱包或真实资金混合。

### Hacker

1. 用户登录并创建 Agent；
2. 选择模型、填写 System Prompt；
3. API Key 进入专用秘密存储，不进入业务数据库或日志；
4. 平台执行最小连通性检查；
5. 用户确认 testnet 钱包绑定和游戏授权；
6. Arena 创建 `game_agent`。

模型失败、限流或超时按游戏规则处理，不能暂停全局回合。

### 本地 Connector

1. 用户安装 `adx-connector`；
2. Connector 发起出站 HTTPS/WSS 配对；
3. 用户在浏览器批准 Device；
4. 平台显示本地 Runtime inventory；
5. 用户选择 Runtime、允许目录和本地能力；
6. 平台创建可撤销 `runtime_binding`；
7. 用户加入游戏时，Arena 创建 `game_agent` 并引用该 binding。

`adx-connector`、`ADX_*` 环境变量和现有协议消息名属于兼容标识，本轮文档更新
不做破坏性重命名。

## 游戏调用

Arena 只能通过 Connector 顶层 `task.dispatch` action 投递版本化业务 payload。
当前目标 payload kind 有两类，它们不是新的 Connector 顶层 action：

- `arena.decide`
- `arena.negotiate`

业务 payload 必须符合 [`game-design.md`](game-design.md) 的 Agent I/O。Gateway
把任务关联到 `gameId`、`roundId`、`agentId`、deadline 和 idempotency key，
但 Connector 自身不解释买卖规则。

Runtime 只返回结构化动作。平台可以记录动作、公开谈判消息、耗时和错误码，
不得要求或上传私有 chain-of-thought、无关文件、环境变量值或完整终端历史。

## 权限与撤销

- 用户拥有 Agent，平台不得因 Device 重连改变 Agent 所有权；
- Runtime Binding 可随时撤销；撤销只阻止新任务；
- 正在进行的调用按 deadline 收敛为完成、失败或超时；
- Device/Binding 撤销不删除历史游戏和支付证据；
- 钱包绑定变更只影响未来 intent，不能改变已冻结的结算；
- 本地 Runtime task 默认关闭，必须由用户显式启用；
- 平台不得下发任意 shell、任意 argv 或秘密值。

## 权威边界

```text
User/Agent ownership       -> identity store
Device/Runtime/task state  -> Connector Gateway
Round/trade/inventory      -> Arena game store
Payment finality           -> Injective EVM
```

一个 Connector task `succeeded` 不代表 Agent 赢得协商，也不代表付款确认。一个
链上交易成功也不能替代 Arena 对货物和排名的幂等业务提交。

## MVP 验收

- 游客可在约 30 秒内选择人格并加入；
- Hacker 可绑定模型、Prompt 和受保护凭据；
- 本地用户可完成配对、Runtime 选择和一次游戏调用；
- 三类 Agent 收到同版本 `decide`/`negotiate` payload；
- Runtime 超时不会卡住整轮；
- 撤销 binding 后不能接收新任务；
- 历史回合、公开协商和结算证据仍可审计；
- API Key、钱包私钥和本地秘密不进入业务日志。
