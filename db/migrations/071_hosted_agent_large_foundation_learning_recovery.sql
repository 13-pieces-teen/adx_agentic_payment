BEGIN;

-- The v2 learner initially rendered both the recovered official foundation and
-- a duplicate catalog preset. Foundations above 3000 characters could exceed
-- the 4000-character strategy revision bound after a valid model proposal.
-- Requeue only jobs that reached that exact internal-failure shape and never
-- created a candidate revision.
SET LOCAL ROLE adx_arena_migration;

WITH RECURSIVE strategy_lineage (
    learning_job_id,
    strategy_revision_id,
    parent_strategy_revision_id,
    source,
    instructions,
    depth
) AS (
    SELECT
        job.learning_job_id,
        strategy.strategy_revision_id,
        strategy.parent_strategy_revision_id,
        strategy.source,
        strategy.instructions,
        0
    FROM public.hosted_agent_learning_jobs AS job
    JOIN public.hosted_agent_strategy_revisions AS strategy
      ON strategy.strategy_revision_id = job.base_strategy_revision_id
     AND strategy.agent_id = job.agent_id
    JOIN arena402.games AS game
      ON game.game_id = job.game_id
    WHERE job.status = 'failed'
      AND job.error_class = 'internal_learning_failure'
      AND job.candidate_strategy_revision_id IS NULL
      AND game.phase = 'completed'

    UNION ALL

    SELECT
        lineage.learning_job_id,
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
),
foundation AS (
    SELECT DISTINCT ON (lineage.learning_job_id)
        lineage.learning_job_id,
        lineage.source,
        lineage.instructions
    FROM strategy_lineage AS lineage
    WHERE lineage.source <> 'learned'
    ORDER BY lineage.learning_job_id, lineage.depth
),
recoverable_job AS (
    SELECT foundation.learning_job_id
    FROM foundation
    JOIN public.hosted_agent_learning_jobs AS job
      ON job.learning_job_id = foundation.learning_job_id
    WHERE foundation.source <> 'learned'
      AND char_length(foundation.instructions) > 3000
      AND job.candidate_strategy_revision_id IS NULL
)
UPDATE public.hosted_agent_learning_jobs AS job
SET status = 'pending',
    attempt_count = 0,
    not_before = clock_timestamp(),
    leased_by = NULL,
    lease_expires_at = NULL,
    evidence_hash = NULL,
    proposal = NULL,
    gate_summary = NULL,
    candidate_strategy_revision_id = NULL,
    error_class = NULL,
    completed_at = NULL
FROM recoverable_job AS recoverable
WHERE job.learning_job_id = recoverable.learning_job_id;

RESET ROLE;

COMMIT;
