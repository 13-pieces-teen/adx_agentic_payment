BEGIN;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE arena402.game_goods
    DROP CONSTRAINT IF EXISTS game_goods_fixed_trade_quantity_check;
ALTER TABLE arena402.game_goods
    ADD CONSTRAINT game_goods_fixed_trade_quantity_check
    CHECK (fixed_trade_quantity > 0);

ALTER TABLE arena402.pool_entries
    ADD COLUMN quantity BIGINT NOT NULL DEFAULT 1
        CHECK (quantity > 0),
    ADD COLUMN limit_price_atomic NUMERIC(78, 0)
        CHECK (limit_price_atomic IS NULL OR limit_price_atomic > 0);

ALTER TABLE arena402.pairings
    ADD COLUMN quantity BIGINT NOT NULL DEFAULT 1
        CHECK (quantity > 0),
    ADD COLUMN buyer_limit_price_atomic NUMERIC(78, 0)
        CHECK (
            buyer_limit_price_atomic IS NULL
            OR buyer_limit_price_atomic > 0
        ),
    ADD COLUMN seller_limit_price_atomic NUMERIC(78, 0)
        CHECK (
            seller_limit_price_atomic IS NULL
            OR seller_limit_price_atomic > 0
        );

ALTER TABLE arena402.settlement_intents
    DROP CONSTRAINT IF EXISTS settlement_intents_quantity_check;
ALTER TABLE arena402.settlement_intents
    ADD CONSTRAINT settlement_intents_quantity_check
    CHECK (quantity > 0);

COMMIT;
