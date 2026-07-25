from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = (
    ROOT / "db" / "migrations" / "019_arena_wallet_encrypted_secret_vault.sql"
).read_text(encoding="utf-8")


def test_wallet_vault_stores_only_envelope_ciphertext() -> None:
    assert "CREATE SCHEMA wallet_secret_vault" in SQL
    assert "CREATE TABLE wallet_secret_vault.encrypted_wallet_keys" in SQL
    assert "private_key_ciphertext BYTEA NOT NULL" in SQL
    assert "encrypted_data_key BYTEA NOT NULL" in SQL
    assert "private_key TEXT" not in SQL
    assert "master_key" not in SQL
    assert "FOREIGN KEY (wallet_id, account_address)" in SQL


def test_only_signer_function_can_read_full_ciphertext() -> None:
    assert "CREATE ROLE adx_wallet_signer NOLOGIN" in SQL
    assert "CREATE ROLE adx_wallet_importer NOLOGIN" in SQL
    assert (
        "wallet_secret_vault.read_wallet_encrypted_secret(TEXT)\n"
        "    TO adx_wallet_signer;"
    ) in SQL
    assert "encrypted_wallet_keys\n    TO adx_wallet_signer" not in SQL
    assert "encrypted_wallet_keys\n    TO adx_wallet_importer" not in SQL
    assert (
        "wallet_secret_vault.read_wallet_data_key_for_rotation()\n"
        "    TO adx_wallet_importer;"
    ) in SQL


def test_import_and_rotation_are_bounded_security_definer_functions() -> None:
    assert "SECURITY DEFINER" in SQL
    assert "wallet-vault://" in SQL
    assert "p_expected_key_version INTEGER" in SQL
    assert "key_version = p_expected_key_version" in SQL
    assert "p_new_key_version <= p_expected_key_version" in SQL
