from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")
PROVISION = (
    ROOT / "deploy" / "scripts" / "provision_db_roles.py"
).read_text(encoding="utf-8")


def _service(name: str, next_name: str) -> str:
    return COMPOSE.split(f"\n  {name}:", 1)[1].split(f"\n  {next_name}:", 1)[0]


def test_runtime_signer_has_ciphertext_database_and_kek_but_no_csv() -> None:
    signer = _service("wallet-signer", "wallet-vault-admin")
    assert "ADX_WALLET_SIGNER_BACKEND: postgres_aesgcm" in signer
    assert "adx_wallet_signer_login:" in signer
    assert "ADX_WALLET_MASTER_KEY_FILE:" in signer
    assert "wallet-master.key" in signer
    assert "CSV" not in signer
    assert "agent-wallets.csv" not in signer
    assert "read_only: true" in signer


def test_wallet_kek_is_not_mounted_into_settlement_worker() -> None:
    settlement = _service("settlement-worker", "wallet-signer")
    assert "ADX_WALLET_MASTER_KEY_FILE" not in settlement
    assert "wallet-master.key" not in settlement


def test_csv_is_limited_to_non_running_wallet_admin_profile() -> None:
    admin = _service("wallet-vault-admin", "wallet-vault-rotate")
    assert "- wallet-admin" in admin
    assert 'restart: "no"' in admin
    assert "agent-wallets.csv" in admin
    assert "adx_wallet_importer_login:" in admin
    assert "${POSTGRES_USER" not in admin
    assert 'command: ["npm", "run", "wallet:vault-import"]' in admin


def test_rotation_profile_needs_no_csv_or_private_key_ciphertext_access() -> None:
    rotation = _service("wallet-vault-rotate", "arena-facilitator-1")
    assert "- wallet-admin" in rotation
    assert 'restart: "no"' in rotation
    assert 'command: ["npm", "run", "wallet:vault-rotate"]' in rotation
    assert "agent-wallets.csv" not in rotation
    assert "adx_wallet_importer_login:" in rotation


def test_deploy_rejects_mutable_or_malformed_wallet_kek() -> None:
    assert 'stat -c %s "${wallet_key_file}"' in DEPLOY
    assert 'find "${wallet_key_file}" -perm /077' in DEPLOY
    assert 'find "${wallet_key_file}" -perm /200' in DEPLOY
    assert "ADX_WALLET_SIGNER_CSV_HOST_PATH" not in DEPLOY


def test_signer_login_receives_only_signer_role() -> None:
    assert '"adx_wallet_signer_login": (' in PROVISION
    assert '("adx_wallet_signer",)' in PROVISION
    assert '"adx_wallet_importer_login": (' in PROVISION
    assert '("adx_wallet_importer",)' in PROVISION
