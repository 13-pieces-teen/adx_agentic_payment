from __future__ import annotations

import os
from pathlib import Path

import pytest

from hosted_agent_runtime.encrypted_secret_store import (
    PostgresEncryptedSecretController,
    PostgresEncryptedSecretWriter,
)
from hosted_agent_runtime.production_secrets import (
    build_production_secret_controller,
    build_production_secret_writer,
)
from hosted_agent_runtime.secret_store import SecretStoreConfigurationError


def _configure_postgres_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADX_HOSTED_SECRET_BACKEND", "postgres_aesgcm")
    monkeypatch.setenv("ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED", "true")
    monkeypatch.delenv("ADX_TENCENT_SSM_IAM_VERIFIED", raising=False)


def test_postgres_controller_does_not_require_or_receive_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_postgres_backend(monkeypatch)
    monkeypatch.delenv("ADX_HOSTED_MASTER_KEY_FILE", raising=False)

    controller = build_production_secret_controller(
        "postgresql://controller:test@postgres/test"
    )

    assert isinstance(controller, PostgresEncryptedSecretController)
    assert not hasattr(controller, "_cipher")
    assert not hasattr(controller, "resolve_for_worker")


def test_postgres_writer_requires_verified_backend_and_safe_key_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_postgres_backend(monkeypatch)
    key_file = tmp_path / "hosted-master.key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o400)
    monkeypatch.setenv("ADX_HOSTED_MASTER_KEY_FILE", str(key_file))

    writer = build_production_secret_writer(
        "postgresql://api:test@postgres/test"
    )
    assert isinstance(writer, PostgresEncryptedSecretWriter)

    monkeypatch.setenv("ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED", "false")
    with pytest.raises(SecretStoreConfigurationError) as exc:
        build_production_secret_writer(
            "postgresql://api:test@postgres/test"
        )
    assert exc.value.code == "credential_backend_unverified"
