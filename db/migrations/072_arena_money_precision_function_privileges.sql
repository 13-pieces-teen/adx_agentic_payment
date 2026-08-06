BEGIN;

-- Migration 068's fixed-search-path SECURITY DEFINER wrapper can correct only
-- the three applied-action columns and the matching safe event metadata. Keep
-- the non-login function owner on column-level UPDATE privileges rather than
-- granting broad table mutation.
SET LOCAL ROLE adx_arena_migration;

GRANT UPDATE (
    application_outcome,
    applied_action,
    authoritative_entered_at
) ON public.arena_applied_agent_actions
TO adx_arena_function_owner;

GRANT UPDATE (safe_metadata)
ON public.arena_agent_task_events
TO adx_arena_function_owner;

RESET ROLE;

COMMIT;
