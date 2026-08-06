BEGIN;

SET LOCAL ROLE adx_arena_migration;

-- The Arena Core projection path re-reads authoritative tasks, Results, and
-- applied actions before changing market state. Keep these grants read-only;
-- task/result mutation remains behind the existing SECURITY DEFINER
-- functions and dedicated Runtime roles.
GRANT SELECT ON TABLE
    public.arena_agent_tasks,
    public.arena_agent_task_results,
    public.arena_applied_agent_actions
TO adx_arena_core;

RESET ROLE;

COMMIT;
