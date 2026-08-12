BEGIN;

-- One current WSS owner per Device. The monotonically increasing token fences
-- stale Gateway processes even while their local socket is still open.
CREATE TABLE IF NOT EXISTS connector_device_connection_leases (
    device_id TEXT PRIMARY KEY
        REFERENCES connector_devices(device_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL CHECK (
        char_length(instance_id) BETWEEN 1 AND 128
    ),
    fencing_token BIGINT NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
    lease_expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS connector_device_connection_lease_expiry_idx
    ON connector_device_connection_leases (lease_expires_at);

GRANT SELECT, INSERT, UPDATE ON
    connector_device_connection_leases
TO adx_connector_gateway;

-- Keep the immutable Result body unchanged. This separate transition records
-- when the Arena-owned Result Sink has accepted the Gateway inbox record.
ALTER TABLE connector_agent_task_results
    ADD COLUMN IF NOT EXISTS arena_sink_accepted_at TIMESTAMPTZ;

GRANT UPDATE (arena_sink_accepted_at)
    ON connector_agent_task_results
    TO adx_connector_gateway;

COMMIT;
