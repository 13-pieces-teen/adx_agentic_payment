BEGIN;

-- Migration 080 emitted a recovery event with runtimeKind='hosted' even when
-- the recovered durable run was mixed Hosted/Connector. Preserve the applied
-- migration and repair its event from the authoritative runtime_runs row.
SET LOCAL ROLE adx_arena_migration;

UPDATE arena402.game_events AS event
SET public_payload = jsonb_set(
        event.public_payload,
        '{runtimeKind}',
        to_jsonb(run.runtime_kind),
        true
    )
FROM arena402.runtime_runs AS run
WHERE event.event_type = 'runtime.run_queued'
  AND event.source_idempotency_key
      = run.runtime_run_id || ':recovery:080'
  AND event.public_payload->>'runtimeRunId' = run.runtime_run_id
  AND run.runtime_kind IN ('hosted', 'mixed')
  AND event.public_payload->>'runtimeKind' IS DISTINCT FROM run.runtime_kind;

RESET ROLE;

COMMIT;
