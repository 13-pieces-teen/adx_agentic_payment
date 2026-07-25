BEGIN;

-- Connector binding registration may replay after a Gateway restart or
-- advance to a new frozen binding epoch. The API already has SELECT/INSERT on
-- Arena runtime routes and may update route status, but PostgreSQL also checks
-- the ON CONFLICT update target. Grant only the epoch column required by that
-- owner-scoped registrar rather than widening unrestricted table updates.
GRANT UPDATE (connector_binding_epoch)
ON arena_runtime_bindings
TO adx_arena_api;

COMMIT;
