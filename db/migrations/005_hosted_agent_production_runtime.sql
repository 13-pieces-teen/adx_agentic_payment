BEGIN;

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'adx_connector_gateway'
    ) THEN
        CREATE ROLE adx_connector_gateway NOLOGIN;
    END IF;
END
$roles$;

GRANT USAGE ON SCHEMA public TO
    adx_connector_gateway,
    adx_arena_api,
    adx_arena_core,
    adx_hosted_worker,
    adx_credential_controller;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    connector_users,
    connector_invites,
    connector_sessions,
    connector_pairings,
    connector_devices,
    connector_runtimes,
    connector_bindings,
    connector_commands,
    connector_events,
    connector_audit
TO adx_connector_gateway;

-- Production Hosted control-plane replay lookup.
--
-- The application service must check a completed create replay before it
-- revalidates mutable Credential/Provider capability state. Calling
-- reserve_arena_api_idempotency for that lookup would create a reservation as
-- a side effect when the key is new, so production uses this read-only,
-- owner-scoped SECURITY DEFINER function instead.
CREATE OR REPLACE FUNCTION lookup_completed_arena_api_idempotency(
    p_owner_user_id TEXT,
    p_route_key TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT
)
RETURNS TABLE (
    disposition TEXT,
    resource_kind TEXT,
    resource_id TEXT,
    http_status INTEGER
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $lookup_completed_arena_api_idempotency$
DECLARE
    v_record public.arena_api_idempotency%ROWTYPE;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128
       OR p_route_key IS NULL
       OR p_route_key NOT IN (
           'model_credentials.create',
           'model_credentials.replace',
           'model_credentials.revoke',
           'model_credentials.revalidate',
           'hosted_agents.create',
           'hosted_agents.update',
           'hosted_agents.disable',
           'game_participants.create'
       )
       OR p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid API idempotency lookup'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
    FROM public.connector_users AS u
    WHERE u.user_id = p_owner_user_id
      AND u.disabled_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'API idempotency owner is not eligible'
            USING ERRCODE = 'P0002';
    END IF;

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
      AND i.expires_at > clock_timestamp();

    IF NOT FOUND OR v_record.state <> 'completed' THEN
        RETURN QUERY
        SELECT
            'not_found'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::INTEGER;
        RETURN;
    END IF;

    IF v_record.request_digest <> p_request_digest THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::INTEGER;
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        'replay'::TEXT,
        v_record.resource_kind,
        v_record.resource_id,
        (v_record.safe_response ->> 'httpStatus')::INTEGER;
END
$lookup_completed_arena_api_idempotency$;

ALTER FUNCTION lookup_completed_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION lookup_completed_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION lookup_completed_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO adx_arena_api;

-- Joining a Game is allowed only while it is open. The API role has SELECT
-- but deliberately no UPDATE privilege on Game state, so the authoritative
-- row lock and status check live in this definer trigger. It serializes an
-- Arena Core open->running transition against the final participant insert.
CREATE OR REPLACE FUNCTION enforce_game_agent_open_join()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $enforce_game_agent_open_join$
DECLARE
    v_game_status TEXT;
BEGIN
    SELECT game.status
    INTO v_game_status
    FROM public.games AS game
    WHERE game.game_id = NEW.game_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'game not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_game_status <> 'open' THEN
        RAISE EXCEPTION 'game is not open' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$enforce_game_agent_open_join$;

ALTER FUNCTION enforce_game_agent_open_join()
    OWNER TO adx_arena_migration;
REVOKE ALL ON FUNCTION enforce_game_agent_open_join() FROM PUBLIC;

CREATE TRIGGER game_agents_require_open_game
BEFORE INSERT ON game_agents
FOR EACH ROW
EXECUTE FUNCTION enforce_game_agent_open_join();

-- Reclaim both never-started leases and expired running executions. The
-- follow-up recovery function classifies the last durable Attempt before a
-- Worker is allowed to issue another model request.
CREATE OR REPLACE FUNCTION claim_hosted_agent_tasks_v2(
    p_worker_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS SETOF arena_hosted_task_execution_v
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $claim_hosted_tasks_v2$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_worker_id IS NULL OR p_worker_id = ''
       OR char_length(p_worker_id) > 200
       OR p_limit NOT BETWEEN 1 AND 50
       OR p_lease_seconds NOT BETWEEN 1 AND 600 THEN
        RAISE EXCEPTION 'invalid hosted task claim'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT t.task_id
        FROM public.arena_agent_tasks AS t
        JOIN public.arena_runtime_bindings AS b
          ON b.runtime_binding_id = t.runtime_binding_id
        JOIN public.arena_model_credentials AS c
          ON c.credential_id = t.credential_id
        WHERE t.deadline_at > v_now
          AND b.runtime_kind = 'hosted'
          AND b.route_status = 'ready'
          AND b.disabled_at IS NULL
          AND c.status = 'valid'
          AND (
              t.status = 'queued'
              OR (
                  t.status IN ('leased', 'running')
                  AND t.lease_expires_at <= v_now
              )
          )
        ORDER BY t.deadline_at, t.created_at, t.task_id
        FOR UPDATE OF t SKIP LOCKED
        LIMIT p_limit
    ),
    updated AS (
        UPDATE public.arena_agent_tasks AS t
        SET status = 'leased',
            leased_by = p_worker_id,
            lease_expires_at = v_now
                + pg_catalog.make_interval(secs => p_lease_seconds)
        FROM candidates AS c
        WHERE t.task_id = c.task_id
        RETURNING t.task_id
    )
    SELECT execution.*
    FROM public.arena_hosted_task_execution_v AS execution
    JOIN updated AS u ON u.task_id = execution.task_id
    ORDER BY execution.deadline_at, execution.task_id;
END
$claim_hosted_tasks_v2$;

CREATE OR REPLACE FUNCTION prepare_reclaimed_hosted_task(
    p_task_id TEXT,
    p_worker_id TEXT
)
RETURNS TABLE (
    disposition TEXT,
    next_attempt_no SMALLINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $prepare_reclaimed_hosted_task$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_task public.arena_agent_tasks%ROWTYPE;
    v_attempt public.arena_agent_task_attempts%ROWTYPE;
BEGIN
    SELECT *
    INTO v_task
    FROM public.arena_agent_tasks
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND
       OR v_task.status <> 'leased'
       OR v_task.leased_by IS DISTINCT FROM p_worker_id
       OR v_task.lease_expires_at <= v_now THEN
        RAISE EXCEPTION 'hosted task lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF v_task.attempt_count = 0 THEN
        RETURN QUERY SELECT 'execute'::TEXT, 1::SMALLINT;
        RETURN;
    END IF;

    SELECT *
    INTO v_attempt
    FROM public.arena_agent_task_attempts
    WHERE task_id = p_task_id
    ORDER BY attempt_no DESC
    LIMIT 1
    FOR UPDATE;

    IF NOT FOUND THEN
        UPDATE public.arena_agent_tasks
        SET status = 'running'
        WHERE task_id = p_task_id;
        RETURN QUERY SELECT 'terminal_unknown'::TEXT, NULL::SMALLINT;
        RETURN;
    END IF;

    IF v_attempt.status = 'created' THEN
        UPDATE public.arena_agent_task_attempts
        SET status = 'failed',
            runtime_completed_at = v_now,
            duration_ms = 0,
            usage_complete = FALSE,
            error_class = 'interrupted_before_send'
        WHERE attempt_id = v_attempt.attempt_id;
    ELSIF v_attempt.status = 'request_sent' THEN
        UPDATE public.arena_agent_task_attempts
        SET status = 'unknown',
            runtime_completed_at = v_now,
            duration_ms = GREATEST(
                0,
                floor(
                    EXTRACT(
                        EPOCH FROM (v_now - v_attempt.request_sent_at)
                    ) * 1000
                )::BIGINT
            ),
            usage_complete = FALSE,
            error_class = 'request_outcome_unknown'
        WHERE attempt_id = v_attempt.attempt_id;
        UPDATE public.arena_agent_tasks
        SET status = 'running'
        WHERE task_id = p_task_id;
        RETURN QUERY SELECT 'terminal_unknown'::TEXT, NULL::SMALLINT;
        RETURN;
    ELSIF v_attempt.status IN ('succeeded', 'unknown') THEN
        UPDATE public.arena_agent_tasks
        SET status = 'running'
        WHERE task_id = p_task_id;
        RETURN QUERY SELECT 'terminal_unknown'::TEXT, NULL::SMALLINT;
        RETURN;
    END IF;

    IF v_task.attempt_count < 2 THEN
        RETURN QUERY
        SELECT
            'execute'::TEXT,
            (v_task.attempt_count + 1)::SMALLINT;
        RETURN;
    END IF;

    UPDATE public.arena_agent_tasks
    SET status = 'running'
    WHERE task_id = p_task_id;
    RETURN QUERY SELECT 'terminal_failed'::TEXT, NULL::SMALLINT;
END
$prepare_reclaimed_hosted_task$;

ALTER FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION prepare_reclaimed_hosted_task(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION prepare_reclaimed_hosted_task(TEXT, TEXT)
    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION prepare_reclaimed_hosted_task(TEXT, TEXT)
    TO adx_hosted_worker;

COMMIT;
