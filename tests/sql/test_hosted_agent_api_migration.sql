\set ON_ERROR_STOP on

BEGIN;

INSERT INTO connector_users (
    user_id,
    username,
    temporary
)
VALUES
    ('user:api-idempotency-1', 'api-idempotency-1', TRUE),
    ('user:api-idempotency-2', 'api-idempotency-2', TRUE),
    ('user:api-idempotency-quota', 'api-idempotency-quota', TRUE);

INSERT INTO arena_model_credentials (
    credential_id,
    owner_user_id,
    provider,
    secret_ref,
    fingerprint,
    fingerprint_pepper_version,
    status
)
VALUES
    (
        'credential:api-idempotency-1',
        'user:api-idempotency-1',
        'fake',
        'ssm://arena/api-idempotency-1',
        repeat('a', 64),
        1,
        'pending_write'
    ),
    (
        'credential:api-idempotency-2',
        'user:api-idempotency-1',
        'fake',
        'ssm://arena/api-idempotency-2',
        repeat('b', 64),
        1,
        'stored'
    ),
    (
        'credential:api-idempotency-3',
        'user:api-idempotency-1',
        'fake',
        'ssm://arena/api-idempotency-3',
        repeat('c', 64),
        1,
        'pending_write'
    );

INSERT INTO arena_agents (
    agent_id,
    owner_user_id,
    name
)
VALUES (
    'agent:api-idempotency-1',
    'user:api-idempotency-1',
    'Idempotency Smoke Agent'
);

-- Prepare one expired key and a separate owner at the exact active-record cap.
INSERT INTO arena_api_idempotency (
    owner_user_id,
    route_key,
    key_digest,
    request_digest,
    state,
    created_at,
    expires_at
)
VALUES (
    'user:api-idempotency-1',
    'hosted_agents.create',
    'sha256:' || repeat('d', 64),
    'sha256:' || repeat('1', 64),
    'reserved',
    clock_timestamp() - INTERVAL '2 hours',
    clock_timestamp() - INTERVAL '1 hour'
);

INSERT INTO arena_api_idempotency (
    owner_user_id,
    route_key,
    key_digest,
    request_digest,
    state,
    created_at,
    expires_at
)
SELECT
    'user:api-idempotency-quota',
    'model_credentials.revalidate',
    'sha256:' || lpad(to_hex(item), 64, '0'),
    'sha256:' || repeat('e', 64),
    'reserved',
    clock_timestamp(),
    clock_timestamp() + INTERVAL '1 hour'
FROM generate_series(1, 256) AS item;

DO $assert_api_privileges$
BEGIN
    IF has_table_privilege(
        'adx_arena_api',
        'public.arena_api_idempotency',
        'SELECT'
    ) OR has_table_privilege(
        'adx_arena_api',
        'public.arena_api_idempotency',
        'INSERT'
    ) OR has_table_privilege(
        'adx_arena_api',
        'public.arena_api_idempotency',
        'UPDATE'
    ) OR has_table_privilege(
        'adx_arena_api',
        'public.arena_api_idempotency',
        'DELETE'
    ) THEN
        RAISE EXCEPTION 'Arena API received direct idempotency table access';
    END IF;

    IF NOT has_function_privilege(
        'adx_arena_api',
        'public.reserve_arena_api_idempotency(text,text,text,text,integer)',
        'EXECUTE'
    ) OR NOT has_function_privilege(
        'adx_arena_api',
        'public.attach_arena_api_idempotency_resource(text,text,text,text,text,text)',
        'EXECUTE'
    ) OR NOT has_function_privilege(
        'adx_arena_api',
        'public.complete_arena_api_idempotency(text,text,text,text,text,text,integer)',
        'EXECUTE'
    ) OR NOT has_function_privilege(
        'adx_arena_api',
        'public.release_arena_api_idempotency_for_retry(text,text,text,text,text,text)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Arena API is missing bounded idempotency functions';
    END IF;
END
$assert_api_privileges$;

SET ROLE adx_arena_api;

DO $assert_no_direct_table_access$
BEGIN
    BEGIN
        EXECUTE 'SELECT count(*) FROM public.arena_api_idempotency';
        RAISE EXCEPTION 'direct idempotency SELECT unexpectedly succeeded';
    EXCEPTION
        WHEN insufficient_privilege THEN
            NULL;
    END;
END
$assert_no_direct_table_access$;

DO $assert_reserve_complete_replay$
DECLARE
    v_record RECORD;
    v_key_digest CONSTANT TEXT := 'sha256:' || repeat('a', 64);
    v_request_digest CONSTANT TEXT := 'sha256:' || repeat('b', 64);
BEGIN
    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        3600
    );
    IF v_record.disposition <> 'reserved'
       OR v_record.record_state <> 'reserved' THEN
        RAISE EXCEPTION 'first reservation was not accepted';
    END IF;

    SELECT *
    INTO v_record
    FROM attach_arena_api_idempotency_resource(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        'model_credential',
        'credential:api-idempotency-1'
    );
    IF v_record.disposition <> 'attached'
       OR v_record.record_state <> 'reserved'
       OR v_record.resource_id <> 'credential:api-idempotency-1' THEN
        RAISE EXCEPTION 'resource was not attached to its reservation';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        3600
    );
    IF v_record.disposition <> 'in_progress'
       OR v_record.resource_id <> 'credential:api-idempotency-1' THEN
        RAISE EXCEPTION 'same in-flight request lost its recovery resource';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        'sha256:' || repeat('c', 64),
        3600
    );
    IF v_record.disposition <> 'conflict'
       OR v_record.resource_id IS NOT NULL
       OR v_record.safe_response IS NOT NULL THEN
        RAISE EXCEPTION 'changed request did not close as a safe conflict';
    END IF;

    BEGIN
        PERFORM *
        FROM complete_arena_api_idempotency(
            'user:api-idempotency-1',
            'model_credentials.create',
            v_key_digest,
            v_request_digest,
            'model_credential',
            'credential:api-idempotency-1',
            201
        );
        RAISE EXCEPTION 'pending_write resource was completed';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN
            NULL;
    END;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        3600
    );
    IF v_record.disposition <> 'in_progress'
       OR v_record.record_state <> 'reserved'
       OR v_record.resource_id <> 'credential:api-idempotency-1' THEN
        RAISE EXCEPTION 'pending_write resource was mistaken for a replay';
    END IF;

    UPDATE arena_model_credentials
    SET status = 'stored',
        updated_at = clock_timestamp()
    WHERE credential_id = 'credential:api-idempotency-1';

    SELECT *
    INTO v_record
    FROM complete_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        'model_credential',
        'credential:api-idempotency-1',
        201
    );
    IF v_record.disposition <> 'completed'
       OR v_record.record_state <> 'completed'
       OR v_record.resource_id <> 'credential:api-idempotency-1'
       OR v_record.safe_response <> jsonb_build_object(
           'httpStatus',
           201,
           'projectionVersion',
           'arena-api-response.v1'
       ) THEN
        RAISE EXCEPTION 'completion did not persist the safe replay projection';
    END IF;

    SELECT *
    INTO v_record
    FROM complete_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        'model_credential',
        'credential:api-idempotency-1',
        201
    );
    IF v_record.disposition <> 'replay' THEN
        RAISE EXCEPTION 'same completion was not replayable';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        3600
    );
    IF v_record.disposition <> 'replay'
       OR v_record.resource_id <> 'credential:api-idempotency-1' THEN
        RAISE EXCEPTION 'completed request was not replayable from reserve';
    END IF;

    SELECT *
    INTO v_record
    FROM complete_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        v_key_digest,
        v_request_digest,
        'model_credential',
        'credential:api-idempotency-2',
        201
    );
    IF v_record.disposition <> 'conflict'
       OR v_record.resource_id IS NOT NULL
       OR v_record.safe_response IS NOT NULL THEN
        RAISE EXCEPTION 'changed completion did not close as a safe conflict';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        'sha256:' || repeat('7', 64),
        'sha256:' || repeat('8', 64),
        3600
    );
    IF v_record.disposition <> 'reserved' THEN
        RAISE EXCEPTION 'retry scenario was not initially reserved';
    END IF;

    SELECT *
    INTO v_record
    FROM release_arena_api_idempotency_for_retry(
        'user:api-idempotency-1',
        'model_credentials.create',
        'sha256:' || repeat('7', 64),
        'sha256:' || repeat('8', 64),
        'model_credential',
        'credential:api-idempotency-3'
    );
    IF v_record.disposition <> 'retryable_failure'
       OR v_record.record_state <> 'retryable_failure'
       OR v_record.safe_response IS NOT NULL THEN
        RAISE EXCEPTION 'SecretWriter failure was not safely released';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        'sha256:' || repeat('7', 64),
        'sha256:' || repeat('8', 64),
        3600
    );
    IF v_record.disposition <> 'retry'
       OR v_record.record_state <> 'reserved'
       OR v_record.resource_id <> 'credential:api-idempotency-3'
       OR v_record.safe_response IS NOT NULL THEN
        RAISE EXCEPTION 'same request could not atomically resume';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        'sha256:' || repeat('7', 64),
        'sha256:' || repeat('8', 64),
        3600
    );
    IF v_record.disposition <> 'in_progress'
       OR v_record.record_state <> 'reserved' THEN
        RAISE EXCEPTION 'concurrent retry was not deduplicated';
    END IF;

    BEGIN
        PERFORM *
        FROM complete_arena_api_idempotency(
            'user:api-idempotency-1',
            'model_credentials.create',
            'sha256:' || repeat('7', 64),
            'sha256:' || repeat('8', 64),
            'model_credential',
            'credential:api-idempotency-3',
            201
        );
        RAISE EXCEPTION 'retryable pending_write resource was completed';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN
            NULL;
    END;

    UPDATE arena_model_credentials
    SET status = 'stored',
        updated_at = clock_timestamp()
    WHERE credential_id = 'credential:api-idempotency-3';

    SELECT *
    INTO v_record
    FROM complete_arena_api_idempotency(
        'user:api-idempotency-1',
        'model_credentials.create',
        'sha256:' || repeat('7', 64),
        'sha256:' || repeat('8', 64),
        'model_credential',
        'credential:api-idempotency-3',
        201
    );
    IF v_record.disposition <> 'completed'
       OR v_record.record_state <> 'completed' THEN
        RAISE EXCEPTION 'resumed SecretWriter success did not complete';
    END IF;
END
$assert_reserve_complete_replay$;

DO $assert_owner_isolation_and_expired_reuse$
DECLARE
    v_record RECORD;
BEGIN
    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-2',
        'model_credentials.create',
        'sha256:' || repeat('a', 64),
        'sha256:' || repeat('b', 64),
        3600
    );
    IF v_record.disposition <> 'reserved' THEN
        RAISE EXCEPTION 'same digest was not isolated by owner';
    END IF;

    SELECT *
    INTO v_record
    FROM reserve_arena_api_idempotency(
        'user:api-idempotency-1',
        'hosted_agents.create',
        'sha256:' || repeat('d', 64),
        'sha256:' || repeat('2', 64),
        3600
    );
    IF v_record.disposition <> 'reserved'
       OR v_record.record_state <> 'reserved' THEN
        RAISE EXCEPTION 'expired reservation was not safely reset';
    END IF;
END
$assert_owner_isolation_and_expired_reuse$;

DO $assert_invalid_inputs_and_quota$
BEGIN
    BEGIN
        PERFORM *
        FROM reserve_arena_api_idempotency(
            'user:api-idempotency-1',
            'model_credentials.create',
            'raw-idempotency-key-do-not-store',
            'sha256:' || repeat('a', 64),
            3600
        );
        RAISE EXCEPTION 'raw idempotency key was accepted';
    EXCEPTION
        WHEN SQLSTATE '22023' THEN
            NULL;
    END;

    BEGIN
        PERFORM *
        FROM reserve_arena_api_idempotency(
            'user:api-idempotency-1',
            'model_credentials.create',
            'sha256:' || repeat('5', 64),
            'sha256:' || repeat('6', 64),
            86401
        );
        RAISE EXCEPTION 'oversized idempotency TTL was accepted';
    EXCEPTION
        WHEN SQLSTATE '22023' THEN
            NULL;
    END;

    BEGIN
        PERFORM *
        FROM reserve_arena_api_idempotency(
            'user:api-idempotency-quota',
            'model_credentials.create',
            'sha256:' || repeat('f', 64),
            'sha256:' || repeat('0', 64),
            3600
        );
        RAISE EXCEPTION 'per-owner active record cap was not enforced';
    EXCEPTION
        WHEN SQLSTATE '54000' THEN
            NULL;
    END;

    BEGIN
        PERFORM *
        FROM complete_arena_api_idempotency(
            'user:api-idempotency-2',
            'model_credentials.create',
            'sha256:' || repeat('a', 64),
            'sha256:' || repeat('b', 64),
            'model_credential',
            'credential:api-idempotency-1',
            201
        );
        RAISE EXCEPTION 'cross-owner resource completion was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0002' THEN
            NULL;
    END;

    BEGIN
        PERFORM *
        FROM attach_arena_api_idempotency_resource(
            'user:api-idempotency-2',
            'model_credentials.create',
            'sha256:' || repeat('a', 64),
            'sha256:' || repeat('b', 64),
            'model_credential',
            'credential:api-idempotency-1'
        );
        RAISE EXCEPTION 'cross-owner resource attachment was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0002' THEN
            NULL;
    END;
END
$assert_invalid_inputs_and_quota$;

RESET ROLE;

DO $assert_no_raw_key_persisted$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM arena_api_idempotency AS i
        WHERE to_jsonb(i)::TEXT LIKE '%raw-idempotency-key-do-not-store%'
    ) THEN
        RAISE EXCEPTION 'raw idempotency key reached persistent state';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM arena_api_idempotency AS i
        WHERE i.key_digest !~ '^sha256:[0-9a-f]{64}$'
           OR i.request_digest !~ '^sha256:[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'non-digest idempotency material was persisted';
    END IF;
END
$assert_no_raw_key_persisted$;

ROLLBACK;
