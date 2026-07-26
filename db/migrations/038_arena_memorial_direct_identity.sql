BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE OR REPLACE FUNCTION arena402.reconcile_memorial_awards(
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
          AND candidate.identity_provider IN ('github', 'password')
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

CREATE OR REPLACE FUNCTION arena402.on_connector_user_memorial()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $trigger$
BEGIN
    IF NEW.temporary = FALSE
       AND NEW.identity_provider IN ('github', 'password') THEN
        PERFORM arena402.reconcile_memorial_awards('arena402-genesis');
    END IF;
    RETURN NEW;
END;
$trigger$;

GRANT EXECUTE ON FUNCTION arena402.reconcile_memorial_awards(TEXT)
    TO adx_arena_api;

RESET ROLE;

COMMIT;
