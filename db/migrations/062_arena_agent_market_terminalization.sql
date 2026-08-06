-- Backfill unfinished A2A market state left behind by completed or cancelled
-- rounds. Runtime round close performs the same transition for new games.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

UPDATE arena402.market_rfq_sessions AS session
SET status = 'expired',
    deadline_at = LEAST(session.deadline_at, clock_timestamp()),
    updated_at = clock_timestamp()
FROM arena402.rounds AS round_row
JOIN arena402.games AS game
  ON game.game_id = round_row.game_id
WHERE session.game_id = round_row.game_id
  AND session.round_id = round_row.round_id
  AND session.status = 'active'
  AND (
      round_row.phase IN ('completed', 'cancelled')
      OR game.phase IN ('completed', 'cancelled')
  );

UPDATE arena402.market_negotiation_requests AS request
SET status = 'expired'
FROM arena402.rounds AS round_row
JOIN arena402.games AS game
  ON game.game_id = round_row.game_id
WHERE request.game_id = round_row.game_id
  AND request.round_id = round_row.round_id
  AND request.status = 'pending'
  AND (
      round_row.phase IN ('completed', 'cancelled')
      OR game.phase IN ('completed', 'cancelled')
  );

UPDATE arena402.participant_round_slots AS slot
SET status = 'available',
    engagement_id = NULL,
    version = slot.version + 1,
    updated_at = clock_timestamp()
FROM arena402.rounds AS round_row
JOIN arena402.games AS game
  ON game.game_id = round_row.game_id
WHERE slot.game_id = round_row.game_id
  AND slot.round_id = round_row.round_id
  AND slot.status = 'reserved'
  AND (
      round_row.phase IN ('completed', 'cancelled')
      OR game.phase IN ('completed', 'cancelled')
  );

UPDATE arena402.market_intents AS intent
SET status = 'expired',
    expires_at = LEAST(intent.expires_at, clock_timestamp())
FROM arena402.rounds AS round_row
JOIN arena402.games AS game
  ON game.game_id = round_row.game_id
WHERE intent.game_id = round_row.game_id
  AND intent.round_id = round_row.round_id
  AND intent.status IN ('open', 'reserved')
  AND (
      round_row.phase IN ('completed', 'cancelled')
      OR game.phase IN ('completed', 'cancelled')
  );

RESET ROLE;

COMMIT;
