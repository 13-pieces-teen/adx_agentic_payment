BEGIN;

GRANT INSERT ON
    arena402.game_participants,
    arena402.balances,
    arena402.holdings,
    arena402.game_events
TO adx_arena_api;

GRANT UPDATE (phase)
ON arena402.games
TO adx_arena_api;

GRANT USAGE, SELECT
ON SEQUENCE arena402.game_events_event_sequence_seq
TO adx_arena_api;

COMMIT;
