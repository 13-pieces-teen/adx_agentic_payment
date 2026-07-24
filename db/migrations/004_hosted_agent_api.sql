BEGIN;

-- Arena 402 Phase 4 HTTP idempotency persistence foundation.
--
-- This forward-only migration depends on 002_connector_gateway.sql for the
-- User authority and 003_arena_agent_runtime.sql for the Arena roles and
-- resource tables. The raw Idempotency-Key and raw Provider API key are never
-- accepted by this schema or its functions. Callers must derive:
--
--   * key_digest     = sha256(raw Idempotency-Key)
--   * request_digest = sha256(canonical non-secret request metadata plus the
--                      peppered credential fingerprint when applicable)
--
-- Credential ingress must exclude the raw Provider API key from canonical
-- request metadata and include only the separately peppered fingerprint
-- defined by the Hosted Agent contract.

CREATE TABLE arena_api_idempotency (
    owner_user_id TEXT NOT NULL
        REFERENCES connector_users(user_id) ON DELETE CASCADE,
    route_key TEXT NOT NULL CHECK (
        route_key IN (
            'model_credentials.create',
            'model_credentials.replace',
            'model_credentials.revoke',
            'model_credentials.revalidate',
            'hosted_agents.create',
            'hosted_agents.update',
            'hosted_agents.disable',
            'game_participants.create'
        )
    ),
    key_digest TEXT NOT NULL CHECK (
        key_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    request_digest TEXT NOT NULL CHECK (
        request_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    state TEXT NOT NULL DEFAULT 'reserved' CHECK (
        state IN ('reserved', 'retryable_failure', 'completed')
    ),
    resource_kind TEXT CHECK (
        resource_kind IN (
            'model_credential',
            'arena_agent',
            'game_agent'
        )
    ),
    resource_id TEXT CHECK (
        resource_id IS NULL
        OR (
            char_length(resource_id) BETWEEN 1 AND 128
            AND resource_id ~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'
        )
    ),
    -- This is deliberately not an arbitrary cached HTTP body. The completion
    -- function constructs the only accepted projection metadata, and the API
    -- rehydrates the owner-scoped resource for an exact replay.
    safe_response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (owner_user_id, route_key, key_digest),
    CHECK (char_length(owner_user_id) BETWEEN 1 AND 128),
    CHECK (
        expires_at >= created_at + INTERVAL '60 seconds'
        AND expires_at <= created_at + INTERVAL '24 hours'
    ),
    CHECK (
        safe_response IS NULL
        OR (
            jsonb_typeof(safe_response) = 'object'
            AND safe_response ? 'httpStatus'
            AND safe_response ? 'projectionVersion'
            AND (
                safe_response
                - 'httpStatus'
                - 'projectionVersion'
            ) = '{}'::jsonb
            AND jsonb_typeof(safe_response -> 'httpStatus') = 'number'
            AND safe_response ->> 'httpStatus' IN ('200', '201', '202', '204')
            AND safe_response ->> 'projectionVersion'
                = 'arena-api-response.v1'
        )
    ),
    CHECK (
        (
            state = 'reserved'
            AND safe_response IS NULL
            AND completed_at IS NULL
            AND (
                (resource_kind IS NULL AND resource_id IS NULL)
                OR (resource_kind IS NOT NULL AND resource_id IS NOT NULL)
            )
        )
        OR (
            state = 'retryable_failure'
            AND resource_kind IS NOT NULL
            AND resource_id IS NOT NULL
            AND safe_response IS NULL
            AND completed_at IS NULL
        )
        OR (
            state = 'completed'
            AND resource_kind IS NOT NULL
            AND resource_id IS NOT NULL
            AND safe_response IS NOT NULL
            AND completed_at IS NOT NULL
            AND completed_at >= created_at
            AND completed_at <= expires_at
        )
    ),
    CHECK (
        resource_kind IS NULL
        OR (
            (
                route_key IN (
                    'model_credentials.create',
                    'model_credentials.replace',
                    'model_credentials.revoke',
                    'model_credentials.revalidate'
                )
                AND resource_kind = 'model_credential'
            )
            OR (
                route_key IN (
                    'hosted_agents.create',
                    'hosted_agents.update',
                    'hosted_agents.disable'
                )
                AND resource_kind = 'arena_agent'
            )
            OR (
                route_key = 'game_participants.create'
                AND resource_kind = 'game_agent'
            )
        )
    )
);

CREATE INDEX arena_api_idempotency_owner_created_idx
    ON arena_api_idempotency (owner_user_id, created_at DESC);

CREATE INDEX arena_api_idempotency_expiry_idx
    ON arena_api_idempotency (expires_at, owner_user_id);

CREATE OR REPLACE FUNCTION reserve_arena_api_idempotency(
    p_owner_user_id TEXT,
    p_route_key TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_ttl_seconds INTEGER DEFAULT 3600
)
RETURNS TABLE (
    disposition TEXT,
    record_state TEXT,
    resource_kind TEXT,
    resource_id TEXT,
    safe_response JSONB,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $reserve_arena_api_idempotency$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_record public.arena_api_idempotency%ROWTYPE;
    v_active_count INTEGER;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'invalid API idempotency owner'
            USING ERRCODE = '22023';
    END IF;
    IF p_route_key IS NULL
       OR p_route_key NOT IN (
           'model_credentials.create',
           'model_credentials.replace',
           'model_credentials.revoke',
           'model_credentials.revalidate',
           'hosted_agents.create',
           'hosted_agents.update',
           'hosted_agents.disable',
           'game_participants.create'
       ) THEN
        RAISE EXCEPTION 'invalid API idempotency route'
            USING ERRCODE = '22023';
    END IF;
    IF p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid API idempotency digest'
            USING ERRCODE = '22023';
    END IF;
    IF p_ttl_seconds IS NULL
       OR p_ttl_seconds NOT BETWEEN 60 AND 86400 THEN
        RAISE EXCEPTION 'invalid API idempotency TTL'
            USING ERRCODE = '22023';
    END IF;

    -- A shared API database role cannot establish owner identity by itself;
    -- the HTTP layer remains responsible for Session/CSRF validation. This
    -- check prevents orphan or disabled principals from reserving records.
    PERFORM 1
    FROM public.connector_users AS u
    WHERE u.user_id = p_owner_user_id
      AND u.disabled_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'API idempotency owner is not eligible'
            USING ERRCODE = 'P0002';
    END IF;

    -- Serialize all reservations for one owner. In addition to making the
    -- record-count cap exact, this makes same-key insert/reset linearizable.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );
    v_now := clock_timestamp();

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF FOUND THEN
        IF v_record.expires_at <= v_now THEN
            UPDATE public.arena_api_idempotency AS i
            SET request_digest = p_request_digest,
                state = 'reserved',
                resource_kind = NULL,
                resource_id = NULL,
                safe_response = NULL,
                created_at = v_now,
                expires_at = v_now
                    + pg_catalog.make_interval(secs => p_ttl_seconds),
                completed_at = NULL
            WHERE i.owner_user_id = p_owner_user_id
              AND i.route_key = p_route_key
              AND i.key_digest = p_key_digest
            RETURNING i.* INTO v_record;

            RETURN QUERY
            SELECT
                'reserved'::TEXT,
                v_record.state,
                v_record.resource_kind,
                v_record.resource_id,
                v_record.safe_response,
                v_record.expires_at;
            RETURN;
        END IF;

        IF v_record.request_digest <> p_request_digest THEN
            RETURN QUERY
            SELECT
                'conflict'::TEXT,
                v_record.state,
                NULL::TEXT,
                NULL::TEXT,
                NULL::JSONB,
                v_record.expires_at;
            RETURN;
        END IF;

        IF v_record.state = 'retryable_failure' THEN
            UPDATE public.arena_api_idempotency AS i
            SET state = 'reserved'
            WHERE i.owner_user_id = p_owner_user_id
              AND i.route_key = p_route_key
              AND i.key_digest = p_key_digest
            RETURNING i.* INTO v_record;

            RETURN QUERY
            SELECT
                'retry'::TEXT,
                v_record.state,
                v_record.resource_kind,
                v_record.resource_id,
                NULL::JSONB,
                v_record.expires_at;
            RETURN;
        END IF;

        IF v_record.state = 'completed' THEN
            RETURN QUERY
            SELECT
                'replay'::TEXT,
                v_record.state,
                v_record.resource_kind,
                v_record.resource_id,
                v_record.safe_response,
                v_record.expires_at;
            RETURN;
        END IF;

        RETURN QUERY
        SELECT
            'in_progress'::TEXT,
            v_record.state,
            v_record.resource_kind,
            v_record.resource_id,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    -- Expired idempotency records are operational cache state, not Arena
    -- audit evidence. Prune only this owner under the same owner lock.
    DELETE FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.expires_at <= v_now;

    SELECT count(*)
    INTO v_active_count
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.expires_at > v_now;

    IF v_active_count >= 256 THEN
        RAISE EXCEPTION 'API idempotency record limit exceeded'
            USING ERRCODE = '54000';
    END IF;

    INSERT INTO public.arena_api_idempotency (
        owner_user_id,
        route_key,
        key_digest,
        request_digest,
        state,
        created_at,
        expires_at
    )
    VALUES (
        p_owner_user_id,
        p_route_key,
        p_key_digest,
        p_request_digest,
        'reserved',
        v_now,
        v_now + pg_catalog.make_interval(secs => p_ttl_seconds)
    )
    RETURNING * INTO v_record;

    RETURN QUERY
    SELECT
        'reserved'::TEXT,
        v_record.state,
        v_record.resource_kind,
        v_record.resource_id,
        v_record.safe_response,
        v_record.expires_at;
END
$reserve_arena_api_idempotency$;

-- Bind the resource created by the same outer database transaction to its
-- reservation before that transaction commits. This closes the crash window
-- between a durable pending_write credential (or provisioning Agent) and the
-- idempotency record needed to recover it. The API still cannot attach an
-- arbitrary object: resource kind, shape, existence, and ownership are
-- checked here, while the table remains inaccessible directly.
CREATE OR REPLACE FUNCTION attach_arena_api_idempotency_resource(
    p_owner_user_id TEXT,
    p_route_key TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_resource_kind TEXT,
    p_resource_id TEXT
)
RETURNS TABLE (
    disposition TEXT,
    record_state TEXT,
    resource_kind TEXT,
    resource_id TEXT,
    safe_response JSONB,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $attach_arena_api_idempotency_resource$
DECLARE
    v_now TIMESTAMPTZ;
    v_record public.arena_api_idempotency%ROWTYPE;
    v_expected_resource_kind TEXT;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'invalid API idempotency owner'
            USING ERRCODE = '22023';
    END IF;
    IF p_route_key IS NULL
       OR p_route_key NOT IN (
           'model_credentials.create',
           'model_credentials.replace',
           'model_credentials.revoke',
           'model_credentials.revalidate',
           'hosted_agents.create',
           'hosted_agents.update',
           'hosted_agents.disable',
           'game_participants.create'
       ) THEN
        RAISE EXCEPTION 'invalid API idempotency route'
            USING ERRCODE = '22023';
    END IF;
    IF p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_resource_id IS NULL
       OR char_length(p_resource_id) NOT BETWEEN 1 AND 128
       OR p_resource_id !~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$' THEN
        RAISE EXCEPTION 'invalid API idempotency resource attachment'
            USING ERRCODE = '22023';
    END IF;

    v_expected_resource_kind := CASE
        WHEN p_route_key IN (
            'model_credentials.create',
            'model_credentials.replace',
            'model_credentials.revoke',
            'model_credentials.revalidate'
        ) THEN 'model_credential'
        WHEN p_route_key IN (
            'hosted_agents.create',
            'hosted_agents.update',
            'hosted_agents.disable'
        ) THEN 'arena_agent'
        WHEN p_route_key = 'game_participants.create' THEN 'game_agent'
        ELSE NULL
    END;

    IF p_resource_kind IS DISTINCT FROM v_expected_resource_kind THEN
        RAISE EXCEPTION 'invalid API idempotency resource kind'
            USING ERRCODE = '22023';
    END IF;

    IF p_resource_kind = 'model_credential' THEN
        PERFORM 1
        FROM public.arena_model_credentials AS credential
        WHERE credential.credential_id = p_resource_id
          AND credential.owner_user_id = p_owner_user_id;
    ELSIF p_resource_kind = 'arena_agent' THEN
        PERFORM 1
        FROM public.arena_agents AS agent
        WHERE agent.agent_id = p_resource_id
          AND agent.owner_user_id = p_owner_user_id;
    ELSIF p_resource_kind = 'game_agent' THEN
        PERFORM 1
        FROM public.game_agents AS participant
        WHERE participant.game_agent_id = p_resource_id
          AND participant.user_id = p_owner_user_id;
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'API idempotency resource not found'
            USING ERRCODE = 'P0002';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );
    v_now := clock_timestamp();

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            'not_found'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    IF v_record.request_digest <> p_request_digest THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.expires_at <= v_now THEN
        RETURN QUERY
        SELECT
            'expired'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.resource_kind IS NOT NULL
       AND (
           v_record.resource_kind <> p_resource_kind
           OR v_record.resource_id <> p_resource_id
       ) THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.state = 'completed' THEN
        RETURN QUERY
        SELECT
            'replay'::TEXT,
            v_record.state,
            v_record.resource_kind,
            v_record.resource_id,
            v_record.safe_response,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.state = 'retryable_failure' THEN
        RETURN QUERY
        SELECT
            'retryable_failure'::TEXT,
            v_record.state,
            v_record.resource_kind,
            v_record.resource_id,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    UPDATE public.arena_api_idempotency AS i
    SET resource_kind = p_resource_kind,
        resource_id = p_resource_id
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    RETURNING i.* INTO v_record;

    RETURN QUERY
    SELECT
        'attached'::TEXT,
        v_record.state,
        v_record.resource_kind,
        v_record.resource_id,
        NULL::JSONB,
        v_record.expires_at;
END
$attach_arena_api_idempotency_resource$;

CREATE OR REPLACE FUNCTION release_arena_api_idempotency_for_retry(
    p_owner_user_id TEXT,
    p_route_key TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_resource_kind TEXT,
    p_resource_id TEXT
)
RETURNS TABLE (
    disposition TEXT,
    record_state TEXT,
    resource_kind TEXT,
    resource_id TEXT,
    safe_response JSONB,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $release_arena_api_idempotency$
DECLARE
    v_now TIMESTAMPTZ;
    v_record public.arena_api_idempotency%ROWTYPE;
    v_expected_resource_kind TEXT;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'invalid API idempotency owner'
            USING ERRCODE = '22023';
    END IF;
    IF p_route_key IS NULL
       OR p_route_key NOT IN (
           'model_credentials.create',
           'model_credentials.replace',
           'model_credentials.revoke',
           'model_credentials.revalidate',
           'hosted_agents.create',
           'hosted_agents.update',
           'hosted_agents.disable',
           'game_participants.create'
       ) THEN
        RAISE EXCEPTION 'invalid API idempotency route'
            USING ERRCODE = '22023';
    END IF;
    IF p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_resource_id IS NULL
       OR char_length(p_resource_id) NOT BETWEEN 1 AND 128
       OR p_resource_id !~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$' THEN
        RAISE EXCEPTION 'invalid API idempotency retry release'
            USING ERRCODE = '22023';
    END IF;

    v_expected_resource_kind := CASE
        WHEN p_route_key IN (
            'model_credentials.create',
            'model_credentials.replace',
            'model_credentials.revoke',
            'model_credentials.revalidate'
        ) THEN 'model_credential'
        WHEN p_route_key IN (
            'hosted_agents.create',
            'hosted_agents.update',
            'hosted_agents.disable'
        ) THEN 'arena_agent'
        WHEN p_route_key = 'game_participants.create' THEN 'game_agent'
        ELSE NULL
    END;

    IF p_resource_kind IS DISTINCT FROM v_expected_resource_kind THEN
        RAISE EXCEPTION 'invalid API idempotency resource kind'
            USING ERRCODE = '22023';
    END IF;

    IF p_resource_kind = 'model_credential' THEN
        PERFORM 1
        FROM public.arena_model_credentials AS credential
        WHERE credential.credential_id = p_resource_id
          AND credential.owner_user_id = p_owner_user_id;
    ELSIF p_resource_kind = 'arena_agent' THEN
        PERFORM 1
        FROM public.arena_agents AS agent
        WHERE agent.agent_id = p_resource_id
          AND agent.owner_user_id = p_owner_user_id;
    ELSIF p_resource_kind = 'game_agent' THEN
        PERFORM 1
        FROM public.game_agents AS participant
        WHERE participant.game_agent_id = p_resource_id
          AND participant.user_id = p_owner_user_id;
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'API idempotency resource not found'
            USING ERRCODE = 'P0002';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );
    v_now := clock_timestamp();

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            'not_found'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    IF v_record.request_digest <> p_request_digest THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.expires_at <= v_now THEN
        RETURN QUERY
        SELECT
            'expired'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.state = 'completed' THEN
        IF v_record.resource_kind = p_resource_kind
           AND v_record.resource_id = p_resource_id THEN
            RETURN QUERY
            SELECT
                'replay'::TEXT,
                v_record.state,
                v_record.resource_kind,
                v_record.resource_id,
                v_record.safe_response,
                v_record.expires_at;
            RETURN;
        END IF;

        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.resource_kind IS NOT NULL
       AND (
           v_record.resource_kind <> p_resource_kind
           OR v_record.resource_id <> p_resource_id
       ) THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.state = 'retryable_failure' THEN
        RETURN QUERY
        SELECT
            'retryable_failure'::TEXT,
            v_record.state,
            v_record.resource_kind,
            v_record.resource_id,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    UPDATE public.arena_api_idempotency AS i
    SET state = 'retryable_failure',
        resource_kind = p_resource_kind,
        resource_id = p_resource_id
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    RETURNING i.* INTO v_record;

    RETURN QUERY
    SELECT
        'retryable_failure'::TEXT,
        v_record.state,
        v_record.resource_kind,
        v_record.resource_id,
        NULL::JSONB,
        v_record.expires_at;
END
$release_arena_api_idempotency$;

CREATE OR REPLACE FUNCTION complete_arena_api_idempotency(
    p_owner_user_id TEXT,
    p_route_key TEXT,
    p_key_digest TEXT,
    p_request_digest TEXT,
    p_resource_kind TEXT,
    p_resource_id TEXT,
    p_http_status INTEGER
)
RETURNS TABLE (
    disposition TEXT,
    record_state TEXT,
    resource_kind TEXT,
    resource_id TEXT,
    safe_response JSONB,
    expires_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $complete_arena_api_idempotency$
DECLARE
    v_now TIMESTAMPTZ := clock_timestamp();
    v_record public.arena_api_idempotency%ROWTYPE;
    v_expected_resource_kind TEXT;
    v_resource_status TEXT;
    v_safe_response JSONB;
BEGIN
    IF p_owner_user_id IS NULL
       OR char_length(p_owner_user_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'invalid API idempotency owner'
            USING ERRCODE = '22023';
    END IF;
    IF p_route_key IS NULL
       OR p_route_key NOT IN (
           'model_credentials.create',
           'model_credentials.replace',
           'model_credentials.revoke',
           'model_credentials.revalidate',
           'hosted_agents.create',
           'hosted_agents.update',
           'hosted_agents.disable',
           'game_participants.create'
       ) THEN
        RAISE EXCEPTION 'invalid API idempotency route'
            USING ERRCODE = '22023';
    END IF;
    IF p_key_digest IS NULL
       OR p_key_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_request_digest IS NULL
       OR p_request_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid API idempotency digest'
            USING ERRCODE = '22023';
    END IF;
    IF p_resource_id IS NULL
       OR char_length(p_resource_id) NOT BETWEEN 1 AND 128
       OR p_resource_id !~ '^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$'
       OR p_http_status IS NULL
       OR p_http_status NOT IN (200, 201, 202, 204) THEN
        RAISE EXCEPTION 'invalid API idempotency completion'
            USING ERRCODE = '22023';
    END IF;

    v_expected_resource_kind := CASE
        WHEN p_route_key IN (
            'model_credentials.create',
            'model_credentials.replace',
            'model_credentials.revoke',
            'model_credentials.revalidate'
        ) THEN 'model_credential'
        WHEN p_route_key IN (
            'hosted_agents.create',
            'hosted_agents.update',
            'hosted_agents.disable'
        ) THEN 'arena_agent'
        WHEN p_route_key = 'game_participants.create' THEN 'game_agent'
        ELSE NULL
    END;

    IF p_resource_kind IS DISTINCT FROM v_expected_resource_kind THEN
        RAISE EXCEPTION 'invalid API idempotency resource kind'
            USING ERRCODE = '22023';
    END IF;

    -- Completion accepts only an existing resource owned by the same
    -- principal. The caller cannot use safe_response or resource_id as an
    -- arbitrary Secret persistence channel.
    IF p_resource_kind = 'model_credential' THEN
        SELECT credential.status
        INTO v_resource_status
        FROM public.arena_model_credentials AS credential
        WHERE credential.credential_id = p_resource_id
          AND credential.owner_user_id = p_owner_user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'API idempotency resource not found'
                USING ERRCODE = 'P0002';
        END IF;

        IF (
            p_route_key = 'model_credentials.create'
            AND v_resource_status = 'pending_write'
        ) OR (
            p_route_key = 'model_credentials.replace'
            AND v_resource_status IN ('pending_write', 'stored')
        ) OR (
            p_route_key = 'model_credentials.revoke'
            AND v_resource_status NOT IN ('revoking', 'revoked')
        ) OR (
            p_route_key = 'model_credentials.revalidate'
            AND v_resource_status NOT IN (
                'pending_validation',
                'valid',
                'invalid'
            )
        ) THEN
            RAISE EXCEPTION 'API idempotency resource is not ready'
                USING ERRCODE = '55000';
        END IF;
    ELSIF p_resource_kind = 'arena_agent' THEN
        SELECT agent.status
        INTO v_resource_status
        FROM public.arena_agents AS agent
        WHERE agent.agent_id = p_resource_id
          AND agent.owner_user_id = p_owner_user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'API idempotency resource not found'
                USING ERRCODE = 'P0002';
        END IF;

        IF p_route_key = 'hosted_agents.create' THEN
            PERFORM 1
            FROM public.arena_hosted_configs AS config
            JOIN public.arena_runtime_bindings AS binding
              ON binding.hosted_config_id = config.hosted_config_id
             AND binding.agent_id = config.agent_id
            WHERE config.agent_id = p_resource_id
              AND config.owner_user_id = p_owner_user_id
              AND binding.runtime_kind = 'hosted';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'API idempotency resource is not ready'
                    USING ERRCODE = '55000';
            END IF;
        ELSIF p_route_key = 'hosted_agents.disable'
              AND v_resource_status <> 'disabled' THEN
            RAISE EXCEPTION 'API idempotency resource is not ready'
                USING ERRCODE = '55000';
        END IF;
    ELSIF p_resource_kind = 'game_agent' THEN
        PERFORM 1
        FROM public.game_agents AS participant
        WHERE participant.game_agent_id = p_resource_id
          AND participant.user_id = p_owner_user_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'API idempotency resource not found'
                USING ERRCODE = 'P0002';
        END IF;
    END IF;

    v_safe_response := pg_catalog.jsonb_build_object(
        'httpStatus',
        p_http_status,
        'projectionVersion',
        'arena-api-response.v1'
    );

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'arena-api-idempotency-owner:' || p_owner_user_id,
            0
        )
    );
    v_now := clock_timestamp();

    SELECT i.*
    INTO v_record
    FROM public.arena_api_idempotency AS i
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT
            'not_found'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            NULL::TIMESTAMPTZ;
        RETURN;
    END IF;

    IF v_record.request_digest <> p_request_digest THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.expires_at <= v_now THEN
        RETURN QUERY
        SELECT
            'expired'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.state = 'completed' THEN
        IF v_record.resource_kind = p_resource_kind
           AND v_record.resource_id = p_resource_id
           AND v_record.safe_response = v_safe_response THEN
            RETURN QUERY
            SELECT
                'replay'::TEXT,
                v_record.state,
                v_record.resource_kind,
                v_record.resource_id,
                v_record.safe_response,
                v_record.expires_at;
            RETURN;
        END IF;

        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    IF v_record.resource_kind IS NULL THEN
        RAISE EXCEPTION 'API idempotency resource is not attached'
            USING ERRCODE = '55000';
    END IF;

    IF v_record.resource_kind <> p_resource_kind
       OR v_record.resource_id <> p_resource_id THEN
        RETURN QUERY
        SELECT
            'conflict'::TEXT,
            v_record.state,
            NULL::TEXT,
            NULL::TEXT,
            NULL::JSONB,
            v_record.expires_at;
        RETURN;
    END IF;

    UPDATE public.arena_api_idempotency AS i
    SET state = 'completed',
        resource_kind = p_resource_kind,
        resource_id = p_resource_id,
        safe_response = v_safe_response,
        completed_at = v_now
    WHERE i.owner_user_id = p_owner_user_id
      AND i.route_key = p_route_key
      AND i.key_digest = p_key_digest
    RETURNING i.* INTO v_record;

    RETURN QUERY
    SELECT
        'completed'::TEXT,
        v_record.state,
        v_record.resource_kind,
        v_record.resource_id,
        v_record.safe_response,
        v_record.expires_at;
END
$complete_arena_api_idempotency$;

-- Migration role owns DDL state. The API has no direct access to the
-- idempotency table and can only call the four bounded SECURITY DEFINER
-- transitions.
ALTER TABLE arena_api_idempotency OWNER TO adx_arena_migration;
ALTER FUNCTION reserve_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION attach_arena_api_idempotency_resource(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION complete_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION release_arena_api_idempotency_for_retry(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) OWNER TO adx_arena_function_owner;

REVOKE ALL ON arena_api_idempotency FROM PUBLIC;
REVOKE ALL ON arena_api_idempotency FROM
    adx_arena_api,
    adx_arena_core,
    adx_hosted_worker,
    adx_credential_controller,
    adx_arena_function_owner;

REVOKE ALL ON FUNCTION reserve_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_arena_api_idempotency_resource(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION complete_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION release_arena_api_idempotency_for_retry(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC;

GRANT SELECT ON connector_users TO adx_arena_function_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON arena_api_idempotency
    TO adx_arena_function_owner;

GRANT EXECUTE ON FUNCTION reserve_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) TO adx_arena_api;
GRANT EXECUTE ON FUNCTION attach_arena_api_idempotency_resource(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO adx_arena_api;
GRANT EXECUTE ON FUNCTION complete_arena_api_idempotency(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    INTEGER
) TO adx_arena_api;
GRANT EXECUTE ON FUNCTION release_arena_api_idempotency_for_retry(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO adx_arena_api;

COMMIT;
