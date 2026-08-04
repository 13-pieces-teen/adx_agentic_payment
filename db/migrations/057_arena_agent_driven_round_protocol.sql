BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Freeze the market protocol on every Game. Existing and Current Games stay
-- on fcfs.v1; agent_a2a.v1 is opt-in for isolated development/E2E Games.
ALTER TABLE arena402.games
    ADD COLUMN market_protocol TEXT NOT NULL DEFAULT 'fcfs.v1'
        CHECK (market_protocol IN ('fcfs.v1', 'agent_a2a.v1'));

UPDATE arena402.games
SET config_snapshot = config_snapshot || jsonb_build_object(
    'marketProtocol',
    market_protocol
)
WHERE config_snapshot ->> 'marketProtocol' IS DISTINCT FROM market_protocol;

UPDATE public.games AS public_game
SET config_snapshot = public_game.config_snapshot || jsonb_build_object(
    'marketProtocol',
    arena_game.market_protocol
)
FROM arena402.games AS arena_game
WHERE arena_game.game_id = public_game.game_id
  AND public_game.config_snapshot ->> 'marketProtocol'
      IS DISTINCT FROM arena_game.market_protocol;

ALTER TABLE arena402.games
    ADD CONSTRAINT games_market_protocol_snapshot_check CHECK (
        config_snapshot ->> 'marketProtocol' = market_protocol
    );

RESET ROLE;

COMMIT;
