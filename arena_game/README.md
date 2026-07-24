# King's Pawnhouse Game Domain

This package is the clean-slate Arena 402 game domain based on the approved
`WORLD_AND_EVENTS(1).md` world design.

It owns:

- the four canonical goods: `grain`, `iron`, `warhorse`, and `gems`;
- deterministic fixed-point gold arithmetic;
- the equal-20-gold initial portfolio invariant;
- the versioned world-event effect DSL;
- the five-round deterministic demo schedule;
- game and round state transitions;
- terminal valuation and pawn-promotion ranking.
- PostgreSQL-backed pool entry, FCFS pairing, and bounded negotiation state.
- immutable single-payment SettlementIntent snapshots;
- read-only EVM confirmation validation;
- confirmation-gated, idempotent cash and inventory commit.
- durable five-round orchestration and recovery from PostgreSQL state;
- per-round cash/holding snapshots, frozen terminal prices, and rankings.

It does not own:

- Provider invocation or model credentials;
- Connector device/runtime state;
- wallet signing or settlement submission;
- payment finality;
- frontend presentation.

The package is intentionally independent from the legacy in-memory `matching/`
prototype. PostgreSQL persistence begins in
`db/migrations/006_arena_world_game_core.sql` under the isolated `arena402`
schema.

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

With two fresh one-use invitations, run two Hosted Agents through five durable
Runtime runs:

```powershell
python scripts/run_full_hosted_pawnhouse_demo.py
```

Both full-game demonstrations use rejecting negotiations so they can verify
Round close and terminal ranking without inventing a payment. The accepted
trade demonstration remains blocked in `settle` until its exact chain transfer
is confirmed.

## Production worker boundary

`python -m arena_game.production_worker` runs without a public port and
combines four independent loops:

- the durable full-game orchestrator;
- the Pawnhouse Hosted-task coordinator;
- the Arena-owned Deadline Finalizer, which keeps running when Hosted model
  workers are unavailable;
- read-only settlement recovery followed by idempotent inventory commit.

The process has the `adx_arena_core` database role and HTTPS read access to the
configured Injective RPC/Blockscout endpoints. It has no wallet, private key,
Secret Manager permission, Facilitator submission credential, or transaction
broadcast interface.
