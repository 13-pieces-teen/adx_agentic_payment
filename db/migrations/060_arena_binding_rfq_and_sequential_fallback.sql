-- Freeze the buyer directory once, serialize outbound RFQs, and let an
-- accepted RFQ opening carry proposal provenance into the immutable Deal.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.market_intents
    ADD CONSTRAINT market_intents_rfq_session_owner_uidx
        UNIQUE (
            intent_id,
            game_id,
            round_id,
            game_participant_id
        );

CREATE TABLE arena402.market_rfq_sessions (
    buyer_intent_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    buyer_participant_id TEXT NOT NULL,
    frozen_directory JSONB NOT NULL CHECK (
        jsonb_typeof(frozen_directory) = 'array'
    ),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts = 3),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 3
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'stopped', 'completed', 'expired')
    ),
    deadline_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (buyer_intent_id, game_id, round_id),
    FOREIGN KEY (
        buyer_intent_id,
        game_id,
        round_id,
        buyer_participant_id
    )
        REFERENCES arena402.market_intents (
            intent_id,
            game_id,
            round_id,
            game_participant_id
        )
        ON DELETE CASCADE
);

ALTER TABLE arena402.market_negotiation_requests
    ADD COLUMN attempt_sequence INTEGER;

WITH sequenced AS (
    SELECT
        request_id,
        row_number() OVER (
            PARTITION BY buyer_intent_id
            ORDER BY created_at, request_id
        ) AS attempt_sequence
    FROM arena402.market_negotiation_requests
)
UPDATE arena402.market_negotiation_requests AS request
SET attempt_sequence = sequenced.attempt_sequence
FROM sequenced
WHERE sequenced.request_id = request.request_id;

ALTER TABLE arena402.market_negotiation_requests
    ALTER COLUMN attempt_sequence SET NOT NULL,
    ADD CONSTRAINT market_requests_attempt_sequence_check
        CHECK (attempt_sequence BETWEEN 1 AND 3),
    ADD CONSTRAINT market_requests_buyer_attempt_uidx
        UNIQUE (buyer_intent_id, attempt_sequence),
    ADD CONSTRAINT market_requests_request_result_uidx
        UNIQUE (request_id, source_result_id);

CREATE UNIQUE INDEX market_requests_one_active_buyer_rfq_uidx
    ON arena402.market_negotiation_requests (buyer_intent_id)
    WHERE status IN ('pending', 'engaged');

ALTER TABLE arena402.market_engagements
    DROP CONSTRAINT market_engagements_check2,
    ADD CONSTRAINT market_engagements_terminal_status_check
        CHECK (
            (
                status IN (
                    'active',
                    'accepted_pending_settlement',
                    'settling'
                )
                AND completed_at IS NULL
            )
            OR
            (
                status IN (
                    'rejected',
                    'timed_out',
                    'settled',
                    'settlement_failed'
                )
                AND completed_at IS NOT NULL
            )
        );

ALTER TABLE arena402.market_deals
    ADD COLUMN latest_proposal_request_id TEXT;

ALTER TABLE arena402.market_deals
    DROP CONSTRAINT market_deals_latest_proposal_action_kind_check,
    ADD CONSTRAINT market_deals_latest_proposal_action_kind_check
        CHECK (latest_proposal_action_kind IN ('rfq', 'proposal')),
    ADD CONSTRAINT market_deals_latest_proposal_request_shape_check
        CHECK (
            (
                latest_proposal_action_kind = 'rfq'
                AND latest_proposal_request_id = request_id
            )
            OR
            (
                latest_proposal_action_kind = 'proposal'
                AND latest_proposal_request_id IS NULL
            )
        ),
    ADD CONSTRAINT market_deals_latest_proposal_request_result_fk
        FOREIGN KEY (
            latest_proposal_request_id,
            latest_proposal_result_id
        )
        REFERENCES arena402.market_negotiation_requests (
            request_id,
            source_result_id
        )
        ON DELETE RESTRICT;

REVOKE ALL ON TABLE arena402.market_rfq_sessions FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE
    ON TABLE arena402.market_rfq_sessions
    TO adx_arena_core;

RESET ROLE;

COMMIT;
