BEGIN;

-- Single-host beta credential vault.
--
-- PostgreSQL never receives the master key or plaintext. Runtime roles have no
-- direct table privileges and can call only their role-specific bounded
-- functions. The API writes ciphertext, the Hosted Worker reads ciphertext,
-- and the Credential Controller can only revoke/delete.

CREATE SCHEMA IF NOT EXISTS hosted_secret_vault;
ALTER SCHEMA hosted_secret_vault OWNER TO adx_arena_migration;

CREATE TABLE hosted_secret_vault.encrypted_model_credentials (
    secret_ref TEXT PRIMARY KEY CHECK (
        secret_ref ~ '^arena402/hosted-model/[A-Za-z0-9._:-]{1,220}$'
    ),
    ciphertext BYTEA NOT NULL CHECK (
        octet_length(ciphertext) BETWEEN 17 AND 32784
    ),
    nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12),
    key_version INTEGER NOT NULL CHECK (key_version BETWEEN 1 AND 2147483647),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'revoked')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_at TIMESTAMPTZ,
    CHECK (
        (status = 'active' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

ALTER TABLE hosted_secret_vault.encrypted_model_credentials
    OWNER TO adx_arena_migration;
REVOKE ALL ON SCHEMA hosted_secret_vault FROM PUBLIC;
REVOKE ALL ON hosted_secret_vault.encrypted_model_credentials FROM PUBLIC;
REVOKE ALL ON hosted_secret_vault.encrypted_model_credentials FROM
    adx_arena_api,
    adx_arena_core,
    adx_hosted_worker,
    adx_credential_controller,
    adx_arena_function_owner;

CREATE FUNCTION public.store_hosted_encrypted_secret(
    p_secret_ref TEXT,
    p_ciphertext BYTEA,
    p_nonce BYTEA,
    p_key_version INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, hosted_secret_vault
AS $store_hosted_encrypted_secret$
BEGIN
    INSERT INTO hosted_secret_vault.encrypted_model_credentials (
        secret_ref,
        ciphertext,
        nonce,
        key_version
    )
    VALUES (
        p_secret_ref,
        p_ciphertext,
        p_nonce,
        p_key_version
    );
    RETURN TRUE;
END
$store_hosted_encrypted_secret$;

CREATE FUNCTION public.read_hosted_encrypted_secret(p_secret_ref TEXT)
RETURNS TABLE (
    ciphertext BYTEA,
    nonce BYTEA,
    key_version INTEGER
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, hosted_secret_vault
AS $read_hosted_encrypted_secret$
DECLARE
    v_status TEXT;
BEGIN
    SELECT
        item.ciphertext,
        item.nonce,
        item.key_version,
        item.status
    INTO
        ciphertext,
        nonce,
        key_version,
        v_status
    FROM hosted_secret_vault.encrypted_model_credentials AS item
    WHERE item.secret_ref = p_secret_ref;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'encrypted secret not found'
            USING ERRCODE = 'P0002';
    END IF;
    IF v_status <> 'active' THEN
        RAISE EXCEPTION 'encrypted secret is revoked'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEXT;
END
$read_hosted_encrypted_secret$;

CREATE FUNCTION public.revoke_hosted_encrypted_secret(p_secret_ref TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, hosted_secret_vault
AS $revoke_hosted_encrypted_secret$
BEGIN
    UPDATE hosted_secret_vault.encrypted_model_credentials AS item
    SET status = 'revoked',
        revoked_at = clock_timestamp()
    WHERE item.secret_ref = p_secret_ref
      AND item.status = 'active';

    IF FOUND THEN
        RETURN TRUE;
    END IF;
    PERFORM 1
    FROM hosted_secret_vault.encrypted_model_credentials AS item
    WHERE item.secret_ref = p_secret_ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'encrypted secret not found'
            USING ERRCODE = 'P0002';
    END IF;
    RETURN TRUE;
END
$revoke_hosted_encrypted_secret$;

CREATE FUNCTION public.delete_hosted_encrypted_secret(p_secret_ref TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, hosted_secret_vault
AS $delete_hosted_encrypted_secret$
BEGIN
    DELETE FROM hosted_secret_vault.encrypted_model_credentials AS item
    WHERE item.secret_ref = p_secret_ref
      AND item.status = 'revoked';

    IF FOUND THEN
        RETURN TRUE;
    END IF;
    PERFORM 1
    FROM hosted_secret_vault.encrypted_model_credentials AS item
    WHERE item.secret_ref = p_secret_ref;
    IF FOUND THEN
        RAISE EXCEPTION 'encrypted secret must be revoked before deletion'
            USING ERRCODE = '55000';
    END IF;
    RAISE EXCEPTION 'encrypted secret not found'
        USING ERRCODE = 'P0002';
END
$delete_hosted_encrypted_secret$;

ALTER FUNCTION public.store_hosted_encrypted_secret(
    TEXT,
    BYTEA,
    BYTEA,
    INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.read_hosted_encrypted_secret(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.revoke_hosted_encrypted_secret(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION public.delete_hosted_encrypted_secret(TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION public.store_hosted_encrypted_secret(
    TEXT,
    BYTEA,
    BYTEA,
    INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.read_hosted_encrypted_secret(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.revoke_hosted_encrypted_secret(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.delete_hosted_encrypted_secret(TEXT) FROM PUBLIC;

GRANT USAGE ON SCHEMA hosted_secret_vault TO adx_arena_function_owner;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON hosted_secret_vault.encrypted_model_credentials
    TO adx_arena_function_owner;
GRANT EXECUTE ON FUNCTION public.store_hosted_encrypted_secret(
    TEXT,
    BYTEA,
    BYTEA,
    INTEGER
) TO adx_arena_api;
GRANT EXECUTE ON FUNCTION public.read_hosted_encrypted_secret(TEXT)
    TO adx_hosted_worker;
GRANT EXECUTE ON FUNCTION public.revoke_hosted_encrypted_secret(TEXT)
    TO adx_credential_controller;
GRANT EXECUTE ON FUNCTION public.delete_hosted_encrypted_secret(TEXT)
    TO adx_credential_controller;

COMMIT;
