BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- PaymentMandates originally accepted only permanent GitHub-user wallet
-- bindings. Official Arena Agents use isolated platform-owned testnet wallets,
-- so give both authority kinds one explicit FK target without pretending that
-- an Official Agent has a GitHub identity.
CREATE TABLE arena402.payment_wallet_authorities (
    user_id TEXT NOT NULL
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    wallet_id TEXT NOT NULL
        REFERENCES arena402.wallet_inventory(wallet_id) ON DELETE RESTRICT,
    authority_kind TEXT NOT NULL CHECK (
        authority_kind IN ('user', 'platform_official')
    ),
    official_agent_id TEXT
        REFERENCES public.arena_agents(agent_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id, wallet_id),
    UNIQUE (wallet_id),
    CHECK (
        (authority_kind = 'user' AND official_agent_id IS NULL)
        OR (
            authority_kind = 'platform_official'
            AND official_agent_id IS NOT NULL
        )
    )
);

INSERT INTO arena402.payment_wallet_authorities (
    user_id,
    wallet_id,
    authority_kind,
    official_agent_id
)
SELECT user_id, wallet_id, 'user', NULL
FROM arena402.user_wallets;

INSERT INTO arena402.payment_wallet_authorities (
    user_id,
    wallet_id,
    authority_kind,
    official_agent_id
)
SELECT
    agent.owner_user_id,
    official.wallet_id,
    'platform_official',
    official.agent_id
FROM arena402.official_agent_pool AS official
JOIN public.arena_agents AS agent ON agent.agent_id = official.agent_id;

ALTER TABLE arena402.payment_mandates
    DROP CONSTRAINT payment_mandates_user_id_wallet_id_fkey;

ALTER TABLE arena402.payment_mandates
    ADD CONSTRAINT payment_mandates_wallet_authority_fkey
    FOREIGN KEY (user_id, wallet_id)
    REFERENCES arena402.payment_wallet_authorities(user_id, wallet_id)
    ON DELETE RESTRICT;

CREATE FUNCTION arena402.sync_user_payment_wallet_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, arena402
AS $$
BEGIN
    INSERT INTO arena402.payment_wallet_authorities (
        user_id,
        wallet_id,
        authority_kind,
        official_agent_id
    )
    VALUES (NEW.user_id, NEW.wallet_id, 'user', NULL)
    ON CONFLICT (user_id, wallet_id) DO NOTHING;
    RETURN NEW;
END
$$;

CREATE TRIGGER user_wallet_payment_authority_sync
AFTER INSERT ON arena402.user_wallets
FOR EACH ROW EXECUTE FUNCTION
    arena402.sync_user_payment_wallet_authority();

CREATE FUNCTION arena402.sync_official_payment_wallet_authority()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, arena402, public
AS $$
DECLARE
    owner_id TEXT;
BEGIN
    SELECT owner_user_id
    INTO STRICT owner_id
    FROM public.arena_agents
    WHERE agent_id = NEW.agent_id;

    INSERT INTO arena402.payment_wallet_authorities (
        user_id,
        wallet_id,
        authority_kind,
        official_agent_id
    )
    VALUES (
        owner_id,
        NEW.wallet_id,
        'platform_official',
        NEW.agent_id
    )
    ON CONFLICT (user_id, wallet_id) DO UPDATE
    SET authority_kind = EXCLUDED.authority_kind,
        official_agent_id = EXCLUDED.official_agent_id
    WHERE payment_wallet_authorities.authority_kind =
        'platform_official';
    RETURN NEW;
END
$$;

CREATE TRIGGER official_wallet_payment_authority_sync
AFTER INSERT OR UPDATE OF wallet_id
ON arena402.official_agent_pool
FOR EACH ROW EXECUTE FUNCTION
    arena402.sync_official_payment_wallet_authority();

REVOKE ALL ON arena402.payment_wallet_authorities FROM PUBLIC;
GRANT SELECT ON arena402.payment_wallet_authorities TO
    adx_arena_api,
    adx_arena_core,
    adx_settlement;

REVOKE ALL ON FUNCTION
    arena402.sync_user_payment_wallet_authority()
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    arena402.sync_official_payment_wallet_authority()
FROM PUBLIC;

RESET ROLE;

COMMIT;
