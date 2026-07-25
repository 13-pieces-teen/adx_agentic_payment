BEGIN;

-- This forward migration creates Arena-owned references to the Connector User
-- authority. The DDL role needs only REFERENCES; runtime access remains
-- governed by the existing API/Core role grants.
GRANT REFERENCES ON TABLE public.connector_users TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.settlement_intents
    ADD COLUMN token_eip712_name TEXT CHECK (
        token_eip712_name IS NULL
        OR (
            token_eip712_name <> ''
            AND char_length(token_eip712_name) <= 128
        )
    ),
    ADD COLUMN token_eip712_version TEXT CHECK (
        token_eip712_version IS NULL
        OR (
            token_eip712_version <> ''
            AND char_length(token_eip712_version) <= 32
        )
    ),
    ADD CONSTRAINT settlement_intents_token_domain_pair CHECK (
        (token_eip712_name IS NULL) = (token_eip712_version IS NULL)
    );

-- Public wallet inventory only. Raw keys stay in the external signer backend.
CREATE TABLE arena402.wallet_inventory (
    wallet_id TEXT PRIMARY KEY CHECK (
        wallet_id <> '' AND char_length(wallet_id) <= 128
    ),
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    secret_ref TEXT NOT NULL CHECK (
        secret_ref <> '' AND char_length(secret_ref) <= 512
    ),
    status TEXT NOT NULL DEFAULT 'available' CHECK (
        status IN ('available', 'bound', 'disabled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (chain_id, account_address),
    UNIQUE (wallet_id, chain_id, account_address),
    UNIQUE (secret_ref)
);

CREATE INDEX wallet_inventory_available_idx
    ON arena402.wallet_inventory (wallet_id)
    WHERE status = 'available';

CREATE TABLE arena402.user_wallets (
    user_id TEXT PRIMARY KEY
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    github_subject TEXT NOT NULL CHECK (
        github_subject ~ '^[1-9][0-9]{0,30}$'
    ),
    wallet_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.wallet_inventory(wallet_id) ON DELETE RESTRICT,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    bound_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (wallet_id, chain_id, account_address)
        REFERENCES arena402.wallet_inventory(
            wallet_id, chain_id, account_address
        )
        ON DELETE RESTRICT,
    UNIQUE (github_subject),
    UNIQUE (user_id, wallet_id),
    UNIQUE (chain_id, account_address)
);

CREATE TABLE arena402.payment_mandates (
    mandate_id TEXT PRIMARY KEY CHECK (
        mandate_id <> '' AND char_length(mandate_id) <= 128
    ),
    user_id TEXT NOT NULL
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    wallet_id TEXT NOT NULL
        REFERENCES arena402.wallet_inventory(wallet_id) ON DELETE RESTRICT,
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE RESTRICT,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    token_address TEXT NOT NULL CHECK (
        token_address ~ '^0x[0-9a-f]{40}$'
    ),
    max_per_payment_atomic NUMERIC(78, 0) NOT NULL CHECK (
        max_per_payment_atomic > 0
    ),
    max_cumulative_atomic NUMERIC(78, 0) NOT NULL CHECK (
        max_cumulative_atomic >= max_per_payment_atomic
    ),
    reserved_atomic NUMERIC(78, 0) NOT NULL DEFAULT 0 CHECK (
        reserved_atomic >= 0
    ),
    consumed_atomic NUMERIC(78, 0) NOT NULL DEFAULT 0 CHECK (
        consumed_atomic >= 0
    ),
    allowed_payees TEXT[] NOT NULL CHECK (
        cardinality(allowed_payees) > 0
    ),
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (user_id, wallet_id)
        REFERENCES arena402.user_wallets(user_id, wallet_id)
        ON DELETE RESTRICT,
    CHECK (expires_at > valid_from),
    CHECK (
        reserved_atomic + consumed_atomic <= max_cumulative_atomic
    ),
    CHECK (
        array_to_string(allowed_payees, ',')
        ~ '^0x[0-9a-f]{40}(,0x[0-9a-f]{40})*$'
    )
);

CREATE UNIQUE INDEX payment_mandates_active_game_uidx
    ON arena402.payment_mandates (user_id, game_id)
    WHERE revoked_at IS NULL;

CREATE INDEX payment_mandates_wallet_status_idx
    ON arena402.payment_mandates (
        wallet_id,
        expires_at,
        mandate_id
    )
    WHERE revoked_at IS NULL;

CREATE TABLE arena402.payment_reservations (
    reservation_id TEXT PRIMARY KEY CHECK (
        reservation_id ~ '^sha256:[0-9a-f]{64}$'
    ),
    mandate_id TEXT NOT NULL
        REFERENCES arena402.payment_mandates(mandate_id)
        ON DELETE RESTRICT,
    settlement_intent_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    intent_hash TEXT NOT NULL CHECK (
        intent_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    amount_atomic NUMERIC(78, 0) NOT NULL CHECK (amount_atomic > 0),
    payee TEXT NOT NULL CHECK (payee ~ '^0x[0-9a-f]{40}$'),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (
        status IN ('reserved', 'consumed', 'released')
    ),
    tx_hash TEXT CHECK (
        tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    release_reason TEXT CHECK (
        release_reason IS NULL
        OR (
            release_reason <> ''
            AND char_length(release_reason) <= 100
        )
    ),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finalized_at TIMESTAMPTZ,
    CHECK (
        (status = 'reserved' AND finalized_at IS NULL
            AND tx_hash IS NULL AND release_reason IS NULL)
        OR (status = 'consumed' AND finalized_at IS NOT NULL
            AND tx_hash IS NOT NULL AND release_reason IS NULL)
        OR (status = 'released' AND finalized_at IS NOT NULL
            AND tx_hash IS NULL AND release_reason IS NOT NULL)
    )
);

CREATE INDEX payment_reservations_recovery_idx
    ON arena402.payment_reservations (
        status,
        reserved_at,
        reservation_id
    );

CREATE TABLE arena402.x402_settlement_attempts (
    settlement_intent_id TEXT PRIMARY KEY
        REFERENCES arena402.settlement_intents(settlement_intent_id)
        ON DELETE RESTRICT,
    reservation_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.payment_reservations(reservation_id)
        ON DELETE RESTRICT,
    x402_version SMALLINT NOT NULL CHECK (x402_version = 2),
    network TEXT NOT NULL CHECK (
        network ~ '^eip155:[1-9][0-9]*$'
    ),
    payment_required JSONB NOT NULL CHECK (
        jsonb_typeof(payment_required) = 'object'
    ),
    payment_payload_digest TEXT CHECK (
        payment_payload_digest IS NULL
        OR payment_payload_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    facilitator_id TEXT CHECK (
        facilitator_id IS NULL
        OR (
            facilitator_id <> ''
            AND char_length(facilitator_id) <= 128
        )
    ),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (
        status IN (
            'reserved',
            'signed',
            'submitting',
            'submitted',
            'failed',
            'unknown'
        )
    ),
    safe_error_code TEXT CHECK (
        safe_error_code IS NULL
        OR (
            safe_error_code <> ''
            AND char_length(safe_error_code) <= 100
        )
    ),
    lease_owner TEXT CHECK (
        lease_owner IS NULL
        OR (lease_owner <> '' AND char_length(lease_owner) <= 128)
    ),
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

ALTER TABLE arena402.settlement_approvals
    DROP CONSTRAINT settlement_approvals_approval_source_check;

ALTER TABLE arena402.settlement_approvals
    ADD CONSTRAINT settlement_approvals_approval_source_check CHECK (
        approval_source IN (
            'operator_cli',
            'payment_mandate',
            'legacy_migration'
        )
    );

REVOKE ALL ON
    arena402.wallet_inventory,
    arena402.user_wallets,
    arena402.payment_mandates,
    arena402.payment_reservations,
    arena402.x402_settlement_attempts
FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    arena402.wallet_inventory,
    arena402.user_wallets,
    arena402.payment_mandates,
    arena402.payment_reservations,
    arena402.x402_settlement_attempts
TO adx_arena_core;

GRANT SELECT, INSERT, UPDATE ON
    arena402.user_wallets,
    arena402.payment_mandates
TO adx_arena_api;

GRANT SELECT, INSERT ON
    arena402.participant_settlement_accounts
TO adx_arena_api;

GRANT SELECT ON
    arena402.wallet_inventory,
    arena402.payment_reservations,
    arena402.x402_settlement_attempts
TO adx_arena_api;

RESET ROLE;

COMMIT;
