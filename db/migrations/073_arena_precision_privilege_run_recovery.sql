BEGIN;

-- A Run that reached migration 068 before migration 072 could fail on the
-- wrapper's row lock. Requeue only when every produced Result remains pending
-- and no applied action exists, proving that the failed attempt crossed no
-- Arena business-application boundary.
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
      AND run.safe_error_code = 'runtime_insufficientprivilegeerror'
      AND run.runtime_kind IN ('hosted', 'mixed')
      AND round.phase = 'decide'
      AND game.phase = 'running'
      AND game.market_protocol = 'agent_a2a.v1'
      AND EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          WHERE task.round_id = run.round_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          LEFT JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          WHERE task.round_id = run.round_id
            AND (
                result.result_id IS NULL
                OR result.apply_status <> 'pending'
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          LEFT JOIN public.arena_applied_agent_actions AS applied
            ON applied.task_id = task.task_id
          WHERE task.round_id = run.round_id
            AND applied.result_id IS NOT NULL
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
