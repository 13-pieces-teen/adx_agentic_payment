BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- A negotiation accepted a price but was previously left forever at
-- accepted_pending_settlement even after its payment reached a terminal state.
ALTER TABLE arena402.negotiations
    DROP CONSTRAINT IF EXISTS negotiations_status_check;

ALTER TABLE arena402.negotiations
    ADD CONSTRAINT negotiations_status_check CHECK (
        status IN (
            'active',
            'accepted_pending_settlement',
            'settled',
            'settlement_failed',
            'rejected',
            'timeout'
        )
    );

UPDATE arena402.negotiations AS negotiation
SET status = CASE
        WHEN intent.status = 'inventory_committed' THEN 'settled'
        ELSE 'settlement_failed'
    END,
    next_role = 'none',
    completed_at = COALESCE(
        negotiation.completed_at,
        intent.completed_at,
        clock_timestamp()
    )
FROM arena402.settlement_intents AS intent
WHERE intent.negotiation_id = negotiation.negotiation_id
  AND negotiation.status = 'accepted_pending_settlement'
  AND intent.status IN (
      'inventory_committed',
      'authorization_failed',
      'reverted'
  );

RESET ROLE;

COMMIT;
