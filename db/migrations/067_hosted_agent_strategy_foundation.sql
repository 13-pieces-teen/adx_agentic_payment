BEGIN;

SET LOCAL ROLE adx_arena_migration;

CREATE OR REPLACE FUNCTION public.load_hosted_agent_learning_evidence_v2(
    p_learning_job_id TEXT,
    p_worker_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $load_hosted_learning_evidence_v2$
DECLARE
    v_payload JSONB;
    v_foundation_instructions TEXT;
BEGIN
    v_payload := public.load_hosted_agent_learning_evidence(
        p_learning_job_id,
        p_worker_id
    );

    WITH RECURSIVE strategy_lineage (
        strategy_revision_id,
        parent_strategy_revision_id,
        source,
        instructions,
        depth
    ) AS (
        SELECT
            strategy.strategy_revision_id,
            strategy.parent_strategy_revision_id,
            strategy.source,
            strategy.instructions,
            0
        FROM public.hosted_agent_learning_jobs AS job
        JOIN public.hosted_agent_strategy_revisions AS strategy
          ON strategy.strategy_revision_id =
             job.base_strategy_revision_id
         AND strategy.agent_id = job.agent_id
        WHERE job.learning_job_id = p_learning_job_id
          AND job.status = 'leased'
          AND job.leased_by = p_worker_id
          AND job.lease_expires_at > clock_timestamp()

        UNION ALL

        SELECT
            parent.strategy_revision_id,
            parent.parent_strategy_revision_id,
            parent.source,
            parent.instructions,
            lineage.depth + 1
        FROM strategy_lineage AS lineage
        JOIN public.hosted_agent_strategy_revisions AS parent
          ON parent.strategy_revision_id =
             lineage.parent_strategy_revision_id
        WHERE lineage.source = 'learned'
          AND lineage.depth < 100
    )
    SELECT lineage.instructions
    INTO v_foundation_instructions
    FROM strategy_lineage AS lineage
    WHERE lineage.source <> 'learned'
    ORDER BY lineage.depth
    LIMIT 1;

    IF v_foundation_instructions IS NULL
       OR v_foundation_instructions = '' THEN
        RAISE EXCEPTION 'hosted strategy foundation is missing'
            USING ERRCODE = '55000';
    END IF;

    RETURN jsonb_set(
        v_payload,
        '{schemaVersion}',
        to_jsonb('arena.hosted-learning-evidence.v2'::text),
        FALSE
    ) || jsonb_build_object(
        'baseStrategyInstructions',
        v_foundation_instructions
    );
END
$load_hosted_learning_evidence_v2$;

RESET ROLE;

ALTER FUNCTION public.load_hosted_agent_learning_evidence_v2(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION
    public.load_hosted_agent_learning_evidence_v2(TEXT, TEXT)
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    public.load_hosted_agent_learning_evidence_v2(TEXT, TEXT)
TO adx_hosted_worker;

COMMIT;
