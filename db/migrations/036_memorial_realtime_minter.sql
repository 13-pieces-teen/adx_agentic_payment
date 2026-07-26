BEGIN;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.memorial_mint_batches
    ADD COLUMN tx_nonce BIGINT CHECK (tx_nonce IS NULL OR tx_nonce >= 0),
    ADD COLUMN gas_limit BIGINT CHECK (gas_limit IS NULL OR gas_limit > 0),
    ADD COLUMN gas_price_wei NUMERIC(78, 0)
        CHECK (gas_price_wei IS NULL OR gas_price_wei > 0);

GRANT SELECT, INSERT, UPDATE ON
    arena402.memorial_mint_batches,
    arena402.memorial_awards,
    arena402.memorial_wallet_inventory
TO adx_arena_core;

GRANT SELECT ON arena402.memorial_campaigns TO adx_arena_core;

RESET ROLE;

COMMIT;
