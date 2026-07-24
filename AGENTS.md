# Project Agent Guide

## Project

This repository is an early-stage hackathon prototype for **Arena 402**, a
round-based AI trading game:

- every Agent starts with equal cash and inventory;
- each round it chooses `buy`, `sell`, or `pass`;
- buy and sell pools are paired first-come-first-served;
- paired Agents negotiate for at most 2–3 turns;
- accepted trades settle point-to-point on Injective EVM testnet;
- after N rounds, final event-driven prices determine net-worth ranking.

The repository contains three implemented but not yet integrated foundations:

- an in-memory Python matching, negotiation, and Arena/ELO prototype;
- a self-hosted Local Agent Connector and Gateway control plane;
- an Injective EVM testnet EIP-3009 direct-settlement prototype.

The persistent round engine, game-specific Agent adapters, settlement-to-
inventory commit, and full game frontend are not complete yet.

## Read first and documentation authority

- Setup and repository orientation: `README.md`
- Locked game rules and cross-module I/O: `docs/game-design.md`
- Current product scope: `docs/product.md`
- Current implementation status and sequencing: `docs/roadmap.md`
- Agent participation and Runtime binding: `docs/agent-onboarding.md`
- Accepted-trade settlement boundary: `docs/arena-settlement-integration.md`
- Module implementation details: module-local `README.md`, source, tests,
  verified run artifacts, and approved active specs
- Project skills: `.agents/skills/`

Apply authority by question:

- use `docs/game-design.md` for game rules, state transitions, scoring, and
  Agent business I/O;
- use `docs/product.md` for MVP scope, acceptance criteria, and non-goals;
- use `docs/roadmap.md` for implementation status and sequencing;
- use source, tests, verified evidence, and module READMEs for claims about
  implemented behavior;
- use archived/background documents only for historical context.

If an implementation-status claim conflicts with code or verified evidence,
the evidence wins and the active status document must be corrected. Code does
not silently override the product/game contract.

## Compatibility and frozen records

- The active product name is **Arena 402**.
- Preserve real compatibility identifiers such as `adx-connector`, `ADX_*`,
  package names, database identifiers, persisted values, wire URIs, and
  historical archive content unless a migration is explicitly approved.
- Completed specifications under `agent-arena/specs/` are frozen development
  records. Do not rewrite them to match the current game framing.
- When a frozen spec differs from current behavior, document the difference in
  an active module README or `docs/roadmap.md`.
- `docs/injective/` contains fixed external documentation snapshots. Do not
  edit them or treat them as evidence of implemented Arena behavior.

## Architecture boundaries

Keep three authorities separate:

- Connector/Gateway owns Device, Runtime, Binding, Command, receipt, and
  Connector-owned Session state;
- Arena owns Game, Round, pool, pairing, negotiation, event, inventory, and
  ranking state;
- Injective EVM owns payment finality.

Do not infer a successful trade or payment from a Connector acknowledgement.
Do not move inventory before chain confirmation. Do not let Settlement reprice
an accepted negotiation.

Do not request, store, or expose private chain-of-thought, API keys, wallet
private keys, seed phrases, unrelated local files, or full machine activity.
Persist structured actions, public negotiation messages, timestamps, status,
errors, and payment evidence only.

The current settlement code is an EIP-3009 direct relay prototype, not a
complete standard HTTP x402 implementation. Keep that distinction explicit.

## Repository harness

- Keep `README.md`, `docs/game-design.md`, `docs/product.md`,
  `docs/roadmap.md`, and active module entrypoints aligned.
- Add a new maintained document only when it owns a unique purpose.
- Keep `.agents/` limited to shared project skills.
- Treat `.agents/skills/` as the only editable source for project skills.
- Do not edit generated copies under `.claude/skills/`.
- After changing a skill, run:

  ```text
  python scripts/sync_skills.py --write
  python scripts/sync_skills.py --check
  ```

## Documentation lifecycle

- Update active entrypoints in place. Do not archive `README.md`,
  `docs/game-design.md`, `docs/product.md`, `docs/roadmap.md`, or an active
  module entrypoint merely because its content is stale.
- Before moving any document into the archive, present the proposed target,
  reason, and replacement to a human and receive confirmation.
- Preserve superseded documents under
  `docs/archive/YYYY-MM-DD/<original-relative-path>`.
- Update `docs/archive/README.md`, preserve archived file contents, and repair
  inbound links after a move.
- Archived documents are non-current. Do not add new decisions to them.
- Do not edit frozen specs under `agent-arena/specs/`.

## Working rules

- Keep changes scoped and preserve unrelated work.
- Add only commands verified in this repository.
- Never commit secrets or real payment credentials.
- Use testnet by default.
- Require human confirmation before a state-changing chain transaction.
- Keep amount handling deterministic; use token units or fixed-point decimal,
  not binary floating point.
- Make retries idempotent across Agent calls, settlement submission, chain
  recovery, and inventory commit.
