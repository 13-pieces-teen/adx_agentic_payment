BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Runtime v2 compares a small public strategy catalog while keeping each
-- official identity and its private numeric variant persistent.
ALTER TABLE arena402.official_agent_pool
    ADD COLUMN strategy_archetype TEXT NOT NULL DEFAULT 'balanced'
        CHECK (
            strategy_archetype IN (
                'aggressive',
                'conservative',
                'balanced'
            )
        ),
    ADD COLUMN strategy_catalog_version TEXT NOT NULL
        DEFAULT 'arena.hosted-strategy.v1'
        CHECK (
            strategy_catalog_version <> ''
            AND char_length(strategy_catalog_version) <= 100
        );

UPDATE arena402.official_agent_pool
SET strategy_archetype = CASE ((priority - 1) % 10)
    WHEN 0 THEN 'conservative'
    WHEN 1 THEN 'aggressive'
    WHEN 2 THEN 'conservative'
    WHEN 3 THEN 'aggressive'
    WHEN 4 THEN 'balanced'
    WHEN 5 THEN 'conservative'
    WHEN 6 THEN 'aggressive'
    WHEN 7 THEN 'balanced'
    WHEN 8 THEN 'balanced'
    ELSE 'aggressive'
END,
strategy_catalog_version = 'arena.hosted-strategy.v1';

ALTER TABLE public.arena_agent_task_attempts
    ADD COLUMN agent_request_count INTEGER CHECK (
        agent_request_count IS NULL
        OR agent_request_count BETWEEN 0 AND 64
    ),
    ADD COLUMN agent_tool_call_count INTEGER CHECK (
        agent_tool_call_count IS NULL
        OR agent_tool_call_count BETWEEN 0 AND 256
    );

CREATE TABLE public.hosted_agent_strategy_revisions (
    strategy_revision_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL
        REFERENCES public.arena_agents(agent_id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    archetype TEXT NOT NULL CHECK (
        archetype IN (
            'aggressive',
            'conservative',
            'balanced',
            'custom'
        )
    ),
    catalog_version TEXT NOT NULL CHECK (
        catalog_version <> ''
        AND char_length(catalog_version) <= 100
    ),
    instructions TEXT NOT NULL CHECK (
        char_length(instructions) <= 4000
    ),
    source_config_hash TEXT NOT NULL CHECK (
        source_config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    source TEXT NOT NULL CHECK (
        source IN ('preset', 'owner', 'learned')
    ),
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'active', 'superseded', 'rejected')
    ),
    parent_strategy_revision_id TEXT
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id
        ) ON DELETE RESTRICT,
    evidence_summary JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(evidence_summary) = 'object'
        AND octet_length(evidence_summary::text) <= 16384
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    activated_at TIMESTAMPTZ,
    UNIQUE (agent_id, revision_no),
    UNIQUE (agent_id, source_config_hash),
    UNIQUE (strategy_revision_id, agent_id),
    CHECK (
        (status = 'active' AND activated_at IS NOT NULL)
        OR status <> 'active'
    )
);

CREATE UNIQUE INDEX hosted_agent_strategy_one_active_idx
    ON public.hosted_agent_strategy_revisions (agent_id)
    WHERE status = 'active';

CREATE INDEX hosted_agent_strategy_history_idx
    ON public.hosted_agent_strategy_revisions (
        agent_id,
        revision_no DESC
    );

INSERT INTO public.hosted_agent_strategy_revisions (
    strategy_revision_id,
    agent_id,
    revision_no,
    archetype,
    catalog_version,
    instructions,
    source_config_hash,
    source,
    status,
    activated_at
)
SELECT
    'strategy:' || md5(hosted.agent_id || ':' || hosted.config_hash),
    hosted.agent_id,
    1,
    COALESCE(official.strategy_archetype, 'custom'),
    COALESCE(
        official.strategy_catalog_version,
        'arena.hosted-strategy.v1'
    ),
    hosted.strategy_instructions,
    hosted.config_hash,
    CASE WHEN official.agent_id IS NULL THEN 'owner' ELSE 'preset' END,
    'active',
    clock_timestamp()
FROM public.arena_hosted_configs AS hosted
LEFT JOIN arena402.official_agent_pool AS official
  ON official.agent_id = hosted.agent_id
ON CONFLICT (agent_id, source_config_hash) DO NOTHING;

ALTER TABLE public.game_agents
    ADD COLUMN hosted_strategy_revision_id TEXT
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id
        ) ON DELETE RESTRICT;

UPDATE public.game_agents AS game_agent
SET hosted_strategy_revision_id = strategy.strategy_revision_id
FROM public.arena_runtime_bindings AS binding
JOIN public.hosted_agent_strategy_revisions AS strategy
  ON strategy.agent_id = binding.agent_id
 AND strategy.status = 'active'
WHERE binding.runtime_binding_id = game_agent.runtime_binding_id
  AND binding.runtime_kind = 'hosted'
  AND game_agent.hosted_strategy_revision_id IS NULL;

CREATE INDEX game_agents_hosted_strategy_idx
    ON public.game_agents (hosted_strategy_revision_id)
    WHERE hosted_strategy_revision_id IS NOT NULL;

CREATE TABLE public.hosted_agent_game_memory (
    game_agent_id TEXT PRIMARY KEY
        REFERENCES public.game_agents(game_agent_id) ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    strategy_revision_id TEXT NOT NULL,
    memory_version BIGINT NOT NULL DEFAULT 0 CHECK (memory_version >= 0),
    state JSONB NOT NULL DEFAULT
        '{"schemaVersion":"arena.hosted-game-memory.v1"}'::jsonb CHECK (
            jsonb_typeof(state) = 'object'
            AND octet_length(state::text) <= 32768
        ),
    last_applied_task_id TEXT
        REFERENCES public.arena_agent_tasks(task_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (game_agent_id, game_id)
        REFERENCES public.game_agents(game_agent_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (strategy_revision_id, agent_id)
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id,
            agent_id
        ) ON DELETE RESTRICT
);

CREATE INDEX hosted_agent_game_memory_agent_history_idx
    ON public.hosted_agent_game_memory (agent_id, created_at DESC);

INSERT INTO public.hosted_agent_game_memory (
    game_agent_id,
    game_id,
    agent_id,
    strategy_revision_id
)
SELECT
    game_agent.game_agent_id,
    game_agent.game_id,
    game_agent.agent_id,
    game_agent.hosted_strategy_revision_id
FROM public.game_agents AS game_agent
WHERE game_agent.hosted_strategy_revision_id IS NOT NULL
ON CONFLICT (game_agent_id) DO NOTHING;

CREATE TABLE public.hosted_agent_memory_patches (
    task_id TEXT PRIMARY KEY
        REFERENCES public.arena_agent_tasks(task_id) ON DELETE CASCADE,
    game_agent_id TEXT NOT NULL
        REFERENCES public.hosted_agent_game_memory(game_agent_id)
        ON DELETE CASCADE,
    runtime_result_id_digest TEXT NOT NULL CHECK (
        runtime_result_id_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    expected_memory_version BIGINT NOT NULL CHECK (
        expected_memory_version >= 0
    ),
    decision_summary JSONB NOT NULL CHECK (
        jsonb_typeof(decision_summary) = 'object'
        AND octet_length(decision_summary::text) <= 8192
    ),
    memory_patch JSONB NOT NULL CHECK (
        jsonb_typeof(memory_patch) = 'object'
        AND octet_length(memory_patch::text) <= 8192
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'applied', 'discarded', 'stale')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CHECK (
        (status = 'pending' AND completed_at IS NULL)
        OR (status <> 'pending' AND completed_at IS NOT NULL)
    )
);

CREATE INDEX hosted_agent_memory_patch_projection_idx
    ON public.hosted_agent_memory_patches (status, created_at)
    WHERE status = 'pending';

CREATE OR REPLACE FUNCTION public.sync_hosted_agent_strategy_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $sync_hosted_strategy$
DECLARE
    v_previous_revision_id TEXT;
    v_revision_no INTEGER;
    v_archetype TEXT;
    v_catalog_version TEXT;
    v_source TEXT;
    v_strategy_revision_id TEXT;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.config_hash = OLD.config_hash THEN
        RETURN NEW;
    END IF;

    SELECT strategy_revision_id
    INTO v_previous_revision_id
    FROM public.hosted_agent_strategy_revisions
    WHERE agent_id = NEW.agent_id
      AND status = 'active'
    FOR UPDATE;

    SELECT
        COALESCE(official.strategy_archetype, 'custom'),
        COALESCE(
            official.strategy_catalog_version,
            'arena.hosted-strategy.v1'
        ),
        CASE WHEN official.agent_id IS NULL THEN 'owner' ELSE 'preset' END
    INTO v_archetype, v_catalog_version, v_source
    FROM (SELECT 1) AS singleton
    LEFT JOIN arena402.official_agent_pool AS official
      ON official.agent_id = NEW.agent_id;

    SELECT COALESCE(max(revision_no), 0) + 1
    INTO v_revision_no
    FROM public.hosted_agent_strategy_revisions
    WHERE agent_id = NEW.agent_id;

    UPDATE public.hosted_agent_strategy_revisions
    SET status = 'superseded'
    WHERE agent_id = NEW.agent_id
      AND status = 'active';

    v_strategy_revision_id :=
        'strategy:' || md5(NEW.agent_id || ':' || NEW.config_hash);

    INSERT INTO public.hosted_agent_strategy_revisions (
        strategy_revision_id,
        agent_id,
        revision_no,
        archetype,
        catalog_version,
        instructions,
        source_config_hash,
        source,
        status,
        parent_strategy_revision_id,
        activated_at
    )
    VALUES (
        v_strategy_revision_id,
        NEW.agent_id,
        v_revision_no,
        v_archetype,
        v_catalog_version,
        NEW.strategy_instructions,
        NEW.config_hash,
        v_source,
        'active',
        v_previous_revision_id,
        clock_timestamp()
    )
    ON CONFLICT (agent_id, source_config_hash) DO UPDATE
    SET archetype = EXCLUDED.archetype,
        catalog_version = EXCLUDED.catalog_version,
        instructions = EXCLUDED.instructions,
        source = EXCLUDED.source,
        status = 'active',
        parent_strategy_revision_id = EXCLUDED.parent_strategy_revision_id,
        activated_at = EXCLUDED.activated_at;

    RETURN NEW;
END
$sync_hosted_strategy$;

CREATE TRIGGER arena_hosted_configs_strategy_revision
AFTER INSERT OR UPDATE OF config_hash, strategy_instructions
ON public.arena_hosted_configs
FOR EACH ROW
EXECUTE FUNCTION public.sync_hosted_agent_strategy_revision();

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

CREATE OR REPLACE FUNCTION public.stage_hosted_agent_memory_patch(
    p_task_id TEXT,
    p_worker_id TEXT,
    p_runtime_result_id_digest TEXT,
    p_expected_memory_version BIGINT,
    p_decision_summary JSONB,
    p_memory_patch JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $stage_hosted_memory$
DECLARE
    v_game_agent_id TEXT;
    v_matches BOOLEAN;
BEGIN
    IF p_runtime_result_id_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_expected_memory_version < 0
       OR jsonb_typeof(p_decision_summary) <> 'object'
       OR jsonb_typeof(p_memory_patch) <> 'object'
       OR octet_length(p_decision_summary::text) > 8192
       OR octet_length(p_memory_patch::text) > 8192 THEN
        RAISE EXCEPTION 'invalid hosted memory patch'
            USING ERRCODE = '22023';
    END IF;

    SELECT task.game_agent_id
    INTO v_game_agent_id
    FROM public.arena_agent_tasks AS task
    WHERE task.task_id = p_task_id
      AND task.status IN ('leased', 'running')
      AND task.leased_by = p_worker_id
      AND task.lease_expires_at > clock_timestamp()
    FOR SHARE;

    IF v_game_agent_id IS NULL THEN
        RAISE EXCEPTION 'hosted task lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    INSERT INTO public.hosted_agent_memory_patches (
        task_id,
        game_agent_id,
        runtime_result_id_digest,
        expected_memory_version,
        decision_summary,
        memory_patch
    )
    VALUES (
        p_task_id,
        v_game_agent_id,
        p_runtime_result_id_digest,
        p_expected_memory_version,
        p_decision_summary,
        p_memory_patch
    )
    ON CONFLICT (task_id) DO NOTHING;

    SELECT EXISTS (
        SELECT 1
        FROM public.hosted_agent_memory_patches AS patch
        WHERE patch.task_id = p_task_id
          AND patch.game_agent_id = v_game_agent_id
          AND patch.runtime_result_id_digest =
              p_runtime_result_id_digest
          AND patch.expected_memory_version =
              p_expected_memory_version
          AND patch.decision_summary = p_decision_summary
          AND patch.memory_patch = p_memory_patch
    )
    INTO v_matches;

    IF NOT v_matches THEN
        RAISE EXCEPTION 'conflicting hosted memory patch'
            USING ERRCODE = '23505';
    END IF;
    RETURN TRUE;
END
$stage_hosted_memory$;

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
            patch.runtime_result_id_digest,
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
        WHERE patch.status = 'pending'
          AND (
              result.apply_status IN ('applied', 'rejected')
              OR task.status IN ('defaulted', 'cancelled')
          )
        ORDER BY patch.created_at, patch.task_id
        FOR UPDATE OF patch SKIP LOCKED
        LIMIT p_limit
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
$project_hosted_memory$;

CREATE OR REPLACE FUNCTION public.complete_pydantic_agent_task_attempt(
    p_attempt_id TEXT,
    p_worker_id TEXT,
    p_status TEXT,
    p_duration_ms BIGINT,
    p_actual_model TEXT,
    p_input_tokens BIGINT,
    p_output_tokens BIGINT,
    p_cached_tokens BIGINT,
    p_reasoning_tokens BIGINT,
    p_usage_complete BOOLEAN,
    p_provider_request_id_ref TEXT,
    p_error_class TEXT,
    p_agent_request_count INTEGER,
    p_agent_tool_call_count INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_pydantic_attempt$
DECLARE
    v_completed BOOLEAN;
BEGIN
    IF p_agent_request_count NOT BETWEEN 0 AND 64
       OR p_agent_tool_call_count NOT BETWEEN 0 AND 256 THEN
        RAISE EXCEPTION 'invalid Pydantic Agent step counts'
            USING ERRCODE = '22023';
    END IF;

    SELECT public.complete_agent_task_attempt(
        p_attempt_id,
        p_worker_id,
        p_status,
        p_duration_ms,
        p_actual_model,
        p_input_tokens,
        p_output_tokens,
        p_cached_tokens,
        p_reasoning_tokens,
        p_usage_complete,
        p_provider_request_id_ref,
        p_error_class
    )
    INTO v_completed;

    IF NOT v_completed THEN
        RETURN FALSE;
    END IF;

    UPDATE public.arena_agent_task_attempts
    SET agent_request_count = p_agent_request_count,
        agent_tool_call_count = p_agent_tool_call_count
    WHERE attempt_id = p_attempt_id
      AND worker_id = p_worker_id;
    RETURN FOUND;
END
$complete_pydantic_attempt$;

RESET ROLE;

ALTER TABLE public.hosted_agent_strategy_revisions
    OWNER TO adx_arena_migration;
ALTER TABLE public.hosted_agent_game_memory
    OWNER TO adx_arena_migration;
ALTER TABLE public.hosted_agent_memory_patches
    OWNER TO adx_arena_migration;

ALTER FUNCTION public.sync_hosted_agent_strategy_revision()
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.load_hosted_agent_runtime_context(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.stage_hosted_agent_memory_patch(
    TEXT,
    TEXT,
    TEXT,
    BIGINT,
    JSONB,
    JSONB
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.project_hosted_agent_memory_patches(INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.complete_pydantic_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    BIGINT,
    TEXT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BOOLEAN,
    TEXT,
    TEXT,
    INTEGER,
    INTEGER
) OWNER TO adx_arena_function_owner;

REVOKE ALL ON
    public.hosted_agent_strategy_revisions,
    public.hosted_agent_game_memory,
    public.hosted_agent_memory_patches
FROM PUBLIC;

REVOKE ALL ON FUNCTION
    public.load_hosted_agent_runtime_context(TEXT, TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.stage_hosted_agent_memory_patch(
        TEXT,
        TEXT,
        TEXT,
        BIGINT,
        JSONB,
        JSONB
    )
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.project_hosted_agent_memory_patches(INTEGER)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.complete_pydantic_agent_task_attempt(
        TEXT,
        TEXT,
        TEXT,
        BIGINT,
        TEXT,
        BIGINT,
        BIGINT,
        BIGINT,
        BIGINT,
        BOOLEAN,
        TEXT,
        TEXT,
        INTEGER,
        INTEGER
    )
FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON
    public.hosted_agent_strategy_revisions,
    public.hosted_agent_game_memory,
    public.hosted_agent_memory_patches
TO adx_arena_function_owner;
GRANT SELECT ON public.arena_hosted_configs
    TO adx_arena_function_owner;
GRANT SELECT ON arena402.official_agent_pool
    TO adx_arena_function_owner;

GRANT SELECT ON public.hosted_agent_strategy_revisions
    TO adx_arena_core, adx_arena_api;
GRANT SELECT, INSERT ON public.hosted_agent_game_memory
    TO adx_arena_core, adx_arena_api;

GRANT EXECUTE ON FUNCTION
    public.load_hosted_agent_runtime_context(TEXT, TEXT)
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.stage_hosted_agent_memory_patch(
        TEXT,
        TEXT,
        TEXT,
        BIGINT,
        JSONB,
        JSONB
    )
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.project_hosted_agent_memory_patches(INTEGER)
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.complete_pydantic_agent_task_attempt(
        TEXT,
        TEXT,
        TEXT,
        BIGINT,
        TEXT,
        BIGINT,
        BIGINT,
        BIGINT,
        BIGINT,
        BOOLEAN,
        TEXT,
        TEXT,
        INTEGER,
        INTEGER
    )
TO adx_hosted_worker;

COMMIT;
