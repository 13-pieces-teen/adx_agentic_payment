BEGIN;

ALTER FUNCTION public.load_hosted_agent_runtime_context(TEXT, TEXT)
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

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
            result.apply_status
        FROM public.hosted_agent_memory_patches AS patch
        JOIN public.arena_agent_tasks AS task
          ON task.task_id = patch.task_id
        LEFT JOIN public.arena_agent_task_results AS result
          ON result.task_id = patch.task_id
         AND result.runtime_result_id_digest =
             patch.runtime_result_id_digest
        WHERE patch.game_agent_id = p_game_agent_id
          AND patch.status = 'pending'
          AND (
              result.apply_status IN ('applied', 'rejected')
              OR task.status IN ('defaulted', 'cancelled')
          )
        ORDER BY patch.created_at, patch.task_id
        FOR UPDATE OF patch
    LOOP
        IF v_patch.apply_status = 'applied' THEN
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

CREATE OR REPLACE FUNCTION public.load_hosted_agent_runtime_context(
    p_task_id TEXT,
    p_worker_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $load_hosted_context$
DECLARE
    v_task public.arena_agent_tasks%ROWTYPE;
    v_game_agent public.game_agents%ROWTYPE;
    v_strategy public.hosted_agent_strategy_revisions%ROWTYPE;
    v_memory public.hosted_agent_game_memory%ROWTYPE;
BEGIN
    SELECT *
    INTO v_task
    FROM public.arena_agent_tasks
    WHERE task_id = p_task_id
      AND status IN ('leased', 'running')
      AND leased_by = p_worker_id
      AND lease_expires_at > clock_timestamp()
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'hosted task lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO v_game_agent
    FROM public.game_agents
    WHERE game_agent_id = v_task.game_agent_id;

    IF v_game_agent.hosted_strategy_revision_id IS NULL THEN
        RAISE EXCEPTION 'hosted strategy revision is missing'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO STRICT v_strategy
    FROM public.hosted_agent_strategy_revisions
    WHERE strategy_revision_id =
        v_game_agent.hosted_strategy_revision_id
      AND agent_id = v_game_agent.agent_id;

    INSERT INTO public.hosted_agent_game_memory (
        game_agent_id,
        game_id,
        agent_id,
        strategy_revision_id
    )
    VALUES (
        v_game_agent.game_agent_id,
        v_game_agent.game_id,
        v_game_agent.agent_id,
        v_strategy.strategy_revision_id
    )
    ON CONFLICT (game_agent_id) DO NOTHING;

    -- A later round may already be claimable immediately after Arena applies
    -- the previous result. Fold every terminal patch for this logical Agent
    -- before freezing the next PydanticAI context so the new patch cannot be
    -- based on an obsolete memory_version.
    PERFORM public.project_hosted_agent_memory_for_context(
        v_game_agent.game_agent_id
    );

    SELECT *
    INTO STRICT v_memory
    FROM public.hosted_agent_game_memory
    WHERE game_agent_id = v_game_agent.game_agent_id;

    RETURN jsonb_build_object(
        'agentId', v_game_agent.agent_id,
        'gameAgentId', v_game_agent.game_agent_id,
        'strategyRevisionId', v_strategy.strategy_revision_id,
        'strategyRevisionNo', v_strategy.revision_no,
        'strategyArchetype', v_strategy.archetype,
        'strategyCatalogVersion', v_strategy.catalog_version,
        'strategyInstructions', v_strategy.instructions,
        'memoryVersion', v_memory.memory_version,
        'memoryState', v_memory.state
    );
END
$load_hosted_context$;

RESET ROLE;

ALTER FUNCTION public.project_hosted_agent_memory_for_context(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.load_hosted_agent_runtime_context(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION
    public.project_hosted_agent_memory_for_context(TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.load_hosted_agent_runtime_context(TEXT, TEXT)
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    public.load_hosted_agent_runtime_context(TEXT, TEXT)
TO adx_hosted_worker;

COMMIT;
