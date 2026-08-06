BEGIN;

ALTER FUNCTION public.sync_hosted_agent_strategy_revision()
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

ALTER TABLE public.hosted_agent_strategy_revisions
    ADD COLUMN policy_profile JSONB NOT NULL DEFAULT
        '{
            "riskBudgetBps": 5000,
            "minExpectedEdgeBps": 900,
            "maxInventoryConcentrationBps": 7500,
            "negotiationConcessionBps": 1200,
            "explorationBps": 1200
        }'::jsonb
        CHECK (
            jsonb_typeof(policy_profile) = 'object'
            AND octet_length(policy_profile::text) <= 4096
        );

UPDATE public.hosted_agent_strategy_revisions
SET policy_profile = CASE archetype
    WHEN 'aggressive' THEN
        '{
            "riskBudgetBps": 7000,
            "minExpectedEdgeBps": 400,
            "maxInventoryConcentrationBps": 8500,
            "negotiationConcessionBps": 1800,
            "explorationBps": 2500
        }'::jsonb
    WHEN 'conservative' THEN
        '{
            "riskBudgetBps": 3500,
            "minExpectedEdgeBps": 1500,
            "maxInventoryConcentrationBps": 6000,
            "negotiationConcessionBps": 700,
            "explorationBps": 500
        }'::jsonb
    WHEN 'balanced' THEN
        '{
            "riskBudgetBps": 5000,
            "minExpectedEdgeBps": 900,
            "maxInventoryConcentrationBps": 7500,
            "negotiationConcessionBps": 1200,
            "explorationBps": 1200
        }'::jsonb
    ELSE
        '{
            "riskBudgetBps": 5000,
            "minExpectedEdgeBps": 1000,
            "maxInventoryConcentrationBps": 7500,
            "negotiationConcessionBps": 1000,
            "explorationBps": 1000
        }'::jsonb
END;

CREATE OR REPLACE FUNCTION public.default_hosted_agent_policy_profile(
    p_archetype TEXT
)
RETURNS JSONB
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $default_hosted_policy$
    SELECT CASE p_archetype
        WHEN 'aggressive' THEN
            '{
                "riskBudgetBps": 7000,
                "minExpectedEdgeBps": 400,
                "maxInventoryConcentrationBps": 8500,
                "negotiationConcessionBps": 1800,
                "explorationBps": 2500
            }'::jsonb
        WHEN 'conservative' THEN
            '{
                "riskBudgetBps": 3500,
                "minExpectedEdgeBps": 1500,
                "maxInventoryConcentrationBps": 6000,
                "negotiationConcessionBps": 700,
                "explorationBps": 500
            }'::jsonb
        WHEN 'balanced' THEN
            '{
                "riskBudgetBps": 5000,
                "minExpectedEdgeBps": 900,
                "maxInventoryConcentrationBps": 7500,
                "negotiationConcessionBps": 1200,
                "explorationBps": 1200
            }'::jsonb
        ELSE
            '{
                "riskBudgetBps": 5000,
                "minExpectedEdgeBps": 1000,
                "maxInventoryConcentrationBps": 7500,
                "negotiationConcessionBps": 1000,
                "explorationBps": 1000
            }'::jsonb
    END
$default_hosted_policy$;

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
        activated_at,
        policy_profile
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
        clock_timestamp(),
        public.default_hosted_agent_policy_profile(v_archetype)
    )
    ON CONFLICT (agent_id, source_config_hash) DO UPDATE
    SET archetype = EXCLUDED.archetype,
        catalog_version = EXCLUDED.catalog_version,
        instructions = EXCLUDED.instructions,
        source = EXCLUDED.source,
        status = 'active',
        parent_strategy_revision_id = EXCLUDED.parent_strategy_revision_id,
        activated_at = EXCLUDED.activated_at,
        policy_profile = EXCLUDED.policy_profile;

    RETURN NEW;
END
$sync_hosted_strategy$;

CREATE TABLE public.hosted_agent_learning_jobs (
    learning_job_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL
        REFERENCES public.games(game_id) ON DELETE CASCADE,
    game_agent_id TEXT NOT NULL UNIQUE
        REFERENCES public.game_agents(game_agent_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL
        REFERENCES public.arena_agents(agent_id) ON DELETE CASCADE,
    base_strategy_revision_id TEXT NOT NULL
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id
        ) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'leased',
            'activated',
            'rejected',
            'rolled_back',
            'superseded',
            'failed'
        )
    ),
    attempt_count SMALLINT NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 2
    ),
    not_before TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    leased_by TEXT CHECK (
        leased_by IS NULL OR char_length(leased_by) <= 200
    ),
    lease_expires_at TIMESTAMPTZ,
    evidence_hash TEXT CHECK (
        evidence_hash IS NULL
        OR evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    proposal JSONB CHECK (
        proposal IS NULL
        OR (
            jsonb_typeof(proposal) = 'object'
            AND octet_length(proposal::text) <= 16384
        )
    ),
    gate_summary JSONB CHECK (
        gate_summary IS NULL
        OR (
            jsonb_typeof(gate_summary) = 'object'
            AND octet_length(gate_summary::text) <= 16384
        )
    ),
    candidate_strategy_revision_id TEXT
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id
        ) ON DELETE RESTRICT,
    error_class TEXT CHECK (
        error_class IS NULL OR char_length(error_class) <= 100
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (game_agent_id, game_id)
        REFERENCES public.game_agents(game_agent_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (base_strategy_revision_id, agent_id)
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id,
            agent_id
        ) ON DELETE RESTRICT,
    CHECK (
        (
            status = 'pending'
            AND leased_by IS NULL
            AND lease_expires_at IS NULL
            AND completed_at IS NULL
        )
        OR (
            status = 'leased'
            AND leased_by IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND completed_at IS NULL
        )
        OR (
            status IN (
                'activated',
                'rejected',
                'rolled_back',
                'superseded',
                'failed'
            )
            AND leased_by IS NULL
            AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL
        )
    )
);

CREATE INDEX hosted_agent_learning_claim_idx
    ON public.hosted_agent_learning_jobs (
        status,
        not_before,
        created_at,
        learning_job_id
    )
    WHERE status IN ('pending', 'leased');

CREATE INDEX hosted_agent_learning_agent_history_idx
    ON public.hosted_agent_learning_jobs (
        agent_id,
        created_at DESC
    );

CREATE TABLE public.hosted_agent_strategy_evaluations (
    strategy_evaluation_id TEXT PRIMARY KEY,
    learning_job_id TEXT NOT NULL UNIQUE
        REFERENCES public.hosted_agent_learning_jobs(learning_job_id)
        ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    game_agent_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    strategy_revision_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL CHECK (
        evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    outcome_score_bps INTEGER NOT NULL CHECK (
        outcome_score_bps BETWEEN -10000 AND 10000
    ),
    evidence_summary JSONB NOT NULL CHECK (
        jsonb_typeof(evidence_summary) = 'object'
        AND octet_length(evidence_summary::text) <= 16384
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (game_agent_id, game_id)
        REFERENCES public.game_agents(game_agent_id, game_id)
        ON DELETE CASCADE,
    FOREIGN KEY (strategy_revision_id, agent_id)
        REFERENCES public.hosted_agent_strategy_revisions(
            strategy_revision_id,
            agent_id
        ) ON DELETE RESTRICT
);

CREATE INDEX hosted_agent_strategy_evaluation_history_idx
    ON public.hosted_agent_strategy_evaluations (
        strategy_revision_id,
        created_at DESC
    );

CREATE OR REPLACE FUNCTION public.enqueue_hosted_agent_learning_jobs()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $enqueue_hosted_learning$
BEGIN
    IF NEW.status <> 'completed'
       OR (TG_OP = 'UPDATE' AND OLD.status = 'completed') THEN
        RETURN NEW;
    END IF;

    INSERT INTO public.hosted_agent_learning_jobs (
        learning_job_id,
        game_id,
        game_agent_id,
        agent_id,
        base_strategy_revision_id
    )
    SELECT
        'learning:' || md5(
            game_agent.game_id || ':' || game_agent.game_agent_id
        ),
        game_agent.game_id,
        game_agent.game_agent_id,
        game_agent.agent_id,
        game_agent.hosted_strategy_revision_id
    FROM public.game_agents AS game_agent
    JOIN public.arena_runtime_bindings AS binding
      ON binding.runtime_binding_id = game_agent.runtime_binding_id
     AND binding.runtime_kind = 'hosted'
    WHERE game_agent.game_id = NEW.game_id
      AND game_agent.hosted_strategy_revision_id IS NOT NULL
    ON CONFLICT (game_agent_id) DO NOTHING;

    RETURN NEW;
END
$enqueue_hosted_learning$;

CREATE TRIGGER arena_game_completed_hosted_learning
AFTER INSERT OR UPDATE OF status
ON public.games
FOR EACH ROW
EXECUTE FUNCTION public.enqueue_hosted_agent_learning_jobs();

INSERT INTO public.hosted_agent_learning_jobs (
    learning_job_id,
    game_id,
    game_agent_id,
    agent_id,
    base_strategy_revision_id
)
SELECT
    'learning:' || md5(
        game_agent.game_id || ':' || game_agent.game_agent_id
    ),
    game_agent.game_id,
    game_agent.game_agent_id,
    game_agent.agent_id,
    game_agent.hosted_strategy_revision_id
FROM public.game_agents AS game_agent
JOIN public.games AS game
  ON game.game_id = game_agent.game_id
 AND game.status = 'completed'
JOIN public.arena_runtime_bindings AS binding
  ON binding.runtime_binding_id = game_agent.runtime_binding_id
 AND binding.runtime_kind = 'hosted'
WHERE game_agent.hosted_strategy_revision_id IS NOT NULL
ON CONFLICT (game_agent_id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.claim_hosted_agent_learning_jobs(
    p_worker_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS TABLE (
    learning_job_id TEXT,
    game_id TEXT,
    game_agent_id TEXT,
    agent_id TEXT,
    base_strategy_revision_id TEXT,
    provider TEXT,
    model TEXT,
    thinking_enabled BOOLEAN,
    max_output_tokens INTEGER,
    secret_ref TEXT,
    attempt_count SMALLINT,
    lease_expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $claim_hosted_learning$
BEGIN
    IF p_worker_id IS NULL OR p_worker_id = ''
       OR p_limit NOT BETWEEN 1 AND 8
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'invalid hosted learning claim'
            USING ERRCODE = '22023';
    END IF;

    UPDATE public.hosted_agent_learning_jobs AS expired_job
    SET status = 'failed',
        error_class = 'learning_attempts_exhausted',
        leased_by = NULL,
        lease_expires_at = NULL,
        completed_at = clock_timestamp()
    WHERE expired_job.status = 'leased'
      AND expired_job.lease_expires_at <= clock_timestamp()
      AND expired_job.attempt_count >= 2;

    RETURN QUERY
    WITH candidates AS (
        SELECT
            job.learning_job_id,
            COALESCE(
                game_agent.config_snapshot ->> 'provider_id',
                game_agent.config_snapshot ->> 'provider'
            ) AS provider,
            COALESCE(
                game_agent.config_snapshot ->> 'model_id',
                game_agent.config_snapshot ->> 'model'
            ) AS model,
            COALESCE(
                (
                    game_agent.config_snapshot
                    ->> 'thinking_enabled'
                )::boolean,
                FALSE
            ) AS thinking_enabled,
            COALESCE(
                (
                    game_agent.config_snapshot
                    ->> 'max_output_tokens'
                )::integer,
                2048
            ) AS max_output_tokens,
            credential.secret_ref
        FROM public.hosted_agent_learning_jobs AS job
        JOIN public.games AS game
          ON game.game_id = job.game_id
         AND game.status = 'completed'
        JOIN public.game_agents AS game_agent
          ON game_agent.game_agent_id = job.game_agent_id
        JOIN public.arena_model_credentials AS credential
          ON credential.credential_id =
             game_agent.config_snapshot ->> 'credential_id'
         AND credential.status = 'valid'
        WHERE (
                (
                    job.status = 'pending'
                    AND job.not_before <= clock_timestamp()
                )
                OR (
                    job.status = 'leased'
                    AND job.lease_expires_at <= clock_timestamp()
                )
              )
          AND job.attempt_count < 2
        ORDER BY job.created_at, job.learning_job_id
        FOR UPDATE OF job SKIP LOCKED
        LIMIT p_limit
    ),
    claimed AS (
        UPDATE public.hosted_agent_learning_jobs AS job
        SET status = 'leased',
            attempt_count = job.attempt_count + 1,
            leased_by = p_worker_id,
            lease_expires_at =
                clock_timestamp()
                + make_interval(secs => p_lease_seconds),
            error_class = NULL
        FROM candidates
        WHERE job.learning_job_id = candidates.learning_job_id
          AND job.attempt_count < 2
        RETURNING
            job.learning_job_id,
            job.game_id,
            job.game_agent_id,
            job.agent_id,
            job.base_strategy_revision_id,
            job.attempt_count,
            job.lease_expires_at
    )
    SELECT
        claimed.learning_job_id,
        claimed.game_id,
        claimed.game_agent_id,
        claimed.agent_id,
        claimed.base_strategy_revision_id,
        candidates.provider,
        candidates.model,
        candidates.thinking_enabled,
        candidates.max_output_tokens,
        candidates.secret_ref,
        claimed.attempt_count,
        claimed.lease_expires_at
    FROM claimed
    JOIN candidates
      ON candidates.learning_job_id = claimed.learning_job_id;
END
$claim_hosted_learning$;

CREATE OR REPLACE FUNCTION public.load_hosted_agent_learning_evidence(
    p_learning_job_id TEXT,
    p_worker_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, arena402
AS $load_hosted_learning_evidence$
DECLARE
    v_job public.hosted_agent_learning_jobs%ROWTYPE;
    v_strategy public.hosted_agent_strategy_revisions%ROWTYPE;
    v_rank INTEGER;
    v_participant_count INTEGER;
    v_net_worth NUMERIC;
    v_average_net_worth NUMERIC;
    v_outcome_score INTEGER;
    v_behavior JSONB;
    v_final_prices JSONB;
    v_memory JSONB;
BEGIN
    SELECT *
    INTO v_job
    FROM public.hosted_agent_learning_jobs
    WHERE learning_job_id = p_learning_job_id
      AND status = 'leased'
      AND leased_by = p_worker_id
      AND lease_expires_at > clock_timestamp()
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'hosted learning lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO STRICT v_strategy
    FROM public.hosted_agent_strategy_revisions
    WHERE strategy_revision_id = v_job.base_strategy_revision_id
      AND agent_id = v_job.agent_id;

    SELECT
        ranking.rank,
        totals.participant_count,
        ranking.net_worth_atomic,
        totals.average_net_worth
    INTO
        v_rank,
        v_participant_count,
        v_net_worth,
        v_average_net_worth
    FROM arena402.rankings AS ranking
    CROSS JOIN LATERAL (
        SELECT
            count(*)::integer AS participant_count,
            avg(all_rankings.net_worth_atomic) AS average_net_worth
        FROM arena402.rankings AS all_rankings
        WHERE all_rankings.game_id = v_job.game_id
    ) AS totals
    WHERE ranking.game_id = v_job.game_id
      AND ranking.game_participant_id = v_job.game_agent_id;

    IF v_rank IS NULL
       OR v_participant_count IS NULL
       OR v_participant_count < 2
       OR v_average_net_worth IS NULL THEN
        RAISE EXCEPTION 'hosted learning ranking evidence is incomplete'
            USING ERRCODE = '55000';
    END IF;

    IF v_average_net_worth <= 0 THEN
        RAISE EXCEPTION 'hosted learning average net worth is invalid'
            USING ERRCODE = '55000';
    END IF;

    v_outcome_score := GREATEST(
        -10000,
        LEAST(
            10000,
            round(
                (
                    (v_net_worth - v_average_net_worth)
                    * 10000
                )
                / v_average_net_worth
            )::integer
        )
    );

    SELECT COALESCE(
        jsonb_object_agg(
            final_price.good_id,
            final_price.price_atomic::text
            ORDER BY final_price.good_id
        ),
        '{}'::jsonb
    )
    INTO v_final_prices
    FROM arena402.final_settlement_prices AS final_price
    WHERE final_price.game_id = v_job.game_id;

    SELECT jsonb_build_object(
        'taskCount',
        (
            SELECT count(*)::integer
            FROM public.arena_agent_tasks AS task
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
        ),
        'candidateActionCount',
        (
            SELECT count(*)::integer
            FROM public.arena_applied_agent_actions AS action
            WHERE action.game_id = v_job.game_id
              AND action.game_agent_id = v_job.game_agent_id
              AND action.application_outcome = 'candidate'
        ),
        'defaultedTaskCount',
        (
            SELECT count(*)::integer
            FROM public.arena_agent_tasks AS task
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
              AND task.status = 'defaulted'
        ),
        'rejectedResultCount',
        (
            SELECT count(*)::integer
            FROM public.arena_agent_tasks AS task
            JOIN public.arena_agent_task_results AS result
              ON result.task_id = task.task_id
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
              AND result.apply_status = 'rejected'
        ),
        'settledTradeCount',
        (
            SELECT count(*)::integer
            FROM arena402.pairings AS pairing
            WHERE pairing.game_id = v_job.game_id
              AND pairing.status = 'settled'
              AND v_job.game_agent_id IN (
                  pairing.buyer_participant_id,
                  pairing.seller_participant_id
              )
        ),
        'settlementFailureCount',
        (
            SELECT count(*)::integer
            FROM arena402.pairings AS pairing
            WHERE pairing.game_id = v_job.game_id
              AND pairing.status = 'settlement_failed'
              AND v_job.game_agent_id IN (
                  pairing.buyer_participant_id,
                  pairing.seller_participant_id
              )
        ),
        'appliedActionCounts',
        COALESCE(
            (
                SELECT jsonb_object_agg(
                    grouped.action_name,
                    grouped.action_count
                    ORDER BY grouped.action_name
                )
                FROM (
                    SELECT
                        action.applied_action ->> 'action'
                            AS action_name,
                        count(*)::integer AS action_count
                    FROM public.arena_applied_agent_actions AS action
                    WHERE action.game_id = v_job.game_id
                      AND action.game_agent_id = v_job.game_agent_id
                      AND action.applied_action IS NOT NULL
                    GROUP BY action.applied_action ->> 'action'
                ) AS grouped
            ),
            '{}'::jsonb
        ),
        'inputTokens',
        (
            SELECT COALESCE(sum(attempt.input_tokens), 0)::bigint
            FROM public.arena_agent_tasks AS task
            JOIN public.arena_agent_task_attempts AS attempt
              ON attempt.task_id = task.task_id
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
        ),
        'outputTokens',
        (
            SELECT COALESCE(sum(attempt.output_tokens), 0)::bigint
            FROM public.arena_agent_tasks AS task
            JOIN public.arena_agent_task_attempts AS attempt
              ON attempt.task_id = task.task_id
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
        ),
        'reasoningTokens',
        (
            SELECT COALESCE(sum(attempt.reasoning_tokens), 0)::bigint
            FROM public.arena_agent_tasks AS task
            JOIN public.arena_agent_task_attempts AS attempt
              ON attempt.task_id = task.task_id
            WHERE task.game_id = v_job.game_id
              AND task.game_agent_id = v_job.game_agent_id
        )
    )
    INTO v_behavior;

    SELECT COALESCE(memory.state, '{}'::jsonb)
    INTO v_memory
    FROM public.hosted_agent_game_memory AS memory
    WHERE memory.game_agent_id = v_job.game_agent_id;

    RETURN jsonb_build_object(
        'schemaVersion',
        'arena.hosted-learning-evidence.v1',
        'learningJobId',
        v_job.learning_job_id,
        'gameId',
        v_job.game_id,
        'gameAgentId',
        v_job.game_agent_id,
        'agentId',
        v_job.agent_id,
        'baseStrategyRevisionId',
        v_strategy.strategy_revision_id,
        'baseStrategyRevisionNo',
        v_strategy.revision_no,
        'archetype',
        v_strategy.archetype,
        'catalogVersion',
        v_strategy.catalog_version,
        'basePolicyProfile',
        v_strategy.policy_profile,
        'outcome',
        jsonb_build_object(
            'rank',
            v_rank,
            'participantCount',
            v_participant_count,
            'netWorthAtomic',
            v_net_worth::text,
            'averageNetWorthAtomic',
            round(v_average_net_worth)::numeric::text,
            'outcomeScoreBps',
            v_outcome_score
        ),
        'behavior',
        v_behavior,
        'finalPricesAtomic',
        v_final_prices,
        'lastGameMemory',
        COALESCE(v_memory, '{}'::jsonb)
    );
END
$load_hosted_learning_evidence$;

CREATE OR REPLACE FUNCTION public.complete_hosted_agent_learning_job(
    p_learning_job_id TEXT,
    p_worker_id TEXT,
    p_evidence_hash TEXT,
    p_outcome_score_bps INTEGER,
    p_source_config_hash TEXT,
    p_policy_profile JSONB,
    p_instructions TEXT,
    p_proposal JSONB,
    p_gate_summary JSONB,
    p_gate_passed BOOLEAN,
    p_gate_reason TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_hosted_learning$
DECLARE
    v_job public.hosted_agent_learning_jobs%ROWTYPE;
    v_base public.hosted_agent_strategy_revisions%ROWTYPE;
    v_active_revision_id TEXT;
    v_revision_no INTEGER;
    v_candidate_revision_id TEXT;
    v_base_evaluation_count INTEGER;
    v_parent_evaluation_count INTEGER;
    v_base_average NUMERIC;
    v_parent_average NUMERIC;
BEGIN
    IF p_evidence_hash !~ '^sha256:[0-9a-f]{64}$'
       OR p_source_config_hash !~ '^sha256:[0-9a-f]{64}$'
       OR p_outcome_score_bps NOT BETWEEN -10000 AND 10000
       OR jsonb_typeof(p_policy_profile) <> 'object'
       OR octet_length(p_policy_profile::text) > 4096
       OR char_length(p_instructions) NOT BETWEEN 1 AND 4000
       OR jsonb_typeof(p_proposal) <> 'object'
       OR octet_length(p_proposal::text) > 16384
       OR jsonb_typeof(p_gate_summary) <> 'object'
       OR octet_length(p_gate_summary::text) > 16384
       OR p_gate_reason IS NULL
       OR char_length(p_gate_reason) > 100 THEN
        RAISE EXCEPTION 'invalid hosted learning completion'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_job
    FROM public.hosted_agent_learning_jobs
    WHERE learning_job_id = p_learning_job_id
      AND status = 'leased'
      AND leased_by = p_worker_id
      AND lease_expires_at > clock_timestamp()
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'hosted learning lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1
    FROM public.arena_agents
    WHERE agent_id = v_job.agent_id
    FOR UPDATE;

    SELECT *
    INTO STRICT v_base
    FROM public.hosted_agent_strategy_revisions
    WHERE strategy_revision_id = v_job.base_strategy_revision_id
      AND agent_id = v_job.agent_id;

    INSERT INTO public.hosted_agent_strategy_evaluations (
        strategy_evaluation_id,
        learning_job_id,
        game_id,
        game_agent_id,
        agent_id,
        strategy_revision_id,
        evidence_hash,
        outcome_score_bps,
        evidence_summary
    )
    VALUES (
        'evaluation:' || md5(v_job.learning_job_id),
        v_job.learning_job_id,
        v_job.game_id,
        v_job.game_agent_id,
        v_job.agent_id,
        v_job.base_strategy_revision_id,
        p_evidence_hash,
        p_outcome_score_bps,
        p_gate_summary
    )
    ON CONFLICT (learning_job_id) DO NOTHING;

    SELECT strategy_revision_id
    INTO v_active_revision_id
    FROM public.hosted_agent_strategy_revisions
    WHERE agent_id = v_job.agent_id
      AND status = 'active'
    FOR UPDATE;

    IF v_active_revision_id IS DISTINCT FROM
       v_job.base_strategy_revision_id THEN
        UPDATE public.hosted_agent_learning_jobs
        SET status = 'superseded',
            evidence_hash = p_evidence_hash,
            proposal = p_proposal,
            gate_summary = p_gate_summary,
            error_class = 'stale_base_strategy',
            leased_by = NULL,
            lease_expires_at = NULL,
            completed_at = clock_timestamp()
        WHERE learning_job_id = v_job.learning_job_id;
        RETURN jsonb_build_object(
            'disposition',
            'superseded',
            'strategyRevisionId',
            v_active_revision_id
        );
    END IF;

    IF v_base.source = 'learned'
       AND v_base.parent_strategy_revision_id IS NOT NULL THEN
        SELECT count(*)::integer, avg(outcome_score_bps)
        INTO v_base_evaluation_count, v_base_average
        FROM public.hosted_agent_strategy_evaluations
        WHERE strategy_revision_id = v_base.strategy_revision_id;

        SELECT count(*)::integer, avg(outcome_score_bps)
        INTO v_parent_evaluation_count, v_parent_average
        FROM public.hosted_agent_strategy_evaluations
        WHERE strategy_revision_id =
            v_base.parent_strategy_revision_id;

        IF v_base_evaluation_count >= 1
           AND v_parent_evaluation_count >= 1
           AND v_base_average <= v_parent_average - 2000 THEN
            UPDATE public.hosted_agent_strategy_revisions
            SET status = 'rejected'
            WHERE strategy_revision_id = v_base.strategy_revision_id;

            UPDATE public.hosted_agent_strategy_revisions
            SET status = 'active',
                activated_at = clock_timestamp()
            WHERE strategy_revision_id =
                v_base.parent_strategy_revision_id;

            UPDATE public.hosted_agent_learning_jobs
            SET status = 'rolled_back',
                evidence_hash = p_evidence_hash,
                proposal = p_proposal,
                gate_summary = p_gate_summary,
                error_class = 'automatic_regression_rollback',
                candidate_strategy_revision_id =
                    v_base.parent_strategy_revision_id,
                leased_by = NULL,
                lease_expires_at = NULL,
                completed_at = clock_timestamp()
            WHERE learning_job_id = v_job.learning_job_id;

            RETURN jsonb_build_object(
                'disposition',
                'rolled_back',
                'strategyRevisionId',
                v_base.parent_strategy_revision_id
            );
        END IF;
    END IF;

    SELECT COALESCE(max(revision_no), 0) + 1
    INTO v_revision_no
    FROM public.hosted_agent_strategy_revisions
    WHERE agent_id = v_job.agent_id;

    v_candidate_revision_id :=
        'strategy:learned:' || substr(p_source_config_hash, 8, 40);

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
        evidence_summary,
        activated_at,
        policy_profile
    )
    VALUES (
        v_candidate_revision_id,
        v_job.agent_id,
        v_revision_no,
        v_base.archetype,
        v_base.catalog_version,
        p_instructions,
        p_source_config_hash,
        'learned',
        CASE WHEN p_gate_passed THEN 'candidate' ELSE 'rejected' END,
        v_base.strategy_revision_id,
        p_gate_summary,
        NULL,
        p_policy_profile
    );

    IF NOT p_gate_passed THEN
        UPDATE public.hosted_agent_learning_jobs
        SET status = 'rejected',
            evidence_hash = p_evidence_hash,
            proposal = p_proposal,
            gate_summary = p_gate_summary,
            candidate_strategy_revision_id =
                v_candidate_revision_id,
            error_class = p_gate_reason,
            leased_by = NULL,
            lease_expires_at = NULL,
            completed_at = clock_timestamp()
        WHERE learning_job_id = v_job.learning_job_id;
        RETURN jsonb_build_object(
            'disposition',
            'rejected',
            'strategyRevisionId',
            v_candidate_revision_id
        );
    END IF;

    UPDATE public.hosted_agent_strategy_revisions
    SET status = 'superseded'
    WHERE strategy_revision_id = v_base.strategy_revision_id
      AND status = 'active';

    UPDATE public.hosted_agent_strategy_revisions
    SET status = 'active',
        activated_at = clock_timestamp()
    WHERE strategy_revision_id = v_candidate_revision_id
      AND status = 'candidate';

    UPDATE public.hosted_agent_learning_jobs
    SET status = 'activated',
        evidence_hash = p_evidence_hash,
        proposal = p_proposal,
        gate_summary = p_gate_summary,
        candidate_strategy_revision_id = v_candidate_revision_id,
        error_class = NULL,
        leased_by = NULL,
        lease_expires_at = NULL,
        completed_at = clock_timestamp()
    WHERE learning_job_id = v_job.learning_job_id;

    RETURN jsonb_build_object(
        'disposition',
        'activated',
        'strategyRevisionId',
        v_candidate_revision_id
    );
END
$complete_hosted_learning$;

CREATE OR REPLACE FUNCTION public.release_hosted_agent_learning_job(
    p_learning_job_id TEXT,
    p_worker_id TEXT,
    p_error_class TEXT,
    p_retryable BOOLEAN
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $release_hosted_learning$
DECLARE
    v_attempt_count INTEGER;
    v_status TEXT;
BEGIN
    IF p_error_class IS NULL
       OR char_length(p_error_class) NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid hosted learning error'
            USING ERRCODE = '22023';
    END IF;

    SELECT attempt_count
    INTO v_attempt_count
    FROM public.hosted_agent_learning_jobs
    WHERE learning_job_id = p_learning_job_id
      AND status = 'leased'
      AND leased_by = p_worker_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN 'lost_lease';
    END IF;

    v_status := CASE
        WHEN p_retryable AND v_attempt_count < 2 THEN 'pending'
        ELSE 'failed'
    END;

    UPDATE public.hosted_agent_learning_jobs
    SET status = v_status,
        not_before = CASE
            WHEN v_status = 'pending'
            THEN clock_timestamp() + INTERVAL '10 seconds'
            ELSE not_before
        END,
        error_class = p_error_class,
        leased_by = NULL,
        lease_expires_at = NULL,
        completed_at = CASE
            WHEN v_status = 'failed' THEN clock_timestamp()
            ELSE NULL
        END
    WHERE learning_job_id = p_learning_job_id;
    RETURN v_status;
END
$release_hosted_learning$;

RESET ROLE;

ALTER TABLE public.hosted_agent_learning_jobs
    OWNER TO adx_arena_migration;
ALTER TABLE public.hosted_agent_strategy_evaluations
    OWNER TO adx_arena_migration;

ALTER FUNCTION public.default_hosted_agent_policy_profile(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.sync_hosted_agent_strategy_revision()
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.enqueue_hosted_agent_learning_jobs()
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.claim_hosted_agent_learning_jobs(
    TEXT,
    INTEGER,
    INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.load_hosted_agent_learning_evidence(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.complete_hosted_agent_learning_job(
    TEXT,
    TEXT,
    TEXT,
    INTEGER,
    TEXT,
    JSONB,
    TEXT,
    JSONB,
    JSONB,
    BOOLEAN,
    TEXT
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.release_hosted_agent_learning_job(
    TEXT,
    TEXT,
    TEXT,
    BOOLEAN
) OWNER TO adx_arena_function_owner;

REVOKE ALL ON
    public.hosted_agent_learning_jobs,
    public.hosted_agent_strategy_evaluations
FROM PUBLIC;

REVOKE ALL ON FUNCTION
    public.default_hosted_agent_policy_profile(TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.claim_hosted_agent_learning_jobs(TEXT, INTEGER, INTEGER)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.load_hosted_agent_learning_evidence(TEXT, TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.complete_hosted_agent_learning_job(
        TEXT,
        TEXT,
        TEXT,
        INTEGER,
        TEXT,
        JSONB,
        TEXT,
        JSONB,
        JSONB,
        BOOLEAN,
        TEXT
    )
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.release_hosted_agent_learning_job(
        TEXT,
        TEXT,
        TEXT,
        BOOLEAN
    )
FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE ON
    public.hosted_agent_learning_jobs,
    public.hosted_agent_strategy_evaluations,
    public.hosted_agent_strategy_revisions
TO adx_arena_function_owner;

GRANT SELECT ON
    public.games,
    public.game_agents,
    public.arena_agents,
    public.arena_runtime_bindings,
    public.arena_model_credentials,
    public.arena_agent_tasks,
    public.arena_agent_task_results,
    public.arena_applied_agent_actions,
    public.arena_agent_task_attempts,
    public.hosted_agent_game_memory,
    arena402.rankings,
    arena402.final_settlement_prices,
    arena402.pairings
TO adx_arena_function_owner;

GRANT SELECT ON
    public.hosted_agent_learning_jobs,
    public.hosted_agent_strategy_evaluations
TO adx_arena_api;

GRANT EXECUTE ON FUNCTION
    public.claim_hosted_agent_learning_jobs(TEXT, INTEGER, INTEGER)
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.load_hosted_agent_learning_evidence(TEXT, TEXT)
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.complete_hosted_agent_learning_job(
        TEXT,
        TEXT,
        TEXT,
        INTEGER,
        TEXT,
        JSONB,
        TEXT,
        JSONB,
        JSONB,
        BOOLEAN,
        TEXT
    )
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION
    public.release_hosted_agent_learning_job(
        TEXT,
        TEXT,
        TEXT,
        BOOLEAN
    )
TO adx_hosted_worker;

COMMIT;
