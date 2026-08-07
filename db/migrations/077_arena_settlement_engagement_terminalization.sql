BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Settlement owns the terminal transition after an accepted A2A engagement
-- enters `settling`; grant only the table access required for that projection.
GRANT SELECT, UPDATE ON
    arena402.market_engagements
TO adx_settlement;

-- Repair engagements completed before the Settlement path synchronized the
-- A2A projection. SettlementIntent remains the authority for the outcome.
UPDATE arena402.market_engagements AS engagement
SET status = CASE
        WHEN intent.status = 'inventory_committed' THEN 'settled'
        ELSE 'settlement_failed'
    END,
    completed_at = COALESCE(
        engagement.completed_at,
        intent.completed_at,
        clock_timestamp()
    )
FROM arena402.settlement_intents AS intent
WHERE intent.negotiation_id = engagement.negotiation_id
  AND intent.status IN (
      'inventory_committed',
      'authorization_failed',
      'reverted'
  )
  AND engagement.status IN (
      'accepted_pending_settlement',
      'settling'
  );

RESET ROLE;

COMMIT;
