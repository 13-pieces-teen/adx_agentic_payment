# King's Pawnhouse Game Domain

> Current status — 2026-08-09: this domain backs the production
> `agent_a2a.v1` Current Game. One formal mixed-Runtime Game and separate
> payment-disabled/payment-enabled 100-Hosted-Agent × eight-round runs have
> completed. Capacity follow-up and recovery work are tracked in the project
> [Roadmap](../docs/roadmap.md).

This package is the clean-slate Arena 402 game domain based on the approved
`WORLD_AND_EVENTS(1).md` world design.

It owns:

- the four canonical goods: `grain`, `iron`, `warhorse`, and `gems`;
- deterministic fixed-point gold arithmetic;
- the equal-20-gold initial portfolio invariant;
- the versioned world-event effect DSL;
- a five-round deterministic demo schedule and a versioned deterministic
  seed-shuffled event deck for 1–10 rounds;
- game and round state transitions;
- terminal valuation and pawn-promotion ranking.
- PostgreSQL-backed pool entry, FCFS pairing, and bounded negotiation state.
- the `agent_a2a.v1` Phase A protocol foundation: strict Agent-authored
  Intent/RFQ/Engage contracts, an invariant-only state-machine oracle, and
  migration-ready persistence constraints;
- immutable single-payment SettlementIntent snapshots;
- read-only EVM confirmation validation;
- confirmation-gated, idempotent cash and inventory commit.
- quantity-aware FCFS orders with optional fixed-point limit prices;
- deterministic `balanced_auto` portfolio allocation for equal-value starts;
- durable N-round orchestration and recovery from PostgreSQL state;
- per-round cash/holding snapshots, frozen terminal prices, and rankings.

It does not own:

- Provider invocation or model credentials;
- Connector device/runtime state;
- wallet signing or settlement submission;
- payment finality;
- frontend presentation.

This package is the sole maintained game-domain implementation; the former
in-memory matching/ELO prototype has been removed. PostgreSQL persistence begins in
`db/migrations/006_arena_world_game_core.sql` under the isolated `arena402`
schema.

Production Current Game has switched to the frozen `agent_a2a.v1` path;
`fcfs.v1` remains immutable for historical Games and available as an explicit
next-Game rollback protocol. The `AgentDrivenMarket` state machine remains a
protocol oracle rather than an Agent strategy. The
`arena.market.intent/rfq/select` task kinds now pass through the shared Hosted
Driver, Local Connector task envelope, Result Sink, and Deadline Finalizer.
A recoverable Worker projects applied Results into
MarketIntent/RFQ/Engagement state, and an opt-in `agent_a2a.v1` orchestrator
drives intent → RFQ → select → bounded negotiation. Agent-selected
Engagements alone can materialize the compatibility Pairing/Negotiation path,
and accepted negotiations freeze Deal provenance. Real Connector-only,
mixed-Runtime recovery, the formal payment-enabled Phase D Game, and two
100-Hosted-Agent capacity runs have completed this path. Follow-up capacity and
recovery work is tracked in the Roadmap.

## Core invariants

- Every initial portfolio is worth exactly 20 gold at the canonical initial
  prices.
- Gold uses six-decimal atomic integers; binary floating point is rejected.
- One scheduled event is revealed per round.
- Event calculations use integer basis points and deterministic rounding.
- Public market prices and terminal valuation prices are separate.
- The event schedule is committed before play and can be verified after the
  seed is revealed.
- Rankings use only terminal net worth. Ties are ordered by stable Agent ID.
- Current AgentTask buy/sell actions are fixed to one unit; the lower
  persistence model remains quantity-aware for historical records and internal
  settlement arithmetic.
- A `balanced_auto` game assigns each ready participant one deterministic good
  unit plus cash at portfolio lock; `manual` remains the default mode.
- A completed A2A round has no `open`/`reserved` Intent, `pending` RFQ,
  `active` RFQ session, or `reserved` participant slot. Round close owns the
  normal transition and game completion repeats it idempotently across the
  game.
- A product Current Game freezes one exact 10–100-Agent target before the first
  participant history. The first player Agent to become Ready triggers
  immediate allowlisted official fill, and Arena starts only when that exact
  target is Ready; there is no scheduled-start or business waiting timer.

## Persistent rule-Agent demonstration

With the local Compose stack running, execute:

```powershell
python scripts/run_rule_pawnhouse_demo.py
```

The script creates a game, joins one deterministic buyer and one deterministic
seller, starts round one, records their decisions, pairs them by database
`result_received_at`, performs a bounded public negotiation, and prints the
persisted public timeline. A Game without settlement configuration remains at
`accepted_pending_settlement`. A Game using `single_eip3009` freezes an
immutable intent at `authorization_requested`; neither path moves cash or
inventory until Arena has persisted and checked the exact chain confirmation.

## Complete backend-only demonstrations

Run eight deterministic Rule Agents across all four goods and five rounds:

```powershell
python scripts/run_full_pawnhouse_game_demo.py
```

With two fresh one-use invitations, run two scripted Hosted actors through one
opt-in `agent_a2a.v1` round:

```powershell
python scripts/run_full_hosted_pawnhouse_demo.py
```

Run 12 Hosted Agents through ten rounds after generating one JSON batch of 12
fresh invitations:

```powershell
$env:ARENA_HOSTED_INVITES = docker compose -f docker-compose.local.yml exec -T api python -m connector_gateway.invite_cli --persist --ttl-hours 1 --count 12 --json
python scripts/run_many_hosted_pawnhouse_demo.py --agents 12 --rounds 10
```

The `agent_a2a.v1` full Hosted script uses development-only scripted Agents and
`authorizationMode=none`: it may freeze an accepted Deal but creates no
SettlementIntent and moves no assets. It is Fake E2E evidence. The accepted
payment-enabled demonstration remains blocked in `settle` until its exact
chain transfer is confirmed.

## Production worker boundary

`python -m arena_game.production_worker` runs without a public port and
combines four independent loops:

- the durable full-game orchestrator;
- the Pawnhouse Hosted-task coordinator;
- the Arena-owned Deadline Finalizer, which keeps running when Hosted model
  workers are unavailable;
- read-only settlement recovery followed by idempotent inventory commit.

Before any loop starts, the worker requires the database's applied migration
names and SHA-256 values to exactly match the migration manifest packaged in
its image. The game orchestrator discovers only actionable transitions with
one set-based PostgreSQL query per idle poll instead of probing every running
Game independently.

The process has the `adx_arena_core` database role and HTTPS read access to the
configured Injective RPC/Blockscout endpoints. It has no wallet, private key,
Secret Manager permission, Facilitator submission credential, or transaction
broadcast interface.

Automatic mandate-authorized payment submission runs separately as
`python -m arena_payments.production_worker` with the least-privilege
`adx_settlement` database role. That process has no public port and is the only
backend process configured with the signer and Facilitator capabilities.
