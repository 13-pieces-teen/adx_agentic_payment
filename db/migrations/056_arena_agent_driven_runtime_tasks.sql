BEGIN;

-- Preserve the frozen fcfs.v1 projection under a private compatibility name.
-- The new public entrypoint delegates legacy decide/negotiate tasks to it and
-- handles only the versioned agent_a2a.v1 market task kinds.
ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    RENAME TO apply_arena_agent_task_result_fcfs_v1;

DO $drop_old_task_checks$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        WHERE con.conrelid = 'public.arena_agent_tasks'::regclass
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%task_kind%'
    LOOP
        EXECUTE format(
            'ALTER TABLE public.arena_agent_tasks DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$drop_old_task_checks$;

ALTER TABLE public.arena_agent_tasks
    ADD CONSTRAINT arena_agent_tasks_kind_v2_check
    CHECK (
        task_kind IN (
            'arena.decide',
            'arena.negotiate',
            'arena.market.intent',
            'arena.market.rfq',
            'arena.market.select'
        )
    ),
    ADD CONSTRAINT arena_agent_tasks_shape_v2_check
    CHECK (
        (
            task_kind = 'arena.decide'
            AND negotiation_id IS NULL
            AND turn_sequence IS NULL
        )
        OR
        (
            task_kind = 'arena.negotiate'
            AND negotiation_id IS NOT NULL
            AND negotiation_id <> ''
            AND turn_sequence > 0
        )
        OR
        (
            task_kind IN (
                'arena.market.intent',
                'arena.market.rfq',
                'arena.market.select'
            )
            AND negotiation_id IS NULL
            AND turn_sequence IS NULL
        )
    );

DO $drop_old_applied_checks$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        WHERE con.conrelid =
              'public.arena_applied_agent_actions'::regclass
          AND con.contype = 'c'
          AND (
              pg_get_constraintdef(con.oid) LIKE '%task_kind%'
              OR pg_get_constraintdef(con.oid)
                 LIKE '%application_outcome%'
          )
    LOOP
        EXECUTE format(
            'ALTER TABLE public.arena_applied_agent_actions '
            'DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$drop_old_applied_checks$;

ALTER TABLE public.arena_applied_agent_actions
    ADD CONSTRAINT arena_applied_agent_actions_kind_v2_check
    CHECK (
        task_kind IN (
            'arena.decide',
            'arena.negotiate',
            'arena.market.intent',
            'arena.market.rfq',
            'arena.market.select'
        )
    ),
    ADD CONSTRAINT arena_applied_agent_actions_outcome_v2_check
    CHECK (
        application_outcome IN (
            'candidate',
            'default_pass',
            'negotiation_timeout',
            'market_timeout',
            'cancelled'
        )
    ),
    ADD CONSTRAINT arena_applied_agent_actions_shape_v2_check
    CHECK (
        (
            application_outcome IN ('candidate', 'default_pass')
            AND applied_action IS NOT NULL
            AND jsonb_typeof(applied_action) = 'object'
        )
        OR
        (
            application_outcome IN (
                'negotiation_timeout',
                'market_timeout',
                'cancelled'
            )
            AND applied_action IS NULL
        )
    );

CREATE OR REPLACE FUNCTION apply_arena_agent_task_result(
    p_result_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apply_agent_driven_task_result$
DECLARE
    v_result public.arena_agent_task_results%ROWTYPE;
    v_task public.arena_agent_tasks%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_action_name TEXT;
    v_application_outcome TEXT;
    v_applied_action JSONB;
    v_rejection_reason TEXT;
    v_allowed_actions JSONB;
    v_allowed_goods JSONB;
    v_public_price NUMERIC;
    v_limit_price NUMERIC;
    v_quantity BIGINT;
    v_max_outbound_rfq INTEGER;
BEGIN
    SELECT *
    INTO v_result
    FROM public.arena_agent_task_results
    WHERE result_id = p_result_id
    FOR UPDATE;

    IF NOT FOUND OR v_result.apply_status <> 'pending' THEN
        RETURN FALSE;
    END IF;

    SELECT *
    INTO STRICT v_task
    FROM public.arena_agent_tasks
    WHERE task_id = v_result.task_id
    FOR UPDATE;

    IF v_task.task_kind = 'arena.negotiate'
       OR (
           v_task.task_kind = 'arena.decide'
           AND NOT (
               COALESCE(v_result.candidate_action ? 'publicPrice', FALSE)
               OR COALESCE(v_result.candidate_action ? 'message', FALSE)
           )
       ) THEN
        RETURN public.apply_arena_agent_task_result_fcfs_v1(p_result_id);
    END IF;

    v_action_name := v_result.candidate_action ->> 'action';
    v_quantity := COALESCE(
        (v_result.candidate_action ->> 'quantity')::BIGINT,
        1
    );

    IF v_task.task_kind = 'arena.decide' THEN
        v_rejection_reason := 'public_price_not_allowed';
    ELSIF v_result.runtime_status <> 'succeeded' THEN
        v_rejection_reason := 'runtime_' || v_result.runtime_status;
    ELSIF v_task.task_kind = 'arena.market.intent' THEN
        v_allowed_actions := v_task.input_snapshot
            #> '{limits,allowedActions}';
        v_allowed_goods := v_task.input_snapshot
            #> '{limits,allowedGoods}';

        IF v_action_name IS NULL
           OR v_action_name NOT IN ('buy', 'sell', 'pass') THEN
            v_rejection_reason := 'action_kind_mismatch';
        ELSIF jsonb_typeof(v_allowed_actions) = 'array'
              AND NOT (
                  v_allowed_actions @> jsonb_build_array(v_action_name)
              ) THEN
            v_rejection_reason := 'action_not_allowed';
        ELSIF v_action_name <> 'pass'
              AND (
                  v_result.candidate_action ->> 'good' IS NULL
                  OR (
                      jsonb_typeof(v_allowed_goods) = 'array'
                      AND jsonb_array_length(v_allowed_goods) > 0
                      AND NOT (
                          v_allowed_goods @> jsonb_build_array(
                              v_result.candidate_action ->> 'good'
                          )
                      )
                  )
              ) THEN
            v_rejection_reason := 'good_not_allowed';
        ELSIF v_action_name <> 'pass'
              AND (
                  v_result.candidate_action ->> 'publicPrice' IS NULL
                  OR v_result.candidate_action ->> 'limitPrice' IS NULL
              ) THEN
            v_rejection_reason := 'market_price_required';
        ELSIF v_action_name <> 'pass' THEN
            v_public_price := (
                v_result.candidate_action ->> 'publicPrice'
            )::NUMERIC;
            v_limit_price := (
                v_result.candidate_action ->> 'limitPrice'
            )::NUMERIC;

            IF (
                v_action_name = 'buy'
                AND v_public_price > v_limit_price
            ) OR (
                v_action_name = 'sell'
                AND v_public_price < v_limit_price
            ) THEN
                v_rejection_reason :=
                    'market_price_boundary_violation';
            ELSIF v_action_name = 'buy'
                  AND v_limit_price * v_quantity
                      > (v_task.input_snapshot ->> 'cash')::NUMERIC THEN
                v_rejection_reason := 'insufficient_cash';
            ELSIF v_action_name = 'sell'
                  AND COALESCE(
                      (
                          v_task.input_snapshot #>> ARRAY[
                              'holdings',
                              v_result.candidate_action ->> 'good'
                          ]
                      )::BIGINT,
                      0
                  ) < v_quantity THEN
                v_rejection_reason := 'insufficient_inventory';
            END IF;
        END IF;
    ELSIF v_task.task_kind = 'arena.market.rfq' THEN
        v_limit_price := (
            v_task.input_snapshot ->> 'limitPrice'
        )::NUMERIC;
        v_max_outbound_rfq := (
            v_task.input_snapshot ->> 'maxOutboundRfq'
        )::INTEGER;

        IF v_action_name IS NULL
           OR v_action_name NOT IN (
               'request_negotiations',
               'pass'
           ) THEN
            v_rejection_reason := 'action_kind_mismatch';
        ELSIF v_action_name = 'request_negotiations'
              AND (
                  jsonb_typeof(
                      v_result.candidate_action -> 'requests'
                  ) <> 'array'
                  OR jsonb_array_length(
                      v_result.candidate_action -> 'requests'
                  ) < 1
                  OR jsonb_array_length(
                      v_result.candidate_action -> 'requests'
                  ) > v_max_outbound_rfq
              ) THEN
            v_rejection_reason := 'rfq_budget_exceeded';
        ELSIF v_action_name = 'request_negotiations'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      v_result.candidate_action -> 'requests'
                  ) AS request
                  WHERE NOT EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(
                          v_task.input_snapshot -> 'directory'
                      ) AS directory_entry
                      WHERE directory_entry ->> 'intentId'
                            = request ->> 'targetIntentId'
                  )
              ) THEN
            v_rejection_reason := 'rfq_target_not_visible';
        ELSIF v_action_name = 'request_negotiations'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      v_result.candidate_action -> 'requests'
                  ) AS request
                  WHERE (request ->> 'openingPrice')::NUMERIC
                        > v_limit_price
              ) THEN
            v_rejection_reason := 'limit_price_violation';
        END IF;
    ELSIF v_task.task_kind = 'arena.market.select' THEN
        IF v_action_name IS NULL
           OR v_action_name NOT IN ('engage', 'reject_all') THEN
            v_rejection_reason := 'action_kind_mismatch';
        ELSIF v_action_name = 'engage'
              AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      v_task.input_snapshot -> 'requests'
                  ) AS request
                  WHERE request ->> 'requestId'
                        = v_result.candidate_action ->> 'requestId'
              ) THEN
            v_rejection_reason := 'request_not_visible';
        ELSIF v_action_name = 'engage'
              AND (
                  v_task.input_snapshot ->> 'inventoryAvailable'
              )::BIGINT < (
                  v_task.input_snapshot ->> 'quantity'
              )::BIGINT THEN
            v_rejection_reason := 'insufficient_inventory';
        END IF;
    ELSE
        v_rejection_reason := 'action_kind_mismatch';
    END IF;

    IF v_rejection_reason IS NULL THEN
        v_application_outcome := 'candidate';
        v_applied_action := v_result.candidate_action;
    ELSIF v_task.task_kind IN (
        'arena.decide',
        'arena.market.intent'
    ) THEN
        v_application_outcome := 'default_pass';
        v_applied_action := '{"action":"pass"}'::JSONB;
    ELSE
        v_application_outcome := 'market_timeout';
        v_applied_action := NULL;
    END IF;

    INSERT INTO public.arena_applied_agent_actions (
        task_id,
        result_id,
        game_id,
        round_id,
        game_agent_id,
        task_kind,
        application_outcome,
        applied_action,
        authoritative_entered_at,
        applied_at
    )
    VALUES (
        v_task.task_id,
        v_result.result_id,
        v_task.game_id,
        v_task.round_id,
        v_task.game_agent_id,
        v_task.task_kind,
        v_application_outcome,
        v_applied_action,
        CASE
            WHEN v_application_outcome = 'candidate'
            THEN v_result.result_received_at
            ELSE v_now
        END,
        v_now
    );

    UPDATE public.arena_agent_task_results
    SET apply_status = 'applied',
        arena_applied_at = v_now,
        error_class = COALESCE(error_class, v_rejection_reason)
    WHERE result_id = p_result_id
      AND apply_status = 'pending';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'result apply CAS failed' USING ERRCODE = '40001';
    END IF;

    INSERT INTO public.arena_agent_task_events (
        event_id,
        task_id,
        event_type,
        created_at,
        safe_metadata
    )
    VALUES (
        v_task.task_id || ':event:applied:'
            || substring(v_result.result_hash FROM 8),
        v_task.task_id,
        'result_applied',
        v_now,
        jsonb_strip_nulls(
            jsonb_build_object(
                'result_hash',
                v_result.result_hash,
                'application_outcome',
                v_application_outcome,
                'reason',
                v_rejection_reason
            )
        )
    );

    RETURN TRUE;
END
$apply_agent_driven_task_result$;

RESET ROLE;

ALTER FUNCTION apply_arena_agent_task_result_fcfs_v1(TEXT)
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION
    apply_arena_agent_task_result_fcfs_v1(TEXT)
FROM PUBLIC;

ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION apply_arena_agent_task_result(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION apply_arena_agent_task_result(TEXT)
    TO adx_arena_core;

COMMIT;
