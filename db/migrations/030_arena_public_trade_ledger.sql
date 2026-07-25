BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- The EVM transaction sender is the gas-paying facilitator for the current
-- EIP-3009 direct-relay settlement. Historical confirmations remain nullable
-- because their sender was not captured when those rows were written.
ALTER TABLE arena402.settlement_confirmations
    ADD COLUMN facilitator_address TEXT;

ALTER TABLE arena402.settlement_confirmations
    ADD CONSTRAINT settlement_confirmations_facilitator_address_check CHECK (
        facilitator_address IS NULL
        OR facilitator_address ~ '^0x[0-9a-f]{40}$'
    );

-- The public ledger is newest-first across Games. The remaining indexes keep
-- its supported filters bounded without introducing a second trade authority.
CREATE INDEX settlement_intents_ledger_created_idx
    ON arena402.settlement_intents (
        created_at DESC,
        settlement_intent_id DESC
    );

CREATE INDEX settlement_intents_ledger_game_idx
    ON arena402.settlement_intents (
        game_id,
        created_at DESC,
        settlement_intent_id DESC
    );

CREATE INDEX settlement_intents_ledger_buyer_agent_idx
    ON arena402.settlement_intents (
        buyer_agent_id,
        created_at DESC,
        settlement_intent_id DESC
    );

CREATE INDEX settlement_intents_ledger_seller_agent_idx
    ON arena402.settlement_intents (
        seller_agent_id,
        created_at DESC,
        settlement_intent_id DESC
    );

CREATE INDEX settlement_intents_ledger_good_idx
    ON arena402.settlement_intents (
        good_id,
        created_at DESC,
        settlement_intent_id DESC
    );

RESET ROLE;

COMMIT;
