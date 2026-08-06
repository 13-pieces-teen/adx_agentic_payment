BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- agent_a2a.v1 persistence foundation. These tables are intentionally not
-- wired into Current Game yet: fcfs.v1 remains active until real Hosted/Local
-- Agent E2E evidence exists. Every economic transition carries the exact
-- AgentTaskResult that authored it.

CREATE TABLE arena402.market_result_applications (
    result_id TEXT PRIMARY KEY
        REFERENCES public.arena_agent_task_results(result_id)
        ON DELETE RESTRICT,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    game_participant_id TEXT NOT NULL,
    action_kind TEXT NOT NULL CHECK (
        action_kind IN (
            'intent',
            'rfq',
            'engage',
            'proposal',
            'acceptance',
            'reject'
        )
    ),
    action_id TEXT NOT NULL CHECK (action_id <> ''),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (action_kind, action_id),
    UNIQUE (result_id, action_kind),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT
);

CREATE TABLE arena402.market_projection_receipts (
    result_id TEXT PRIMARY KEY
        REFERENCES public.arena_agent_task_results(result_id)
        ON DELETE RESTRICT,
    task_id TEXT NOT NULL UNIQUE
        REFERENCES public.arena_agent_tasks(task_id)
        ON DELETE RESTRICT,
    task_kind TEXT NOT NULL CHECK (
        task_kind IN (
            'arena.market.intent',
            'arena.market.rfq',
            'arena.market.select'
        )
    ),
    application_outcome TEXT NOT NULL CHECK (
        application_outcome IN (
            'candidate',
            'default_pass',
            'market_timeout'
        )
    ),
    projected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE arena402.market_intents (
    intent_id TEXT PRIMARY KEY CHECK (intent_id <> ''),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    game_participant_id TEXT NOT NULL,
    source_result_id TEXT NOT NULL UNIQUE,
    source_action_kind TEXT NOT NULL DEFAULT 'intent'
        CHECK (source_action_kind = 'intent'),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    good_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity = 1),
    public_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        public_price_atomic > 0
    ),
    -- Private Agent boundary. Never grant the API role direct table access.
    limit_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        limit_price_atomic > 0
    ),
    public_message TEXT CHECK (
        public_message IS NULL
        OR (
            public_message <> ''
            AND char_length(public_message) <= 100
        )
    ),
    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN (
            'open',
            'reserved',
            'withdrawn',
            'expired',
            'consumed'
        )
    ),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (round_id, game_participant_id),
    UNIQUE (
        intent_id,
        game_id,
        round_id,
        game_participant_id,
        side,
        good_id
    ),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (source_result_id, source_action_kind)
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    CHECK (expires_at > created_at),
    CHECK (
        (side = 'buy' AND public_price_atomic <= limit_price_atomic)
        OR
        (side = 'sell' AND public_price_atomic >= limit_price_atomic)
    )
);

CREATE INDEX market_intents_directory_idx
    ON arena402.market_intents (
        round_id,
        good_id,
        side,
        intent_id
    )
    WHERE status = 'open';

CREATE TABLE arena402.market_negotiation_requests (
    request_id TEXT PRIMARY KEY CHECK (request_id <> ''),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    buyer_intent_id TEXT NOT NULL,
    seller_intent_id TEXT NOT NULL,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    buyer_side TEXT NOT NULL DEFAULT 'buy' CHECK (buyer_side = 'buy'),
    seller_side TEXT NOT NULL DEFAULT 'sell' CHECK (seller_side = 'sell'),
    good_id TEXT NOT NULL,
    source_result_id TEXT NOT NULL,
    source_action_kind TEXT NOT NULL DEFAULT 'rfq'
        CHECK (source_action_kind = 'rfq'),
    opening_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        opening_price_atomic > 0
    ),
    public_message TEXT NOT NULL CHECK (
        public_message <> ''
        AND char_length(public_message) <= 100
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'engaged',
            'rejected',
            'counterparty_busy',
            'expired',
            'cancelled'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (buyer_intent_id, seller_intent_id),
    UNIQUE (source_result_id, seller_intent_id),
    UNIQUE (request_id, game_id, round_id),
    UNIQUE (
        request_id,
        game_id,
        round_id,
        buyer_intent_id,
        seller_intent_id,
        buyer_participant_id,
        seller_participant_id,
        good_id
    ),
    FOREIGN KEY (
        buyer_intent_id,
        game_id,
        round_id,
        buyer_participant_id,
        buyer_side,
        good_id
    )
        REFERENCES arena402.market_intents (
            intent_id,
            game_id,
            round_id,
            game_participant_id,
            side,
            good_id
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (
        seller_intent_id,
        game_id,
        round_id,
        seller_participant_id,
        seller_side,
        good_id
    )
        REFERENCES arena402.market_intents (
            intent_id,
            game_id,
            round_id,
            game_participant_id,
            side,
            good_id
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (source_result_id, source_action_kind)
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    CHECK (buyer_participant_id <> seller_participant_id)
);

CREATE INDEX market_requests_seller_inbox_idx
    ON arena402.market_negotiation_requests (
        round_id,
        seller_participant_id,
        request_id
    )
    WHERE status = 'pending';

CREATE TABLE arena402.market_engagements (
    engagement_id TEXT PRIMARY KEY CHECK (engagement_id <> ''),
    negotiation_id TEXT NOT NULL UNIQUE CHECK (negotiation_id <> ''),
    request_id TEXT NOT NULL UNIQUE,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    buyer_intent_id TEXT NOT NULL,
    seller_intent_id TEXT NOT NULL,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    good_id TEXT NOT NULL,
    selection_result_id TEXT NOT NULL UNIQUE,
    selection_action_kind TEXT NOT NULL DEFAULT 'engage'
        CHECK (selection_action_kind = 'engage'),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN (
            'active',
            'accepted_pending_settlement',
            'rejected',
            'timed_out',
            'settling',
            'settled',
            'settlement_failed'
        )
    ),
    terminal_source_result_id TEXT UNIQUE,
    terminal_action_kind TEXT CHECK (
        terminal_action_kind IN ('acceptance', 'reject')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (
        engagement_id,
        request_id,
        game_id,
        round_id,
        buyer_participant_id,
        seller_participant_id,
        good_id
    ),
    FOREIGN KEY (
        request_id,
        game_id,
        round_id,
        buyer_intent_id,
        seller_intent_id,
        buyer_participant_id,
        seller_participant_id,
        good_id
    )
        REFERENCES arena402.market_negotiation_requests(
            request_id,
            game_id,
            round_id,
            buyer_intent_id,
            seller_intent_id,
            buyer_participant_id,
            seller_participant_id,
            good_id
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (buyer_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (seller_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (selection_result_id, selection_action_kind)
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (terminal_source_result_id, terminal_action_kind)
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    CHECK (buyer_participant_id <> seller_participant_id),
    CHECK (
        (terminal_source_result_id IS NULL AND terminal_action_kind IS NULL)
        OR
        (
            terminal_source_result_id IS NOT NULL
            AND terminal_action_kind IS NOT NULL
        )
    ),
    CHECK (
        (
            status IN ('active', 'accepted_pending_settlement', 'settling')
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
    )
);

CREATE INDEX market_engagements_round_status_idx
    ON arena402.market_engagements (round_id, status, engagement_id);

CREATE TABLE arena402.participant_round_slots (
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    game_participant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('available', 'reserved', 'consumed')
    ),
    engagement_id TEXT,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (round_id, game_participant_id),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (engagement_id)
        REFERENCES arena402.market_engagements(engagement_id)
        ON DELETE RESTRICT,
    CHECK (
        (status = 'available' AND engagement_id IS NULL)
        OR
        (status IN ('reserved', 'consumed') AND engagement_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX participant_round_reserved_engagement_uidx
    ON arena402.participant_round_slots (
        round_id,
        game_participant_id,
        engagement_id
    )
    WHERE status IN ('reserved', 'consumed');

CREATE TABLE arena402.market_deals (
    deal_id TEXT PRIMARY KEY CHECK (deal_id <> ''),
    engagement_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    good_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity = 1),
    unit_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        unit_price_atomic > 0
    ),
    latest_proposal_result_id TEXT NOT NULL UNIQUE,
    latest_proposal_action_kind TEXT NOT NULL DEFAULT 'proposal'
        CHECK (latest_proposal_action_kind = 'proposal'),
    acceptance_result_id TEXT NOT NULL UNIQUE,
    acceptance_action_kind TEXT NOT NULL DEFAULT 'acceptance'
        CHECK (acceptance_action_kind = 'acceptance'),
    accepted_by_participant_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        engagement_id,
        request_id,
        game_id,
        round_id,
        buyer_participant_id,
        seller_participant_id,
        good_id
    )
        REFERENCES arena402.market_engagements (
            engagement_id,
            request_id,
            game_id,
            round_id,
            buyer_participant_id,
            seller_participant_id,
            good_id
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (buyer_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (seller_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (accepted_by_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        latest_proposal_result_id,
        latest_proposal_action_kind
    )
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    FOREIGN KEY (acceptance_result_id, acceptance_action_kind)
        REFERENCES arena402.market_result_applications(
            result_id,
            action_kind
        )
        ON DELETE RESTRICT,
    CHECK (buyer_participant_id <> seller_participant_id),
    CHECK (
        accepted_by_participant_id IN (
            buyer_participant_id,
            seller_participant_id
        )
    ),
    CHECK (latest_proposal_result_id <> acceptance_result_id)
);

-- The public directory deliberately omits limit_price_atomic and source
-- Runtime evidence. Opponents only receive public listing state.
CREATE VIEW arena402.market_directory_public AS
SELECT
    intent.intent_id,
    intent.game_id,
    intent.round_id,
    participant.agent_id,
    intent.side,
    intent.good_id,
    intent.quantity,
    intent.public_price_atomic,
    intent.public_message,
    intent.expires_at,
    intent.created_at
FROM arena402.market_intents AS intent
JOIN arena402.game_participants AS participant
  ON participant.game_participant_id = intent.game_participant_id
 AND participant.game_id = intent.game_id
WHERE intent.status = 'open';

REVOKE ALL ON TABLE
    arena402.market_result_applications,
    arena402.market_projection_receipts,
    arena402.market_intents,
    arena402.market_negotiation_requests,
    arena402.market_engagements,
    arena402.participant_round_slots,
    arena402.market_deals
FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON TABLE
    arena402.market_result_applications,
    arena402.market_projection_receipts,
    arena402.market_intents,
    arena402.market_negotiation_requests,
    arena402.market_engagements,
    arena402.participant_round_slots
TO adx_arena_core;

-- Deal terms are immutable. Status belongs to Engagement/Settlement records.
GRANT SELECT, INSERT ON TABLE arena402.market_deals TO adx_arena_core;

GRANT SELECT ON TABLE arena402.market_directory_public TO
    adx_arena_core,
    adx_arena_api;

COMMIT;
