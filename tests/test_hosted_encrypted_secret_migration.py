from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = (
    ROOT
    / "db"
    / "migrations"
    / "017_hosted_agent_encrypted_secret_vault.sql"
)


def test_encrypted_vault_has_no_runtime_direct_table_privileges() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE hosted_secret_vault.encrypted_model_credentials" in sql
    assert "ciphertext BYTEA NOT NULL" in sql
    assert "nonce BYTEA NOT NULL CHECK (octet_length(nonce) = 12)" in sql
    assert "REVOKE ALL ON hosted_secret_vault.encrypted_model_credentials" in sql
    assert "TO adx_arena_api;" not in sql.split(
        "GRANT EXECUTE ON FUNCTION public.store_hosted_encrypted_secret", 1
    )[0]


def test_encrypted_vault_exposes_only_role_specific_functions() -> None:
    sql = SQL_PATH.read_text(encoding="utf-8")
    assert (
        "public.store_hosted_encrypted_secret(\n"
        "    TEXT,\n"
        "    BYTEA,\n"
        "    BYTEA,\n"
        "    INTEGER\n"
        ") TO adx_arena_api;"
    ) in sql
    assert "public.read_hosted_encrypted_secret(TEXT)\n    TO adx_hosted_worker;" in sql
    assert (
        "public.revoke_hosted_encrypted_secret(TEXT)\n"
        "    TO adx_credential_controller;"
    ) in sql
    assert (
        "public.delete_hosted_encrypted_secret(TEXT)\n"
        "    TO adx_credential_controller;"
    ) in sql
