BEGIN;

ALTER TABLE arena402.games
    ADD COLUMN operator_user_id TEXT
        REFERENCES public.connector_users(user_id) ON DELETE RESTRICT;

CREATE INDEX arena402_games_operator_created_idx
    ON arena402.games (operator_user_id, created_at DESC, game_id);

INSERT INTO public.arena_agents (
    agent_id,
    owner_user_id,
    name,
    status
)
SELECT
    cb.agent_id,
    cd.owner_id,
    left(
        coalesce(
            nullif(cb.record ->> 'display_name', ''),
            nullif(cr.record ->> 'display_name', ''),
            'Local Agent'
        ),
        120
    ),
    'active'
FROM public.connector_bindings AS cb
JOIN public.connector_devices AS cd
  ON cd.device_id = cb.device_id
JOIN public.connector_runtimes AS cr
  ON cr.device_id = cb.device_id
 AND cr.runtime_id = cb.runtime_id
ON CONFLICT (agent_id) DO NOTHING;

INSERT INTO public.arena_runtime_bindings (
    runtime_binding_id,
    agent_id,
    runtime_kind,
    connector_binding_id,
    connector_binding_epoch,
    route_status
)
SELECT
    'rbind:connector:' || cb.binding_id,
    cb.agent_id,
    'connector',
    cb.binding_id,
    (cb.record ->> 'binding_epoch')::BIGINT,
    CASE
        WHEN cd.revoked_at IS NULL
         AND cr.available
         AND cb.status IN ('available', 'running')
         AND (cr.record -> 'capabilities') ? 'arena.decide'
         AND (cr.record -> 'capabilities') ? 'arena.negotiate'
        THEN 'ready'
        ELSE 'provisioning'
    END
FROM public.connector_bindings AS cb
JOIN public.connector_devices AS cd
  ON cd.device_id = cb.device_id
JOIN public.connector_runtimes AS cr
  ON cr.device_id = cb.device_id
 AND cr.runtime_id = cb.runtime_id
WHERE (cb.record ->> 'binding_epoch') ~ '^[1-9][0-9]*$'
  AND NOT EXISTS (
      SELECT 1
      FROM public.arena_runtime_bindings AS existing
      WHERE existing.agent_id = cb.agent_id
        AND existing.disabled_at IS NULL
  )
ON CONFLICT (runtime_binding_id) DO NOTHING;

COMMIT;
