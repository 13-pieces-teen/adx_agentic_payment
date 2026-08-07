BEGIN;

-- Migration 079 requeued the exact pre-task context-query failure, but a
-- running old Worker could claim that row before deployment replaced it. If
-- the round deadline had elapsed, the new Worker then correctly refused to
-- create a fresh immutable task. Recover only that proven chain: the public
-- event ledger must retain the original undefined-column failure and the
-- round must still have no tasks or business progress.
SET LOCAL ROLE adx_arena_migration;

WITH recoverable_run AS MATERIALIZED (
    SELECT
        run.runtime_run_id,
        run.game_id,
        run.round_id,
        game.action_timeout_ms
    FROM arena402.runtime_runs AS run
    JOIN arena402.rounds AS round
      ON round.round_id = run.round_id
     AND round.game_id = run.game_id
    JOIN arena402.games AS game
      ON game.game_id = run.game_id
    WHERE run.status = 'failed'
      AND run.safe_error_code IN (
          'runtime_undefinedcolumnerror',
          'runtime_valueerror'
      )
      AND run.runtime_kind IN ('hosted', 'mixed')
      AND run.stage = 'decide'
      AND round.phase = 'decide'
      AND game.phase = 'running'
      AND game.market_protocol = 'agent_a2a.v1'
      AND EXISTS (
          SELECT 1
          FROM arena402.game_events AS event
          WHERE event.game_id = run.game_id
            AND event.round_id = run.round_id
            AND event.event_type = 'runtime.run_failed'
            AND event.public_payload->>'runtimeRunId'
                = run.runtime_run_id
            AND event.public_payload->>'errorCode'
                = 'runtime_undefinedcolumnerror'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.arena_agent_tasks AS task
          WHERE task.round_id = run.round_id
      )
    FOR UPDATE OF run, round
),
rearmed_round AS (
    UPDATE arena402.rounds AS round
    SET phase_deadline_at = (
        clock_timestamp()
        + recoverable.action_timeout_ms * interval '1 millisecond'
    )
    FROM recoverable_run AS recoverable
    WHERE round.round_id = recoverable.round_id
      AND round.game_id = recoverable.game_id
      AND round.phase = 'decide'
    RETURNING
        recoverable.runtime_run_id,
        recoverable.game_id,
        recoverable.round_id,
        round.phase_deadline_at
),
public_round_rearmed AS (
    UPDATE public.rounds AS public_round
    SET deadline_at = rearmed.phase_deadline_at
    FROM rearmed_round AS rearmed
    WHERE public_round.round_id = rearmed.round_id
      AND public_round.game_id = rearmed.game_id
      AND public_round.phase = 'decide'
    RETURNING
        rearmed.runtime_run_id,
        rearmed.game_id,
        rearmed.round_id
),
requeued_run AS (
    UPDATE arena402.runtime_runs AS run
    SET status = 'queued',
        stage = 'decide',
        leased_by = NULL,
        lease_expires_at = NULL,
        safe_error_code = NULL,
        completed_at = NULL
    FROM public_round_rearmed AS recovered
    WHERE run.runtime_run_id = recovered.runtime_run_id
      AND run.status = 'failed'
    RETURNING
        run.runtime_run_id,
        recovered.game_id,
        recovered.round_id
)
INSERT INTO arena402.game_events (
    game_id,
    round_id,
    event_type,
    public_payload,
    source_idempotency_key
)
SELECT
    recovered.game_id,
    recovered.round_id,
    'runtime.run_queued',
    jsonb_build_object(
        'runtimeRunId', recovered.runtime_run_id,
        'runtimeKind', 'hosted',
        'marketProtocol', 'agent_a2a.v1',
        'recovery', 'context_liquidity_deadline_v1'
    ),
    recovered.runtime_run_id || ':recovery:080'
FROM requeued_run AS recovered
ON CONFLICT (game_id, source_idempotency_key) DO NOTHING;

RESET ROLE;

COMMIT;
