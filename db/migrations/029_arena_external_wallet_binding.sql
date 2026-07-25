BEGIN;

-- External user wallets are distinct from arena402.user_wallets, which is the
-- platform-managed wallet inventory used by the settlement prototype.
SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.external_wallet_bindings (
    user_id TEXT PRIMARY KEY
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    chain_id BIGINT NOT NULL CHECK (chain_id = 1439),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (chain_id, account_address)
);

CREATE TABLE arena402.wallet_binding_challenges (
    challenge_id TEXT PRIMARY KEY CHECK (
        challenge_id <> '' AND char_length(challenge_id) <= 128
    ),
    user_id TEXT NOT NULL
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    chain_id BIGINT NOT NULL CHECK (chain_id = 1439),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    message_digest TEXT NOT NULL CHECK (
        message_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    verification_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        verification_status IN ('pending', 'verified', 'failed')
    ),
    failure_code TEXT CHECK (
        failure_code IS NULL
        OR (failure_code <> '' AND char_length(failure_code) <= 100)
    ),
    CHECK (expires_at > created_at),
    CHECK (
        (verification_status = 'pending' AND consumed_at IS NULL
            AND verified_at IS NULL AND failure_code IS NULL)
        OR (verification_status IN ('verified', 'failed')
            AND consumed_at IS NOT NULL AND verified_at IS NOT NULL)
    )
);

CREATE INDEX wallet_binding_challenges_user_created_idx
    ON arena402.wallet_binding_challenges (user_id, created_at DESC);
CREATE INDEX wallet_binding_challenges_expiry_idx
    ON arena402.wallet_binding_challenges (expires_at)
    WHERE consumed_at IS NULL;

REVOKE ALL ON arena402.external_wallet_bindings FROM PUBLIC;
REVOKE ALL ON arena402.wallet_binding_challenges FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON arena402.external_wallet_bindings, arena402.wallet_binding_challenges
    TO adx_arena_api;

RESET ROLE;
COMMIT;
