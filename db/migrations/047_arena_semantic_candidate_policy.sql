BEGIN;

ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

-- Runtime success remains only a candidate. Keep the production CAS
-- projection aligned with arena_core.candidate_validation, including the hard
-- limit boundary and the deterministic accept/counter/reject rules.
CREATE OR REPLACE FUNCTION apply_arena_agent_task_result(
    p_result_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apply_task_result$
DECLARE
    v_result public.arena_agent_task_results%ROWTYPE;
    v_task public.arena_agent_tasks%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_application_outcome TEXT;
    v_applied_action JSONB;
    v_action_name TEXT;
    v_allowed_actions JSONB;
    v_allowed_goods JSONB;
    v_quantity BIGINT;
    v_limit_price NUMERIC;
    v_action_price NUMERIC;
    v_quote_price NUMERIC;
    v_role TEXT;
    v_remaining_turns INTEGER;
    v_is_quote_in_bound BOOLEAN;
    v_rejection_reason TEXT;
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

    v_action_name := v_result.candidate_action ->> 'action';
    v_allowed_actions := v_task.input_snapshot #> '{limits,allowedActions}';
    v_allowed_goods := v_task.input_snapshot #> '{limits,allowedGoods}';
    v_quantity := COALESCE(
        (v_result.candidate_action ->> 'quantity')::BIGINT,
        1
    );

    IF v_result.runtime_status <> 'succeeded' THEN
        v_rejection_reason := 'runtime_' || v_result.runtime_status;
    ELSIF v_task.task_kind = 'arena.decide' THEN
        IF v_action_name NOT IN ('buy', 'sell', 'pass') THEN
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
        ELSIF v_action_name = 'buy'
              AND v_result.candidate_action ->> 'limitPrice' IS NOT NULL
              AND (
                  (v_result.candidate_action ->> 'limitPrice')::NUMERIC
                  * v_quantity
              ) > (v_task.input_snapshot ->> 'cash')::NUMERIC THEN
            v_rejection_reason := 'insufficient_cash';
        END IF;
    ELSIF v_task.task_kind = 'arena.negotiate' THEN
        v_limit_price := (
            v_task.input_snapshot ->> 'limitPrice'
        )::NUMERIC;
        v_action_price := (
            v_result.candidate_action ->> 'price'
        )::NUMERIC;
        v_quote_price := (
            v_task.input_snapshot
            #>> '{latestCounterpartyQuote,price}'
        )::NUMERIC;
        v_role := v_task.input_snapshot ->> 'role';
        v_remaining_turns := (
            v_task.input_snapshot ->> 'remainingTurns'
        )::INTEGER;
        v_is_quote_in_bound := CASE
            WHEN v_quote_price IS NULL THEN NULL
            WHEN v_limit_price IS NULL THEN TRUE
            WHEN v_role = 'buyer' THEN v_quote_price <= v_limit_price
            ELSE v_quote_price >= v_limit_price
        END;

        IF v_action_name NOT IN ('propose', 'accept', 'reject') THEN
            v_rejection_reason := 'action_kind_mismatch';
        ELSIF (v_task.input_snapshot ->> 'turnSequence')::INTEGER = 1
              AND v_action_name <> 'propose' THEN
            v_rejection_reason := 'buyer_opening_proposal_required';
        ELSIF v_action_name = 'accept'
              AND v_quote_price IS NULL THEN
            v_rejection_reason := 'counterparty_proposal_required';
        ELSIF v_remaining_turns <= 1
              AND v_action_name = 'propose' THEN
            v_rejection_reason := 'final_turn_must_close';
        ELSIF v_limit_price IS NOT NULL
              AND (
                  (
                      v_action_name = 'propose'
                      AND (
                          (v_role = 'buyer' AND v_action_price > v_limit_price)
                          OR
                          (v_role = 'seller' AND v_action_price < v_limit_price)
                      )
                  )
                  OR
                  (
                      v_action_name = 'accept'
                      AND v_quote_price IS NOT NULL
                      AND (
                          (v_role = 'buyer' AND v_quote_price > v_limit_price)
                          OR
                          (v_role = 'seller' AND v_quote_price < v_limit_price)
                      )
                  )
              ) THEN
            v_rejection_reason := 'limit_price_violation';
        ELSIF v_quote_price IS NULL
              AND v_action_name = 'propose'
              AND v_limit_price IS NOT NULL
              AND v_action_price <> v_limit_price THEN
            v_rejection_reason := 'opening_price_must_equal_limit';
        ELSIF v_quote_price IS NOT NULL
              AND v_is_quote_in_bound
              AND v_action_name <> 'accept' THEN
            v_rejection_reason := 'in_bound_quote_must_accept';
        ELSIF v_quote_price IS NOT NULL
              AND NOT v_is_quote_in_bound
              AND v_remaining_turns <= 1
              AND v_action_name <> 'reject' THEN
            v_rejection_reason := 'final_out_of_bound_quote_must_reject';
        ELSIF v_quote_price IS NOT NULL
              AND NOT v_is_quote_in_bound
              AND v_remaining_turns > 1
              AND v_action_name <> 'propose' THEN
            v_rejection_reason := 'out_of_bound_quote_must_counter';
        ELSIF v_quote_price IS NOT NULL
              AND NOT v_is_quote_in_bound
              AND v_remaining_turns > 1
              AND v_action_name = 'propose'
              AND v_limit_price IS NOT NULL
              AND v_action_price <> v_limit_price THEN
            v_rejection_reason := 'counter_must_equal_limit';
        END IF;
    ELSE
        v_rejection_reason := 'action_kind_mismatch';
    END IF;

    IF v_rejection_reason IS NULL THEN
        v_application_outcome := 'candidate';
        v_applied_action := v_result.candidate_action;
    ELSIF v_task.task_kind = 'arena.decide' THEN
        v_application_outcome := 'default_pass';
        v_applied_action := '{"action":"pass"}'::JSONB;
    ELSE
        v_application_outcome := 'negotiation_timeout';
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
$apply_task_result$;

RESET ROLE;

ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_function_owner;
REVOKE ALL ON FUNCTION apply_arena_agent_task_result(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION apply_arena_agent_task_result(TEXT)
    TO adx_arena_core;

COMMIT;
