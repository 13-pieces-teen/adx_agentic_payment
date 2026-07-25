BEGIN;

ALTER TABLE connector_users
    ADD COLUMN identity_provider TEXT NOT NULL DEFAULT 'password',
    ADD COLUMN provider_subject TEXT;

ALTER TABLE connector_users
    DROP CONSTRAINT connector_password_for_durable_user;

ALTER TABLE connector_users
    ADD CONSTRAINT connector_identity_provider_allowed
        CHECK (identity_provider IN ('password', 'github')),
    ADD CONSTRAINT connector_provider_subject_shape
        CHECK (
            provider_subject IS NULL
            OR provider_subject ~ '^[1-9][0-9]{0,30}$'
        ),
    ADD CONSTRAINT connector_durable_auth_identity
        CHECK (
            temporary
            OR (
                identity_provider = 'password'
                AND password_hash IS NOT NULL
                AND provider_subject IS NULL
            )
            OR (
                identity_provider = 'github'
                AND password_hash IS NULL
                AND provider_subject IS NOT NULL
            )
        );

CREATE UNIQUE INDEX connector_users_provider_subject_uidx
    ON connector_users (identity_provider, provider_subject)
    WHERE provider_subject IS NOT NULL;

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'adx_connector_gateway'
    ) THEN
        CREATE ROLE adx_connector_gateway NOLOGIN;
    END IF;
END
$roles$;

GRANT SELECT, INSERT, UPDATE, DELETE ON connector_users
    TO adx_connector_gateway;

COMMIT;
