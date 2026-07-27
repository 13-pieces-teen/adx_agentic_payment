BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Event schedules are immutable once a player has joined. An empty waiting
-- Current Game may be retired so the lifecycle worker recreates it from the
-- newly deployed event deck.
UPDATE arena402.games AS game
SET phase = 'cancelled',
    completed_at = clock_timestamp()
FROM arena402.current_game AS pointer
WHERE pointer.singleton
  AND pointer.game_id = game.game_id
  AND game.phase = 'registration'
  AND NOT EXISTS (
      SELECT 1
      FROM arena402.game_participants AS participant
      WHERE participant.game_id = game.game_id
  );

RESET ROLE;

COMMIT;
