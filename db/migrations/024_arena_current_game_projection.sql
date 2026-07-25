BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- The singleton row is the authority for the product-facing Current Game.
-- Game Core may continue to retain multiple historical or development games;
-- clients must never infer Current from creation timestamps.
CREATE TABLE arena402.current_game (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    game_id TEXT NOT NULL UNIQUE
        REFERENCES arena402.games(game_id) ON DELETE RESTRICT,
    start_threshold INTEGER NOT NULL CHECK (
        start_threshold BETWEEN 2 AND 12
    ),
    max_participants INTEGER NOT NULL CHECK (
        max_participants BETWEEN start_threshold AND 12
    ),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON arena402.current_game FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE
ON arena402.current_game
TO adx_arena_core;

GRANT SELECT ON arena402.current_game TO adx_arena_api;

-- Bootstrap only when the database has exactly one eligible non-terminal
-- product game. Ambiguous legacy state stays pointerless and fails closed.
INSERT INTO arena402.current_game (
    singleton,
    game_id,
    start_threshold,
    max_participants
)
SELECT
    TRUE,
    min(game_id),
    min(min_participants),
    min(max_participants)
FROM arena402.games
WHERE phase NOT IN ('completed', 'cancelled')
  AND min_participants BETWEEN 2 AND 12
  AND max_participants BETWEEN min_participants AND 12
HAVING count(*) = 1;

RESET ROLE;

COMMIT;
