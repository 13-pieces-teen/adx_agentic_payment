BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- CurrentGameLifecycleWorker refreshes this timestamp whenever it atomically
-- rotates the singleton pointer to a newly created product game.
ALTER TABLE arena402.current_game
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL
    DEFAULT clock_timestamp();

RESET ROLE;

COMMIT;
