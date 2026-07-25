BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Arena 402 product games are deliberately fixed at twenty seats. The
-- previous 100-seat migration was a scalability experiment, not the product
-- matchmaking contract.
ALTER TABLE arena402.current_game
    DROP CONSTRAINT IF EXISTS current_game_start_threshold_check;

ALTER TABLE arena402.current_game
    DROP CONSTRAINT IF EXISTS current_game_max_participants_check;

ALTER TABLE arena402.current_game
    ADD CONSTRAINT current_game_start_threshold_check
    CHECK (start_threshold BETWEEN 2 AND 20);

ALTER TABLE arena402.current_game
    ADD CONSTRAINT current_game_max_participants_check
    CHECK (max_participants BETWEEN start_threshold AND 20);

-- Membership is explicit and operator-managed. A player's BYOK agent can
-- never become an official filler merely because it uses the same provider.
CREATE TABLE arena402.official_agent_pool (
    agent_id TEXT PRIMARY KEY
        REFERENCES public.arena_agents(agent_id) ON DELETE RESTRICT,
    wallet_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.wallet_inventory(wallet_id) ON DELETE RESTRICT,
    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    disabled_at TIMESTAMPTZ,
    CHECK (
        (enabled AND disabled_at IS NULL)
        OR (NOT enabled AND disabled_at IS NOT NULL)
    )
);

CREATE INDEX official_agent_pool_enabled_idx
    ON arena402.official_agent_pool (priority, agent_id)
    WHERE enabled;

REVOKE ALL ON arena402.official_agent_pool FROM PUBLIC;

GRANT SELECT ON arena402.official_agent_pool TO adx_arena_api;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON arena402.official_agent_pool TO adx_arena_core;

RESET ROLE;

COMMIT;
