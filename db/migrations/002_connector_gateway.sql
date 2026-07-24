BEGIN;

CREATE TABLE IF NOT EXISTS connector_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    temporary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at TIMESTAMPTZ,
    CONSTRAINT connector_username_normalized CHECK (username = lower(username)),
    CONSTRAINT connector_password_for_durable_user
        CHECK (temporary OR password_hash IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS connector_invites (
    token_hash CHAR(64) PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    consumed_by TEXT REFERENCES connector_users(user_id),
    CONSTRAINT connector_invite_consumption_pair
        CHECK (
            (consumed_at IS NULL AND consumed_by IS NULL)
            OR (consumed_at IS NOT NULL AND consumed_by IS NOT NULL)
        )
);

CREATE TABLE IF NOT EXISTS connector_sessions (
    token_hash CHAR(64) PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES connector_users(user_id) ON DELETE CASCADE,
    csrf_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS connector_sessions_user_idx
    ON connector_sessions (user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS connector_pairings (
    pairing_id TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    owner_id TEXT REFERENCES connector_users(user_id),
    device_code_hash CHAR(64) NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'consumed', 'expired')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS connector_pairings_expiry_idx
    ON connector_pairings (status, expires_at);

CREATE TABLE IF NOT EXISTS connector_devices (
    device_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES connector_users(user_id),
    token_hash CHAR(64) NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    record JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS connector_devices_owner_idx
    ON connector_devices (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS connector_runtimes (
    device_id TEXT NOT NULL REFERENCES connector_devices(device_id) ON DELETE CASCADE,
    runtime_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    record JSONB NOT NULL,
    PRIMARY KEY (device_id, runtime_id)
);

CREATE TABLE IF NOT EXISTS connector_bindings (
    binding_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES connector_devices(device_id) ON DELETE CASCADE,
    runtime_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL,
    UNIQUE (device_id, runtime_id)
);
CREATE INDEX IF NOT EXISTS connector_bindings_device_idx
    ON connector_bindings (device_id, created_at DESC);

CREATE TABLE IF NOT EXISTS connector_commands (
    command_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES connector_bindings(binding_id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES connector_devices(device_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL,
    UNIQUE (binding_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS connector_commands_delivery_idx
    ON connector_commands (device_id, status, created_at);

CREATE TABLE IF NOT EXISTS connector_events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES connector_devices(device_id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL REFERENCES connector_bindings(binding_id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL,
    UNIQUE (device_id, sequence)
);
CREATE INDEX IF NOT EXISTS connector_events_binding_idx
    ON connector_events (binding_id, received_at DESC);
CREATE INDEX IF NOT EXISTS connector_events_retention_idx
    ON connector_events (received_at DESC, event_id DESC);

CREATE TABLE IF NOT EXISTS connector_audit (
    audit_id TEXT PRIMARY KEY,
    owner_id TEXT REFERENCES connector_users(user_id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    record JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS connector_audit_owner_idx
    ON connector_audit (owner_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS connector_audit_retention_idx
    ON connector_audit (occurred_at DESC, audit_id DESC);

COMMIT;
