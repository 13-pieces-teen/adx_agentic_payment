BEGIN;

-- The first production Run after adding prior-round liquidity context failed
-- before task creation because the context query referenced legacy event
-- column names. Requeue only that proven pre-task failure after the query has
-- been corrected to use game_events.created_at and event_sequence.
SET LOCAL ROLE adx_arena_migration;

WITH recoverable_run AS (
    SELECT run.runtime_run_id
    FROM arena402.runtime_runs AS run
    JOIN arena402.rounds AS round
      ON round.round_id = run.round_id
     AND round.game_id = run.game_id
    JOIN arena402.games AS game
      ON game.game_id = run.game_id
    WHERE run.status = 'failed'
      AND run.safe_error_code = 'runtime_undefinedcolumnerror'
      AND run.runtime_kind IN ('hosted', 'mixed')
      AND run.stage = 'decide'
      AND round.phase = 'decide'
      AND game.phase = 'running'
      AND game.market_protocol = 'agent_a2a.v1'
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          WHERE task.round_id = run.round_id
      )
)
UPDATE arena402.runtime_runs AS run
SET status = 'queued',
    stage = 'decide',
    leased_by = NULL,
    lease_expires_at = NULL,
    safe_error_code = NULL,
    completed_at = NULL
FROM recoverable_run AS recoverable
WHERE run.runtime_run_id = recoverable.runtime_run_id;

RESET ROLE;

COMMIT;
