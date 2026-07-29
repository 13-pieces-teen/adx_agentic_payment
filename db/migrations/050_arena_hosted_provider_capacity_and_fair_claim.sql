-- Apply cross-replica Provider admission control and fair per-Game claims.
--
-- Leased/running AgentTasks are the durable in-flight records. Serializing
-- the short claim transaction and counting those records prevents multiple
-- Worker replicas from independently admitting a full local concurrency
-- window against the same upstream Provider.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE TABLE hosted_provider_capacity (
    provider TEXT PRIMARY KEY,
    max_inflight INTEGER NOT NULL CHECK (
        max_inflight BETWEEN 1 AND 1000
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO hosted_provider_capacity (provider, max_inflight)
VALUES
    ('deepseek', 32),
    ('openai-compatible', 16),
    ('arena-scripted', 100)
ON CONFLICT (provider) DO NOTHING;

RESET ROLE;

REVOKE ALL ON hosted_provider_capacity FROM PUBLIC;
GRANT SELECT ON hosted_provider_capacity TO adx_arena_function_owner;

ALTER FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

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

    -- Claims are short. This lock makes capacity calculation and leasing one
    -- global admission decision without holding a lock during Provider I/O.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('arena402:hosted-provider-admission', 0)
    );

    RETURN QUERY
    WITH inflight AS MATERIALIZED (
        SELECT
            credential.provider,
            count(*)::INTEGER AS task_count
        FROM public.arena_agent_tasks AS task
        JOIN public.arena_model_credentials AS credential
          ON credential.credential_id = task.credential_id
        WHERE task.status IN ('leased', 'running')
          AND task.lease_expires_at > v_now
        GROUP BY credential.provider
    ),
    eligible AS MATERIALIZED (
        SELECT
            task.task_id,
            task.game_id,
            credential.provider,
            task.deadline_at,
            task.created_at,
            row_number() OVER (
                PARTITION BY task.game_id
                ORDER BY task.deadline_at, task.created_at, task.task_id
            ) AS game_rank
        FROM public.arena_agent_tasks AS task
        JOIN public.arena_runtime_bindings AS binding
          ON binding.runtime_binding_id = task.runtime_binding_id
        JOIN public.arena_model_credentials AS credential
          ON credential.credential_id = task.credential_id
        JOIN public.hosted_provider_capacity AS capacity
          ON capacity.provider = credential.provider
        WHERE task.deadline_at > v_now
          AND binding.runtime_kind = 'hosted'
          AND binding.route_status = 'ready'
          AND binding.disabled_at IS NULL
          AND credential.status = 'valid'
          AND (
              task.status = 'queued'
              OR (
                  task.status IN ('leased', 'running')
                  AND task.lease_expires_at <= v_now
              )
          )
    ),
    provider_ranked AS MATERIALIZED (
        SELECT
            eligible.*,
            row_number() OVER (
                PARTITION BY eligible.provider
                ORDER BY
                    eligible.game_rank,
                    eligible.deadline_at,
                    eligible.created_at,
                    eligible.task_id
            ) AS provider_rank
        FROM eligible
    ),
    admitted AS MATERIALIZED (
        SELECT ranked.task_id
        FROM provider_ranked AS ranked
        JOIN public.hosted_provider_capacity AS capacity
          ON capacity.provider = ranked.provider
        LEFT JOIN inflight
          ON inflight.provider = ranked.provider
        WHERE ranked.provider_rank <=
            capacity.max_inflight - COALESCE(inflight.task_count, 0)
        ORDER BY
            ranked.game_rank,
            ranked.deadline_at,
            ranked.created_at,
            ranked.task_id
        LIMIT p_limit
    ),
    candidates AS MATERIALIZED (
        SELECT task.task_id
        FROM public.arena_agent_tasks AS task
        JOIN admitted ON admitted.task_id = task.task_id
        FOR UPDATE OF task SKIP LOCKED
    ),
    updated AS (
        UPDATE public.arena_agent_tasks AS task
        SET status = 'leased',
            leased_by = p_worker_id,
            lease_expires_at = v_now
                + pg_catalog.make_interval(secs => p_lease_seconds)
        FROM candidates
        WHERE task.task_id = candidates.task_id
        RETURNING task.task_id
    )
    SELECT execution.*
    FROM public.arena_hosted_task_execution_v AS execution
    JOIN updated ON updated.task_id = execution.task_id
    ORDER BY execution.deadline_at, execution.task_id;
END
$claim_hosted_tasks_v2$;

RESET ROLE;

ALTER FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_hosted_agent_tasks_v2(
    TEXT,
    INTEGER,
    INTEGER
) TO adx_hosted_worker;

COMMIT;
