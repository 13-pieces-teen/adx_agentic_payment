BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Migration 006 declared max_participants with an inline CHECK, which
-- PostgreSQL named games_check. Migration 025 removed only the later explicit
-- constraint name, so remove both possible names before installing one
-- authoritative per-Game bound.
ALTER TABLE arena402.games
    DROP CONSTRAINT IF EXISTS games_check;

ALTER TABLE arena402.games
    DROP CONSTRAINT IF EXISTS games_max_participants_check;

ALTER TABLE arena402.games
    ADD CONSTRAINT games_max_participants_check
    CHECK (max_participants >= min_participants);

RESET ROLE;

COMMIT;
