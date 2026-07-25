"""Tests for the single-host encrypted PostgreSQL Secret Store."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from hosted_agent_runtime.encrypted_secret_store import (
    AesGcmSecretCipher,
    EncryptedSecretRecord,
    PostgresEncryptedSecretController,
    PostgresEncryptedSecretReader,
    PostgresEncryptedSecretVault,
    PostgresEncryptedSecretWriter,
    load_master_key,
)
from hosted_agent_runtime.secret_store import (
    SecretReference,
    SecretStoreConfigurationError,
    SecretStoreOperationError,
    SecretWrite,
)

_T = TypeVar("_T")


def _run(coroutine: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coroutine)


class _SqlStateError(RuntimeError):
    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate
        super().__init__("redacted database error")


class _Pool:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.closed = False

    async def fetchval(self, sql: str, *args: object) -> bool:
        secret_ref = str(args[0])
        if "store_hosted" in sql:
            if secret_ref in self.rows:
                raise _SqlStateError("23505")
            self.rows[secret_ref] = {
                "ciphertext": args[1],
                "nonce": args[2],
                "key_version": args[3],
                "status": "active",
            }
            return True
        row = self.rows.get(secret_ref)
        if row is None:
            raise _SqlStateError("P0002")
        if "revoke_hosted" in sql:
            row["status"] = "revoked"
            return True
        if row["status"] != "revoked":
            raise _SqlStateError("55000")
        del self.rows[secret_ref]
        return True

    async def fetchrow(self, _sql: str, secret_ref: str) -> object:
        row = self.rows.get(secret_ref)
        if row is None:
            raise _SqlStateError("P0002")
        if row["status"] == "revoked":
            raise _SqlStateError("55000")
        return row

    async def close(self) -> None:
        self.closed = True


def _ports() -> tuple[
    PostgresEncryptedSecretWriter,
    PostgresEncryptedSecretReader,
    PostgresEncryptedSecretController,
    _Pool,
]:
    pool = _Pool()
    cipher = AesGcmSecretCipher(b"k" * 32)
    return (
        PostgresEncryptedSecretWriter(
            PostgresEncryptedSecretVault(
                "",
                role="adx_arena_api",
                pool=pool,
            ),
            cipher,
        ),
        PostgresEncryptedSecretReader(
            PostgresEncryptedSecretVault(
                "",
                role="adx_hosted_worker",
                pool=pool,
            ),
            cipher,
        ),
        PostgresEncryptedSecretController(
            PostgresEncryptedSecretVault(
                "",
                role="adx_credential_controller",
                pool=pool,
            )
        ),
        pool,
    )


def test_ciphertext_round_trip_binds_reference_and_detects_tampering() -> None:
    cipher = AesGcmSecretCipher(b"k" * 32)
    secret_ref = SecretReference("arena402/hosted-model/credential-001")
    record = cipher.encrypt(secret_ref, b"deepseek-secret")

    assert b"deepseek-secret" not in record.ciphertext
    assert cipher.decrypt(secret_ref, record) == b"deepseek-secret"

    tampered = EncryptedSecretRecord(
        ciphertext=record.ciphertext[:-1] + bytes([record.ciphertext[-1] ^ 1]),
        nonce=record.nonce,
        key_version=record.key_version,
    )
    with pytest.raises(SecretStoreOperationError) as exc:
        cipher.decrypt(secret_ref, tampered)
    assert exc.value.code == "invalid_secret_material"

    with pytest.raises(SecretStoreOperationError):
        cipher.decrypt(
            SecretReference("arena402/hosted-model/credential-002"),
            record,
        )


def test_postgres_ports_preserve_write_read_revoke_delete_boundaries() -> None:
    writer, reader, controller, pool = _ports()
    raw_secret = "deepseek-key-never-logged"
    secret_ref = SecretReference("arena402/hosted-model/credential-003")

    with SecretWrite.from_text(raw_secret) as secret:
        assert _run(writer.create(secret_ref, secret)) == secret_ref
    stored = pool.rows[secret_ref.value]
    assert raw_secret.encode() not in bytes(stored["ciphertext"])

    resolved = _run(reader.resolve_for_worker(secret_ref))
    try:
        assert resolved.reveal_for_worker() == raw_secret
    finally:
        resolved.close()

    with (
        SecretWrite.from_text("replacement") as duplicate,
        pytest.raises(SecretStoreOperationError) as exc,
    ):
        _run(writer.create(secret_ref, duplicate))
    assert exc.value.code == "secret_already_exists"

    with pytest.raises(SecretStoreOperationError) as exc:
        _run(controller.delete_after_retention(secret_ref))
    assert exc.value.code == "secret_must_be_revoked"

    _run(controller.revoke(secret_ref))
    with pytest.raises(SecretStoreOperationError) as exc:
        _run(reader.resolve_for_worker(secret_ref))
    assert exc.value.code == "secret_revoked"

    _run(controller.delete_after_retention(secret_ref))
    with pytest.raises(SecretStoreOperationError) as exc:
        _run(reader.resolve_for_worker(secret_ref))
    assert exc.value.code == "secret_not_found"

    assert not hasattr(writer, "resolve_for_worker")
    assert not hasattr(reader, "create")
    assert not hasattr(controller, "resolve_for_worker")


def test_master_key_file_requires_raw_32_bytes_and_safe_write_mode(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "hosted-master.key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o400)
    assert len(load_master_key(key_file)) == 32

    key_file.chmod(0o440)
    with pytest.raises(SecretStoreConfigurationError) as exc:
        load_master_key(key_file)
    assert exc.value.code == "invalid_settings"

    key_file.chmod(0o600)
    key_file.write_bytes(b"short")
    key_file.chmod(0o400)
    with pytest.raises(SecretStoreConfigurationError) as exc:
        load_master_key(key_file)
    assert exc.value.code == "invalid_settings"
