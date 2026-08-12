# Repository Structure

This document is the maintainer map for the Arena 402 backend repository. It
defines where new code belongs and which existing paths are compatibility
boundaries. Product behavior remains authoritative in `product.md` and
`game-design.md`; implementation status remains authoritative in `roadmap.md`.

## Layout

| Layer | Paths | Ownership |
|---|---|---|
| Business contracts | `arena_agent_contracts/` | Versioned, provider-independent AgentTask and AgentTaskResult schemas |
| Arena application core | `arena_core/` | Task snapshots, Result Sink/Consumer, deadline finalization, and participation |
| Game domain | `arena_game/` | Rules, market state, negotiation, ranking, and confirmation-gated inventory commit |
| Runtime execution | `hosted_agent_runtime/` | Provider invocation, attempts, safe tools, memory, and candidate results |
| Runtime control plane | `hosted_agent_control_plane/` | Hosted Agent configuration, credential lifecycle, readiness, and bindings |
| Local Agent transport | `connector/`, `connector_gateway/`, `arena_mcp/` | Device/runtime sessions, leased task transport, WSS wakeups, and stateless MCP |
| Payment and wallet | `arena_payments/`, `arena_wallets/`, `agent-arena/settlement/` | Mandates, settlement workers, wallet binding, signer boundary, SDK, Facilitator, and contracts |
| Public projections | `arena_memorial/` | Privacy-safe memorial projections and API support |
| HTTP composition | `web/` | FastAPI composition and route adapters; no product frontend lives here |
| Persistence | `db/migrations/`, `db_pool_config.py` | Forward-only PostgreSQL schema and shared pool sizing |
| Operations | `deploy/`, root Compose files | Build, release, migration, backup, rollback, and single-host orchestration |
| Developer tooling | `scripts/`, `requirements/` | Explicit demos, calibration/export tools, and locked Python dependency sets |
| Verification | `tests/test_*.py`, `tests/e2e/`, `.github/workflows/` | Fast pytest contracts, explicit acceptance harnesses, and CI/release gates |
| Documentation | `docs/`, module `README.md` files | Active contracts, runbooks, status, frozen external snapshots, and approved archives |

The Python packages remain at the repository root deliberately. Their import
names are referenced by production process commands, Docker build rules,
Compose services, migrations, tests, and active documentation. Moving them
behind a new `src/` prefix is a compatibility migration, not a cosmetic
cleanup, and requires a separately approved release plan.

## Dependency direction

```text
web (composition) -> owning API and application packages

arena_game -> arena_core -> arena_agent_contracts
hosted_agent_runtime -> arena_core + arena_agent_contracts
hosted_agent_control_plane -> hosted_agent_runtime + connector_gateway
connector_gateway / arena_mcp -> arena_core + arena_agent_contracts
arena_payments -> arena_game + arena_wallets
```

This is an ownership guide rather than a ban on every lateral import. Preserve
these hard boundaries:

- Runtime and Connector code produce candidate results; only Arena consumes
  and applies them.
- Settlement owns submission and confirmation recovery; only Arena commits
  inventory after confirmation.
- `web/` composes APIs and authentication adapters but does not become the
  owner of game, Runtime, Connector, wallet, or settlement state.
- The separate `sunruize93-cmyk/arena402` repository owns the product UI.

## Placement rules

- Put deterministic schemas shared across Runtime kinds in
  `arena_agent_contracts/`.
- Put Arena task/result lifecycle policy in `arena_core/`; put actual game
  rules and state transitions in `arena_game/`.
- Put HTTP-only request mapping in `web/`, while keeping reusable services in
  their owning package.
- Add PostgreSQL changes as a new forward migration under `db/migrations/`;
  never rewrite an applied migration.
- Put repeatable operator/demo programs in `scripts/`. Put Docker-, browser-,
  external-process-, or real-Runtime acceptance drivers in `tests/e2e/`.
- Keep pytest-discovered tests as `tests/test_*.py`; shared test-only helpers
  may stay directly under `tests/` or in a narrowly named helper package.
- Keep root files limited to cross-repository entrypoints and configuration.
  Tool caches, release validations, local backups, virtual environments,
  installed `node_modules/`, and secrets are local artifacts and never source
  of truth.

## Protected paths

- `agent-arena/specs/` is a frozen development record.
- `docs/injective/` is a fixed external snapshot.
- `docs/archive/` changes only through the documented human-approved archive
  lifecycle.
- `.agents/skills/` is the editable project-skill source;
  `.claude/skills/` is generated.
- Compatibility identifiers such as `adx-connector`, `ADX_*`, persisted
  values, wire URIs, database names, and package imports are not renamed as
  part of routine structure cleanup.
