-- Admit LiteLLM-backed Official Agent tasks to the Hosted Worker queue.

BEGIN;

SET LOCAL ROLE adx_arena_migration;

INSERT INTO hosted_provider_capacity (provider, max_inflight)
VALUES ('official-deepseek', 32)
ON CONFLICT (provider) DO NOTHING;

RESET ROLE;

COMMIT;
