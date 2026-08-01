BEGIN;

GRANT SELECT ON TABLE public.adx_schema_migrations TO
    adx_connector_gateway,
    adx_arena_api,
    adx_arena_core,
    adx_hosted_worker,
    adx_credential_controller,
    adx_settlement,
    adx_wallet_signer;

COMMIT;
