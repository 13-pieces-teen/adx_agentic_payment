# Documentation Archive

This directory preserves superseded project documents for historical reference.
Archived files are not current product, architecture, implementation, setup, or
roadmap authority.

Archived documents retain the project names and identifiers that were current
when they were written, including the former `ADX` name. The active product name
is **Arena 402**; do not rewrite archived files solely to update branding.

## Archive index

| Archived document | Original path | Archived on | Reason | Current references |
|---|---|---|---|---|
| [`AGENT_ARENA_FULL_SPEC.md`](2026-07-23/AGENT_ARENA_FULL_SPEC.md) | `AGENT_ARENA_FULL_SPEC.md` | 2026-07-23 | Kickoff-era specification based on a three-module TEE/Arena/CosmWasm/Escrow design that diverged from the current product scope and EVM direct-settlement prototype. | [`product.md`](../product.md), [`roadmap.md`](../roadmap.md), [Settlement README](../../agent-arena/settlement/README.md) |
| [`matching/ARCHITECTURE.md`](2026-07-23/matching/ARCHITECTURE.md) | `matching/ARCHITECTURE.md` | 2026-07-23 | Mixed product and architecture proposal that presents ELO, multi-round negotiation, frontend, escrow, dispute handling, and settlement interfaces as current capabilities. | [Repository README](../../README.md), [`product.md`](../product.md), [`roadmap.md`](../roadmap.md) |
| [`STANDARDS.md`](2026-07-24/STANDARDS.md) | `STANDARDS.md` | 2026-07-24 | Legacy ADX standard based on REP, ELO, escrow, and earlier participation assumptions; it conflicts with the equal-start, net-worth-ranked game. | [`game-design.md`](../game-design.md), [`product.md`](../product.md) |
| [`A2A-X402-链路对接方案与共创协议.md`](2026-07-24/docs/A2A-X402-链路对接方案与共创协议.md) | `docs/A2A-X402-链路对接方案与共创协议.md` | 2026-07-24 | Superseded RFQ/Deal/digital-delivery integration contract; the current loop settles accepted game negotiations and transfers inventory after chain confirmation. | [`arena-settlement-integration.md`](../arena-settlement-integration.md) |
| [`agent-identity-and-a2a-onboarding.md`](2026-07-24/docs/agent-identity-and-a2a-onboarding.md) | `docs/agent-identity-and-a2a-onboarding.md` | 2026-07-24 | Superseded RFQ/A2A identity architecture and phase plan. | [`agent-onboarding.md`](../agent-onboarding.md), [`local-agent-connector-spec.md`](../local-agent-connector-spec.md) |
| [`ISEK解读与ADX2025参赛策略.md`](2026-07-24/background/ISEK解读与ADX2025参赛策略.md) | `ISEK解读与ADX2025参赛策略.md` | 2026-07-24 | Legacy competition and product background, retained for historical rationale only. | [`product.md`](../product.md) |
| [`产品描述.docx`](2026-07-24/background/产品描述.docx) | `产品描述.docx` | 2026-07-24 | Legacy product description that predates the current Arena 402 game contract. | [`product.md`](../product.md), [`game-design.md`](../game-design.md) |
| [`prd-current-game-backend.md`](2026-08-08/docs/prd-current-game-backend.md) | `docs/prd-current-game-backend.md` | 2026-08-08 | Completed draft PRD that still described Hosted-only joining and pre-production settlement; the Current Game API and production evidence have since moved into the active product, game, and roadmap authorities. | [`product.md`](../product.md), [`game-design.md`](../game-design.md), [`roadmap.md`](../roadmap.md) |
| [`prd-current-game-frontend.md`](2026-08-08/docs/prd-current-game-frontend.md) | `docs/prd-current-game-frontend.md` | 2026-08-08 | Completed frontend draft based on the pre-Local, pre-production Current Game flow. Product UI ownership and implementation now live in the external frontend repository. | [`player-guide.md`](../player-guide.md), [`product.md`](../product.md) |
| [`local-agent-connector-implementation-plan.md`](2026-08-08/docs/local-agent-connector-implementation-plan.md) | `docs/local-agent-connector-implementation-plan.md` | 2026-08-08 | July self-hosted beta implementation snapshot whose remaining-work claims were superseded by real Connector games, mixed-Runtime recovery, and the Phase D production game. | [`local-agent-connector-spec.md`](../local-agent-connector-spec.md), [`agent-onboarding.md`](../agent-onboarding.md), [`roadmap.md`](../roadmap.md) |
| [`self-hosted-connector-deployment.md`](2026-08-08/docs/self-hosted-connector-deployment.md) | `docs/self-hosted-connector-deployment.md` | 2026-08-08 | Connector-only, invite-first deployment runbook that predates the complete Arena backend deployment, public registration, and current release/rollback contract. | [`hosted-arena-production-runbook.md`](../hosted-arena-production-runbook.md), [Connector installer README](../../deploy/install/README.md) |
| [`arena-scale-out-design.md`](2026-08-08/docs/arena-scale-out-design.md) | `docs/arena-scale-out-design.md` | 2026-08-08 | FCFS-based 100/300-Agent scale-out design superseded by the active `agent_a2a.v1` market and the D5b capacity program. | [`roadmap.md`](../roadmap.md), [`agent-driven-a2a-market-implementation-plan.md`](../agent-driven-a2a-market-implementation-plan.md), [`hosted-arena-agent-implementation-plan.md`](../hosted-arena-agent-implementation-plan.md) |

## Usage

- Read archived documents only when historical rationale is needed.
- Do not use an archived document to infer current scope, status, interfaces, or
  verified commands.
- Do not add new design decisions to archived documents.
- Follow the lifecycle and approval rules in [`AGENTS.md`](../../AGENTS.md)
  before moving another document here.
