BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Some databases applied the original Current Game max-participant check as
-- the table-level name `current_game_check`. Migrations 032 and 036 replaced
-- the canonical named checks, but could not remove this legacy alias. Keep
-- the active 100-seat constraint authoritative by removing only that alias.
ALTER TABLE arena402.current_game
    DROP CONSTRAINT IF EXISTS current_game_check;

RESET ROLE;

COMMIT;
