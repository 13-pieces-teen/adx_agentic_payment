BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE arena402.game_coin_provisions (
    provision_id TEXT PRIMARY KEY CHECK (
        provision_id <> ''
        AND char_length(provision_id) <= 256
    ),
    game_id TEXT NOT NULL
        REFERENCES arena402.games(game_id) ON DELETE CASCADE,
    game_participant_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.game_participants(game_participant_id)
        ON DELETE CASCADE,
    chain_id BIGINT NOT NULL CHECK (chain_id > 0),
    token_address TEXT NOT NULL CHECK (
        token_address ~ '^0x[0-9a-f]{40}$'
    ),
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    amount_atomic NUMERIC(78, 0) NOT NULL CHECK (amount_atomic >= 0),
    balance_before_atomic NUMERIC(78, 0) CHECK (
        balance_before_atomic IS NULL OR balance_before_atomic >= 0
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'whitelist_submitted',
            'mint_submitted',
            'confirmed',
            'failed'
        )
    ),
    whitelist_tx_hash TEXT CHECK (
        whitelist_tx_hash IS NULL
        OR whitelist_tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    whitelist_tx_nonce BIGINT CHECK (
        whitelist_tx_nonce IS NULL OR whitelist_tx_nonce >= 0
    ),
    whitelist_gas_limit BIGINT CHECK (
        whitelist_gas_limit IS NULL OR whitelist_gas_limit > 0
    ),
    whitelist_gas_price_wei NUMERIC(78, 0) CHECK (
        whitelist_gas_price_wei IS NULL OR whitelist_gas_price_wei > 0
    ),
    mint_tx_hash TEXT CHECK (
        mint_tx_hash IS NULL OR mint_tx_hash ~ '^0x[0-9a-f]{64}$'
    ),
    mint_tx_nonce BIGINT CHECK (
        mint_tx_nonce IS NULL OR mint_tx_nonce >= 0
    ),
    mint_gas_limit BIGINT CHECK (
        mint_gas_limit IS NULL OR mint_gas_limit > 0
    ),
    mint_gas_price_wei NUMERIC(78, 0) CHECK (
        mint_gas_price_wei IS NULL OR mint_gas_price_wei > 0
    ),
    whitelist_block_number BIGINT CHECK (
        whitelist_block_number IS NULL OR whitelist_block_number >= 0
    ),
    mint_block_number BIGINT CHECK (
        mint_block_number IS NULL OR mint_block_number >= 0
    ),
    submitted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    last_error TEXT CHECK (
        last_error IS NULL OR char_length(last_error) <= 160
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        status <> 'confirmed'
        OR (
            confirmed_at IS NOT NULL
            AND balance_before_atomic IS NOT NULL
        )
    )
);

CREATE INDEX game_coin_provisions_work_idx
    ON arena402.game_coin_provisions (status, created_at, provision_id)
    WHERE status IN ('pending', 'whitelist_submitted', 'mint_submitted');

CREATE INDEX game_coin_provisions_activation_idx
    ON arena402.game_coin_provisions (game_id, confirmed_at)
    WHERE status = 'confirmed';

-- Readiness now also waits for chain-side game-coin preparation. Human
-- participants still require a mandate in application code; official
-- participants use the same provisioning gate without a user mandate.
ALTER TABLE arena402.game_participants
    DROP CONSTRAINT IF EXISTS game_participants_readiness_check;

ALTER TABLE arena402.game_participants
    ADD CONSTRAINT game_participants_readiness_check CHECK (
        (
            readiness = 'pending'
            AND ready_at IS NULL
            AND withdrawn_at IS NULL
        )
        OR (
            readiness = 'ready'
            AND portfolio_locked_at IS NOT NULL
            AND ready_at IS NOT NULL
            AND withdrawn_at IS NULL
        )
        OR (
            readiness = 'withdrawn'
            AND withdrawn_at IS NOT NULL
        )
    );

REVOKE ALL ON arena402.game_coin_provisions FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    arena402.game_coin_provisions
TO adx_arena_core;

-- Cut over an empty waiting Current Game in place. Once any participant has
-- joined, its settlement snapshot remains immutable and the next Game gets
-- the new worker configuration instead.
UPDATE arena402.games AS game
SET config_snapshot = jsonb_set(
        game.config_snapshot,
        '{settlement}',
        jsonb_build_object(
            'authorizationMode', 'single_eip3009',
            'chainId', 1439,
            'tokenAddress', '0xbf7b7268ce82d92bac7a95a741f4003fe84e1884',
            'tokenSymbol', 'arena402-g',
            'tokenDecimals', 6,
            'tokenEip712Name', 'Arena 402 Gold',
            'tokenEip712Version', '1',
            'requiredConfirmations', 2
        ),
        TRUE
    )
FROM arena402.current_game AS pointer
WHERE pointer.singleton
  AND pointer.game_id = game.game_id
  AND game.phase IN ('registration', 'portfolio_setup')
  AND NOT EXISTS (
      SELECT 1
      FROM arena402.game_participants AS participant
      WHERE participant.game_id = game.game_id
  );

UPDATE public.games AS public_game
SET config_snapshot = arena_game.config_snapshot
FROM arena402.current_game AS pointer
JOIN arena402.games AS arena_game ON arena_game.game_id = pointer.game_id
WHERE pointer.singleton
  AND public_game.game_id = pointer.game_id
  AND arena_game.phase IN ('registration', 'portfolio_setup')
  AND NOT EXISTS (
      SELECT 1
      FROM arena402.game_participants AS participant
      WHERE participant.game_id = arena_game.game_id
  );

RESET ROLE;

COMMIT;
