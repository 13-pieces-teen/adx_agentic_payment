BEGIN;

-- Migration 068 repaired over-precise candidates that had not crossed the
-- durable market projection boundary. Requeue only the Runtime Runs whose
-- recorded failure is proven to be that exact historical projector error and
-- whose complete set of applied market results is now durably receipted.
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
      AND run.safe_error_code = 'runtime_moneyerror'
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
            AND result.error_class = 'price_precision_exceeded'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_applied_agent_actions AS applied
          LEFT JOIN arena402.market_projection_receipts AS receipt
            ON receipt.result_id = applied.result_id
          WHERE applied.round_id = run.round_id
            AND applied.task_kind IN (
                'arena.market.intent',
                'arena.market.rfq',
                'arena.market.select'
            )
            AND receipt.result_id IS NULL
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
