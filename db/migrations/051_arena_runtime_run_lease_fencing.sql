-- Fence stale Runtime Run coordinators after a lease is reclaimed.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.runtime_runs
    ADD COLUMN lease_epoch BIGINT NOT NULL DEFAULT 0
        CHECK (lease_epoch >= 0);

RESET ROLE;

COMMIT;
