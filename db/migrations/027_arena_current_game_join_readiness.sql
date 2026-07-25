BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.join_authorizations (
    join_authorization_id TEXT PRIMARY KEY CHECK (
        join_authorization_id <> ''
        AND char_length(join_authorization_id) <= 128
    ),
    user_id TEXT NOT NULL
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL
        REFERENCES public.arena_agents(agent_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'consumed', 'expired')
    ),
    key_digest TEXT NOT NULL CHECK (
        key_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    request_digest TEXT NOT NULL CHECK (
        request_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    consumed_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK (
        (status = 'pending' AND consumed_at IS NULL)
        OR (status = 'consumed' AND consumed_at IS NOT NULL)
        OR (status = 'expired' AND consumed_at IS NULL)
    )
);

CREATE UNIQUE INDEX join_authorizations_active_user_game_uidx
    ON arena402.join_authorizations (user_id, game_id)
    WHERE status = 'pending';

CREATE UNIQUE INDEX join_authorizations_idempotency_uidx
    ON arena402.join_authorizations (user_id, key_digest);

ALTER TABLE arena402.payment_mandates
    ADD COLUMN join_authorization_id TEXT
        REFERENCES arena402.join_authorizations(join_authorization_id)
        ON DELETE RESTRICT,
    ADD COLUMN allowed_payee_rule TEXT CHECK (
        allowed_payee_rule IS NULL
        OR allowed_payee_rule = 'same_game_settlement_account'
    );

DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_catalog.pg_constraint AS con
        JOIN pg_catalog.pg_class AS relation
          ON relation.oid = con.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'arena402'
          AND relation.relname = 'payment_mandates'
          AND con.contype = 'c'
          AND pg_catalog.pg_get_constraintdef(con.oid)
              ILIKE '%allowed_payees%'
    LOOP
        EXECUTE format(
            'ALTER TABLE arena402.payment_mandates DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$$;

ALTER TABLE arena402.payment_mandates
    ADD CONSTRAINT payment_mandates_payee_scope_check CHECK (
        (
            allowed_payee_rule IS NULL
            AND join_authorization_id IS NULL
            AND cardinality(allowed_payees) > 0
            AND array_to_string(allowed_payees, ',')
                ~ '^0x[0-9a-f]{40}(,0x[0-9a-f]{40})*$'
        )
        OR (
            allowed_payee_rule = 'same_game_settlement_account'
            AND join_authorization_id IS NOT NULL
            AND cardinality(allowed_payees) = 0
        )
    );

ALTER TABLE arena402.game_participants
    ADD COLUMN payment_mandate_id TEXT
        REFERENCES arena402.payment_mandates(mandate_id)
        ON DELETE RESTRICT,
    ADD COLUMN readiness TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN ready_at TIMESTAMPTZ,
    ADD COLUMN withdrawn_at TIMESTAMPTZ,
    ADD CONSTRAINT game_participants_readiness_check CHECK (
        (
            readiness = 'pending'
            AND ready_at IS NULL
            AND withdrawn_at IS NULL
        )
        OR (
            readiness = 'ready'
            AND payment_mandate_id IS NOT NULL
            AND portfolio_locked_at IS NOT NULL
            AND ready_at IS NOT NULL
            AND withdrawn_at IS NULL
        )
        OR (
            readiness = 'withdrawn'
            AND withdrawn_at IS NOT NULL
        )
    );

CREATE INDEX game_participants_ready_idx
    ON arena402.game_participants (game_id, joined_at, game_participant_id)
    WHERE readiness = 'ready';

REVOKE ALL ON arena402.join_authorizations FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON
    arena402.join_authorizations
TO adx_arena_api;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    arena402.join_authorizations
TO adx_arena_core;

GRANT SELECT ON
    arena402.join_authorizations
TO adx_settlement;

RESET ROLE;

COMMIT;
