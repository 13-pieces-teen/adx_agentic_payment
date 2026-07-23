# Agent Arena — Monorepo

> Confidential Agent-to-Agent Trading Arena on **Injective × x402 × Intel TEE**
> AdventureX 2026 Hackathon MVP

本仓库是三人协作 monorepo。完整产品/架构 spec 见 `../AGENT_ARENA_FULL_SPEC.md`。

## 模块与负责人

| 模块 | 负责人 | 说明 |
|------|--------|------|
| `settlement/` | **Felix** | x402 agentic payment + Injective EVM 结算（本仓库当前重点）|
| `tee/` | Teammate 1 | TEE enclave + attestation（待加入）|
| `arena/` | Teammate 2 | 撮合引擎 + 订单簿 + Leaderboard（待加入）|

## 开发模式：SDD（Spec-Driven Development）

**先写规范，再写实现。** 所有规范在 `specs/`，每份 spec 有编号 + 状态 + 验收标准。
代码与提交都引用 spec 编号，方便三人异步协作时对齐。

- 规范索引：[`specs/README.md`](specs/README.md)
- 改需求先改 spec，再改代码。

## 协作方式（本地优先，无网络）

本项目**纯本地开发**，不使用 GitHub / 远程 git。协作方式：
1. Felix 在本机开发 → `git commit`（纯本地历史）
2. 硬盘拷贝整个 `agent-arena/` 目录到公司电脑
3. 队友在公司电脑 `npm install` 恢复依赖后继续开发

> `node_modules/` 不进包（见 `.gitignore`），公司电脑首次需联网 `npm install`。
> `.env`（含私钥）**绝不打包**，各自本地配置。

## 快速开始（Felix 当前进度）

```bash
cd settlement && cat README.md   # settlement 模块技术方案
cat specs/README.md              # 规范索引与进度
```
