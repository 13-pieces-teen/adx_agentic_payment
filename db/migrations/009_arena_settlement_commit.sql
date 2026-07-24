BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.participant_settlement_accounts (
    game_participant_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    custody_mode TEXT NOT NULL CHECK (
        custody_mode IN ('wallet', 'sandbox_guest')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (game_participant_id, game_id)
        REFERENCES arena402.game_participants(game_participant_id, game_id)
        ON DELETE CASCADE
);

CREATE INDEX participant_settlement_accounts_game_idx
    ON arena402.participant_settlement_accounts (
        game_id,
        chain_id,
        account_address
    );

CREATE TABLE arena402.settlement_intents (
    settlement_intent_id TEXT PRIMARY KEY CHECK (
        settlement_intent_id <> ''
    ),
    game_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    pairing_id TEXT NOT NULL UNIQUE,
    negotiation_id TEXT NOT NULL UNIQUE,
    buyer_participant_id TEXT NOT NULL,
    seller_participant_id TEXT NOT NULL,
    buyer_agent_id TEXT NOT NULL CHECK (buyer_agent_id <> ''),
    seller_agent_id TEXT NOT NULL CHECK (seller_agent_id <> ''),
    buyer_account TEXT NOT NULL CHECK (
        buyer_account ~ '^0x[0-9a-f]{40}$'
    ),
    seller_account TEXT NOT NULL CHECK (
        seller_account ~ '^0x[0-9a-f]{40}$'
    ),
    good_id TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity = 1),
    unit_price_atomic NUMERIC(78, 0) NOT NULL CHECK (
        unit_price_atomic > 0
    ),
    amount_atomic NUMERIC(78, 0) NOT NULL CHECK (
        amount_atomic = unit_price_atomic * quantity
    ),
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    token_address TEXT NOT NULL CHECK (
        token_address ~ '^0x[0-9a-f]{40}$'
    ),
    token_symbol TEXT NOT NULL CHECK (
        token_symbol <> '' AND char_length(token_symbol) <= 20
    ),
    token_decimals SMALLINT NOT NULL CHECK (token_decimals = 6),
    required_confirmations INTEGER NOT NULL CHECK (
        required_confirmations BETWEEN 1 AND 100
    ),
    authorization_mode TEXT NOT NULL CHECK (
        authorization_mode = 'single_eip3009'
    ),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        idempotency_key <> ''
    ),
    intent_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(intent_snapshot) = 'object'
    ),
    intent_hash TEXT NOT NULL CHECK (
        intent_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'authorization_requested' CHECK (
        status IN (
            'authorization_requested',
            'submitted',
            'confirmation_timeout',
            'chain_confirmed_uncommitted',
            'inventory_committed',
            'authorization_failed',
            'submission_failed',
            'expired',
            'reverted'
        )
    ),
    safe_error_code TEXT CHECK (
        safe_error_code IS NULL
        OR (
            safe_error_code <> ''
            AND char_length(safe_error_code) <= 100
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    submitted_at TIMESTAMPTZ,
    chain_confirmed_at TIMESTAMPTZ,
    inventory_committed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (pairing_id, game_id, round_id)
        REFERENCES arena402.pairings(pairing_id, game_id, round_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (negotiation_id, game_id, round_id)
        REFERENCES arena402.negotiations(negotiation_id, game_id, round_id)
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
    CHECK (buyer_account <> seller_account),
    CHECK (
        status <> 'submitted'
        OR submitted_at IS NOT NULL
    ),
    CHECK (
        status NOT IN (
            'chain_confirmed_uncommitted',
            'inventory_committed'
        )
        OR chain_confirmed_at IS NOT NULL
    ),
    CHECK (
        status <> 'inventory_committed'
        OR inventory_committed_at IS NOT NULL
    )
);

CREATE INDEX settlement_intents_recovery_idx
    ON arena402.settlement_intents (
        status,
        created_at,
        settlement_intent_id
    );

CREATE TABLE arena402.settlement_submissions (
    settlement_intent_id TEXT PRIMARY KEY
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    tx_hash TEXT NOT NULL UNIQUE CHECK (
        tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    authorization_nonce_digest TEXT NOT NULL CHECK (
        authorization_nonce_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    submission_source TEXT NOT NULL CHECK (
        submission_source IN ('wallet', 'sandbox_guest')
    ),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (settlement_intent_id, tx_hash)
);

CREATE TABLE arena402.settlement_confirmations (
    settlement_intent_id TEXT PRIMARY KEY
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    tx_hash TEXT NOT NULL UNIQUE,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    token_address TEXT NOT NULL CHECK (
        token_address ~ '^0x[0-9a-f]{40}$'
    ),
    from_account TEXT NOT NULL CHECK (
        from_account ~ '^0x[0-9a-f]{40}$'
    ),
    to_account TEXT NOT NULL CHECK (
        to_account ~ '^0x[0-9a-f]{40}$'
    ),
    amount_atomic NUMERIC(78, 0) NOT NULL CHECK (amount_atomic > 0),
    block_number NUMERIC(78, 0) NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL CHECK (
        block_hash ~ '^0x[0-9a-f]{64}$'
    ),
    confirmation_count INTEGER NOT NULL CHECK (
        confirmation_count >= 1
    ),
    evidence_hash TEXT NOT NULL CHECK (
        evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (settlement_intent_id, tx_hash)
        REFERENCES arena402.settlement_submissions(
            settlement_intent_id,
            tx_hash
        )
        ON DELETE RESTRICT
);

CREATE TABLE arena402.inventory_commits (
    inventory_commit_id TEXT PRIMARY KEY CHECK (
        inventory_commit_id <> ''
    ),
    settlement_intent_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    buyer_cash_before_atomic NUMERIC(78, 0) NOT NULL CHECK (
        buyer_cash_before_atomic >= 0
    ),
    buyer_cash_after_atomic NUMERIC(78, 0) NOT NULL CHECK (
        buyer_cash_after_atomic >= 0
    ),
    seller_cash_before_atomic NUMERIC(78, 0) NOT NULL CHECK (
        seller_cash_before_atomic >= 0
    ),
    seller_cash_after_atomic NUMERIC(78, 0) NOT NULL CHECK (
        seller_cash_after_atomic >= 0
    ),
    buyer_holding_before BIGINT NOT NULL CHECK (
        buyer_holding_before >= 0
    ),
    buyer_holding_after BIGINT NOT NULL CHECK (
        buyer_holding_after >= 0
    ),
    seller_holding_before BIGINT NOT NULL CHECK (
        seller_holding_before >= 1
    ),
    seller_holding_after BIGINT NOT NULL CHECK (
        seller_holding_after >= 0
    ),
    committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (buyer_holding_after = buyer_holding_before + 1),
    CHECK (seller_holding_after = seller_holding_before - 1)
);

REVOKE ALL ON ALL TABLES IN SCHEMA arena402 FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA arena402 TO adx_arena_core;
GRANT SELECT ON ALL TABLES IN SCHEMA arena402 TO adx_arena_api;

RESET ROLE;

COMMIT;
