-- Finalize a bounded batch of expired AgentTasks in one database call.
--
-- The authoritative single-task transition remains
-- finalize_expired_agent_task(TEXT).  This wrapper only moves candidate
-- selection and result collection next to that transition so an API worker
-- does not perform two network round trips per expired task.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE OR REPLACE FUNCTION finalize_expired_agent_tasks_batch(
    p_limit INTEGER
)
RETURNS SETOF public.arena_agent_task_results
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $finalize_expired_tasks_batch$
DECLARE
    v_task_id TEXT;
BEGIN
    IF p_limit <= 0 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'finalizer batch limit must be between 1 and 1000'
            USING ERRCODE = '22023';
    END IF;

    FOR v_task_id IN
        SELECT task.task_id
        FROM public.arena_agent_tasks AS task
        WHERE task.status IN ('queued', 'leased', 'running')
          AND task.deadline_at <= clock_timestamp()
        ORDER BY task.deadline_at, task.task_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    LOOP
        IF public.finalize_expired_agent_task(v_task_id) THEN
            RETURN QUERY
            SELECT result.*
            FROM public.arena_agent_task_results AS result
            WHERE result.task_id = v_task_id;
        END IF;
    END LOOP;
END
$finalize_expired_tasks_batch$;

RESET ROLE;

ALTER FUNCTION finalize_expired_agent_tasks_batch(INTEGER)
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION finalize_expired_agent_tasks_batch(INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finalize_expired_agent_tasks_batch(INTEGER)
    TO adx_arena_core;

COMMIT;
