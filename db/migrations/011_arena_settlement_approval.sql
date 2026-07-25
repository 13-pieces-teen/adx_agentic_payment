BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.settlement_approvals (
    settlement_intent_id TEXT PRIMARY KEY
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    approved_intent_hash TEXT NOT NULL CHECK (
        approved_intent_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    authorization_nonce_digest TEXT NOT NULL UNIQUE CHECK (
        authorization_nonce_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    approval_source TEXT NOT NULL CHECK (
        approval_source IN ('operator_cli', 'legacy_migration')
    ),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (settlement_intent_id, approved_intent_hash)
);

-- Preserve already submitted development evidence without claiming that a
-- historical operator approval was observed. New submissions are accepted by
-- the application only after an explicit operator_cli approval.
INSERT INTO arena402.settlement_approvals (
    settlement_intent_id,
    approved_intent_hash,
    authorization_nonce_digest,
    approval_source,
    approved_at
)
SELECT
    submission.settlement_intent_id,
    intent.intent_hash,
    submission.authorization_nonce_digest,
    'legacy_migration',
    submission.submitted_at
FROM arena402.settlement_submissions AS submission
JOIN arena402.settlement_intents AS intent
  ON intent.settlement_intent_id = submission.settlement_intent_id
ON CONFLICT (settlement_intent_id) DO NOTHING;

REVOKE ALL ON arena402.settlement_approvals FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON arena402.settlement_approvals TO adx_arena_core;
GRANT SELECT
    ON arena402.settlement_approvals TO adx_arena_api;

RESET ROLE;

COMMIT;
