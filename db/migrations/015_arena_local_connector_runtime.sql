BEGIN;

-- One durable coordinator run may now contain Hosted and Connector tasks.
-- Runtime dispatch still remains per-task and follows each frozen binding.
ALTER TABLE arena402.runtime_runs
    DROP CONSTRAINT runtime_runs_runtime_kind_check;
ALTER TABLE arena402.runtime_runs
    ADD CONSTRAINT runtime_runs_runtime_kind_check CHECK (
        runtime_kind IN ('hosted', 'mixed')
    );

-- Local Agent creation is an Arena API mutation with the same bounded
-- idempotency semantics as Hosted Agent creation.
ALTER TABLE arena_api_idempotency
    DROP CONSTRAINT arena_api_idempotency_route_key_check;
ALTER TABLE arena_api_idempotency
    ADD CONSTRAINT arena_api_idempotency_route_key_check CHECK (
        route_key IN (
            'model_credentials.create',
            'model_credentials.replace',
            'model_credentials.revoke',
            'model_credentials.revalidate',
            'hosted_agents.create',
            'hosted_agents.update',
            'hosted_agents.disable',
            'local_agents.create',
            'game_participants.create'
        )
    );

DO $drop_old_route_resource_check$
DECLARE
    v_constraint_name TEXT;
BEGIN
    SELECT conname
    INTO v_constraint_name
    FROM pg_catalog.pg_constraint
    WHERE conrelid = 'public.arena_api_idempotency'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%hosted_agents.create%'
      AND pg_get_constraintdef(oid) LIKE '%resource_kind%'
    ORDER BY conname
    LIMIT 1;

    IF v_constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE public.arena_api_idempotency DROP CONSTRAINT %I',
            v_constraint_name
        );
    END IF;
END
$drop_old_route_resource_check$;

ALTER TABLE arena_api_idempotency
    ADD CONSTRAINT arena_api_idempotency_route_resource_check CHECK (
        resource_kind IS NULL
        OR (
            (
                route_key IN (
                    'model_credentials.create',
                    'model_credentials.replace',
                    'model_credentials.revoke',
                    'model_credentials.revalidate'
                )
                AND resource_kind = 'model_credential'
            )
            OR (
                route_key IN (
                    'hosted_agents.create',
                    'hosted_agents.update',
                    'hosted_agents.disable',
                    'local_agents.create'
                )
                AND resource_kind = 'arena_agent'
            )
            OR (
                route_key = 'game_participants.create'
                AND resource_kind = 'game_agent'
            )
        )
    );

-- Local Agent registration has no external Secret Manager side effect, so its
-- idempotency transition can remain a compact, single-transaction reserve /
-- complete pair instead of widening the Hosted credential functions.
CREATE FUNCTION reserve_local_agent_idempotency(
    p_owner_user_id TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_ttl_seconds INTEGER
)
RETURNS TABLE (
    disposition TEXT,
    resource_id TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $reserve_local_agent_idempotency$
DECLARE
    v_record public.arena_api_idempotency%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_active_count BIGINT;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128
       OR p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_ttl_seconds IS NULL
       OR p_ttl_seconds NOT BETWEEN 60 AND 86400 THEN
        RAISE EXCEPTION 'invalid Local Agent idempotency reservation'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM public.connector_users AS u
    WHERE u.user_id = p_owner_user_id
      AND u.disabled_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Local Agent idempotency owner is not eligible'
            USING ERRCODE = 'P0002';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = 'local_agents.create'
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF FOUND THEN
        IF v_record.expires_at <= v_now THEN
            UPDATE public.arena_api_idempotency AS i
            SET request_digest = p_request_digest,
                state = 'reserved',
                resource_kind = NULL,
                resource_id = NULL,
                safe_response = NULL,
                created_at = v_now,
                expires_at = (
                    v_now + p_ttl_seconds * interval '1 second'
                ),
                completed_at = NULL
            WHERE i.owner_user_id = p_owner_user_id
              AND i.route_key = 'local_agents.create'
              AND i.key_digest = p_key_digest;
            RETURN QUERY SELECT 'reserved'::TEXT, NULL::TEXT;
        ELSIF v_record.request_digest <> p_request_digest THEN
            RETURN QUERY SELECT 'conflict'::TEXT, NULL::TEXT;
        ELSIF v_record.state = 'completed' THEN
            RETURN QUERY SELECT 'replay'::TEXT, v_record.resource_id;
        ELSE
            RETURN QUERY SELECT 'in_progress'::TEXT, v_record.resource_id;
        END IF;
        RETURN;
    END IF;

    DELETE FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.expires_at <= v_now;
    SELECT count(*)
    INTO v_active_count
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.expires_at > v_now;
    IF v_active_count >= 256 THEN
        RAISE EXCEPTION 'Local Agent idempotency record limit exceeded'
            USING ERRCODE = '54000';
    END IF;

    INSERT INTO public.arena_api_idempotency (
        owner_user_id,
        route_key,
        key_digest,
        request_digest,
        state,
        created_at,
        expires_at
    )
    VALUES (
        p_owner_user_id,
        'local_agents.create',
        p_key_digest,
        p_request_digest,
        'reserved',
        v_now,
        v_now + p_ttl_seconds * interval '1 second'
    );

    RETURN QUERY SELECT 'reserved'::TEXT, NULL::TEXT;
END
$reserve_local_agent_idempotency$;

CREATE FUNCTION complete_local_agent_idempotency(
    p_owner_user_id TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_agent_id TEXT
)
RETURNS TABLE (
    disposition TEXT,
    resource_id TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_local_agent_idempotency$
DECLARE
    v_record public.arena_api_idempotency%ROWTYPE;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128
       OR p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_agent_id IS NULL
       OR char_length(p_agent_id) NOT BETWEEN 1 AND 128
       OR p_agent_id !~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$' THEN
        RAISE EXCEPTION 'invalid Local Agent idempotency completion'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM public.arena_agents AS a
    JOIN public.arena_runtime_bindings AS b
      ON b.agent_id = a.agent_id
     AND b.runtime_kind = 'connector'
     AND b.disabled_at IS NULL
    WHERE a.agent_id = p_agent_id
      AND a.owner_user_id = p_owner_user_id
      AND a.status = 'active'
      AND b.route_status = 'ready';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Local Agent idempotency resource not found'
            USING ERRCODE = 'P0002';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = 'local_agents.create'
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF NOT FOUND
       OR v_record.request_digest <> p_request_digest
       OR (
           v_record.resource_id IS NOT NULL
           AND v_record.resource_id <> p_agent_id
       ) THEN
        RETURN QUERY SELECT 'conflict'::TEXT, NULL::TEXT;
        RETURN;
    END IF;
    IF v_record.state = 'completed' THEN
        RETURN QUERY SELECT 'replay'::TEXT, v_record.resource_id;
        RETURN;
    END IF;

    UPDATE public.arena_api_idempotency AS i
    SET state = 'completed',
        resource_kind = 'arena_agent',
        resource_id = p_agent_id,
        safe_response = pg_catalog.jsonb_build_object(
            'httpStatus', 201,
            'projectionVersion', 'arena-api-response.v1'
        ),
        completed_at = clock_timestamp()
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = 'local_agents.create'
      AND i.key_digest = p_key_digest;

    RETURN QUERY SELECT 'completed'::TEXT, p_agent_id;
END
$complete_local_agent_idempotency$;

CREATE UNIQUE INDEX arena_runtime_bindings_connector_route_unique_idx
    ON arena_runtime_bindings (
        connector_binding_id,
        connector_binding_epoch
    )
    WHERE runtime_kind = 'connector';

-- Adopt routes provisioned by the earlier compatibility bridge once the
-- Connector explicitly advertises the actual top-level managed-task
-- capabilities. arena.decide/arena.negotiate remain typed payload kinds
-- inside task.dispatch rather than inventory capability names.
UPDATE arena_runtime_bindings AS route
SET route_status = 'ready',
    updated_at = clock_timestamp()
FROM connector_bindings AS binding
JOIN connector_devices AS device
  ON device.device_id = binding.device_id
JOIN connector_runtimes AS runtime
  ON runtime.device_id = binding.device_id
 AND runtime.runtime_id = binding.runtime_id
WHERE route.runtime_kind = 'connector'
  AND route.disabled_at IS NULL
  AND route.connector_binding_id = binding.binding_id
  AND route.connector_binding_epoch =
      CASE
          WHEN (binding.record ->> 'binding_epoch') ~ '^[1-9][0-9]*$'
          THEN (binding.record ->> 'binding_epoch')::BIGINT
          ELSE NULL
      END
  AND device.revoked_at IS NULL
  AND runtime.available = TRUE
  AND NULLIF(
      btrim(binding.record ->> 'working_directory'),
      ''
  ) IS NOT NULL
  AND (runtime.record -> 'capabilities') ? 'session.start'
  AND (runtime.record -> 'capabilities') ? 'task.dispatch';

-- This function exposes only the minimum non-secret Connector route required
-- by Arena. It proves owner scope without copying Session or Device authority.
-- Later dispatch revalidates the current binding epoch and fails closed if the
-- Connector route changed after this snapshot was read.
CREATE FUNCTION resolve_connector_binding_for_arena(
    p_owner_user_id TEXT,
    p_connector_binding_id TEXT
)
RETURNS TABLE (
    binding_id TEXT,
    agent_id TEXT,
    binding_epoch BIGINT,
    working_directory TEXT
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $resolve_connector_binding$
    SELECT
        b.binding_id,
        b.agent_id,
        (b.record ->> 'binding_epoch')::BIGINT,
        b.record ->> 'working_directory'
    FROM public.connector_bindings AS b
    JOIN public.connector_devices AS d
      ON d.device_id = b.device_id
    JOIN public.connector_runtimes AS r
      ON r.device_id = b.device_id
     AND r.runtime_id = b.runtime_id
    WHERE d.owner_id = p_owner_user_id
      AND b.binding_id = p_connector_binding_id
      AND d.revoked_at IS NULL
      AND r.available = TRUE
      AND (r.record -> 'capabilities') ? 'session.start'
      AND (r.record -> 'capabilities') ? 'task.dispatch'
      AND (b.record ->> 'binding_epoch') ~ '^[1-9][0-9]*$'
      AND NULLIF(btrim(b.record ->> 'working_directory'), '') IS NOT NULL
$resolve_connector_binding$;

ALTER FUNCTION resolve_connector_binding_for_arena(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION reserve_local_agent_idempotency(TEXT, TEXT, TEXT, INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION complete_local_agent_idempotency(TEXT, TEXT, TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION resolve_connector_binding_for_arena(TEXT, TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION reserve_local_agent_idempotency(
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_local_agent_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC;
GRANT SELECT ON
    connector_bindings,
    connector_devices,
    connector_runtimes
TO adx_arena_function_owner;
GRANT EXECUTE ON FUNCTION resolve_connector_binding_for_arena(TEXT, TEXT)
    TO adx_arena_api, adx_arena_core;
GRANT EXECUTE ON FUNCTION reserve_local_agent_idempotency(
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) TO adx_arena_api;
GRANT EXECUTE ON FUNCTION complete_local_agent_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO adx_arena_api;

COMMIT;
