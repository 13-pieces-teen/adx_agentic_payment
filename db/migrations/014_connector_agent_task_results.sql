BEGIN;

CREATE TABLE IF NOT EXISTS connector_agent_task_results (
    task_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE,
    binding_id TEXT NOT NULL
        REFERENCES connector_bindings(binding_id),
    device_id TEXT NOT NULL
        REFERENCES connector_devices(device_id),
    command_id TEXT NOT NULL
        REFERENCES connector_commands(command_id),
    binding_epoch BIGINT NOT NULL CHECK (binding_epoch > 0),
    result_hash CHAR(64) NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS connector_agent_task_results_binding_idx
    ON connector_agent_task_results (binding_id, received_at DESC);

COMMIT;
