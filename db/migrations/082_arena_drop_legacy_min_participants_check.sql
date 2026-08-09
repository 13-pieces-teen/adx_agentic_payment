BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Migration 006 declared the minimum-participant bound inline, which
-- PostgreSQL named games_min_participants_check. Later capacity migrations
-- removed the legacy max-participant check but left this independent
-- min_participants <= 64 clause behind. Keep the per-Game max_participants
-- value authoritative and retain only the domain minimum here.
ALTER TABLE arena402.games
    DROP CONSTRAINT IF EXISTS games_min_participants_check;

ALTER TABLE arena402.games
    ADD CONSTRAINT games_min_participants_check
    CHECK (min_participants >= 2);

RESET ROLE;

COMMIT;
