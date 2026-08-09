BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Preserve the individual pre-broadcast stages so capacity reports can
-- distinguish Facilitator scheduling delay from RPC and confirmation delay.
-- Historical attempts remain NULL rather than receiving fabricated timing.
ALTER TABLE arena402.x402_settlement_attempts
    ADD COLUMN signed_at TIMESTAMPTZ,
    ADD COLUMN submitting_at TIMESTAMPTZ,
    ADD COLUMN submitted_at TIMESTAMPTZ,
    ADD COLUMN facilitator_deferred_at TIMESTAMPTZ,
    ADD COLUMN facilitator_defer_count INTEGER NOT NULL DEFAULT 0
        CHECK (facilitator_defer_count >= 0);

CREATE INDEX x402_attempts_facilitator_stage_metrics_idx
    ON arena402.x402_settlement_attempts (
        facilitator_id,
        created_at,
        settlement_intent_id
    )
    WHERE status IN ('reserved', 'signed', 'submitting', 'unknown');

RESET ROLE;

COMMIT;
