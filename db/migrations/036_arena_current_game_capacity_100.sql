BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Migration 033 temporarily narrowed the product matchmaking contract to
-- twenty seats. The active Current Game contract now keeps a ten-seat start
-- threshold while allowing an operator-controlled hard cap of one hundred.
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
