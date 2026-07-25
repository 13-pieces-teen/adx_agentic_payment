BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Large operator-created benchmark Games are bounded by their frozen
-- max_participants value, not by a repository-wide constant. The
-- product-facing arena402.current_game projection intentionally retains its
-- independent 12-participant limit.
ALTER TABLE arena402.games
    DROP CONSTRAINT IF EXISTS games_max_participants_check;

ALTER TABLE arena402.games
    ADD CONSTRAINT games_max_participants_check
    CHECK (max_participants >= min_participants);

RESET ROLE;

COMMIT;
