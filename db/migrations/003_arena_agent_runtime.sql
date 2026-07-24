BEGIN;

-- Arena 402 Phase 1 persistence foundation.
--
-- This migration deliberately depends on 002_connector_gateway.sql:
-- connector_users is the shared self-hosted beta User authority, and
-- connector_bindings remains the Connector-owned runtime binding authority.
-- Legacy 001_initial_schema.sql is not a dependency and must not be applied by
-- the self-hosted migration runner.

DO $arena_roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_arena_migration') THEN
        CREATE ROLE adx_arena_migration
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_arena_core') THEN
        CREATE ROLE adx_arena_core
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_arena_api') THEN
        CREATE ROLE adx_arena_api
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_hosted_worker') THEN
        CREATE ROLE adx_hosted_worker
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'adx_credential_controller'
    ) THEN
        CREATE ROLE adx_credential_controller
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'adx_arena_function_owner'
    ) THEN
        CREATE ROLE adx_arena_function_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    -- The migration connection must remain able to own/alter Arena objects on
    -- subsequent forward-only migrations without granting DDL to runtime roles.
    IF current_user <> 'adx_arena_migration'
       AND NOT pg_has_role(current_user, 'adx_arena_migration', 'MEMBER') THEN
        EXECUTE format('GRANT adx_arena_migration TO %I', current_user);
    END IF;
    IF current_user <> 'adx_arena_function_owner'
       AND NOT pg_has_role(current_user, 'adx_arena_function_owner', 'MEMBER') THEN
        EXECUTE format('GRANT adx_arena_function_owner TO %I', current_user);
    END IF;
END
$arena_roles$;

CREATE TABLE games (
    game_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (
        status IN (
            'draft',
            'open',
            'running',
            'settling',
            'completed',
            'cancelled'
        )
    ),
    action_timeout_ms INTEGER NOT NULL CHECK (
        action_timeout_ms BETWEEN 100 AND 900000
    ),
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(config_snapshot) = 'object'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CHECK (game_id <> ''),
    CHECK (
        (status IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (status NOT IN ('completed', 'cancelled') AND completed_at IS NULL)
    )
);

CREATE INDEX games_status_created_idx
    ON games (status, created_at);

CREATE TABLE rounds (
    round_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL CHECK (round_index >= 0),
    phase TEXT NOT NULL DEFAULT 'pending' CHECK (
        phase IN (
            'pending',
            'decide',
            'matching',
            'negotiate',
            'settling',
            'completed',
            'cancelled'
        )
    ),
    deadline_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (game_id, round_index),
    UNIQUE (round_id, game_id),
    CHECK (round_id <> ''),
    CHECK (
        (phase IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (
            phase NOT IN ('completed', 'cancelled')
            AND completed_at IS NULL
        )
    )
);

CREATE INDEX rounds_game_phase_idx
    ON rounds (game_id, phase, round_index);

CREATE TABLE arena_agents (
    agent_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL
        REFERENCES connector_users(user_id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (
        name <> '' AND char_length(name) <= 120
    ),
    avatar_ref TEXT CHECK (
        avatar_ref IS NULL OR char_length(avatar_ref) <= 512
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'disabled')
    ),
    runtime_update_job_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    disabled_at TIMESTAMPTZ,
    UNIQUE (owner_user_id, agent_id),
    CHECK (agent_id <> ''),
    CHECK (
        (status = 'disabled' AND disabled_at IS NOT NULL)
        OR (status = 'active' AND disabled_at IS NULL)
    )
);

CREATE INDEX arena_agents_owner_created_idx
    ON arena_agents (owner_user_id, created_at DESC);

CREATE TABLE arena_model_credentials (
    credential_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL
        REFERENCES connector_users(user_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL CHECK (
        provider ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    secret_ref TEXT NOT NULL UNIQUE CHECK (
        secret_ref <> '' AND char_length(secret_ref) <= 512
    ),
    fingerprint CHAR(64) NOT NULL CHECK (
        fingerprint ~ '^[0-9a-f]{64}$'
    ),
    fingerprint_pepper_version SMALLINT NOT NULL CHECK (
        fingerprint_pepper_version > 0
    ),
    status TEXT NOT NULL DEFAULT 'pending_write' CHECK (
        status IN (
            'pending_write',
            'stored',
            'pending_validation',
            'valid',
            'invalid',
            'revoking',
            'revoked'
        )
    ),
    unbound_expires_at TIMESTAMPTZ,
    last_validated_at TIMESTAMPTZ,
    replaced_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    secret_deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (credential_id, owner_user_id, provider),
    CHECK (credential_id <> ''),
    CHECK (
        status <> 'revoked' OR revoked_at IS NOT NULL
    )
);

CREATE INDEX arena_model_credentials_owner_status_idx
    ON arena_model_credentials (owner_user_id, status, created_at DESC);
CREATE INDEX arena_model_credentials_unbound_expiry_idx
    ON arena_model_credentials (unbound_expires_at)
    WHERE unbound_expires_at IS NOT NULL
      AND status IN ('pending_write', 'stored');

CREATE TABLE arena_hosted_configs (
    hosted_config_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (
        provider ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    model TEXT NOT NULL CHECK (
        model <> '' AND char_length(model) <= 200
    ),
    thinking_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    strategy_instructions TEXT NOT NULL DEFAULT '' CHECK (
        char_length(strategy_instructions) <= 4000
    ),
    prompt_version TEXT NOT NULL CHECK (
        prompt_version <> '' AND char_length(prompt_version) <= 100
    ),
    task_schema_version TEXT NOT NULL CHECK (
        task_schema_version <> '' AND char_length(task_schema_version) <= 100
    ),
    action_schema_version TEXT NOT NULL CHECK (
        action_schema_version <> '' AND char_length(action_schema_version) <= 100
    ),
    capability_version TEXT NOT NULL CHECK (
        capability_version <> '' AND char_length(capability_version) <= 100
    ),
    adapter_version TEXT NOT NULL CHECK (
        adapter_version <> '' AND char_length(adapter_version) <= 100
    ),
    max_input_bytes INTEGER NOT NULL CHECK (
        max_input_bytes BETWEEN 1 AND 1048576
    ),
    max_context_items INTEGER NOT NULL CHECK (
        max_context_items BETWEEN 1 AND 10000
    ),
    max_output_tokens INTEGER NOT NULL CHECK (
        max_output_tokens BETWEEN 1 AND 65536
    ),
    config_hash TEXT NOT NULL CHECK (
        config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'provisioning' CHECK (
        status IN ('provisioning', 'ready', 'degraded', 'disabled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (agent_id),
    UNIQUE (credential_id),
    UNIQUE (hosted_config_id, agent_id),
    FOREIGN KEY (owner_user_id, agent_id)
        REFERENCES arena_agents(owner_user_id, agent_id)
        ON DELETE CASCADE,
    FOREIGN KEY (credential_id, owner_user_id, provider)
        REFERENCES arena_model_credentials(
            credential_id,
            owner_user_id,
            provider
        )
        ON DELETE RESTRICT,
    CHECK (hosted_config_id <> '')
);

CREATE INDEX arena_hosted_configs_owner_status_idx
    ON arena_hosted_configs (owner_user_id, status);

CREATE TABLE arena_runtime_bindings (
    runtime_binding_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES arena_agents(agent_id) ON DELETE CASCADE,
    runtime_kind TEXT NOT NULL CHECK (
        runtime_kind IN ('hosted', 'connector', 'native_a2a')
    ),
    hosted_config_id TEXT,
    connector_binding_id TEXT
        REFERENCES connector_bindings(binding_id) ON DELETE RESTRICT,
    connector_binding_epoch BIGINT,
    native_endpoint_ref TEXT,
    route_status TEXT NOT NULL DEFAULT 'provisioning' CHECK (
        route_status IN ('provisioning', 'ready', 'degraded', 'disabled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    disabled_at TIMESTAMPTZ,
    UNIQUE (runtime_binding_id, agent_id),
    FOREIGN KEY (hosted_config_id, agent_id)
        REFERENCES arena_hosted_configs(hosted_config_id, agent_id)
        ON DELETE RESTRICT,
    CHECK (runtime_binding_id <> ''),
    CHECK (
        (
            runtime_kind = 'hosted'
            AND hosted_config_id IS NOT NULL
            AND connector_binding_id IS NULL
            AND connector_binding_epoch IS NULL
            AND native_endpoint_ref IS NULL
        )
        OR (
            runtime_kind = 'connector'
            AND hosted_config_id IS NULL
            AND connector_binding_id IS NOT NULL
            AND connector_binding_epoch > 0
            AND native_endpoint_ref IS NULL
        )
        OR (
            runtime_kind = 'native_a2a'
            AND hosted_config_id IS NULL
            AND connector_binding_id IS NULL
            AND connector_binding_epoch IS NULL
            AND native_endpoint_ref IS NOT NULL
            AND native_endpoint_ref <> ''
        )
    ),
    CHECK (
        (route_status = 'disabled' AND disabled_at IS NOT NULL)
        OR (route_status <> 'disabled' AND disabled_at IS NULL)
    )
);

CREATE UNIQUE INDEX arena_runtime_bindings_one_active_route_idx
    ON arena_runtime_bindings (agent_id)
    WHERE disabled_at IS NULL;
CREATE INDEX arena_runtime_bindings_connector_ref_idx
    ON arena_runtime_bindings (connector_binding_id, connector_binding_epoch)
    WHERE runtime_kind = 'connector';

CREATE TABLE game_agents (
    game_agent_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL
        REFERENCES connector_users(user_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL,
    runtime_binding_id TEXT NOT NULL,
    config_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(config_snapshot) = 'object'
    ),
    config_hash TEXT NOT NULL CHECK (
        config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'joined' CHECK (
        status IN (
            'joined',
            'active',
            'settling',
            'completed',
            'cancelled'
        )
    ),
    initial_cash_atomic NUMERIC(78, 0) NOT NULL DEFAULT 0 CHECK (
        initial_cash_atomic >= 0
    ),
    initial_inventory JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(initial_inventory) = 'object'
    ),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (game_id, user_id),
    UNIQUE (game_id, agent_id),
    UNIQUE (game_agent_id, game_id),
    UNIQUE (game_agent_id, runtime_binding_id),
    FOREIGN KEY (user_id, agent_id)
        REFERENCES arena_agents(owner_user_id, agent_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (runtime_binding_id, agent_id)
        REFERENCES arena_runtime_bindings(runtime_binding_id, agent_id)
        ON DELETE RESTRICT,
    CHECK (game_agent_id <> ''),
    CHECK (
        (status IN ('completed', 'cancelled') AND completed_at IS NOT NULL)
        OR (status IN ('joined', 'active', 'settling') AND completed_at IS NULL)
    )
);

CREATE INDEX game_agents_game_status_idx
    ON game_agents (game_id, status, joined_at);
CREATE INDEX game_agents_agent_history_idx
    ON game_agents (agent_id, joined_at DESC);

CREATE TABLE arena_agent_tasks (
    task_id TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL CHECK (
        task_kind IN ('arena.decide', 'arena.negotiate')
    ),
    schema_version TEXT NOT NULL CHECK (
        schema_version <> '' AND char_length(schema_version) <= 100
    ),
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    round_id TEXT NOT NULL,
    game_agent_id TEXT NOT NULL,
    runtime_binding_id TEXT NOT NULL
        REFERENCES arena_runtime_bindings(runtime_binding_id) ON DELETE RESTRICT,
    credential_id TEXT
        REFERENCES arena_model_credentials(credential_id) ON DELETE RESTRICT,
    negotiation_id TEXT,
    turn_sequence INTEGER,
    deadline_at TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (
        idempotency_key <> '' AND char_length(idempotency_key) <= 1024
    ),
    input_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(input_snapshot) = 'object'
    ),
    input_hash TEXT NOT NULL CHECK (
        input_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    runtime_config_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(runtime_config_snapshot) = 'object'
    ),
    config_hash TEXT NOT NULL CHECK (
        config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    default_result_id TEXT NOT NULL CHECK (
        default_result_id ~ '^default:[0-9a-f]{64}$'
    ),
    default_result_hash TEXT NOT NULL CHECK (
        default_result_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN (
            'queued',
            'leased',
            'running',
            'completed',
            'defaulted',
            'cancelled'
        )
    ),
    attempt_count SMALLINT NOT NULL DEFAULT 0 CHECK (
        attempt_count BETWEEN 0 AND 2
    ),
    leased_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    terminal_reason TEXT CHECK (
        terminal_reason IS NULL OR char_length(terminal_reason) <= 100
    ),
    UNIQUE (game_agent_id, idempotency_key),
    UNIQUE (task_id, game_id),
    FOREIGN KEY (round_id, game_id)
        REFERENCES rounds(round_id, game_id) ON DELETE CASCADE,
    FOREIGN KEY (game_agent_id, game_id)
        REFERENCES game_agents(game_agent_id, game_id) ON DELETE CASCADE,
    FOREIGN KEY (game_agent_id, runtime_binding_id)
        REFERENCES game_agents(game_agent_id, runtime_binding_id)
        ON DELETE RESTRICT,
    CHECK (task_id <> ''),
    CHECK (
        (
            task_kind = 'arena.decide'
            AND negotiation_id IS NULL
            AND turn_sequence IS NULL
        )
        OR (
            task_kind = 'arena.negotiate'
            AND negotiation_id IS NOT NULL
            AND negotiation_id <> ''
            AND turn_sequence > 0
        )
    ),
    CHECK (
        (
            status IN ('leased', 'running')
            AND leased_by IS NOT NULL
            AND leased_by <> ''
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status NOT IN ('leased', 'running')
            AND leased_by IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        (
            status IN ('completed', 'defaulted', 'cancelled')
            AND completed_at IS NOT NULL
        )
        OR (
            status IN ('queued', 'leased', 'running')
            AND completed_at IS NULL
        )
    )
);

CREATE INDEX arena_agent_tasks_claim_idx
    ON arena_agent_tasks (status, deadline_at, created_at);
CREATE INDEX arena_agent_tasks_game_round_idx
    ON arena_agent_tasks (game_id, round_id, game_agent_id);
CREATE INDEX arena_agent_tasks_negotiation_idx
    ON arena_agent_tasks (negotiation_id, turn_sequence)
    WHERE negotiation_id IS NOT NULL;

CREATE TABLE arena_agent_task_results (
    result_id TEXT PRIMARY KEY CHECK (
        result_id ~ '^(runtime|default):[0-9a-f]{64}$'
    ),
    task_id TEXT NOT NULL UNIQUE
        REFERENCES arena_agent_tasks(task_id) ON DELETE CASCADE,
    result_schema_version TEXT NOT NULL CHECK (
        result_schema_version <> ''
        AND char_length(result_schema_version) <= 100
    ),
    runtime_result_id_digest TEXT UNIQUE CHECK (
        runtime_result_id_digest IS NULL
        OR runtime_result_id_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    result_hash TEXT NOT NULL CHECK (
        result_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    runtime_status TEXT NOT NULL CHECK (
        runtime_status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
    ),
    candidate_action JSONB,
    message_replaced BOOLEAN NOT NULL DEFAULT FALSE,
    public_output_policy_version TEXT CHECK (
        public_output_policy_version IS NULL
        OR (
            public_output_policy_version <> ''
            AND char_length(public_output_policy_version) <= 100
        )
    ),
    result_received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    apply_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        apply_status IN ('pending', 'applied', 'rejected')
    ),
    arena_applied_at TIMESTAMPTZ,
    arena_rejected_at TIMESTAMPTZ,
    error_class TEXT CHECK (
        error_class IS NULL OR char_length(error_class) <= 100
    ),
    CHECK (
        (
            runtime_status = 'succeeded'
            AND candidate_action IS NOT NULL
            AND jsonb_typeof(candidate_action) = 'object'
        )
        OR (
            runtime_status <> 'succeeded'
            AND candidate_action IS NULL
        )
    ),
    CHECK (
        (
            apply_status = 'pending'
            AND arena_applied_at IS NULL
            AND arena_rejected_at IS NULL
        )
        OR (
            apply_status = 'applied'
            AND arena_applied_at IS NOT NULL
            AND arena_rejected_at IS NULL
        )
        OR (
            apply_status = 'rejected'
            AND arena_applied_at IS NULL
            AND arena_rejected_at IS NOT NULL
        )
    )
);

CREATE INDEX arena_agent_task_results_apply_idx
    ON arena_agent_task_results (apply_status, result_received_at);

CREATE TABLE arena_applied_agent_actions (
    task_id TEXT PRIMARY KEY
        REFERENCES arena_agent_tasks(task_id) ON DELETE RESTRICT,
    result_id TEXT NOT NULL UNIQUE
        REFERENCES arena_agent_task_results(result_id) ON DELETE RESTRICT,
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE RESTRICT,
    round_id TEXT NOT NULL,
    game_agent_id TEXT NOT NULL,
    task_kind TEXT NOT NULL CHECK (
        task_kind IN ('arena.decide', 'arena.negotiate')
    ),
    application_outcome TEXT NOT NULL CHECK (
        application_outcome IN (
            'candidate',
            'default_pass',
            'negotiation_timeout',
            'cancelled'
        )
    ),
    applied_action JSONB,
    authoritative_entered_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (round_id, game_id)
        REFERENCES rounds(round_id, game_id) ON DELETE RESTRICT,
    FOREIGN KEY (game_agent_id, game_id)
        REFERENCES game_agents(game_agent_id, game_id) ON DELETE RESTRICT,
    CHECK (
        (
            application_outcome IN ('candidate', 'default_pass')
            AND applied_action IS NOT NULL
            AND jsonb_typeof(applied_action) = 'object'
        )
        OR (
            application_outcome IN ('negotiation_timeout', 'cancelled')
            AND applied_action IS NULL
        )
    )
);

CREATE INDEX arena_applied_agent_actions_game_idx
    ON arena_applied_agent_actions (
        game_id,
        round_id,
        authoritative_entered_at,
        task_id
    );

CREATE TABLE arena_agent_task_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL
        REFERENCES arena_agent_tasks(task_id) ON DELETE CASCADE,
    attempt_no SMALLINT NOT NULL CHECK (
        attempt_no BETWEEN 1 AND 2
    ),
    worker_id TEXT NOT NULL CHECK (
        worker_id <> '' AND char_length(worker_id) <= 200
    ),
    provider TEXT NOT NULL CHECK (
        provider ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    requested_model TEXT NOT NULL CHECK (
        requested_model <> '' AND char_length(requested_model) <= 200
    ),
    actual_model TEXT CHECK (
        actual_model IS NULL OR char_length(actual_model) <= 200
    ),
    thinking_enabled BOOLEAN NOT NULL,
    status TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN ('created', 'request_sent', 'succeeded', 'failed', 'unknown')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    request_sent_at TIMESTAMPTZ,
    runtime_completed_at TIMESTAMPTZ,
    duration_ms BIGINT CHECK (
        duration_ms IS NULL OR duration_ms >= 0
    ),
    input_tokens BIGINT CHECK (
        input_tokens IS NULL OR input_tokens >= 0
    ),
    output_tokens BIGINT CHECK (
        output_tokens IS NULL OR output_tokens >= 0
    ),
    cached_tokens BIGINT CHECK (
        cached_tokens IS NULL OR cached_tokens >= 0
    ),
    reasoning_tokens BIGINT CHECK (
        reasoning_tokens IS NULL OR reasoning_tokens >= 0
    ),
    usage_complete BOOLEAN NOT NULL DEFAULT FALSE,
    provider_request_id_ref TEXT CHECK (
        provider_request_id_ref IS NULL
        OR char_length(provider_request_id_ref) <= 300
    ),
    error_class TEXT CHECK (
        error_class IS NULL OR char_length(error_class) <= 100
    ),
    UNIQUE (task_id, attempt_no),
    CHECK (attempt_id <> ''),
    CHECK (status IN ('created', 'failed') OR request_sent_at IS NOT NULL),
    CHECK (
        (
            status IN ('succeeded', 'failed', 'unknown')
            AND runtime_completed_at IS NOT NULL
        )
        OR status IN ('created', 'request_sent')
    )
);

CREATE INDEX arena_agent_task_attempts_task_idx
    ON arena_agent_task_attempts (task_id, attempt_no);

CREATE TABLE arena_agent_task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL
        REFERENCES arena_agent_tasks(task_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'created',
            'leased',
            'attempt_started',
            'attempt_failed',
            'result_submitted',
            'result_applied',
            'result_rejected',
            'duplicate_result_ignored',
            'result_conflict',
            'defaulted',
            'late_result_ignored',
            'cancelled'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    safe_metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(safe_metadata) = 'object'
    )
);

CREATE INDEX arena_agent_task_events_task_idx
    ON arena_agent_task_events (task_id, created_at, event_id);

CREATE TABLE hosted_credential_validation_jobs (
    validation_job_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL
        REFERENCES arena_agents(agent_id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL
        REFERENCES arena_model_credentials(credential_id) ON DELETE RESTRICT,
    hosted_config_id TEXT NOT NULL
        REFERENCES arena_hosted_configs(hosted_config_id) ON DELETE CASCADE,
    job_kind TEXT NOT NULL CHECK (
        job_kind IN ('create', 'update', 'replace')
    ),
    candidate_config_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(candidate_config_snapshot) = 'object'
    ),
    candidate_config_hash TEXT NOT NULL CHECK (
        candidate_config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    expected_current_config_hash TEXT NOT NULL CHECK (
        expected_current_config_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    validation_schema_version TEXT NOT NULL CHECK (
        validation_schema_version <> ''
        AND char_length(validation_schema_version) <= 100
    ),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN (
            'queued',
            'leased',
            'running',
            'succeeded',
            'failed',
            'cancelled'
        )
    ),
    attempt_no SMALLINT NOT NULL DEFAULT 0 CHECK (
        attempt_no >= 0
    ),
    max_attempts SMALLINT NOT NULL CHECK (
        max_attempts BETWEEN 1 AND 10
    ),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deadline_at TIMESTAMPTZ NOT NULL,
    leased_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_class TEXT CHECK (
        last_error_class IS NULL OR char_length(last_error_class) <= 100
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CHECK (validation_job_id <> ''),
    CHECK (attempt_no <= max_attempts),
    CHECK (
        (
            status IN ('leased', 'running')
            AND leased_by IS NOT NULL
            AND leased_by <> ''
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status NOT IN ('leased', 'running')
            AND leased_by IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        (
            status IN ('succeeded', 'failed', 'cancelled')
            AND completed_at IS NOT NULL
        )
        OR (
            status IN ('queued', 'leased', 'running')
            AND completed_at IS NULL
        )
    )
);

CREATE UNIQUE INDEX hosted_validation_one_active_config_credential_idx
    ON hosted_credential_validation_jobs (hosted_config_id, credential_id)
    WHERE status IN ('queued', 'leased', 'running');
CREATE UNIQUE INDEX hosted_validation_one_active_agent_idx
    ON hosted_credential_validation_jobs (agent_id)
    WHERE status IN ('queued', 'leased', 'running');
CREATE INDEX hosted_validation_claim_idx
    ON hosted_credential_validation_jobs (
        status,
        next_attempt_at,
        deadline_at,
        created_at
    );

ALTER TABLE arena_agents
    ADD CONSTRAINT arena_agents_runtime_update_job_fk
    FOREIGN KEY (runtime_update_job_id)
    REFERENCES hosted_credential_validation_jobs(validation_job_id)
    ON DELETE SET NULL
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE TABLE hosted_credential_lifecycle_jobs (
    lifecycle_job_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL
        REFERENCES arena_model_credentials(credential_id) ON DELETE RESTRICT,
    job_kind TEXT NOT NULL CHECK (
        job_kind IN ('revoke', 'delete')
    ),
    idempotency_key TEXT NOT NULL CHECK (
        idempotency_key <> '' AND char_length(idempotency_key) <= 500
    ),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN (
            'queued',
            'leased',
            'running',
            'succeeded',
            'failed',
            'cancelled'
        )
    ),
    attempt_no SMALLINT NOT NULL DEFAULT 0 CHECK (
        attempt_no BETWEEN 0 AND 20
    ),
    max_attempts SMALLINT NOT NULL DEFAULT 10 CHECK (
        max_attempts BETWEEN 1 AND 20
    ),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deadline_at TIMESTAMPTZ NOT NULL,
    leased_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_class TEXT CHECK (
        last_error_class IS NULL OR char_length(last_error_class) <= 100
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE (credential_id, idempotency_key),
    CHECK (lifecycle_job_id <> ''),
    CHECK (attempt_no <= max_attempts),
    CHECK (
        (
            status IN ('leased', 'running')
            AND leased_by IS NOT NULL
            AND leased_by <> ''
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status NOT IN ('leased', 'running')
            AND leased_by IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        (
            status IN ('succeeded', 'failed', 'cancelled')
            AND completed_at IS NOT NULL
        )
        OR (
            status IN ('queued', 'leased', 'running')
            AND completed_at IS NULL
        )
    )
);

CREATE INDEX hosted_lifecycle_claim_idx
    ON hosted_credential_lifecycle_jobs (
        status,
        next_attempt_at,
        deadline_at,
        created_at
    );

-- Hosted task execution is frozen at Task creation. This view intentionally
-- exposes no live Game/Round/Inventory state to the Hosted Worker.
CREATE VIEW arena_hosted_task_execution_v
WITH (security_barrier = true)
AS
SELECT
    t.task_id,
    t.task_kind,
    t.schema_version,
    t.game_id,
    t.round_id,
    t.game_agent_id,
    t.deadline_at,
    t.idempotency_key,
    t.input_snapshot,
    t.input_hash,
    t.runtime_config_snapshot,
    t.config_hash,
    t.attempt_count,
    t.status,
    t.leased_by,
    t.lease_expires_at,
    t.credential_id,
    c.provider,
    c.secret_ref
FROM arena_agent_tasks AS t
JOIN arena_runtime_bindings AS b
  ON b.runtime_binding_id = t.runtime_binding_id
JOIN arena_model_credentials AS c
  ON c.credential_id = t.credential_id
WHERE b.runtime_kind = 'hosted';

-- Validation execution excludes strategy instructions and all Game data even
-- though the private candidate snapshot can contain a strategy for later CAS.
CREATE VIEW hosted_credential_validation_execution_v
WITH (security_barrier = true)
AS
SELECT
    j.validation_job_id,
    j.agent_id,
    j.credential_id,
    j.hosted_config_id,
    j.job_kind,
    j.candidate_config_hash,
    j.expected_current_config_hash,
    j.validation_schema_version,
    j.status,
    j.attempt_no,
    j.max_attempts,
    j.next_attempt_at,
    j.deadline_at,
    j.leased_by,
    j.lease_expires_at,
    j.candidate_config_snapshot ->> 'provider' AS provider,
    j.candidate_config_snapshot ->> 'model' AS model,
    CASE
        WHEN j.candidate_config_snapshot ? 'thinking_enabled'
        THEN (j.candidate_config_snapshot ->> 'thinking_enabled')::boolean
        ELSE NULL
    END AS thinking_enabled,
    c.secret_ref
FROM hosted_credential_validation_jobs AS j
JOIN arena_model_credentials AS c
  ON c.credential_id = j.credential_id;

CREATE OR REPLACE FUNCTION claim_hosted_agent_tasks(
    p_worker_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS SETOF arena_hosted_task_execution_v
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $claim_hosted_tasks$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_worker_id IS NULL OR p_worker_id = ''
       OR char_length(p_worker_id) > 200 THEN
        RAISE EXCEPTION 'invalid worker id' USING ERRCODE = '22023';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 50 THEN
        RAISE EXCEPTION 'invalid claim limit' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds NOT BETWEEN 1 AND 600 THEN
        RAISE EXCEPTION 'invalid lease duration' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT t.task_id
        FROM public.arena_agent_tasks AS t
        JOIN public.arena_runtime_bindings AS b
          ON b.runtime_binding_id = t.runtime_binding_id
        JOIN public.arena_model_credentials AS c
          ON c.credential_id = t.credential_id
        WHERE t.deadline_at > v_now
          AND b.runtime_kind = 'hosted'
          AND b.route_status = 'ready'
          AND b.disabled_at IS NULL
          AND c.status = 'valid'
          AND (
              t.status = 'queued'
              OR (
                  t.status = 'leased'
                  AND t.lease_expires_at <= v_now
              )
          )
        ORDER BY t.deadline_at, t.created_at, t.task_id
        FOR UPDATE OF t SKIP LOCKED
        LIMIT p_limit
    ),
    updated AS (
        UPDATE public.arena_agent_tasks AS t
        SET status = 'leased',
            leased_by = p_worker_id,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
        FROM candidates AS c
        WHERE t.task_id = c.task_id
        RETURNING t.task_id
    ),
    event_rows AS (
        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        SELECT
            u.task_id || ':event:leased:' || txid_current()::text,
            u.task_id,
            'leased',
            v_now,
            jsonb_build_object('worker_id', p_worker_id)
        FROM updated AS u
        ON CONFLICT (event_id) DO NOTHING
        RETURNING task_id
    )
    SELECT execution.*
    FROM public.arena_hosted_task_execution_v AS execution
    JOIN updated AS u
      ON u.task_id = execution.task_id
    ORDER BY execution.deadline_at, execution.task_id;
END
$claim_hosted_tasks$;

CREATE OR REPLACE FUNCTION start_agent_task_attempt(
    p_task_id TEXT,
    p_worker_id TEXT,
    p_attempt_id TEXT,
    p_provider TEXT,
    p_requested_model TEXT,
    p_thinking_enabled BOOLEAN
)
RETURNS SMALLINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $start_task_attempt$
DECLARE
    v_task public.arena_agent_tasks%ROWTYPE;
    v_attempt_no SMALLINT;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    SELECT *
    INTO v_task
    FROM public.arena_agent_tasks
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'task not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_task.status NOT IN ('leased', 'running')
       OR v_task.leased_by IS DISTINCT FROM p_worker_id
       OR v_task.lease_expires_at <= v_now THEN
        RAISE EXCEPTION 'task lease is not owned by worker'
            USING ERRCODE = '55000';
    END IF;
    IF v_task.deadline_at <= v_now THEN
        RAISE EXCEPTION 'task deadline has passed' USING ERRCODE = '57014';
    END IF;
    IF v_task.attempt_count >= 2 THEN
        RAISE EXCEPTION 'task attempt limit reached' USING ERRCODE = '54000';
    END IF;

    v_attempt_no := (v_task.attempt_count + 1)::SMALLINT;

    INSERT INTO public.arena_agent_task_attempts (
        attempt_id,
        task_id,
        attempt_no,
        worker_id,
        provider,
        requested_model,
        thinking_enabled,
        status,
        created_at
    )
    VALUES (
        p_attempt_id,
        p_task_id,
        v_attempt_no,
        p_worker_id,
        p_provider,
        p_requested_model,
        p_thinking_enabled,
        'created',
        v_now
    );

    UPDATE public.arena_agent_tasks
    SET status = 'running',
        attempt_count = v_attempt_no
    WHERE task_id = p_task_id;

    INSERT INTO public.arena_agent_task_events (
        event_id,
        task_id,
        event_type,
        created_at,
        safe_metadata
    )
    VALUES (
        p_task_id || ':event:attempt:' || p_attempt_id,
        p_task_id,
        'attempt_started',
        v_now,
        jsonb_build_object('attempt_no', v_attempt_no)
    );

    RETURN v_attempt_no;
END
$start_task_attempt$;

CREATE OR REPLACE FUNCTION mark_agent_task_attempt_request_sent(
    p_attempt_id TEXT,
    p_worker_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $mark_attempt_sent$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    UPDATE public.arena_agent_task_attempts AS a
    SET status = 'request_sent',
        request_sent_at = v_now
    FROM public.arena_agent_tasks AS t
    WHERE a.attempt_id = p_attempt_id
      AND a.task_id = t.task_id
      AND a.worker_id = p_worker_id
      AND a.status = 'created'
      AND t.status = 'running'
      AND t.leased_by = p_worker_id
      AND t.lease_expires_at > v_now;

    RETURN FOUND;
END
$mark_attempt_sent$;

CREATE OR REPLACE FUNCTION complete_agent_task_attempt(
    p_attempt_id TEXT,
    p_worker_id TEXT,
    p_status TEXT,
    p_duration_ms BIGINT,
    p_actual_model TEXT DEFAULT NULL,
    p_input_tokens BIGINT DEFAULT NULL,
    p_output_tokens BIGINT DEFAULT NULL,
    p_cached_tokens BIGINT DEFAULT NULL,
    p_reasoning_tokens BIGINT DEFAULT NULL,
    p_usage_complete BOOLEAN DEFAULT FALSE,
    p_provider_request_id_ref TEXT DEFAULT NULL,
    p_error_class TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_task_attempt$
DECLARE
    v_task_id TEXT;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_status NOT IN ('succeeded', 'failed', 'unknown') THEN
        RAISE EXCEPTION 'invalid attempt terminal status' USING ERRCODE = '22023';
    END IF;

    UPDATE public.arena_agent_task_attempts AS a
    SET status = p_status,
        runtime_completed_at = v_now,
        duration_ms = p_duration_ms,
        actual_model = p_actual_model,
        input_tokens = p_input_tokens,
        output_tokens = p_output_tokens,
        cached_tokens = p_cached_tokens,
        reasoning_tokens = p_reasoning_tokens,
        usage_complete = p_usage_complete,
        provider_request_id_ref = p_provider_request_id_ref,
        error_class = p_error_class
    WHERE a.attempt_id = p_attempt_id
      AND a.worker_id = p_worker_id
      AND a.status IN ('created', 'request_sent')
    RETURNING a.task_id INTO v_task_id;

    IF v_task_id IS NULL THEN
        RETURN FALSE;
    END IF;

    IF p_status IN ('failed', 'unknown') THEN
        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        VALUES (
            v_task_id || ':event:attempt-terminal:' || p_attempt_id,
            v_task_id,
            'attempt_failed',
            v_now,
            jsonb_build_object(
                'status',
                p_status,
                'error_class',
                p_error_class
            )
        )
        ON CONFLICT (event_id) DO NOTHING;
    END IF;

    RETURN TRUE;
END
$complete_task_attempt$;

CREATE OR REPLACE FUNCTION submit_agent_task_result(
    p_task_id TEXT,
    p_runtime_result_id_digest TEXT,
    p_result_hash TEXT,
    p_result_schema_version TEXT,
    p_runtime_status TEXT,
    p_candidate_action JSONB DEFAULT NULL,
    p_message_replaced BOOLEAN DEFAULT FALSE,
    p_public_output_policy_version TEXT DEFAULT NULL,
    p_error_class TEXT DEFAULT NULL
)
RETURNS TABLE (
    disposition TEXT,
    authoritative_result_id TEXT,
    terminal_task_status TEXT,
    result_received_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $submit_task_result$
DECLARE
    v_task public.arena_agent_tasks%ROWTYPE;
    v_existing public.arena_agent_task_results%ROWTYPE;
    v_received_at TIMESTAMPTZ := clock_timestamp();
    v_terminal_status TEXT;
    v_internal_result_id TEXT;
BEGIN
    IF p_runtime_result_id_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_result_hash !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid Runtime result digest'
            USING ERRCODE = '22023';
    END IF;
    IF p_result_schema_version IS NULL
       OR p_result_schema_version = ''
       OR char_length(p_result_schema_version) > 100 THEN
        RAISE EXCEPTION 'invalid Result schema version'
            USING ERRCODE = '22023';
    END IF;
    IF p_runtime_status NOT IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN
        RAISE EXCEPTION 'invalid runtime result status' USING ERRCODE = '22023';
    END IF;
    IF (p_runtime_status = 'succeeded')
       IS DISTINCT FROM (
           p_candidate_action IS NOT NULL
           AND jsonb_typeof(p_candidate_action) = 'object'
       ) THEN
        RAISE EXCEPTION 'candidate action does not match runtime status'
            USING ERRCODE = '22023';
    END IF;

    v_internal_result_id :=
        'runtime:' || substring(p_runtime_result_id_digest FROM 8);

    -- Serialize the same opaque Runtime result id even when two different
    -- Task rows are submitted concurrently. The unique digest constraint is
    -- the final guard; this lock lets the loser produce a durable conflict
    -- receipt/event instead of surfacing an implementation-level unique-key
    -- error.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_runtime_result_id_digest, 0)
    );

    SELECT *
    INTO v_task
    FROM public.arena_agent_tasks
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'task not found' USING ERRCODE = 'P0002';
    END IF;

    SELECT *
    INTO v_existing
    FROM public.arena_agent_task_results AS r
    WHERE r.runtime_result_id_digest = p_runtime_result_id_digest
       OR r.task_id = p_task_id
    ORDER BY
        (r.runtime_result_id_digest = p_runtime_result_id_digest) DESC
    LIMIT 1;

    IF FOUND
       AND v_existing.runtime_result_id_digest = p_runtime_result_id_digest THEN
        IF v_existing.task_id = p_task_id
           AND v_existing.result_hash = p_result_hash THEN
            INSERT INTO public.arena_agent_task_events (
                event_id,
                task_id,
                event_type,
                created_at,
                safe_metadata
            )
            VALUES (
                p_task_id || ':event:duplicate:'
                    || substring(p_runtime_result_id_digest FROM 8),
                p_task_id,
                'duplicate_result_ignored',
                v_received_at,
                jsonb_build_object(
                    'runtime_result_id_digest',
                    p_runtime_result_id_digest,
                    'result_hash',
                    p_result_hash
                )
            )
            ON CONFLICT (event_id) DO NOTHING;

            RETURN QUERY
            SELECT
                'duplicate',
                v_existing.result_id,
                v_task.status,
                v_existing.result_received_at;
            RETURN;
        END IF;

        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        VALUES (
            p_task_id || ':event:conflict:'
                || substring(p_runtime_result_id_digest FROM 8),
            p_task_id,
            'result_conflict',
            v_received_at,
            jsonb_build_object(
                'runtime_result_id_digest',
                p_runtime_result_id_digest,
                'authoritative_result_hash',
                v_existing.result_hash,
                'incoming_result_hash',
                p_result_hash
            )
        )
        ON CONFLICT (event_id) DO NOTHING;

        RETURN QUERY
        SELECT
            'conflict',
            v_existing.result_id,
            v_task.status,
            v_existing.result_received_at;
        RETURN;
    END IF;

    IF FOUND
       OR v_task.status IN ('completed', 'defaulted', 'cancelled') THEN
        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        VALUES (
            p_task_id || ':event:late:'
                || substring(p_runtime_result_id_digest FROM 8),
            p_task_id,
            'late_result_ignored',
            v_received_at,
            jsonb_build_object(
                'runtime_result_id_digest',
                p_runtime_result_id_digest,
                'result_hash',
                p_result_hash,
                'runtime_status',
                p_runtime_status,
                'reason',
                'task_already_terminal'
            )
        )
        ON CONFLICT (event_id) DO NOTHING;

        RETURN QUERY
        SELECT
            'late',
            v_existing.result_id,
            v_task.status,
            v_received_at;
        RETURN;
    END IF;

    IF v_task.deadline_at <= v_received_at THEN
        UPDATE public.arena_agent_tasks
        SET status = 'defaulted',
            leased_by = NULL,
            lease_expires_at = NULL,
            completed_at = v_received_at,
            terminal_reason = 'deadline_exceeded'
        WHERE task_id = p_task_id
          AND status IN ('queued', 'leased', 'running');

        IF NOT FOUND THEN
            RETURN QUERY
            SELECT
                'late',
                NULL::TEXT,
                v_task.status,
                v_received_at;
            RETURN;
        END IF;

        INSERT INTO public.arena_agent_task_results (
            result_id,
            task_id,
            result_schema_version,
            runtime_result_id_digest,
            result_hash,
            runtime_status,
            candidate_action,
            result_received_at,
            apply_status,
            error_class
        )
        VALUES (
            v_task.default_result_id,
            p_task_id,
            'arena.agent-result.v1',
            NULL,
            v_task.default_result_hash,
            'timed_out',
            NULL,
            v_received_at,
            'pending',
            'deadline_exceeded'
        );

        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        VALUES (
            p_task_id || ':event:defaulted',
            p_task_id,
            'defaulted',
            v_received_at,
            jsonb_build_object(
                'result_hash',
                v_task.default_result_hash,
                'reason',
                'deadline_exceeded'
            )
        )
        ON CONFLICT (event_id) DO NOTHING;

        INSERT INTO public.arena_agent_task_events (
            event_id,
            task_id,
            event_type,
            created_at,
            safe_metadata
        )
        VALUES (
            p_task_id || ':event:late:'
                || substring(p_runtime_result_id_digest FROM 8),
            p_task_id,
            'late_result_ignored',
            v_received_at,
            jsonb_build_object(
                'runtime_result_id_digest',
                p_runtime_result_id_digest,
                'result_hash',
                p_result_hash,
                'reason',
                'deadline_exceeded'
            )
        )
        ON CONFLICT (event_id) DO NOTHING;

        RETURN QUERY
        SELECT
            'late',
            v_task.default_result_id,
            'defaulted',
            v_received_at;
        RETURN;
    END IF;

    v_terminal_status := CASE p_runtime_status
        WHEN 'succeeded' THEN 'completed'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'defaulted'
    END;

    UPDATE public.arena_agent_tasks
    SET status = v_terminal_status,
        leased_by = NULL,
        lease_expires_at = NULL,
        completed_at = v_received_at,
        terminal_reason = CASE
            WHEN p_runtime_status = 'succeeded' THEN NULL
            ELSE p_error_class
        END
    WHERE task_id = p_task_id
      AND status IN ('queued', 'leased', 'running');

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            'late',
            NULL::TEXT,
            v_task.status,
            v_received_at;
        RETURN;
    END IF;

    INSERT INTO public.arena_agent_task_results (
        result_id,
        task_id,
        result_schema_version,
        runtime_result_id_digest,
        result_hash,
        runtime_status,
        candidate_action,
        message_replaced,
        public_output_policy_version,
        result_received_at,
        apply_status,
        error_class
    )
    VALUES (
        v_internal_result_id,
        p_task_id,
        p_result_schema_version,
        p_runtime_result_id_digest,
        p_result_hash,
        p_runtime_status,
        p_candidate_action,
        p_message_replaced,
        p_public_output_policy_version,
        v_received_at,
        'pending',
        p_error_class
    );

    INSERT INTO public.arena_agent_task_events (
        event_id,
        task_id,
        event_type,
        created_at,
        safe_metadata
    )
    VALUES (
        p_task_id || ':event:result:'
            || substring(p_runtime_result_id_digest FROM 8),
        p_task_id,
        CASE
            WHEN p_runtime_status = 'cancelled' THEN 'cancelled'
            ELSE 'result_submitted'
        END,
        v_received_at,
        jsonb_build_object(
            'runtime_result_id_digest',
            p_runtime_result_id_digest,
            'result_hash',
            p_result_hash,
            'runtime_status',
            p_runtime_status
        )
    );

    RETURN QUERY
    SELECT
        'accepted',
        v_internal_result_id,
        v_terminal_status,
        v_received_at;
END
$submit_task_result$;

CREATE OR REPLACE FUNCTION submit_hosted_agent_task_result(
    p_worker_id TEXT,
    p_task_id TEXT,
    p_runtime_result_id_digest TEXT,
    p_result_hash TEXT,
    p_result_schema_version TEXT,
    p_runtime_status TEXT,
    p_candidate_action JSONB DEFAULT NULL,
    p_message_replaced BOOLEAN DEFAULT FALSE,
    p_public_output_policy_version TEXT DEFAULT NULL,
    p_error_class TEXT DEFAULT NULL
)
RETURNS TABLE (
    disposition TEXT,
    authoritative_result_id TEXT,
    terminal_task_status TEXT,
    result_received_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $submit_hosted_task_result$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    PERFORM 1
    FROM public.arena_agent_tasks AS t
    JOIN public.arena_runtime_bindings AS b
      ON b.runtime_binding_id = t.runtime_binding_id
    WHERE t.task_id = p_task_id
      AND b.runtime_kind = 'hosted'
      AND (
          (
              t.status = 'running'
              AND t.leased_by = p_worker_id
              AND t.lease_expires_at > v_now
          )
          OR t.status IN ('completed', 'defaulted', 'cancelled')
      )
    FOR UPDATE OF t;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'hosted task lease is not owned by worker'
            USING ERRCODE = '55000';
    END IF;

    RETURN QUERY
    SELECT submitted.*
    FROM public.submit_agent_task_result(
        p_task_id,
        p_runtime_result_id_digest,
        p_result_hash,
        p_result_schema_version,
        p_runtime_status,
        p_candidate_action,
        p_message_replaced,
        p_public_output_policy_version,
        p_error_class
    ) AS submitted;
END
$submit_hosted_task_result$;

CREATE OR REPLACE FUNCTION finalize_expired_agent_task(
    p_task_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $finalize_expired_task$
DECLARE
    v_task public.arena_agent_tasks%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    SELECT *
    INTO v_task
    FROM public.arena_agent_tasks
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    IF v_task.status NOT IN ('queued', 'leased', 'running')
       OR v_task.deadline_at > v_now THEN
        RETURN FALSE;
    END IF;

    UPDATE public.arena_agent_tasks
    SET status = 'defaulted',
        leased_by = NULL,
        lease_expires_at = NULL,
        completed_at = v_now,
        terminal_reason = 'deadline_exceeded'
    WHERE task_id = p_task_id
      AND status IN ('queued', 'leased', 'running');

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    INSERT INTO public.arena_agent_task_results (
        result_id,
        task_id,
        result_schema_version,
        runtime_result_id_digest,
        result_hash,
        runtime_status,
        candidate_action,
        result_received_at,
        apply_status,
        error_class
    )
    VALUES (
        v_task.default_result_id,
        p_task_id,
        'arena.agent-result.v1',
        NULL,
        v_task.default_result_hash,
        'timed_out',
        NULL,
        v_now,
        'pending',
        'deadline_exceeded'
    );

    INSERT INTO public.arena_agent_task_events (
        event_id,
        task_id,
        event_type,
        created_at,
        safe_metadata
    )
    VALUES (
        p_task_id || ':event:defaulted',
        p_task_id,
        'defaulted',
        v_now,
        jsonb_build_object(
            'result_hash',
            v_task.default_result_hash,
            'reason',
            'deadline_exceeded'
        )
    );

    RETURN TRUE;
END
$finalize_expired_task$;

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

    IF v_result.runtime_status = 'succeeded'
       AND v_task.task_kind = 'arena.decide'
       AND v_action_name IN ('buy', 'sell', 'pass')
       AND (
           COALESCE(jsonb_typeof(v_allowed_actions) <> 'array', TRUE)
           OR v_allowed_actions @> jsonb_build_array(v_action_name)
       )
       AND (
           v_action_name = 'pass'
           OR (
               v_result.candidate_action ->> 'good' IS NOT NULL
               AND (
                   COALESCE(jsonb_typeof(v_allowed_goods) <> 'array', TRUE)
                   OR jsonb_array_length(v_allowed_goods) = 0
                   OR v_allowed_goods @> jsonb_build_array(
                       v_result.candidate_action ->> 'good'
                   )
               )
           )
       ) THEN
        v_application_outcome := 'candidate';
        v_applied_action := v_result.candidate_action;
    ELSIF v_result.runtime_status = 'succeeded'
          AND v_task.task_kind = 'arena.negotiate'
          AND v_action_name IN ('propose', 'accept', 'reject') THEN
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
        arena_applied_at = v_now
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
        jsonb_build_object(
            'result_hash',
            v_result.result_hash,
            'application_outcome',
            v_application_outcome
        )
    );

    RETURN TRUE;
END
$apply_task_result$;

CREATE OR REPLACE FUNCTION reject_arena_agent_task_result(
    p_result_id TEXT,
    p_error_class TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $reject_task_result$
DECLARE
    v_task_id TEXT;
    v_result_hash TEXT;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    UPDATE public.arena_agent_task_results
    SET apply_status = 'rejected',
        arena_rejected_at = v_now,
        error_class = COALESCE(p_error_class, error_class)
    WHERE result_id = p_result_id
      AND apply_status = 'pending'
    RETURNING task_id, result_hash INTO v_task_id, v_result_hash;

    IF v_task_id IS NULL THEN
        RETURN FALSE;
    END IF;

    INSERT INTO public.arena_agent_task_events (
        event_id,
        task_id,
        event_type,
        created_at,
        safe_metadata
    )
    VALUES (
        v_task_id || ':event:rejected:'
            || substring(v_result_hash FROM 8),
        v_task_id,
        'result_rejected',
        v_now,
        jsonb_build_object(
            'result_hash',
            v_result_hash,
            'error_class',
            p_error_class
        )
    );

    RETURN TRUE;
END
$reject_task_result$;

CREATE OR REPLACE FUNCTION claim_credential_validation_jobs(
    p_worker_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS TABLE (
    validation_job_id TEXT,
    agent_id TEXT,
    credential_id TEXT,
    hosted_config_id TEXT,
    job_kind TEXT,
    candidate_config_hash TEXT,
    expected_current_config_hash TEXT,
    validation_schema_version TEXT,
    attempt_no SMALLINT,
    max_attempts SMALLINT,
    deadline_at TIMESTAMPTZ,
    provider TEXT,
    model TEXT,
    thinking_enabled BOOLEAN,
    secret_ref TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $claim_validation_jobs$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_worker_id IS NULL OR p_worker_id = ''
       OR char_length(p_worker_id) > 200 THEN
        RAISE EXCEPTION 'invalid worker id' USING ERRCODE = '22023';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 20 THEN
        RAISE EXCEPTION 'invalid claim limit' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds NOT BETWEEN 1 AND 600 THEN
        RAISE EXCEPTION 'invalid lease duration' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT j.validation_job_id
        FROM public.hosted_credential_validation_jobs AS j
        JOIN public.arena_agents AS a
          ON a.agent_id = j.agent_id
        JOIN public.arena_model_credentials AS c
          ON c.credential_id = j.credential_id
        WHERE j.deadline_at > v_now
          AND j.attempt_no < j.max_attempts
          AND a.status = 'active'
          AND a.runtime_update_job_id = j.validation_job_id
          AND c.status = 'pending_validation'
          AND (
              (
                  j.status = 'queued'
                  AND j.next_attempt_at <= v_now
              )
              OR (
                  j.status IN ('leased', 'running')
                  AND j.lease_expires_at <= v_now
              )
          )
        ORDER BY j.next_attempt_at, j.created_at, j.validation_job_id
        FOR UPDATE OF j SKIP LOCKED
        LIMIT p_limit
    ),
    updated AS (
        UPDATE public.hosted_credential_validation_jobs AS j
        SET status = 'leased',
            leased_by = p_worker_id,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
        FROM candidates AS c
        WHERE j.validation_job_id = c.validation_job_id
        RETURNING j.validation_job_id
    )
    SELECT
        execution.validation_job_id,
        execution.agent_id,
        execution.credential_id,
        execution.hosted_config_id,
        execution.job_kind,
        execution.candidate_config_hash,
        execution.expected_current_config_hash,
        execution.validation_schema_version,
        execution.attempt_no,
        execution.max_attempts,
        execution.deadline_at,
        execution.provider,
        execution.model,
        execution.thinking_enabled,
        execution.secret_ref
    FROM public.hosted_credential_validation_execution_v AS execution
    JOIN updated AS u
      ON u.validation_job_id = execution.validation_job_id
    ORDER BY execution.deadline_at, execution.validation_job_id;
END
$claim_validation_jobs$;

CREATE OR REPLACE FUNCTION record_credential_validation_attempt(
    p_validation_job_id TEXT,
    p_worker_id TEXT
)
RETURNS SMALLINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $record_validation_attempt$
DECLARE
    v_attempt_no SMALLINT;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    UPDATE public.hosted_credential_validation_jobs
    SET status = 'running',
        attempt_no = attempt_no + 1
    WHERE validation_job_id = p_validation_job_id
      AND status IN ('leased', 'running')
      AND leased_by = p_worker_id
      AND lease_expires_at > v_now
      AND deadline_at > v_now
      AND attempt_no < max_attempts
    RETURNING attempt_no INTO v_attempt_no;

    IF v_attempt_no IS NULL THEN
        RAISE EXCEPTION 'validation job lease or attempt budget is invalid'
            USING ERRCODE = '55000';
    END IF;

    RETURN v_attempt_no;
END
$record_validation_attempt$;

CREATE OR REPLACE FUNCTION complete_credential_validation(
    p_validation_job_id TEXT,
    p_worker_id TEXT,
    p_candidate_config_hash TEXT,
    p_expected_current_config_hash TEXT,
    p_outcome TEXT,
    p_error_class TEXT DEFAULT NULL,
    p_retry_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_validation$
DECLARE
    v_job public.hosted_credential_validation_jobs%ROWTYPE;
    v_agent public.arena_agents%ROWTYPE;
    v_config public.arena_hosted_configs%ROWTYPE;
    v_credential public.arena_model_credentials%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
    v_effective_outcome TEXT := p_outcome;
    v_old_credential_id TEXT;
    v_provider TEXT;
    v_model TEXT;
    v_prompt_version TEXT;
    v_task_schema_version TEXT;
    v_action_schema_version TEXT;
    v_capability_version TEXT;
    v_adapter_version TEXT;
    v_strategy TEXT;
    v_thinking BOOLEAN;
    v_max_input_bytes INTEGER;
    v_max_context_items INTEGER;
    v_max_output_tokens INTEGER;
BEGIN
    IF p_outcome NOT IN (
        'succeeded',
        'permanent_failure',
        'transient_failure',
        'cancelled'
    ) THEN
        RAISE EXCEPTION 'invalid validation outcome' USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_job
    FROM public.hosted_credential_validation_jobs
    WHERE validation_job_id = p_validation_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'validation job not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_job.status <> 'running'
       OR v_job.leased_by IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at <= v_now THEN
        RAISE EXCEPTION 'validation job lease is invalid'
            USING ERRCODE = '55000';
    END IF;
    IF v_job.candidate_config_hash IS DISTINCT FROM p_candidate_config_hash
       OR v_job.expected_current_config_hash
          IS DISTINCT FROM p_expected_current_config_hash THEN
        RAISE EXCEPTION 'validation job hash mismatch' USING ERRCODE = '40001';
    END IF;

    SELECT *
    INTO STRICT v_agent
    FROM public.arena_agents
    WHERE agent_id = v_job.agent_id
    FOR UPDATE;

    IF v_agent.runtime_update_job_id IS DISTINCT FROM p_validation_job_id THEN
        RAISE EXCEPTION 'stale validation job' USING ERRCODE = '40001';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM public.game_agents AS ga
        WHERE ga.agent_id = v_job.agent_id
          AND ga.status IN ('joined', 'active', 'settling')
    ) THEN
        RAISE EXCEPTION 'agent has an active game' USING ERRCODE = '55000';
    END IF;

    SELECT *
    INTO STRICT v_config
    FROM public.arena_hosted_configs
    WHERE hosted_config_id = v_job.hosted_config_id
      AND agent_id = v_job.agent_id
    FOR UPDATE;

    IF v_config.config_hash
       IS DISTINCT FROM v_job.expected_current_config_hash THEN
        RAISE EXCEPTION 'hosted config changed during validation'
            USING ERRCODE = '40001';
    END IF;

    SELECT *
    INTO STRICT v_credential
    FROM public.arena_model_credentials
    WHERE credential_id = v_job.credential_id
    FOR UPDATE;

    IF v_credential.owner_user_id IS DISTINCT FROM v_config.owner_user_id THEN
        RAISE EXCEPTION 'credential owner mismatch' USING ERRCODE = '42501';
    END IF;

    IF v_effective_outcome = 'transient_failure'
       AND (
           v_job.attempt_no >= v_job.max_attempts
           OR v_job.deadline_at <= v_now
           OR p_retry_at IS NULL
           OR p_retry_at <= v_now
           OR p_retry_at >= v_job.deadline_at
       ) THEN
        v_effective_outcome := 'permanent_failure';
    END IF;

    IF v_effective_outcome = 'transient_failure' THEN
        UPDATE public.hosted_credential_validation_jobs
        SET status = 'queued',
            next_attempt_at = p_retry_at,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = p_error_class
        WHERE validation_job_id = p_validation_job_id;
        RETURN 'queued';
    END IF;

    IF v_effective_outcome = 'succeeded' THEN
        v_provider := v_job.candidate_config_snapshot ->> 'provider';
        v_model := v_job.candidate_config_snapshot ->> 'model';
        v_prompt_version :=
            v_job.candidate_config_snapshot ->> 'prompt_version';
        v_task_schema_version :=
            v_job.candidate_config_snapshot ->> 'task_schema_version';
        v_action_schema_version :=
            v_job.candidate_config_snapshot ->> 'action_schema_version';
        v_capability_version :=
            v_job.candidate_config_snapshot ->> 'capability_version';
        v_adapter_version :=
            v_job.candidate_config_snapshot ->> 'adapter_version';
        v_strategy := COALESCE(
            v_job.candidate_config_snapshot ->> 'strategy_instructions',
            ''
        );
        v_thinking :=
            (v_job.candidate_config_snapshot ->> 'thinking_enabled')::BOOLEAN;
        v_max_input_bytes :=
            (v_job.candidate_config_snapshot ->> 'max_input_bytes')::INTEGER;
        v_max_context_items :=
            (v_job.candidate_config_snapshot ->> 'max_context_items')::INTEGER;
        v_max_output_tokens :=
            (v_job.candidate_config_snapshot ->> 'max_output_tokens')::INTEGER;

        IF v_provider IS NULL OR v_model IS NULL
           OR v_prompt_version IS NULL
           OR v_task_schema_version IS NULL
           OR v_action_schema_version IS NULL
           OR v_capability_version IS NULL
           OR v_adapter_version IS NULL THEN
            RAISE EXCEPTION 'candidate config is incomplete'
                USING ERRCODE = '22023';
        END IF;
        IF v_provider IS DISTINCT FROM v_credential.provider THEN
            RAISE EXCEPTION 'candidate provider does not match credential'
                USING ERRCODE = '22023';
        END IF;

        v_old_credential_id := v_config.credential_id;

        UPDATE public.arena_hosted_configs
        SET credential_id = v_job.credential_id,
            provider = v_provider,
            model = v_model,
            thinking_enabled = v_thinking,
            strategy_instructions = v_strategy,
            prompt_version = v_prompt_version,
            task_schema_version = v_task_schema_version,
            action_schema_version = v_action_schema_version,
            capability_version = v_capability_version,
            adapter_version = v_adapter_version,
            max_input_bytes = v_max_input_bytes,
            max_context_items = v_max_context_items,
            max_output_tokens = v_max_output_tokens,
            config_hash = v_job.candidate_config_hash,
            status = 'ready',
            updated_at = v_now
        WHERE hosted_config_id = v_job.hosted_config_id
          AND config_hash = v_job.expected_current_config_hash;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'hosted config CAS failed' USING ERRCODE = '40001';
        END IF;

        UPDATE public.arena_model_credentials
        SET status = 'valid',
            last_validated_at = v_now,
            unbound_expires_at = NULL,
            updated_at = v_now
        WHERE credential_id = v_job.credential_id;

        UPDATE public.arena_runtime_bindings
        SET route_status = 'ready',
            updated_at = v_now
        WHERE hosted_config_id = v_job.hosted_config_id
          AND disabled_at IS NULL;

        UPDATE public.hosted_credential_validation_jobs
        SET status = 'succeeded',
            candidate_config_snapshot = '{}'::JSONB,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = NULL,
            completed_at = v_now
        WHERE validation_job_id = p_validation_job_id;

        UPDATE public.arena_agents
        SET runtime_update_job_id = NULL,
            updated_at = v_now
        WHERE agent_id = v_job.agent_id
          AND runtime_update_job_id = p_validation_job_id;

        IF v_old_credential_id <> v_job.credential_id THEN
            UPDATE public.arena_model_credentials
            SET status = 'revoking',
                replaced_at = v_now,
                updated_at = v_now
            WHERE credential_id = v_old_credential_id
              AND status NOT IN ('revoking', 'revoked');

            INSERT INTO public.hosted_credential_lifecycle_jobs (
                lifecycle_job_id,
                credential_id,
                job_kind,
                idempotency_key,
                status,
                max_attempts,
                next_attempt_at,
                deadline_at,
                created_at
            )
            VALUES (
                'lifecycle:replace:' || p_validation_job_id,
                v_old_credential_id,
                'revoke',
                'replace:' || p_validation_job_id,
                'queued',
                10,
                v_now,
                v_now + INTERVAL '7 days',
                v_now
            )
            ON CONFLICT (credential_id, idempotency_key) DO NOTHING;
        END IF;

        RETURN 'succeeded';
    END IF;

    IF v_effective_outcome = 'permanent_failure' THEN
        IF v_job.job_kind IN ('create', 'replace') THEN
            UPDATE public.arena_model_credentials
            SET status = 'invalid',
                updated_at = v_now
            WHERE credential_id = v_job.credential_id
              AND status <> 'revoked';
        END IF;

        IF v_job.job_kind = 'create'
           AND v_config.credential_id = v_job.credential_id
           AND v_config.status = 'provisioning' THEN
            UPDATE public.arena_hosted_configs
            SET status = 'degraded',
                updated_at = v_now
            WHERE hosted_config_id = v_job.hosted_config_id;

            UPDATE public.arena_runtime_bindings
            SET route_status = 'degraded',
                updated_at = v_now
            WHERE hosted_config_id = v_job.hosted_config_id
              AND disabled_at IS NULL;
        END IF;

        UPDATE public.hosted_credential_validation_jobs
        SET status = 'failed',
            candidate_config_snapshot = '{}'::JSONB,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = p_error_class,
            completed_at = v_now
        WHERE validation_job_id = p_validation_job_id;
    ELSE
        UPDATE public.hosted_credential_validation_jobs
        SET status = 'cancelled',
            candidate_config_snapshot = '{}'::JSONB,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = p_error_class,
            completed_at = v_now
        WHERE validation_job_id = p_validation_job_id;
    END IF;

    UPDATE public.arena_agents
    SET runtime_update_job_id = NULL,
        updated_at = v_now
    WHERE agent_id = v_job.agent_id
      AND runtime_update_job_id = p_validation_job_id;

    RETURN CASE
        WHEN v_effective_outcome = 'permanent_failure' THEN 'failed'
        ELSE 'cancelled'
    END;
END
$complete_validation$;

CREATE OR REPLACE FUNCTION claim_credential_lifecycle_jobs(
    p_controller_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS TABLE (
    lifecycle_job_id TEXT,
    credential_id TEXT,
    job_kind TEXT,
    secret_ref TEXT,
    attempt_no SMALLINT,
    max_attempts SMALLINT,
    deadline_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $claim_lifecycle_jobs$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF p_controller_id IS NULL OR p_controller_id = ''
       OR char_length(p_controller_id) > 200 THEN
        RAISE EXCEPTION 'invalid controller id' USING ERRCODE = '22023';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 20 THEN
        RAISE EXCEPTION 'invalid claim limit' USING ERRCODE = '22023';
    END IF;
    IF p_lease_seconds NOT BETWEEN 1 AND 600 THEN
        RAISE EXCEPTION 'invalid lease duration' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT j.lifecycle_job_id
        FROM public.hosted_credential_lifecycle_jobs AS j
        WHERE j.deadline_at > v_now
          AND j.attempt_no < j.max_attempts
          AND (
              (
                  j.status = 'queued'
                  AND j.next_attempt_at <= v_now
              )
              OR (
                  j.status IN ('leased', 'running')
                  AND j.lease_expires_at <= v_now
              )
          )
        ORDER BY j.next_attempt_at, j.created_at, j.lifecycle_job_id
        FOR UPDATE OF j SKIP LOCKED
        LIMIT p_limit
    ),
    updated AS (
        UPDATE public.hosted_credential_lifecycle_jobs AS j
        SET status = 'running',
            attempt_no = j.attempt_no + 1,
            leased_by = p_controller_id,
            lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
        FROM candidates AS c
        WHERE j.lifecycle_job_id = c.lifecycle_job_id
        RETURNING
            j.lifecycle_job_id,
            j.credential_id,
            j.job_kind,
            j.attempt_no,
            j.max_attempts,
            j.deadline_at
    )
    SELECT
        u.lifecycle_job_id,
        u.credential_id,
        u.job_kind,
        c.secret_ref,
        u.attempt_no,
        u.max_attempts,
        u.deadline_at
    FROM updated AS u
    JOIN public.arena_model_credentials AS c
      ON c.credential_id = u.credential_id
    ORDER BY u.deadline_at, u.lifecycle_job_id;
END
$claim_lifecycle_jobs$;

CREATE OR REPLACE FUNCTION complete_credential_lifecycle_job(
    p_lifecycle_job_id TEXT,
    p_controller_id TEXT,
    p_succeeded BOOLEAN,
    p_error_class TEXT DEFAULT NULL,
    p_retry_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_lifecycle_job$
DECLARE
    v_job public.hosted_credential_lifecycle_jobs%ROWTYPE;
    v_now TIMESTAMPTZ := clock_timestamp();
BEGIN
    SELECT *
    INTO v_job
    FROM public.hosted_credential_lifecycle_jobs
    WHERE lifecycle_job_id = p_lifecycle_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'lifecycle job not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_job.status <> 'running'
       OR v_job.leased_by IS DISTINCT FROM p_controller_id
       OR v_job.lease_expires_at <= v_now THEN
        RAISE EXCEPTION 'lifecycle job lease is invalid'
            USING ERRCODE = '55000';
    END IF;

    IF p_succeeded THEN
        UPDATE public.arena_model_credentials
        SET status = 'revoked',
            revoked_at = COALESCE(revoked_at, v_now),
            secret_deleted_at = CASE
                WHEN v_job.job_kind = 'delete' THEN v_now
                ELSE secret_deleted_at
            END,
            updated_at = v_now
        WHERE credential_id = v_job.credential_id;

        UPDATE public.hosted_credential_lifecycle_jobs
        SET status = 'succeeded',
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = NULL,
            completed_at = v_now
        WHERE lifecycle_job_id = p_lifecycle_job_id;
        RETURN 'succeeded';
    END IF;

    IF v_job.attempt_no < v_job.max_attempts
       AND v_job.deadline_at > v_now
       AND p_retry_at IS NOT NULL
       AND p_retry_at > v_now
       AND p_retry_at < v_job.deadline_at THEN
        UPDATE public.hosted_credential_lifecycle_jobs
        SET status = 'queued',
            next_attempt_at = p_retry_at,
            leased_by = NULL,
            lease_expires_at = NULL,
            last_error_class = p_error_class
        WHERE lifecycle_job_id = p_lifecycle_job_id;
        RETURN 'queued';
    END IF;

    UPDATE public.hosted_credential_lifecycle_jobs
    SET status = 'failed',
        leased_by = NULL,
        lease_expires_at = NULL,
        last_error_class = p_error_class,
        completed_at = v_now
    WHERE lifecycle_job_id = p_lifecycle_job_id;
    RETURN 'failed';
END
$complete_lifecycle_job$;

-- Arena migration role owns DDL objects. Runtime roles receive no ownership
-- and no CREATE privilege in the shared schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO adx_arena_migration;
GRANT USAGE ON SCHEMA public TO
    adx_arena_core,
    adx_arena_api,
    adx_hosted_worker,
    adx_credential_controller,
    adx_arena_function_owner;

ALTER TABLE games OWNER TO adx_arena_migration;
ALTER TABLE rounds OWNER TO adx_arena_migration;
ALTER TABLE arena_agents OWNER TO adx_arena_migration;
ALTER TABLE arena_model_credentials OWNER TO adx_arena_migration;
ALTER TABLE arena_hosted_configs OWNER TO adx_arena_migration;
ALTER TABLE arena_runtime_bindings OWNER TO adx_arena_migration;
ALTER TABLE game_agents OWNER TO adx_arena_migration;
ALTER TABLE arena_agent_tasks OWNER TO adx_arena_migration;
ALTER TABLE arena_agent_task_results OWNER TO adx_arena_migration;
ALTER TABLE arena_applied_agent_actions OWNER TO adx_arena_migration;
ALTER TABLE arena_agent_task_attempts OWNER TO adx_arena_migration;
ALTER TABLE arena_agent_task_events OWNER TO adx_arena_migration;
ALTER TABLE hosted_credential_validation_jobs OWNER TO adx_arena_migration;
ALTER TABLE hosted_credential_lifecycle_jobs OWNER TO adx_arena_migration;
ALTER VIEW arena_hosted_task_execution_v OWNER TO adx_arena_migration;
ALTER VIEW hosted_credential_validation_execution_v
    OWNER TO adx_arena_migration;

ALTER FUNCTION claim_hosted_agent_tasks(TEXT, INTEGER, INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION start_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    BOOLEAN
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION mark_agent_task_attempt_request_sent(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION complete_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    BIGINT,
    TEXT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BOOLEAN,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION submit_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION submit_hosted_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION finalize_expired_agent_task(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION apply_arena_agent_task_result(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION reject_arena_agent_task_result(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION claim_credential_validation_jobs(TEXT, INTEGER, INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION record_credential_validation_attempt(TEXT, TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION complete_credential_validation(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TIMESTAMPTZ
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION claim_credential_lifecycle_jobs(TEXT, INTEGER, INTEGER)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION complete_credential_lifecycle_job(
    TEXT,
    TEXT,
    BOOLEAN,
    TEXT,
    TIMESTAMPTZ
) OWNER TO adx_arena_function_owner;

REVOKE ALL ON
    games,
    rounds,
    arena_agents,
    arena_model_credentials,
    arena_hosted_configs,
    arena_runtime_bindings,
    game_agents,
    arena_agent_tasks,
    arena_agent_task_results,
    arena_applied_agent_actions,
    arena_agent_task_attempts,
    arena_agent_task_events,
    hosted_credential_validation_jobs,
    hosted_credential_lifecycle_jobs,
    arena_hosted_task_execution_v,
    hosted_credential_validation_execution_v
FROM PUBLIC;

REVOKE ALL ON FUNCTION claim_hosted_agent_tasks(TEXT, INTEGER, INTEGER)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION start_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    BOOLEAN
) FROM PUBLIC;
REVOKE ALL ON FUNCTION mark_agent_task_attempt_request_sent(TEXT, TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    BIGINT,
    TEXT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BOOLEAN,
    TEXT,
    TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION submit_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION submit_hosted_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION finalize_expired_agent_task(TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION apply_arena_agent_task_result(TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION reject_arena_agent_task_result(TEXT, TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION claim_credential_validation_jobs(
    TEXT,
    INTEGER,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION record_credential_validation_attempt(TEXT, TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_credential_validation(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION claim_credential_lifecycle_jobs(
    TEXT,
    INTEGER,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_credential_lifecycle_job(
    TEXT,
    TEXT,
    BOOLEAN,
    TEXT,
    TIMESTAMPTZ
) FROM PUBLIC;

-- SECURITY DEFINER owner receives only the base privileges its fixed-search-
-- path functions require. It cannot create objects and cannot log in.
GRANT SELECT ON
    games,
    rounds,
    arena_agents,
    arena_model_credentials,
    arena_hosted_configs,
    arena_runtime_bindings,
    game_agents,
    arena_agent_tasks,
    arena_agent_task_results,
    arena_applied_agent_actions,
    arena_agent_task_attempts,
    arena_agent_task_events,
    hosted_credential_validation_jobs,
    hosted_credential_lifecycle_jobs,
    arena_hosted_task_execution_v,
    hosted_credential_validation_execution_v
TO adx_arena_function_owner;
GRANT INSERT ON
    arena_agent_task_results,
    arena_applied_agent_actions,
    arena_agent_task_attempts,
    arena_agent_task_events,
    hosted_credential_lifecycle_jobs
TO adx_arena_function_owner;
GRANT UPDATE ON
    arena_agents,
    arena_model_credentials,
    arena_hosted_configs,
    arena_runtime_bindings,
    arena_agent_tasks,
    arena_agent_task_results,
    arena_agent_task_attempts,
    hosted_credential_validation_jobs,
    hosted_credential_lifecycle_jobs
TO adx_arena_function_owner;

-- Control-plane API metadata. Raw Secret values never enter these tables.
GRANT SELECT, INSERT ON
    arena_agents,
    arena_model_credentials,
    arena_hosted_configs,
    arena_runtime_bindings,
    game_agents,
    hosted_credential_validation_jobs,
    hosted_credential_lifecycle_jobs
TO adx_arena_api;
GRANT UPDATE (
    name,
    avatar_ref,
    status,
    runtime_update_job_id,
    updated_at,
    disabled_at
) ON arena_agents TO adx_arena_api;
GRANT UPDATE (
    status,
    unbound_expires_at,
    replaced_at,
    revoked_at,
    updated_at
) ON arena_model_credentials TO adx_arena_api;
GRANT UPDATE (
    route_status,
    updated_at,
    disabled_at
) ON arena_runtime_bindings TO adx_arena_api;
GRANT SELECT ON games, rounds TO adx_arena_api;

-- Arena Core owns Game/Task creation and consumes Results. It cannot read a
-- Secret value and uses functions for terminal Result application/finalizing.
GRANT SELECT ON
    games,
    rounds,
    arena_agents,
    arena_model_credentials,
    arena_hosted_configs,
    arena_runtime_bindings,
    game_agents,
    arena_agent_tasks,
    arena_agent_task_results,
    arena_applied_agent_actions,
    arena_agent_task_attempts,
    arena_agent_task_events
TO adx_arena_core;
GRANT INSERT, UPDATE ON
    games,
    rounds,
    game_agents,
    arena_agent_tasks
TO adx_arena_core;
GRANT INSERT ON arena_agent_task_events TO adx_arena_core;
GRANT EXECUTE ON FUNCTION submit_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) TO adx_arena_core;
GRANT EXECUTE ON FUNCTION finalize_expired_agent_task(TEXT)
    TO adx_arena_core;
GRANT EXECUTE ON FUNCTION apply_arena_agent_task_result(TEXT)
    TO adx_arena_core;
GRANT EXECUTE ON FUNCTION reject_arena_agent_task_result(TEXT, TEXT)
    TO adx_arena_core;

-- Hosted Worker can see only frozen execution views and invoke bounded state
-- transitions. It has no direct write privilege on Game, pool, inventory,
-- settlement, Credential, Config, Binding, Result, or validation terminal rows.
GRANT SELECT ON
    arena_hosted_task_execution_v,
    hosted_credential_validation_execution_v
TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION claim_hosted_agent_tasks(TEXT, INTEGER, INTEGER)
    TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION start_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    BOOLEAN
) TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION mark_agent_task_attempt_request_sent(TEXT, TEXT)
    TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION complete_agent_task_attempt(
    TEXT,
    TEXT,
    TEXT,
    BIGINT,
    TEXT,
    BIGINT,
    BIGINT,
    BIGINT,
    BIGINT,
    BOOLEAN,
    TEXT,
    TEXT
) TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION submit_hosted_agent_task_result(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB,
    BOOLEAN,
    TEXT,
    TEXT
) TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION claim_credential_validation_jobs(
    TEXT,
    INTEGER,
    INTEGER
) TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION record_credential_validation_attempt(TEXT, TEXT)
    TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION complete_credential_validation(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TIMESTAMPTZ
) TO adx_hosted_worker;

-- Credential Controller can receive only lifecycle jobs and opaque Secret
-- references through the claim function. It has no GetSecretValue DB path.
GRANT EXECUTE ON FUNCTION claim_credential_lifecycle_jobs(
    TEXT,
    INTEGER,
    INTEGER
) TO adx_credential_controller;
GRANT EXECUTE ON FUNCTION complete_credential_lifecycle_job(
    TEXT,
    TEXT,
    BOOLEAN,
    TEXT,
    TIMESTAMPTZ
) TO adx_credential_controller;

COMMIT;
