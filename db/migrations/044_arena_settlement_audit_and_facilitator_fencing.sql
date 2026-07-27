BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Preserve a durable audit link between every signed/broadcast x402 attempt
-- and the exact canonical payload handed to the Facilitator. Existing rows
-- predate this invariant, so enforce it for new or updated attempts without
-- claiming that historical null digests have been reconstructed.
ALTER TABLE arena402.x402_settlement_attempts
    ADD CONSTRAINT x402_attempt_payment_payload_digest_required CHECK (
        status IN ('reserved', 'failed')
        OR payment_payload_digest IS NOT NULL
    ) NOT VALID;

-- One durable broadcaster lease per Facilitator account. An expired lease is
-- replaceable only when no unresolved submitting/unknown attempt remains for
-- that Facilitator; the worker applies that recovery condition atomically in
-- its UPSERT.
CREATE TABLE arena402.facilitator_broadcast_fences (
    facilitator_id TEXT PRIMARY KEY CHECK (
        facilitator_id <> ''
        AND char_length(facilitator_id) <= 128
    ),
    settlement_intent_id TEXT NOT NULL
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    lease_owner TEXT NOT NULL CHECK (
        lease_owner <> ''
        AND char_length(lease_owner) <= 128
    ),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (settlement_intent_id)
);

CREATE INDEX facilitator_broadcast_fences_expiry_idx
    ON arena402.facilitator_broadcast_fences (
        lease_expires_at,
        facilitator_id
    );

REVOKE ALL ON arena402.facilitator_broadcast_fences FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    arena402.facilitator_broadcast_fences
TO adx_arena_core;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    arena402.facilitator_broadcast_fences
TO adx_settlement;

RESET ROLE;

COMMIT;
