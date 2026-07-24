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

It does not own:

- Provider invocation or model credentials;
- Connector device/runtime state;
- matching, negotiation, or settlement;
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
