BEGIN;

-- The generic wire schema intentionally accepts up to 18 decimal places, but
-- Arena gold and settlement amounts use six atomic decimal places. Enforce the
-- game-specific boundary inside the authoritative apply function before any
-- market projector can convert a candidate to atomic units.
ALTER FUNCTION public.apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_migration;

SET LOCAL ROLE adx_arena_migration;

ALTER FUNCTION public.apply_arena_agent_task_result(TEXT)
    RENAME TO apply_arena_agent_task_result_pre_precision_v1;

CREATE OR REPLACE FUNCTION public.arena_money_value_has_atomic_precision(
    p_value TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $money_precision$
DECLARE
    v_atomic NUMERIC;
BEGIN
    v_atomic := p_value::NUMERIC * 1000000;
    RETURN v_atomic = trunc(v_atomic);
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN FALSE;
END
$money_precision$;

CREATE OR REPLACE FUNCTION public.arena_action_price_precision_valid(
    p_task_kind TEXT,
    p_action JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $action_precision$
DECLARE
    v_action_name TEXT := p_action ->> 'action';
BEGIN
    IF p_action IS NULL THEN
        RETURN TRUE;
    END IF;

    IF p_task_kind = 'arena.decide'
       AND v_action_name IN ('buy', 'sell') THEN
        RETURN (
            p_action ->> 'limitPrice' IS NULL
            OR public.arena_money_value_has_atomic_precision(
                p_action ->> 'limitPrice'
            )
        );
    ELSIF p_task_kind = 'arena.market.intent'
          AND v_action_name IN ('buy', 'sell') THEN
        RETURN (
            (
                p_action ->> 'publicPrice' IS NULL
                OR public.arena_money_value_has_atomic_precision(
                    p_action ->> 'publicPrice'
                )
            )
            AND
            (
                p_action ->> 'limitPrice' IS NULL
                OR public.arena_money_value_has_atomic_precision(
                    p_action ->> 'limitPrice'
                )
            )
        );
    ELSIF p_task_kind = 'arena.market.rfq'
          AND v_action_name = 'request_negotiations' THEN
        RETURN NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                COALESCE(p_action -> 'requests', '[]'::JSONB)
            ) AS request
            WHERE request ->> 'openingPrice' IS NOT NULL
              AND NOT public.arena_money_value_has_atomic_precision(
                  request ->> 'openingPrice'
              )
        );
    ELSIF p_task_kind = 'arena.negotiate'
          AND v_action_name = 'propose' THEN
        RETURN (
            p_action ->> 'price' IS NULL
            OR public.arena_money_value_has_atomic_precision(
                p_action ->> 'price'
            )
        );
    END IF;

    RETURN TRUE;
END
$action_precision$;

CREATE OR REPLACE FUNCTION public.apply_arena_agent_task_result(
    p_result_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apply_with_precision$
DECLARE
    v_applied BOOLEAN;
    v_action public.arena_applied_agent_actions%ROWTYPE;
    v_repaired_outcome TEXT;
    v_repaired_action JSONB;
BEGIN
    v_applied := public.apply_arena_agent_task_result_pre_precision_v1(
        p_result_id
    );
    IF NOT v_applied THEN
        RETURN FALSE;
    END IF;

    SELECT *
    INTO STRICT v_action
    FROM public.arena_applied_agent_actions
    WHERE result_id = p_result_id
    FOR UPDATE;

    IF v_action.application_outcome <> 'candidate'
       OR public.arena_action_price_precision_valid(
           v_action.task_kind,
           v_action.applied_action
       ) THEN
        RETURN TRUE;
    END IF;

    IF v_action.task_kind IN (
        'arena.decide',
        'arena.market.intent'
    ) THEN
        v_repaired_outcome := 'default_pass';
        v_repaired_action := '{"action":"pass"}'::JSONB;
    ELSIF v_action.task_kind IN (
        'arena.market.rfq',
        'arena.market.select'
    ) THEN
        v_repaired_outcome := 'market_timeout';
        v_repaired_action := NULL;
    ELSE
        v_repaired_outcome := 'negotiation_timeout';
        v_repaired_action := NULL;
    END IF;

    UPDATE public.arena_applied_agent_actions
    SET application_outcome = v_repaired_outcome,
        applied_action = v_repaired_action,
        authoritative_entered_at = applied_at
    WHERE result_id = p_result_id;

    UPDATE public.arena_agent_task_results
    SET error_class = COALESCE(
        error_class,
        'price_precision_exceeded'
    )
    WHERE result_id = p_result_id;

    UPDATE public.arena_agent_task_events
    SET safe_metadata = jsonb_strip_nulls(
        safe_metadata
        || jsonb_build_object(
            'application_outcome',
            v_repaired_outcome,
            'reason',
            'price_precision_exceeded'
        )
    )
    WHERE task_id = v_action.task_id
      AND event_type = 'result_applied'
      AND safe_metadata ->> 'result_hash' = (
          SELECT result_hash
          FROM public.arena_agent_task_results
          WHERE result_id = p_result_id
      );

    RETURN TRUE;
END
$apply_with_precision$;

-- Repair only legacy market candidates that have not crossed the durable
-- projection boundary. Already-projected market state is immutable.
CREATE TEMP TABLE arena_money_precision_repair
ON COMMIT DROP
AS
SELECT
    applied.result_id,
    applied.task_id,
    applied.task_kind
FROM public.arena_applied_agent_actions AS applied
LEFT JOIN arena402.market_projection_receipts AS receipt
  ON receipt.result_id = applied.result_id
WHERE applied.application_outcome = 'candidate'
  AND applied.task_kind IN ('arena.market.intent', 'arena.market.rfq')
  AND receipt.result_id IS NULL
  AND NOT public.arena_action_price_precision_valid(
      applied.task_kind,
      applied.applied_action
  );

UPDATE public.arena_applied_agent_actions AS applied
SET application_outcome = CASE
        WHEN repair.task_kind = 'arena.market.intent'
        THEN 'default_pass'
        ELSE 'market_timeout'
    END,
    applied_action = CASE
        WHEN repair.task_kind = 'arena.market.intent'
        THEN '{"action":"pass"}'::JSONB
        ELSE NULL
    END,
    authoritative_entered_at = applied.applied_at
FROM arena_money_precision_repair AS repair
WHERE applied.result_id = repair.result_id;

UPDATE public.arena_agent_task_results AS result
SET error_class = COALESCE(
    result.error_class,
    'price_precision_exceeded'
)
FROM arena_money_precision_repair AS repair
WHERE result.result_id = repair.result_id;

UPDATE public.arena_agent_task_events AS event
SET safe_metadata = jsonb_strip_nulls(
    event.safe_metadata
    || jsonb_build_object(
        'application_outcome',
        CASE
            WHEN repair.task_kind = 'arena.market.intent'
            THEN 'default_pass'
            ELSE 'market_timeout'
        END,
        'reason',
        'price_precision_exceeded'
    )
)
FROM arena_money_precision_repair AS repair
JOIN public.arena_agent_task_results AS result
  ON result.result_id = repair.result_id
WHERE event.task_id = repair.task_id
  AND event.event_type = 'result_applied'
  AND event.safe_metadata ->> 'result_hash' = result.result_hash;

RESET ROLE;

ALTER FUNCTION public.arena_money_value_has_atomic_precision(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.arena_action_price_precision_valid(TEXT, JSONB)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.apply_arena_agent_task_result_pre_precision_v1(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION
    public.arena_money_value_has_atomic_precision(TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.arena_action_price_precision_valid(TEXT, JSONB)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.apply_arena_agent_task_result_pre_precision_v1(TEXT)
FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.apply_arena_agent_task_result_pre_precision_v1(TEXT)
FROM adx_arena_core;
REVOKE ALL ON FUNCTION
    public.apply_arena_agent_task_result(TEXT)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    public.apply_arena_agent_task_result(TEXT)
TO adx_arena_core;

COMMIT;
