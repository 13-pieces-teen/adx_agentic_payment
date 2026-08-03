BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- Arena 402 currently settles one unit per matched order. Keep historical
-- Runtime results readable, but reject any newly inserted or updated
-- successful buy/sell result that tries to expand the trade quantity.
ALTER TABLE public.arena_agent_task_results
    ADD CONSTRAINT arena_agent_task_results_fixed_trade_quantity_check
    CHECK (
        runtime_status <> 'succeeded'
        OR candidate_action IS NULL
        OR candidate_action ->> 'action' NOT IN ('buy', 'sell')
        OR NOT (candidate_action ? 'quantity')
        OR (
            candidate_action ? 'quantity'
            AND candidate_action -> 'quantity' = '1'::JSONB
        )
    ) NOT VALID;

COMMIT;
