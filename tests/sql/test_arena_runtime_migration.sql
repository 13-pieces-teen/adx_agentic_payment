\set ON_ERROR_STOP on

BEGIN;

INSERT INTO connector_users (
    user_id,
    username,
    temporary
)
VALUES (
    'user:migration-smoke',
    'migration.smoke',
    TRUE
);

INSERT INTO arena_agents (
    agent_id,
    owner_user_id,
    name
)
VALUES (
    'agent:ready',
    'user:migration-smoke',
    'Ready Agent'
);

INSERT INTO arena_model_credentials (
    credential_id,
    owner_user_id,
    provider,
    secret_ref,
    fingerprint,
    fingerprint_pepper_version,
    status
)
VALUES (
    'credential:ready',
    'user:migration-smoke',
    'fake',
    'ssm://arena/credential-ready',
    repeat('a', 64),
    1,
    'valid'
);

INSERT INTO arena_hosted_configs (
    hosted_config_id,
    agent_id,
    owner_user_id,
    credential_id,
    provider,
    model,
    thinking_enabled,
    strategy_instructions,
    prompt_version,
    task_schema_version,
    action_schema_version,
    capability_version,
    adapter_version,
    max_input_bytes,
    max_context_items,
    max_output_tokens,
    config_hash,
    status
)
VALUES (
    'config:ready',
    'agent:ready',
    'user:migration-smoke',
    'credential:ready',
    'fake',
    'fake-model-v1',
    TRUE,
    'Prefer legal, bounded actions.',
    'prompt.v1',
    'arena.agent-task.v1',
    'arena.action.v1',
    'capability.v1',
    'fake-adapter.v1',
    65536,
    100,
    1024,
    'sha256:' || repeat('1', 64),
    'ready'
);

INSERT INTO arena_runtime_bindings (
    runtime_binding_id,
    agent_id,
    runtime_kind,
    hosted_config_id,
    route_status
)
VALUES (
    'binding:ready',
    'agent:ready',
    'hosted',
    'config:ready',
    'ready'
);

INSERT INTO games (
    game_id,
    status,
    action_timeout_ms
)
VALUES (
    'game:smoke',
    'open',
    30000
);

INSERT INTO rounds (
    round_id,
    game_id,
    round_index,
    phase,
    deadline_at
)
VALUES (
    'round:smoke:1',
    'game:smoke',
    1,
    'decide',
    clock_timestamp() + INTERVAL '10 minutes'
);

INSERT INTO game_agents (
    game_agent_id,
    game_id,
    user_id,
    agent_id,
    runtime_binding_id,
    config_snapshot,
    config_hash,
    status,
    initial_cash_atomic
)
VALUES (
    'game-agent:ready',
    'game:smoke',
    'user:migration-smoke',
    'agent:ready',
    'binding:ready',
    jsonb_build_object(
        'provider',
        'fake',
        'model',
        'fake-model-v1',
        'credential_id',
        'credential:ready'
    ),
    'sha256:' || repeat('1', 64),
    'active',
    1000000
);

DO $assert_one_agent_per_user$
BEGIN
    BEGIN
        INSERT INTO game_agents (
            game_agent_id,
            game_id,
            user_id,
            agent_id,
            runtime_binding_id,
            config_snapshot,
            config_hash,
            status
        )
        VALUES (
            'game-agent:duplicate',
            'game:smoke',
            'user:migration-smoke',
            'agent:ready',
            'binding:ready',
            '{}'::jsonb,
            'sha256:' || repeat('1', 64),
            'joined'
        );
        RAISE EXCEPTION 'one-user-one-game constraint was bypassed';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;
END
$assert_one_agent_per_user$;

INSERT INTO arena_agent_tasks (
    task_id,
    task_kind,
    schema_version,
    game_id,
    round_id,
    game_agent_id,
    runtime_binding_id,
    credential_id,
    deadline_at,
    idempotency_key,
    input_snapshot,
    input_hash,
    runtime_config_snapshot,
    config_hash,
    default_result_id,
    default_result_hash
)
VALUES (
    'task:ready',
    'arena.decide',
    'arena.agent-task.v1',
    'game:smoke',
    'round:smoke:1',
    'game-agent:ready',
    'binding:ready',
    'credential:ready',
    clock_timestamp() + INTERVAL '5 minutes',
    'game:smoke:round:smoke:1:game-agent:ready:decide',
    '{"goods":["ruby"]}'::jsonb,
    'sha256:' || repeat('2', 64),
    '{"strategy_instructions":"Prefer legal, bounded actions."}'::jsonb,
    'sha256:' || repeat('1', 64),
    'default:ba245390e907935809a0b2be7f8aae728b54edbdcd8af39e8e80e8a7c54a7074',
    'sha256:' || repeat('5', 64)
);

SET ROLE adx_hosted_worker;

DO $worker_cannot_write_business_tables$
BEGIN
    BEGIN
        UPDATE public.arena_agent_tasks
        SET status = 'cancelled',
            completed_at = clock_timestamp()
        WHERE task_id = 'task:ready';
        RAISE EXCEPTION 'worker unexpectedly updated Arena Task directly';
    EXCEPTION
        WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        INSERT INTO public.arena_applied_agent_actions (
            task_id,
            result_id,
            game_id,
            round_id,
            game_agent_id,
            task_kind,
            application_outcome,
            applied_action,
            authoritative_entered_at
        )
        VALUES (
            'task:ready',
            'result:missing',
            'game:smoke',
            'round:smoke:1',
            'game-agent:ready',
            'arena.decide',
            'candidate',
            '{"action":"buy","good":"ruby"}'::jsonb,
            clock_timestamp()
        );
        RAISE EXCEPTION 'worker unexpectedly wrote applied action directly';
    EXCEPTION
        WHEN insufficient_privilege THEN NULL;
    END;

    BEGIN
        PERFORM public.submit_agent_task_result(
            'task:ready',
            'sha256:' || repeat('6', 64),
            'sha256:' || repeat('7', 64),
            'arena.agent-result.v1',
            'succeeded',
            '{"action":"buy","good":"ruby"}'::jsonb
        );
        RAISE EXCEPTION 'worker unexpectedly executed generic Result Sink';
    EXCEPTION
        WHEN insufficient_privilege THEN NULL;
    END;
END
$worker_cannot_write_business_tables$;

SELECT task_id
FROM claim_hosted_agent_tasks('worker:smoke', 1, 60);
SELECT start_agent_task_attempt(
    'task:ready',
    'worker:smoke',
    'attempt:ready:1',
    'fake',
    'fake-model-v1',
    TRUE
);
SELECT mark_agent_task_attempt_request_sent(
    'attempt:ready:1',
    'worker:smoke'
);
SELECT complete_agent_task_attempt(
    'attempt:ready:1',
    'worker:smoke',
    'succeeded',
    12,
    'fake-model-v1',
    10,
    3,
    0,
    1,
    TRUE,
    'request-ref:smoke',
    NULL
);
SELECT disposition, terminal_task_status
FROM submit_hosted_agent_task_result(
    'worker:smoke',
    'task:ready',
    'sha256:' || repeat('6', 64),
    'sha256:' || repeat('7', 64),
    'arena.agent-result.v1',
    'succeeded',
    '{"action":"buy","good":"ruby"}'::jsonb,
    FALSE,
    'public-output.v1',
    NULL
);
SELECT disposition
FROM submit_hosted_agent_task_result(
    'worker:smoke',
    'task:ready',
    'sha256:' || repeat('6', 64),
    'sha256:' || repeat('7', 64),
    'arena.agent-result.v1',
    'succeeded',
    '{"action":"buy","good":"ruby"}'::jsonb,
    FALSE,
    'public-output.v1',
    NULL
);
SELECT disposition
FROM submit_hosted_agent_task_result(
    'worker:smoke',
    'task:ready',
    'sha256:' || repeat('6', 64),
    'sha256:' || repeat('8', 64),
    'arena.agent-result.v1',
    'succeeded',
    '{"action":"sell","good":"ruby"}'::jsonb,
    FALSE,
    'public-output.v1',
    NULL
);

RESET ROLE;
SET ROLE adx_arena_core;

SELECT apply_arena_agent_task_result(
    'runtime:' || repeat('6', 64)
);

RESET ROLE;

DO $assert_result_applied_once$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM arena_agent_task_results
        WHERE result_id = 'runtime:' || repeat('6', 64)
          AND apply_status = 'applied'
          AND arena_applied_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'result did not reach applied state';
    END IF;
    IF (
        SELECT count(*)
        FROM arena_applied_agent_actions
        WHERE result_id = 'runtime:' || repeat('6', 64)
          AND task_id = 'task:ready'
          AND authoritative_entered_at = (
              SELECT result_received_at
              FROM arena_agent_task_results
              WHERE result_id = 'runtime:' || repeat('6', 64)
          )
    ) <> 1 THEN
        RAISE EXCEPTION 'applied projection is missing or has wrong FCFS time';
    END IF;
    IF (
        SELECT count(*)
        FROM arena_agent_task_events
        WHERE task_id = 'task:ready'
          AND event_type IN (
              'duplicate_result_ignored',
              'result_conflict'
          )
          AND safe_metadata ? 'runtime_result_id_digest'
          AND (
              (
                  event_type = 'duplicate_result_ignored'
                  AND safe_metadata ? 'result_hash'
              )
              OR (
                  event_type = 'result_conflict'
                  AND safe_metadata ? 'authoritative_result_hash'
                  AND safe_metadata ? 'incoming_result_hash'
              )
          )
          AND NOT (safe_metadata ? 'result_id')
    ) <> 2 THEN
        RAISE EXCEPTION 'duplicate/conflict audit is missing safe digests';
    END IF;
END
$assert_result_applied_once$;

INSERT INTO arena_agent_tasks (
    task_id,
    task_kind,
    schema_version,
    game_id,
    round_id,
    game_agent_id,
    runtime_binding_id,
    credential_id,
    deadline_at,
    idempotency_key,
    input_snapshot,
    input_hash,
    runtime_config_snapshot,
    config_hash,
    default_result_id,
    default_result_hash
)
VALUES (
    'task:expired',
    'arena.decide',
    'arena.agent-task.v1',
    'game:smoke',
    'round:smoke:1',
    'game-agent:ready',
    'binding:ready',
    'credential:ready',
    clock_timestamp() - INTERVAL '1 second',
    'game:smoke:round:smoke:1:game-agent:ready:expired',
    '{}'::jsonb,
    'sha256:' || repeat('3', 64),
    '{}'::jsonb,
    'sha256:' || repeat('1', 64),
    'default:b997d7ad4345433da65ff31fb98d4dc8c8c062a004d54f99c4ac2f282cc9df1f',
    'sha256:' || repeat('9', 64)
);

SET ROLE adx_arena_core;
SELECT finalize_expired_agent_task('task:expired');
SELECT disposition
FROM submit_agent_task_result(
    'task:expired',
    'sha256:' || repeat('a', 64),
    'sha256:' || repeat('b', 64),
    'arena.agent-result.v1',
    'succeeded',
    '{"action":"sell","good":"ruby"}'::jsonb,
    FALSE,
    'public-output.v1',
    NULL
);
RESET ROLE;

DO $assert_finalizer_won$
BEGIN
    IF (
        SELECT count(*)
        FROM arena_agent_task_results
        WHERE task_id = 'task:expired'
    ) <> 1 THEN
        RAISE EXCEPTION 'expired Task has more than one Result';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM arena_agent_task_events
        WHERE task_id = 'task:expired'
          AND event_type = 'late_result_ignored'
    ) THEN
        RAISE EXCEPTION 'late Result was not audited';
    END IF;
END
$assert_finalizer_won$;

INSERT INTO arena_agent_tasks (
    task_id,
    task_kind,
    schema_version,
    game_id,
    round_id,
    game_agent_id,
    runtime_binding_id,
    credential_id,
    deadline_at,
    idempotency_key,
    input_snapshot,
    input_hash,
    runtime_config_snapshot,
    config_hash,
    default_result_id,
    default_result_hash
)
VALUES (
    'task:wrong-kind',
    'arena.decide',
    'arena.agent-task.v1',
    'game:smoke',
    'round:smoke:1',
    'game-agent:ready',
    'binding:ready',
    'credential:ready',
    clock_timestamp() + INTERVAL '5 minutes',
    'game:smoke:round:smoke:1:game-agent:ready:wrong-kind',
    '{"limits":{"allowedActions":["buy","sell","pass"],"allowedGoods":["ruby"]}}'::jsonb,
    'sha256:' || repeat('c', 64),
    '{}'::jsonb,
    'sha256:' || repeat('1', 64),
    'default:23754f018acabd54e2db198d283c31a2b3b1d86afa788ab2f4b80d5c496f180a',
    'sha256:' || repeat('d', 64)
);

SET ROLE adx_arena_core;
SELECT disposition
FROM submit_agent_task_result(
    'task:wrong-kind',
    'sha256:' || repeat('c', 64),
    'sha256:' || repeat('d', 64),
    'arena.agent-result.v1',
    'succeeded',
    '{"action":"propose","price":"1.000000"}'::jsonb,
    FALSE,
    'public-output.v1',
    NULL
);
SELECT apply_arena_agent_task_result('runtime:' || repeat('c', 64));
SELECT apply_arena_agent_task_result('runtime:' || repeat('c', 64));
RESET ROLE;

DO $assert_wrong_kind_defaults_once$
BEGIN
    IF (
        SELECT count(*)
        FROM arena_applied_agent_actions
        WHERE task_id = 'task:wrong-kind'
          AND application_outcome = 'default_pass'
          AND applied_action = '{"action":"pass"}'::jsonb
    ) <> 1 THEN
        RAISE EXCEPTION 'wrong-kind Decide did not default exactly once';
    END IF;
END
$assert_wrong_kind_defaults_once$;

INSERT INTO arena_agents (
    agent_id,
    owner_user_id,
    name
)
VALUES (
    'agent:provisioning',
    'user:migration-smoke',
    'Provisioning Agent'
);

INSERT INTO arena_model_credentials (
    credential_id,
    owner_user_id,
    provider,
    secret_ref,
    fingerprint,
    fingerprint_pepper_version,
    status
)
VALUES (
    'credential:provisioning',
    'user:migration-smoke',
    'fake',
    'ssm://arena/credential-provisioning',
    repeat('b', 64),
    1,
    'pending_validation'
);

INSERT INTO arena_hosted_configs (
    hosted_config_id,
    agent_id,
    owner_user_id,
    credential_id,
    provider,
    model,
    thinking_enabled,
    strategy_instructions,
    prompt_version,
    task_schema_version,
    action_schema_version,
    capability_version,
    adapter_version,
    max_input_bytes,
    max_context_items,
    max_output_tokens,
    config_hash,
    status
)
VALUES (
    'config:provisioning',
    'agent:provisioning',
    'user:migration-smoke',
    'credential:provisioning',
    'fake',
    'fake-model-v1',
    FALSE,
    'Private candidate strategy.',
    'prompt.v1',
    'arena.agent-task.v1',
    'arena.action.v1',
    'capability.v1',
    'fake-adapter.v1',
    65536,
    100,
    1024,
    'sha256:' || repeat('4', 64),
    'provisioning'
);

INSERT INTO arena_runtime_bindings (
    runtime_binding_id,
    agent_id,
    runtime_kind,
    hosted_config_id,
    route_status
)
VALUES (
    'binding:provisioning',
    'agent:provisioning',
    'hosted',
    'config:provisioning',
    'provisioning'
);

INSERT INTO hosted_credential_validation_jobs (
    validation_job_id,
    agent_id,
    credential_id,
    hosted_config_id,
    job_kind,
    candidate_config_snapshot,
    candidate_config_hash,
    expected_current_config_hash,
    validation_schema_version,
    status,
    max_attempts,
    deadline_at
)
VALUES (
    'validation:create',
    'agent:provisioning',
    'credential:provisioning',
    'config:provisioning',
    'create',
    jsonb_build_object(
        'provider',
        'fake',
        'model',
        'fake-model-v1',
        'thinking_enabled',
        FALSE,
        'strategy_instructions',
        'Private candidate strategy.',
        'prompt_version',
        'prompt.v1',
        'task_schema_version',
        'arena.agent-task.v1',
        'action_schema_version',
        'arena.action.v1',
        'capability_version',
        'capability.v1',
        'adapter_version',
        'fake-adapter.v1',
        'max_input_bytes',
        65536,
        'max_context_items',
        100,
        'max_output_tokens',
        1024
    ),
    'sha256:' || repeat('4', 64),
    'sha256:' || repeat('4', 64),
    'credential-validation.v1',
    'queued',
    3,
    clock_timestamp() + INTERVAL '10 minutes'
);

UPDATE arena_agents
SET runtime_update_job_id = 'validation:create'
WHERE agent_id = 'agent:provisioning';

SET ROLE adx_hosted_worker;
SELECT validation_job_id, provider, model, thinking_enabled
FROM claim_credential_validation_jobs('worker:validation', 1, 60);
SELECT record_credential_validation_attempt(
    'validation:create',
    'worker:validation'
);
SELECT complete_credential_validation(
    'validation:create',
    'worker:validation',
    'sha256:' || repeat('4', 64),
    'sha256:' || repeat('4', 64),
    'succeeded',
    NULL,
    NULL
);
RESET ROLE;

DO $assert_validation_cas$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM arena_agents AS a
        JOIN arena_hosted_configs AS c
          ON c.agent_id = a.agent_id
        JOIN arena_model_credentials AS credential
          ON credential.credential_id = c.credential_id
        JOIN arena_runtime_bindings AS b
          ON b.hosted_config_id = c.hosted_config_id
        WHERE a.agent_id = 'agent:provisioning'
          AND a.runtime_update_job_id IS NULL
          AND c.status = 'ready'
          AND credential.status = 'valid'
          AND b.route_status = 'ready'
    ) THEN
        RAISE EXCEPTION 'validation completion did not apply one complete CAS';
    END IF;
END
$assert_validation_cas$;

INSERT INTO arena_model_credentials (
    credential_id,
    owner_user_id,
    provider,
    secret_ref,
    fingerprint,
    fingerprint_pepper_version,
    status
)
VALUES (
    'credential:revoke',
    'user:migration-smoke',
    'fake',
    'ssm://arena/credential-revoke',
    repeat('c', 64),
    1,
    'revoking'
);

INSERT INTO hosted_credential_lifecycle_jobs (
    lifecycle_job_id,
    credential_id,
    job_kind,
    idempotency_key,
    status,
    max_attempts,
    deadline_at
)
VALUES (
    'lifecycle:revoke',
    'credential:revoke',
    'revoke',
    'revoke:smoke',
    'queued',
    3,
    clock_timestamp() + INTERVAL '10 minutes'
);

SET ROLE adx_credential_controller;
SELECT lifecycle_job_id, job_kind, secret_ref
FROM claim_credential_lifecycle_jobs('controller:smoke', 1, 60);
SELECT complete_credential_lifecycle_job(
    'lifecycle:revoke',
    'controller:smoke',
    TRUE,
    NULL,
    NULL
);
RESET ROLE;

DO $assert_lifecycle_completion$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM arena_model_credentials
        WHERE credential_id = 'credential:revoke'
          AND status = 'revoked'
          AND revoked_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'credential lifecycle completion was not persisted';
    END IF;
END
$assert_lifecycle_completion$;

ROLLBACK;
