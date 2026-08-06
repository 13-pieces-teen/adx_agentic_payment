BEGIN;

-- Arena can consume an invalid Runtime candidate by applying a deterministic
-- default action.  arena_agent_task_results.apply_status is therefore
-- "applied" for both the original candidate and a default_pass outcome.
-- Hosted memory must only learn from the former.
ALTER FUNCTION public.project_hosted_agent_memory_patches(INTEGER)
    OWNER TO adx_arena_migration;
ALTER FUNCTION public.project_hosted_agent_memory_for_context(TEXT)
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

CREATE OR REPLACE FUNCTION public.project_hosted_agent_memory_patches(
    p_limit INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $project_hosted_memory$
DECLARE
    v_patch RECORD;
    v_projected INTEGER := 0;
    v_changed INTEGER;
BEGIN
    IF p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid hosted memory projection limit'
            USING ERRCODE = '22023';
    END IF;

    FOR v_patch IN
        SELECT
            patch.task_id,
            patch.game_agent_id,
            patch.expected_memory_version,
            patch.decision_summary,
            patch.memory_patch,
            task.status AS task_status,
            result.apply_status,
            application.application_outcome
        FROM public.hosted_agent_memory_patches AS patch
        JOIN public.arena_agent_tasks AS task
          ON task.task_id = patch.task_id
        LEFT JOIN public.arena_agent_task_results AS result
          ON result.task_id = patch.task_id
         AND result.runtime_result_id_digest =
             patch.runtime_result_id_digest
        LEFT JOIN public.arena_applied_agent_actions AS application
          ON application.result_id = result.result_id
        WHERE patch.status = 'pending'
          AND (
              result.apply_status IN ('applied', 'rejected')
              OR task.status IN ('defaulted', 'cancelled')
          )
        ORDER BY patch.created_at, patch.task_id
        FOR UPDATE OF patch SKIP LOCKED
        LIMIT p_limit
    LOOP
        IF v_patch.apply_status = 'applied'
           AND v_patch.application_outcome = 'candidate' THEN
            UPDATE public.hosted_agent_game_memory
            SET memory_version = memory_version + 1,
                state = jsonb_build_object(
                    'schemaVersion',
                    'arena.hosted-game-memory.v1',
                    'latestPatch',
                    v_patch.memory_patch,
                    'lastDecisionSummary',
                    v_patch.decision_summary
                ),
                last_applied_task_id = v_patch.task_id,
                updated_at = clock_timestamp()
            WHERE game_agent_id = v_patch.game_agent_id
              AND memory_version = v_patch.expected_memory_version;
            GET DIAGNOSTICS v_changed = ROW_COUNT;

            UPDATE public.hosted_agent_memory_patches
            SET status = CASE
                    WHEN v_changed = 1 THEN 'applied'
                    ELSE 'stale'
                END,
                completed_at = clock_timestamp()
            WHERE task_id = v_patch.task_id;
        ELSE
            UPDATE public.hosted_agent_memory_patches
            SET status = 'discarded',
                completed_at = clock_timestamp()
            WHERE task_id = v_patch.task_id;
        END IF;
        v_projected := v_projected + 1;
    END LOOP;

    RETURN v_projected;
END
$project_hosted_memory$;

CREATE OR REPLACE FUNCTION public.project_hosted_agent_memory_for_context(
    p_game_agent_id TEXT
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $project_hosted_memory_for_context$
DECLARE
    v_patch RECORD;
    v_projected INTEGER := 0;
    v_changed INTEGER;
BEGIN
    IF p_game_agent_id IS NULL OR p_game_agent_id = '' THEN
        RAISE EXCEPTION 'invalid hosted Game Agent id'
            USING ERRCODE = '22023';
    END IF;

    FOR v_patch IN
        SELECT
            patch.task_id,
            patch.game_agent_id,
            patch.expected_memory_version,
            patch.decision_summary,
            patch.memory_patch,
            task.status AS task_status,
            result.apply_status,
            application.application_outcome
        FROM public.hosted_agent_memory_patches AS patch
        JOIN public.arena_agent_tasks AS task
          ON task.task_id = patch.task_id
        LEFT JOIN public.arena_agent_task_results AS result
          ON result.task_id = patch.task_id
         AND result.runtime_result_id_digest =
             patch.runtime_result_id_digest
        LEFT JOIN public.arena_applied_agent_actions AS application
          ON application.result_id = result.result_id
        WHERE patch.game_agent_id = p_game_agent_id
          AND patch.status = 'pending'
          AND (
              result.apply_status IN ('applied', 'rejected')
              OR task.status IN ('defaulted', 'cancelled')
          )
        ORDER BY patch.created_at, patch.task_id
        FOR UPDATE OF patch
    LOOP
        IF v_patch.apply_status = 'applied'
           AND v_patch.application_outcome = 'candidate' THEN
            UPDATE public.hosted_agent_game_memory
            SET memory_version = memory_version + 1,
                state = jsonb_build_object(
                    'schemaVersion',
                    'arena.hosted-game-memory.v1',
                    'latestPatch',
                    v_patch.memory_patch,
                    'lastDecisionSummary',
                    v_patch.decision_summary
                ),
                last_applied_task_id = v_patch.task_id,
                updated_at = clock_timestamp()
            WHERE game_agent_id = v_patch.game_agent_id
              AND memory_version = v_patch.expected_memory_version;
            GET DIAGNOSTICS v_changed = ROW_COUNT;

            UPDATE public.hosted_agent_memory_patches
            SET status = CASE
                    WHEN v_changed = 1 THEN 'applied'
                    ELSE 'stale'
                END,
                completed_at = clock_timestamp()
            WHERE task_id = v_patch.task_id;
        ELSE
            UPDATE public.hosted_agent_memory_patches
            SET status = 'discarded',
                completed_at = clock_timestamp()
            WHERE task_id = v_patch.task_id;
        END IF;
        v_projected := v_projected + 1;
    END LOOP;

    RETURN v_projected;
END
$project_hosted_memory_for_context$;

RESET ROLE;

ALTER FUNCTION public.project_hosted_agent_memory_patches(INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.project_hosted_agent_memory_for_context(TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION
    public.project_hosted_agent_memory_patches(INTEGER)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.project_hosted_agent_memory_for_context(TEXT)
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    public.project_hosted_agent_memory_patches(INTEGER)
TO adx_hosted_worker;

COMMIT;
