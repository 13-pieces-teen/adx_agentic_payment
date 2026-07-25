BEGIN;

-- 020 introduced the durable Connector result outbox after the production
-- runtime roles had already been granted access to the original Connector
-- tables. Keep results immutable to the Gateway role: it may load them during
-- restart recovery and insert newly received terminal results, but it cannot
-- rewrite or delete accepted evidence.
GRANT SELECT, INSERT ON connector_agent_task_results
    TO adx_connector_gateway;

COMMIT;
