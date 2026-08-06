BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Phase D changes only the next Game. Retire the current FCFS Game solely when
-- it is still an empty registration shell; joined or running Games retain
-- their frozen protocol and remain untouched.
UPDATE arena402.games AS game
SET phase = 'cancelled',
    completed_at = clock_timestamp()
FROM arena402.current_game AS pointer
WHERE pointer.singleton
  AND pointer.game_id = game.game_id
  AND game.phase = 'registration'
  AND game.market_protocol = 'fcfs.v1'
  AND NOT EXISTS (
      SELECT 1
      FROM arena402.game_participants AS participant
      WHERE participant.game_id = game.game_id
  );

RESET ROLE;

COMMIT;
