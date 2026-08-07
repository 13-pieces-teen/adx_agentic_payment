BEGIN;

-- A market Result can be projected concurrently by the Runtime coordinator
-- and the projection worker. Before the projector claimed both unique keys
-- atomically, the losing transaction could fail the Runtime Run even though
-- every Intent was already applied and durably receipted. Requeue only that
-- proven recovery state; the coordinator will reuse the frozen tasks and
-- advance the still-open stage.
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
      AND run.safe_error_code = 'runtime_uniqueviolationerror'
      AND run.runtime_kind IN ('hosted', 'mixed')
      AND run.stage = 'decide'
      AND round.phase = 'decide'
      AND game.phase = 'running'
      AND game.market_protocol = 'agent_a2a.v1'
      AND EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          WHERE task.round_id = run.round_id
            AND task.task_kind = 'arena.market.intent'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          LEFT JOIN public.arena_agent_task_results AS result
            ON result.task_id = task.task_id
          LEFT JOIN public.arena_applied_agent_actions AS applied
            ON applied.task_id = task.task_id
          LEFT JOIN arena402.market_projection_receipts AS receipt
            ON receipt.result_id = applied.result_id
          WHERE task.round_id = run.round_id
            AND task.task_kind = 'arena.market.intent'
            AND (
                task.status <> 'completed'
                OR result.result_id IS NULL
                OR result.apply_status <> 'applied'
                OR applied.result_id IS NULL
                OR receipt.result_id IS NULL
            )
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
