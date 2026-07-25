BEGIN;

-- Application-layer envelope encryption for platform-managed testnet wallets.
-- PostgreSQL stores ciphertext only. The 32-byte KEK stays in a separate,
-- read-only host file mounted only into the wallet signer/import tooling.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_wallet_signer') THEN
        CREATE ROLE adx_wallet_signer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adx_wallet_importer') THEN
        CREATE ROLE adx_wallet_importer NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA wallet_secret_vault;
ALTER SCHEMA wallet_secret_vault OWNER TO adx_arena_migration;

ALTER TABLE arena402.wallet_inventory
    ADD CONSTRAINT wallet_inventory_wallet_address_unique
    UNIQUE (wallet_id, account_address);

CREATE TABLE wallet_secret_vault.encrypted_wallet_keys (
    wallet_id TEXT PRIMARY KEY,
    account_address TEXT NOT NULL CHECK (
        account_address ~ '^0x[0-9a-f]{40}$'
    ),
    private_key_ciphertext BYTEA NOT NULL CHECK (
        octet_length(private_key_ciphertext) = 48
    ),
    private_key_nonce BYTEA NOT NULL CHECK (
        octet_length(private_key_nonce) = 12
    ),
    encrypted_data_key BYTEA NOT NULL CHECK (
        octet_length(encrypted_data_key) = 48
    ),
    data_key_nonce BYTEA NOT NULL CHECK (
        octet_length(data_key_nonce) = 12
    ),
    key_version INTEGER NOT NULL CHECK (
        key_version BETWEEN 1 AND 2147483647
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'disabled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (wallet_id, account_address)
        REFERENCES arena402.wallet_inventory(wallet_id, account_address)
        ON DELETE RESTRICT,
    UNIQUE (wallet_id, account_address)
);

ALTER TABLE wallet_secret_vault.encrypted_wallet_keys
    OWNER TO adx_arena_migration;
REVOKE ALL ON SCHEMA wallet_secret_vault FROM PUBLIC;
REVOKE ALL ON wallet_secret_vault.encrypted_wallet_keys FROM PUBLIC;
REVOKE ALL ON wallet_secret_vault.encrypted_wallet_keys FROM
    adx_arena_api,
    adx_arena_core,
    adx_settlement,
    adx_wallet_signer,
    adx_wallet_importer,
    adx_arena_function_owner;

CREATE FUNCTION wallet_secret_vault.import_wallet_encrypted_secret(
    p_wallet_id TEXT,
    p_chain_id BIGINT,
    p_account_address TEXT,
    p_private_key_ciphertext BYTEA,
    p_private_key_nonce BYTEA,
    p_encrypted_data_key BYTEA,
    p_data_key_nonce BYTEA,
    p_key_version INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, arena402, wallet_secret_vault
AS $import_wallet_encrypted_secret$
DECLARE
    v_inventory arena402.wallet_inventory%ROWTYPE;
    v_existing wallet_secret_vault.encrypted_wallet_keys%ROWTYPE;
BEGIN
    IF p_account_address <> lower(p_account_address) THEN
        RAISE EXCEPTION 'wallet address must be lowercase'
            USING ERRCODE = '22023';
    END IF;

    SELECT *
    INTO v_inventory
    FROM arena402.wallet_inventory
    WHERE wallet_id = p_wallet_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO arena402.wallet_inventory (
            wallet_id,
            chain_id,
            account_address,
            secret_ref
        )
        VALUES (
            p_wallet_id,
            p_chain_id,
            p_account_address,
            'wallet-vault://' || p_wallet_id
        );
    ELSE
        IF v_inventory.chain_id <> p_chain_id
           OR v_inventory.account_address <> p_account_address THEN
            RAISE EXCEPTION 'wallet inventory identity mismatch'
                USING ERRCODE = '23514';
        END IF;
        UPDATE arena402.wallet_inventory
        SET secret_ref = 'wallet-vault://' || p_wallet_id
        WHERE wallet_id = p_wallet_id
          AND secret_ref <> 'wallet-vault://' || p_wallet_id;
    END IF;

    SELECT *
    INTO v_existing
    FROM wallet_secret_vault.encrypted_wallet_keys
    WHERE wallet_id = p_wallet_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_existing.account_address <> p_account_address THEN
            RAISE EXCEPTION 'encrypted wallet identity mismatch'
                USING ERRCODE = '23514';
        END IF;
        RETURN FALSE;
    END IF;

    INSERT INTO wallet_secret_vault.encrypted_wallet_keys (
        wallet_id,
        account_address,
        private_key_ciphertext,
        private_key_nonce,
        encrypted_data_key,
        data_key_nonce,
        key_version
    )
    VALUES (
        p_wallet_id,
        p_account_address,
        p_private_key_ciphertext,
        p_private_key_nonce,
        p_encrypted_data_key,
        p_data_key_nonce,
        p_key_version
    );
    RETURN TRUE;
END
$import_wallet_encrypted_secret$;

CREATE FUNCTION wallet_secret_vault.read_wallet_encrypted_secret(
    p_wallet_id TEXT
)
RETURNS TABLE (
    wallet_id TEXT,
    account_address TEXT,
    private_key_ciphertext BYTEA,
    private_key_nonce BYTEA,
    encrypted_data_key BYTEA,
    data_key_nonce BYTEA,
    key_version INTEGER,
    status TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, wallet_secret_vault
AS $read_wallet_encrypted_secret$
    SELECT
        item.wallet_id,
        item.account_address,
        item.private_key_ciphertext,
        item.private_key_nonce,
        item.encrypted_data_key,
        item.data_key_nonce,
        item.key_version,
        item.status
    FROM wallet_secret_vault.encrypted_wallet_keys AS item
    WHERE item.wallet_id = p_wallet_id
$read_wallet_encrypted_secret$;

CREATE FUNCTION wallet_secret_vault.read_wallet_data_key_for_rotation()
RETURNS TABLE (
    wallet_id TEXT,
    account_address TEXT,
    encrypted_data_key BYTEA,
    data_key_nonce BYTEA,
    key_version INTEGER
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, wallet_secret_vault
AS $read_wallet_data_key_for_rotation$
    SELECT
        item.wallet_id,
        item.account_address,
        item.encrypted_data_key,
        item.data_key_nonce,
        item.key_version
    FROM wallet_secret_vault.encrypted_wallet_keys AS item
    WHERE item.status = 'active'
    ORDER BY item.wallet_id
$read_wallet_data_key_for_rotation$;

CREATE FUNCTION wallet_secret_vault.rotate_wallet_data_key(
    p_wallet_id TEXT,
    p_expected_key_version INTEGER,
    p_encrypted_data_key BYTEA,
    p_data_key_nonce BYTEA,
    p_new_key_version INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, wallet_secret_vault
AS $rotate_wallet_data_key$
BEGIN
    IF p_new_key_version <= p_expected_key_version THEN
        RAISE EXCEPTION 'new wallet key version must increase'
            USING ERRCODE = '22023';
    END IF;
    UPDATE wallet_secret_vault.encrypted_wallet_keys
    SET encrypted_data_key = p_encrypted_data_key,
        data_key_nonce = p_data_key_nonce,
        key_version = p_new_key_version,
        updated_at = clock_timestamp()
    WHERE wallet_id = p_wallet_id
      AND key_version = p_expected_key_version
      AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'wallet key version changed or wallet unavailable'
            USING ERRCODE = '40001';
    END IF;
    RETURN TRUE;
END
$rotate_wallet_data_key$;

CREATE FUNCTION wallet_secret_vault.disable_wallet_encrypted_secret(
    p_wallet_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, arena402, wallet_secret_vault
AS $disable_wallet_encrypted_secret$
BEGIN
    UPDATE wallet_secret_vault.encrypted_wallet_keys
    SET status = 'disabled',
        updated_at = clock_timestamp()
    WHERE wallet_id = p_wallet_id
      AND status = 'active';
    IF NOT FOUND THEN
        PERFORM 1
        FROM wallet_secret_vault.encrypted_wallet_keys
        WHERE wallet_id = p_wallet_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'encrypted wallet not found'
                USING ERRCODE = 'P0002';
        END IF;
    END IF;
    UPDATE arena402.wallet_inventory
    SET status = 'disabled'
    WHERE wallet_id = p_wallet_id;
    RETURN TRUE;
END
$disable_wallet_encrypted_secret$;

ALTER FUNCTION wallet_secret_vault.import_wallet_encrypted_secret(
    TEXT, BIGINT, TEXT, BYTEA, BYTEA, BYTEA, BYTEA, INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION wallet_secret_vault.read_wallet_encrypted_secret(TEXT)
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION wallet_secret_vault.read_wallet_data_key_for_rotation()
    OWNER TO adx_arena_function_owner;
ALTER FUNCTION wallet_secret_vault.rotate_wallet_data_key(
    TEXT, INTEGER, BYTEA, BYTEA, INTEGER
) OWNER TO adx_arena_function_owner;
ALTER FUNCTION wallet_secret_vault.disable_wallet_encrypted_secret(TEXT)
    OWNER TO adx_arena_function_owner;

REVOKE ALL ON FUNCTION wallet_secret_vault.import_wallet_encrypted_secret(
    TEXT, BIGINT, TEXT, BYTEA, BYTEA, BYTEA, BYTEA, INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION wallet_secret_vault.read_wallet_encrypted_secret(TEXT)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION
    wallet_secret_vault.read_wallet_data_key_for_rotation()
    FROM PUBLIC;
REVOKE ALL ON FUNCTION wallet_secret_vault.rotate_wallet_data_key(
    TEXT, INTEGER, BYTEA, BYTEA, INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    wallet_secret_vault.disable_wallet_encrypted_secret(TEXT)
    FROM PUBLIC;

GRANT USAGE ON SCHEMA arena402, wallet_secret_vault
    TO adx_arena_function_owner;
GRANT SELECT, INSERT, UPDATE ON arena402.wallet_inventory
    TO adx_arena_function_owner;
GRANT SELECT, INSERT, UPDATE
    ON wallet_secret_vault.encrypted_wallet_keys
    TO adx_arena_function_owner;

GRANT USAGE ON SCHEMA wallet_secret_vault
    TO adx_wallet_signer, adx_wallet_importer;
GRANT EXECUTE ON FUNCTION
    wallet_secret_vault.read_wallet_encrypted_secret(TEXT)
    TO adx_wallet_signer;
GRANT EXECUTE ON FUNCTION wallet_secret_vault.import_wallet_encrypted_secret(
    TEXT, BIGINT, TEXT, BYTEA, BYTEA, BYTEA, BYTEA, INTEGER
) TO adx_wallet_importer;
GRANT EXECUTE ON FUNCTION
    wallet_secret_vault.read_wallet_data_key_for_rotation()
    TO adx_wallet_importer;
GRANT EXECUTE ON FUNCTION wallet_secret_vault.rotate_wallet_data_key(
    TEXT, INTEGER, BYTEA, BYTEA, INTEGER
) TO adx_wallet_importer;
GRANT EXECUTE ON FUNCTION
    wallet_secret_vault.disable_wallet_encrypted_secret(TEXT)
    TO adx_wallet_importer;

COMMIT;
