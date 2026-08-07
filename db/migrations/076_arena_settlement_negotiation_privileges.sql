BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- The automatic Settlement Worker closes an accepted negotiation when its
-- PaymentMandate is no longer active before any reservation or submission.
-- Keep the grant limited to the table touched by that terminalization path.
GRANT SELECT, UPDATE ON
    arena402.negotiations
TO adx_settlement;

RESET ROLE;

COMMIT;
