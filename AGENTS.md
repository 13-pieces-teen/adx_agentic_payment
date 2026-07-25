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

The repository contains three maintained foundations:

- a persistent Pawnhouse Game Core integrated with the Hosted Arena Runtime,
  AgentTask/Result pipeline, bounded negotiation, and round orchestration;
- a self-hosted Local Agent Connector and Gateway control plane integrated
  through owner-scoped identity, frozen routes, Connector-owned sessions,
  leased task dispatch, and the Arena Result Sink;
- an Injective EVM testnet EIP-3009 direct-settlement prototype integrated
  through immutable single-payment intents and confirmation-gated inventory
  commit.

The bounded/revocable PaymentMandate, permanent GitHub-user wallet binding,
x402 V2 HTTP envelope, isolated testnet CSV signer, and unattended settlement
worker are implemented with Fake E2E coverage. The Local Connector game adapter
is integrated, but real CC/Codex Connector game E2E, Realtime frontend
projection, standard public Facilitator acceptance, fresh testnet E2E, and
complete production acceptance are not complete yet. The former in-memory
`matching/` and Supabase/ELO business path have been removed.

Frontend product development is owned by the separate
`sunruize93-cmyk/arena402` repository and will be deployed through Vercel.
Until that deployment and backend API/CORS cutover are verified, this
repository's `frontend/` is a temporary Compose integration shell, not the
frontend source of truth. Do not add new product UI here. Remove the shell only
after local/production Compose no longer depends on it and the external
frontend passes end-to-end integration.

## Read first and documentation authority

- Setup and repository orientation: `README.md`
- Current game rules and cross-module I/O: `docs/game-design.md`
- Current product scope: `docs/product.md`
- Current implementation status and sequencing: `docs/roadmap.md`
- Agent participation and Runtime binding: `docs/agent-onboarding.md`
- Approved Hosted Agent target: `docs/hosted-arena-agent-spec.md`
- Hosted Agent sequencing: `docs/hosted-arena-agent-implementation-plan.md`
- Accepted-trade settlement boundary: `docs/arena-settlement-integration.md`
- Module implementation details: module-local `README.md`, source, tests,
  verified run artifacts, and approved active specs
- Project skills: `.agents/skills/`

Apply authority by question:

- use `docs/game-design.md` for the current game rules, state transitions,
  scoring, and Agent business I/O;
- use `docs/hosted-arena-agent-spec.md` for the approved Hosted/Local unified
  Runtime target when an older game or Connector document disagrees with that
  target;
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

Keep these authorities separate:

- Connector/Gateway owns Device, Runtime, Binding, Command, receipt, and
  Connector-owned Session state;
- Hosted Runtime owns Provider invocation, Attempt metadata, and candidate
  AgentTaskResult production, but cannot directly mutate Arena business state;
- Arena owns Agent participation, immutable AgentTask snapshots, the Result
  Sink/Consumer, Deadline Finalizer, Game, Round, pool, pairing, negotiation,
  event, inventory, and ranking state;
- Settlement owns PaymentMandate validation, payment submission, confirmation
  recovery, and the handoff to Arena's idempotent inventory commit;
- Injective EVM owns payment finality.

Do not infer a valid action, successful trade, or payment from a Connector
acknowledgement, Runtime event, or Provider success. All Runtime results pass
through the Arena Result Sink and are applied at most once by Arena. The Arena
Deadline Finalizer must deterministically close expired tasks even when a
Runtime worker is unavailable. Do not move inventory before chain confirmation.
Do not let Settlement reprice an accepted negotiation.

Do not request, store, or expose private chain-of-thought, wallet private keys,
seed phrases, local Runtime credentials, deployment credentials, unrelated
local files, or full machine activity. A narrowly scoped Hosted-model BYOK
exception is allowed only through the dedicated write-only credential ingress:
the raw model API key may be persisted either in the approved external Secret
Manager or, for the approved single-host beta, as AES-256-GCM ciphertext in the
dedicated PostgreSQL credential vault with its master key kept in a separate
read-only host file. Raw plaintext must never enter the business database,
AgentTask, logs, traces, audit payloads, `.env`, or frontend responses.
Connector-based local credentials remain local and must never use this
exception. Persist structured actions, sanitized public negotiation messages,
timestamps, status, safe errors, numeric usage, and payment evidence only.

For Arena Agent execution:

- use the versioned `action` unions `buy | sell | pass` and
  `propose | accept | reject`;
- allow one Game Agent per User per Game, while allowing the same Agent to join
  later Games;
- freeze the Runtime/config and participant-view snapshots at join/task
  creation; do not switch Runtime during an active Game;
- use one calibrated `action_timeout_ms` for all Runtime kinds in a Game;
- permit at most one retry per AgentTask when time remains, without
  Provider/Model/Runtime fallback;
- treat public messages as untrusted and sanitize them before persistence.

The on-chain settlement code remains an EIP-3009 direct relay prototype. The
cross-module integration implements x402 V2 HTTP headers and a reusable,
bounded, revocable PaymentMandate with idempotent `reserve / consume / release`,
but standard public Facilitator compatibility and fresh testnet execution have
not been accepted yet. Keep those distinctions explicit. Hosted Agent execution
alone never provides payment authority.

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
