BEGIN;

-- The Runtime coordinator and the background market projector can race on the
-- same RFQ Result. Before per-Result serialization, the coordinator could
-- observe an already-advanced RFQ session before it observed the projection
-- receipt, fail the Run, and leave later completed Results unapplied. Requeue
-- only the mixed state that proves this partial projection pattern.
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
      AND run.safe_error_code = 'runtime_pawnhouserepositoryerror'
      AND run.runtime_kind IN ('hosted', 'mixed')
      AND run.stage = 'match'
      AND round.phase = 'match'
      AND game.phase = 'running'
      AND game.market_protocol = 'agent_a2a.v1'
      AND EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          WHERE task.round_id = run.round_id
            AND task.task_kind = 'arena.market.rfq'
            AND task.status = 'completed'
            AND result.apply_status = 'pending'
      )
      AND EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          JOIN public.arena_applied_agent_actions AS applied
            ON applied.task_id = task.task_id
           AND applied.result_id = result.result_id
          JOIN arena402.market_projection_receipts AS receipt
            ON receipt.result_id = applied.result_id
          WHERE task.round_id = run.round_id
            AND task.task_kind = 'arena.market.rfq'
            AND task.status = 'completed'
            AND result.apply_status = 'applied'
            AND receipt.result_id IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          LEFT JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          WHERE task.round_id = run.round_id
            AND task.task_kind = 'arena.market.rfq'
            AND (
                task.status <> 'completed'
                OR result.result_id IS NULL
            )
      )
)
UPDATE arena402.runtime_runs AS run
SET status = 'queued',
    stage = 'match',
    leased_by = NULL,
    lease_expires_at = NULL,
    safe_error_code = NULL,
    completed_at = NULL
FROM recoverable_run AS recoverable
WHERE run.runtime_run_id = recoverable.runtime_run_id;

RESET ROLE;

COMMIT;
