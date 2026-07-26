BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- The authenticated wallet endpoint claims one available inventory row with
-- SELECT ... FOR UPDATE SKIP LOCKED, then marks only its status as bound.
-- PostgreSQL requires UPDATE privilege for that row lock. Keep the grant
-- column-scoped so the API cannot rewrite wallet identity or secret metadata.
GRANT UPDATE (status)
    ON arena402.wallet_inventory
    TO adx_arena_api;

RESET ROLE;

COMMIT;
