BEGIN;

-- The NOLOGIN Arena DDL role owns the SECURITY DEFINER reconciliation
-- functions, so it needs durable read access to the Connector user authority.
-- TRIGGER is needed only while installing the insert hook and is revoked
-- before this migration commits.
GRANT SELECT, TRIGGER ON TABLE public.connector_users
    TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.memorial_campaigns (
    campaign_id TEXT PRIMARY KEY CHECK (
        campaign_id <> '' AND char_length(campaign_id) <= 128
    ),
    chain_id BIGINT NOT NULL CHECK (chain_id = 1439),
    contract_address TEXT CHECK (
        contract_address IS NULL
        OR contract_address ~ '^0x[0-9a-f]{40}$'
    ),
    name TEXT NOT NULL CHECK (name <> '' AND char_length(name) <= 200),
    symbol TEXT NOT NULL CHECK (symbol <> '' AND char_length(symbol) <= 32),
    max_supply INTEGER NOT NULL CHECK (max_supply = 402),
    next_rank INTEGER NOT NULL DEFAULT 1 CHECK (
        next_rank BETWEEN 1 AND max_supply + 1
    ),
    status TEXT NOT NULL DEFAULT 'preparing' CHECK (
        status IN ('preparing', 'active', 'minting', 'completed', 'paused')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    activated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (status = 'preparing' AND activated_at IS NULL)
        OR (status <> 'preparing' AND activated_at IS NOT NULL)
    )
);

INSERT INTO arena402.memorial_campaigns (
    campaign_id,
    chain_id,
    name,
    symbol,
    max_supply
)
VALUES (
    'arena402-genesis',
    1439,
    'Arena 402 Memorial',
    'arena402',
    402
);

CREATE TABLE arena402.memorial_wallet_inventory (
    campaign_id TEXT NOT NULL
        REFERENCES arena402.memorial_campaigns(campaign_id) ON DELETE RESTRICT,
    token_id INTEGER NOT NULL CHECK (token_id BETWEEN 0 AND 401),
    wallet_id TEXT NOT NULL CHECK (
        wallet_id <> '' AND char_length(wallet_id) <= 128
    ),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    status TEXT NOT NULL DEFAULT 'available' CHECK (
        status IN ('available', 'reserved', 'minted', 'disabled')
    ),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (campaign_id, token_id),
    UNIQUE (campaign_id, wallet_id),
    UNIQUE (account_address)
);

CREATE TABLE arena402.memorial_awards (
    campaign_id TEXT NOT NULL
        REFERENCES arena402.memorial_campaigns(campaign_id) ON DELETE RESTRICT,
    user_id TEXT NOT NULL
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT,
    registration_rank INTEGER NOT NULL CHECK (
        registration_rank BETWEEN 1 AND 402
    ),
    token_id INTEGER NOT NULL CHECK (token_id BETWEEN 0 AND 401),
    wallet_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL CHECK (
        wallet_address ~ '^0x[0-9a-f]{40}$'
    ),
    registered_at TIMESTAMPTZ NOT NULL,
    eligibility_status TEXT NOT NULL DEFAULT 'reserved' CHECK (
        eligibility_status IN ('reserved', 'disqualified')
    ),
    mint_status TEXT NOT NULL DEFAULT 'reserved' CHECK (
        mint_status IN ('reserved', 'submitted', 'minted', 'failed')
    ),
    credential_status TEXT NOT NULL DEFAULT 'unclaimed' CHECK (
        credential_status IN ('unclaimed', 'claimed')
    ),
    mint_tx_hash TEXT CHECK (
        mint_tx_hash IS NULL OR mint_tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    mint_block_number BIGINT CHECK (
        mint_block_number IS NULL OR mint_block_number >= 0
    ),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    submitted_at TIMESTAMPTZ,
    minted_at TIMESTAMPTZ,
    last_error TEXT CHECK (
        last_error IS NULL OR char_length(last_error) <= 500
    ),
    PRIMARY KEY (campaign_id, user_id),
    UNIQUE (campaign_id, registration_rank),
    UNIQUE (campaign_id, token_id),
    UNIQUE (campaign_id, wallet_id),
    UNIQUE (campaign_id, wallet_address),
    FOREIGN KEY (campaign_id, token_id)
        REFERENCES arena402.memorial_wallet_inventory(campaign_id, token_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (campaign_id, wallet_id)
        REFERENCES arena402.memorial_wallet_inventory(campaign_id, wallet_id)
        ON DELETE RESTRICT,
    CHECK (token_id = registration_rank - 1),
    CHECK (
        (mint_status = 'reserved'
            AND mint_tx_hash IS NULL
            AND submitted_at IS NULL
            AND minted_at IS NULL)
        OR (mint_status IN ('submitted', 'failed')
            AND submitted_at IS NOT NULL
            AND minted_at IS NULL)
        OR (mint_status = 'minted'
            AND mint_tx_hash IS NOT NULL
            AND submitted_at IS NOT NULL
            AND minted_at IS NOT NULL)
    )
);

CREATE TABLE arena402.memorial_mint_batches (
    batch_id TEXT PRIMARY KEY CHECK (
        batch_id <> '' AND char_length(batch_id) <= 128
    ),
    campaign_id TEXT NOT NULL
        REFERENCES arena402.memorial_campaigns(campaign_id) ON DELETE RESTRICT,
    first_token_id INTEGER NOT NULL CHECK (
        first_token_id BETWEEN 0 AND 401
    ),
    last_token_id INTEGER NOT NULL CHECK (
        last_token_id BETWEEN first_token_id AND 401
    ),
    address_digest TEXT NOT NULL CHECK (
        address_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'prepared' CHECK (
        status IN ('prepared', 'submitted', 'confirmed', 'failed')
    ),
    tx_hash TEXT CHECK (
        tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    block_number BIGINT CHECK (
        block_number IS NULL OR block_number >= 0
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    submitted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_error TEXT CHECK (
        last_error IS NULL OR char_length(last_error) <= 500
    ),
    UNIQUE (campaign_id, first_token_id, last_token_id),
    CHECK (
        (status = 'prepared'
            AND tx_hash IS NULL
            AND submitted_at IS NULL
            AND confirmed_at IS NULL)
        OR (status IN ('submitted', 'failed')
            AND submitted_at IS NOT NULL
            AND confirmed_at IS NULL)
        OR (status = 'confirmed'
            AND tx_hash IS NOT NULL
            AND submitted_at IS NOT NULL
            AND confirmed_at IS NOT NULL)
    )
);

CREATE INDEX memorial_awards_user_idx
    ON arena402.memorial_awards (user_id, campaign_id);
CREATE INDEX memorial_awards_mint_idx
    ON arena402.memorial_awards (campaign_id, mint_status, token_id);

CREATE FUNCTION arena402.reconcile_memorial_awards(
    p_campaign_id TEXT DEFAULT 'arena402-genesis'
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $reconcile$
DECLARE
    v_campaign arena402.memorial_campaigns%ROWTYPE;
    v_user public.connector_users%ROWTYPE;
    v_wallet arena402.memorial_wallet_inventory%ROWTYPE;
    v_created INTEGER := 0;
BEGIN
    SELECT *
    INTO v_campaign
    FROM arena402.memorial_campaigns
    WHERE campaign_id = p_campaign_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'memorial campaign not found'
            USING ERRCODE = 'P0002';
    END IF;
    IF v_campaign.status NOT IN ('active', 'minting') THEN
        RETURN 0;
    END IF;

    WHILE v_campaign.next_rank <= v_campaign.max_supply LOOP
        SELECT candidate.*
        INTO v_user
        FROM public.connector_users AS candidate
        WHERE candidate.temporary = FALSE
          AND candidate.identity_provider = 'github'
          AND NOT EXISTS (
              SELECT 1
              FROM arena402.memorial_awards AS award
              WHERE award.campaign_id = p_campaign_id
                AND award.user_id = candidate.user_id
          )
        ORDER BY candidate.created_at, candidate.user_id
        LIMIT 1;

        EXIT WHEN NOT FOUND;

        SELECT *
        INTO v_wallet
        FROM arena402.memorial_wallet_inventory
        WHERE campaign_id = p_campaign_id
          AND token_id = v_campaign.next_rank - 1
          AND status = 'available'
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'memorial wallet inventory gap at token %',
                v_campaign.next_rank - 1
                USING ERRCODE = '23514';
        END IF;

        INSERT INTO arena402.memorial_awards (
            campaign_id,
            user_id,
            registration_rank,
            token_id,
            wallet_id,
            wallet_address,
            registered_at
        )
        VALUES (
            p_campaign_id,
            v_user.user_id,
            v_campaign.next_rank,
            v_campaign.next_rank - 1,
            v_wallet.wallet_id,
            v_wallet.account_address,
            v_user.created_at
        );

        UPDATE arena402.memorial_wallet_inventory
        SET status = 'reserved',
            updated_at = clock_timestamp()
        WHERE campaign_id = p_campaign_id
          AND token_id = v_wallet.token_id;

        v_campaign.next_rank := v_campaign.next_rank + 1;
        v_created := v_created + 1;
    END LOOP;

    UPDATE arena402.memorial_campaigns
    SET next_rank = v_campaign.next_rank,
        updated_at = clock_timestamp()
    WHERE campaign_id = p_campaign_id;

    RETURN v_created;
END;
$reconcile$;

CREATE FUNCTION arena402.activate_memorial_campaign(
    p_campaign_id TEXT DEFAULT 'arena402-genesis'
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $activate$
DECLARE
    v_campaign arena402.memorial_campaigns%ROWTYPE;
    v_inventory_count INTEGER;
BEGIN
    SELECT *
    INTO v_campaign
    FROM arena402.memorial_campaigns
    WHERE campaign_id = p_campaign_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'memorial campaign not found'
            USING ERRCODE = 'P0002';
    END IF;
    IF v_campaign.status <> 'preparing' THEN
        RETURN arena402.reconcile_memorial_awards(p_campaign_id);
    END IF;

    SELECT count(*)
    INTO v_inventory_count
    FROM arena402.memorial_wallet_inventory
    WHERE campaign_id = p_campaign_id
      AND status = 'available';
    IF v_inventory_count <> v_campaign.max_supply THEN
        RAISE EXCEPTION 'memorial inventory must contain exactly % available wallets',
            v_campaign.max_supply
            USING ERRCODE = '23514';
    END IF;

    UPDATE arena402.memorial_campaigns
    SET status = 'active',
        activated_at = COALESCE(activated_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE campaign_id = p_campaign_id
      AND status = 'preparing';

    RETURN arena402.reconcile_memorial_awards(p_campaign_id);
END;
$activate$;

CREATE FUNCTION arena402.on_connector_user_memorial()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $trigger$
BEGIN
    IF NEW.temporary = FALSE AND NEW.identity_provider = 'github' THEN
        PERFORM arena402.reconcile_memorial_awards('arena402-genesis');
    END IF;
    RETURN NEW;
END;
$trigger$;

CREATE TRIGGER connector_user_memorial_after_insert
AFTER INSERT ON public.connector_users
FOR EACH ROW
EXECUTE FUNCTION arena402.on_connector_user_memorial();

REVOKE ALL ON
    arena402.memorial_campaigns,
    arena402.memorial_wallet_inventory,
    arena402.memorial_awards,
    arena402.memorial_mint_batches
FROM PUBLIC;

GRANT SELECT ON
    arena402.memorial_campaigns,
    arena402.memorial_wallet_inventory,
    arena402.memorial_awards,
    arena402.memorial_mint_batches
TO adx_arena_api;

GRANT EXECUTE ON FUNCTION arena402.reconcile_memorial_awards(TEXT)
    TO adx_arena_api;

RESET ROLE;

REVOKE TRIGGER ON TABLE public.connector_users
    FROM adx_arena_migration;

COMMIT;
