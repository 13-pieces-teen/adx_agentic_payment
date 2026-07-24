BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.round_portfolio_snapshots (
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    game_participant_id TEXT NOT NULL,
    cash_atomic NUMERIC(78, 0) NOT NULL CHECK (cash_atomic >= 0),
    holdings_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(holdings_snapshot) = 'object'
    ),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (round_id, game_participant_id),
    FOREIGN KEY (round_id, game_id)
        REFERENCES arena402.rounds(round_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE
);

CREATE INDEX round_portfolio_snapshots_game_round_idx
    ON arena402.round_portfolio_snapshots (
        game_id,
        round_index,
        game_participant_id
    );

CREATE TABLE arena402.final_settlement_prices (
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    good_id TEXT NOT NULL,
    price_atomic NUMERIC(78, 0) NOT NULL CHECK (price_atomic >= 0),
    source_round_index INTEGER NOT NULL CHECK (source_round_index >= 1),
    frozen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (game_id, good_id),
    FOREIGN KEY (game_id, good_id)
        REFERENCES arena402.game_goods(game_id, good_id)
        ON DELETE RESTRICT
);

REVOKE ALL ON arena402.round_portfolio_snapshots FROM PUBLIC;
REVOKE ALL ON arena402.final_settlement_prices FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON arena402.round_portfolio_snapshots,
       arena402.final_settlement_prices
    TO adx_arena_core;
GRANT SELECT ON arena402.round_portfolio_snapshots TO adx_arena_api;
GRANT SELECT ON arena402.final_settlement_prices TO adx_arena_api;

RESET ROLE;

COMMIT;
