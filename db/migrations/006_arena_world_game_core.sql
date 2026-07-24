BEGIN;

-- Clean-slate King's Pawnhouse game state. Existing public.* Runtime tables
-- remain available while the new vertical slice takes over incrementally.

CREATE SCHEMA IF NOT EXISTS arena402 AUTHORIZATION adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.games (
    game_id TEXT PRIMARY KEY CHECK (game_id <> ''),
    phase TEXT NOT NULL DEFAULT 'registration' CHECK (
        phase IN (
            'registration',
            'portfolio_setup',
            'portfolio_locked',
            'running',
            'final_valuation',
            'completed',
            'cancelled'
        )
    ),
    round_count INTEGER NOT NULL CHECK (round_count BETWEEN 1 AND 20),
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round >= 0),
    action_timeout_ms INTEGER NOT NULL CHECK (
        action_timeout_ms BETWEEN 100 AND 900000
    ),
    max_negotiation_turns INTEGER NOT NULL DEFAULT 3 CHECK (
        max_negotiation_turns BETWEEN 2 AND 6
    ),
    min_participants INTEGER NOT NULL DEFAULT 2 CHECK (
        min_participants BETWEEN 2 AND 64
    ),
    max_participants INTEGER NOT NULL DEFAULT 16 CHECK (
        max_participants BETWEEN min_participants AND 64
    ),
    config_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(config_snapshot) = 'object'
    ),
    event_seed TEXT NOT NULL CHECK (
        event_seed <> '' AND char_length(event_seed) <= 512
    ),
    event_schedule_commitment TEXT NOT NULL CHECK (
        event_schedule_commitment ~ '^sha256:[0-9a-f]{64}$'
    ),
    event_seed_revealed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CHECK (current_round <= round_count),
    CHECK (
        (phase IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (phase NOT IN ('completed', 'cancelled') AND completed_at IS NULL)
    )
);

CREATE INDEX games_phase_created_idx
    ON arena402.games (phase, created_at, game_id);

CREATE TABLE arena402.game_goods (
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    good_id TEXT NOT NULL CHECK (
        good_id IN ('grain', 'iron', 'warhorse', 'gems')
    ),
    display_name TEXT NOT NULL CHECK (
        display_name <> '' AND char_length(display_name) <= 100
    ),
    initial_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        initial_price_atomic > 0
    ),
    price_decimal_places SMALLINT NOT NULL DEFAULT 6 CHECK (
        price_decimal_places BETWEEN 0 AND 18
    ),
    fixed_trade_quantity INTEGER NOT NULL DEFAULT 1 CHECK (
        fixed_trade_quantity = 1
    ),
    PRIMARY KEY (game_id, good_id)
);

CREATE TABLE arena402.game_participants (
    game_participant_id TEXT PRIMARY KEY CHECK (game_participant_id <> ''),
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    agent_id TEXT NOT NULL CHECK (agent_id <> ''),
    runtime_binding_id TEXT NOT NULL CHECK (runtime_binding_id <> ''),
    runtime_kind TEXT NOT NULL CHECK (
        runtime_kind IN ('hosted', 'rule', 'connector', 'native_a2a')
    ),
    status TEXT NOT NULL DEFAULT 'joined' CHECK (
        status IN ('joined', 'active', 'settling', 'completed', 'cancelled')
    ),
    portfolio_locked_at TIMESTAMPTZ,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (game_id, user_id),
    UNIQUE (game_id, agent_id),
    UNIQUE (game_participant_id, game_id),
    CHECK (
        (status IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (status IN ('joined', 'active', 'settling') AND completed_at IS NULL)
    )
);

CREATE INDEX game_participants_status_idx
    ON arena402.game_participants (game_id, status, joined_at);

CREATE TABLE arena402.balances (
    game_participant_id TEXT PRIMARY KEY
        REFERENCES arena402.game_participants(game_participant_id)
        ON DELETE CASCADE,
    cash_atomic NUMERIC(78, 0) NOT NULL CHECK (cash_atomic >= 0),
    initial_cash_atomic NUMERIC(78, 0) NOT NULL CHECK (
        initial_cash_atomic >= 0
    ),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE arena402.holdings (
    game_participant_id TEXT NOT NULL
        REFERENCES arena402.game_participants(game_participant_id)
        ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    good_id TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    initial_quantity BIGINT NOT NULL CHECK (initial_quantity >= 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (game_participant_id, good_id),
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE CASCADE
);

CREATE INDEX holdings_game_good_idx
    ON arena402.holdings (game_id, good_id, game_participant_id);

CREATE TABLE arena402.event_schedule (
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    event_id TEXT NOT NULL CHECK (
        event_id ~ '^[a-z][a-z0-9-]{2,63}$'
    ),
    display_name TEXT NOT NULL CHECK (
        display_name <> '' AND char_length(display_name) <= 100
    ),
    narrative TEXT NOT NULL CHECK (char_length(narrative) <= 1000),
    duration_rounds INTEGER CHECK (duration_rounds >= 1),
    effect_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(effect_snapshot) = 'array'
    ),
    schema_version TEXT NOT NULL DEFAULT 'arena.world-event.v1',
    PRIMARY KEY (game_id, round_index),
    UNIQUE (game_id, event_id)
);

CREATE TABLE arena402.event_occurrences (
    game_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    revealed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    public_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(public_snapshot) = 'object'
    ),
    PRIMARY KEY (game_id, event_id),
    FOREIGN KEY (game_id, round_index)
        REFERENCES arena402.event_schedule(game_id, round_index)
        ON DELETE CASCADE
);

CREATE TABLE arena402.rounds (
    round_id TEXT PRIMARY KEY CHECK (round_id <> ''),
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    phase TEXT NOT NULL DEFAULT 'event_reveal' CHECK (
        phase IN (
            'event_reveal',
            'decide',
            'match',
            'negotiate',
            'settle',
            'round_close',
            'completed',
            'cancelled'
        )
    ),
    phase_deadline_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (game_id, round_index),
    UNIQUE (round_id, game_id),
    CHECK (
        (phase IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (phase NOT IN ('completed', 'cancelled') AND completed_at IS NULL)
    )
);

CREATE INDEX rounds_phase_deadline_idx
    ON arena402.rounds (phase, phase_deadline_at, game_id, round_index);

CREATE TABLE arena402.price_snapshots (
    game_id TEXT NOT NULL,
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    good_id TEXT NOT NULL,
    market_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        market_price_atomic >= 0
    ),
    final_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        final_price_atomic >= 0
    ),
    supply_index_bps INTEGER NOT NULL CHECK (supply_index_bps >= 0),
    bubble_premium_bps INTEGER NOT NULL CHECK (bubble_premium_bps >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (game_id, round_index, good_id),
    FOREIGN KEY (game_id, round_index)
        REFERENCES arena402.rounds(game_id, round_index)
        ON DELETE CASCADE,
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE CASCADE
);

CREATE TABLE arena402.game_events (
    event_sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    round_id TEXT,
    event_type TEXT NOT NULL CHECK (
        event_type <> '' AND char_length(event_type) <= 100
    ),
    public_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(public_payload) = 'object'
    ),
    private_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(private_payload) = 'object'
    ),
    source_idempotency_key TEXT NOT NULL CHECK (
        source_idempotency_key <> ''
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (game_id, source_idempotency_key),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE
);

CREATE INDEX game_events_timeline_idx
    ON arena402.game_events (game_id, event_sequence);

CREATE TABLE arena402.rankings (
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    game_participant_id TEXT NOT NULL
        REFERENCES arena402.game_participants(game_participant_id)
        ON DELETE CASCADE,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    net_worth_atomic NUMERIC(78, 0) NOT NULL CHECK (
        net_worth_atomic >= 0
    ),
    tier TEXT NOT NULL CHECK (
        tier IN ('流浪商贩', '王城行商', '御用商人', '公爵')
    ),
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (game_id, game_participant_id),
    UNIQUE (game_id, rank)
);

REVOKE ALL ON SCHEMA arena402 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA arena402 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA arena402 FROM PUBLIC;

GRANT USAGE ON SCHEMA arena402 TO adx_arena_core, adx_arena_api;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA arena402 TO adx_arena_core;
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA arena402 TO adx_arena_core;
GRANT SELECT ON ALL TABLES IN SCHEMA arena402 TO adx_arena_api;

RESET ROLE;

COMMIT;
