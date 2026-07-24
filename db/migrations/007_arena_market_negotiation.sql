BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.rule_runtime_configs (
    runtime_binding_id TEXT PRIMARY KEY CHECK (runtime_binding_id <> ''),
    game_participant_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.game_participants(game_participant_id)
        ON DELETE CASCADE,
    intent TEXT NOT NULL CHECK (intent IN ('buy', 'sell', 'pass')),
    good_id TEXT NOT NULL CHECK (
        good_id IN ('grain', 'iron', 'warhorse', 'gems')
    ),
    target_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        target_price_atomic > 0
    ),
    public_message TEXT NOT NULL CHECK (
        public_message <> ''
        AND char_length(public_message) <= 100
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE arena402.pool_entries (
    pool_entry_id TEXT PRIMARY KEY CHECK (pool_entry_id <> ''),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    game_participant_id TEXT NOT NULL,
    source_result_id TEXT NOT NULL CHECK (source_result_id <> ''),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    good_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unmatched' CHECK (
        status IN ('unmatched', 'paired', 'cancelled')
    ),
    result_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (round_id, game_participant_id),
    UNIQUE (source_result_id),
    UNIQUE (pool_entry_id, game_id, round_id),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT
);

CREATE INDEX pool_entries_fcfs_idx
    ON arena402.pool_entries (
        round_id,
        good_id,
        side,
        result_received_at,
        pool_entry_id
    )
    WHERE status = 'unmatched';

CREATE TABLE arena402.pairings (
    pairing_id TEXT PRIMARY KEY CHECK (pairing_id <> ''),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    good_id TEXT NOT NULL,
    buyer_entry_id TEXT NOT NULL UNIQUE,
    seller_entry_id TEXT NOT NULL UNIQUE,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    pairing_sequence INTEGER NOT NULL CHECK (pairing_sequence >= 1),
    status TEXT NOT NULL DEFAULT 'negotiating' CHECK (
        status IN (
            'negotiating',
            'accepted_pending_settlement',
            'rejected',
            'timeout',
            'settling',
            'settled',
            'settlement_failed'
        )
    ),
    paired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (round_id, good_id, pairing_sequence),
    UNIQUE (pairing_id, game_id, round_id),
    FOREIGN KEY (buyer_entry_id, game_id, round_id)
        REFERENCES arena402.pool_entries(pool_entry_id, game_id, round_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (seller_entry_id, game_id, round_id)
        REFERENCES arena402.pool_entries(pool_entry_id, game_id, round_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (buyer_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (seller_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT,
    CHECK (buyer_participant_id <> seller_participant_id),
    CHECK (
        (status IN ('negotiating', 'accepted_pending_settlement', 'settling')
         AND completed_at IS NULL)
        OR
        (status IN ('rejected', 'timeout', 'settled', 'settlement_failed')
         AND completed_at IS NOT NULL)
    )
);

CREATE INDEX pairings_round_status_idx
    ON arena402.pairings (round_id, status, pairing_sequence);

CREATE TABLE arena402.negotiations (
    negotiation_id TEXT PRIMARY KEY CHECK (negotiation_id <> ''),
    pairing_id TEXT NOT NULL UNIQUE,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    max_turns INTEGER NOT NULL CHECK (max_turns BETWEEN 2 AND 6),
    turn_count INTEGER NOT NULL DEFAULT 0 CHECK (turn_count >= 0),
    next_role TEXT NOT NULL DEFAULT 'buyer' CHECK (
        next_role IN ('buyer', 'seller', 'none')
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN (
            'active',
            'accepted_pending_settlement',
            'rejected',
            'timeout'
        )
    ),
    latest_proposal_price_atomic NUMERIC(78, 0) CHECK (
        latest_proposal_price_atomic > 0
    ),
    latest_proposal_role TEXT CHECK (
        latest_proposal_role IN ('buyer', 'seller')
    ),
    accepted_price_atomic NUMERIC(78, 0) CHECK (
        accepted_price_atomic > 0
    ),
    action_deadline_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (negotiation_id, game_id, round_id),
    FOREIGN KEY (pairing_id, game_id, round_id)
        REFERENCES arena402.pairings(pairing_id, game_id, round_id)
        ON DELETE CASCADE,
    FOREIGN KEY (buyer_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (seller_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE RESTRICT,
    CHECK (buyer_participant_id <> seller_participant_id),
    CHECK (turn_count <= max_turns),
    CHECK (
        (status = 'active' AND completed_at IS NULL AND next_role <> 'none')
        OR
        (status <> 'active' AND completed_at IS NOT NULL AND next_role = 'none')
    ),
    CHECK (
        (latest_proposal_price_atomic IS NULL) =
        (latest_proposal_role IS NULL)
    ),
    CHECK (
        status <> 'accepted_pending_settlement'
        OR accepted_price_atomic IS NOT NULL
    )
);

CREATE INDEX negotiations_action_deadline_idx
    ON arena402.negotiations (status, action_deadline_at, negotiation_id)
    WHERE status = 'active';

CREATE TABLE arena402.negotiation_messages (
    negotiation_message_id TEXT PRIMARY KEY CHECK (
        negotiation_message_id <> ''
    ),
    negotiation_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    source_result_id TEXT NOT NULL UNIQUE CHECK (source_result_id <> ''),
    turn_sequence INTEGER NOT NULL CHECK (turn_sequence >= 1),
    actor_role TEXT NOT NULL CHECK (actor_role IN ('buyer', 'seller')),
    action TEXT NOT NULL CHECK (action IN ('propose', 'accept', 'reject')),
    price_atomic NUMERIC(78, 0) CHECK (price_atomic > 0),
    public_message TEXT CHECK (
        public_message IS NULL
        OR (
            public_message <> ''
            AND char_length(public_message) <= 100
        )
    ),
    result_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (negotiation_id, turn_sequence),
    FOREIGN KEY (negotiation_id, game_id, round_id)
        REFERENCES arena402.negotiations(negotiation_id, game_id, round_id)
        ON DELETE CASCADE,
    CHECK (
        (action = 'propose' AND price_atomic IS NOT NULL)
        OR (action <> 'propose' AND price_atomic IS NULL)
    ),
    CHECK (action <> 'accept' OR public_message IS NULL)
);

CREATE INDEX negotiation_messages_timeline_idx
    ON arena402.negotiation_messages (
        game_id,
        round_id,
        result_received_at,
        negotiation_message_id
    );

CREATE TABLE arena402.royal_orders (
    royal_order_id TEXT PRIMARY KEY CHECK (royal_order_id <> ''),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side = 'buy'),
    good_id TEXT NOT NULL,
    price_atomic NUMERIC(78, 0) NOT NULL CHECK (price_atomic > 0),
    quantity_limit BIGINT NOT NULL CHECK (quantity_limit > 0),
    quantity_filled BIGINT NOT NULL DEFAULT 0 CHECK (quantity_filled >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'filled', 'expired', 'cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (game_id, round_id, source_event_id, good_id),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_id, source_event_id)
        REFERENCES arena402.event_occurrences(game_id, event_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT,
    CHECK (quantity_filled <= quantity_limit)
);

REVOKE ALL ON ALL TABLES IN SCHEMA arena402 FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA arena402 TO adx_arena_core;
GRANT SELECT ON ALL TABLES IN SCHEMA arena402 TO adx_arena_api;

RESET ROLE;

COMMIT;
