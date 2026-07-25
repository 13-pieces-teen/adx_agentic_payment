BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Current Game admission now permits up to 100 participants. Historical
-- migration 024 remains immutable; this migration replaces only its live
-- product checks.
ALTER TABLE arena402.current_game
    DROP CONSTRAINT IF EXISTS current_game_start_threshold_check;

ALTER TABLE arena402.current_game
    DROP CONSTRAINT IF EXISTS current_game_max_participants_check;

ALTER TABLE arena402.current_game
    ADD CONSTRAINT current_game_start_threshold_check
    CHECK (start_threshold BETWEEN 2 AND 100);

ALTER TABLE arena402.current_game
    ADD CONSTRAINT current_game_max_participants_check
    CHECK (max_participants BETWEEN start_threshold AND 100);

RESET ROLE;

COMMIT;
