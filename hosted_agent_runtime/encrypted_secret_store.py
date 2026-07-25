"""PostgreSQL-backed AES-GCM Secret Store for the single-host beta.

The database stores only authenticated ciphertext.  The API and Hosted Worker
load the same 256-bit key from a read-only file mounted by the deployment; the
Credential Controller can revoke/delete rows without receiving that key.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .secret_store import (
    SecretReference,
    SecretStoreConfigurationError,
    SecretStoreOperationError,
    SecretWrite,
    WorkerSecret,
)

_NONCE_BYTES: Final[int] = 12
_MASTER_KEY_BYTES: Final[int] = 32
_AAD_PREFIX: Final[bytes] = b"arena402:hosted-model-key:aesgcm:v1\x00"
_Role = Literal[
    "adx_arena_api",
    "adx_hosted_worker",
    "adx_credential_controller",
]


@dataclass(frozen=True, slots=True)
class EncryptedSecretRecord:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def load_master_key(path: str | os.PathLike[str]) -> bytes:
    """Load one raw 256-bit key from a non-writable regular file."""

    descriptor = -1
    try:
        key_path = Path(path)
        descriptor = os.open(
            key_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SecretStoreConfigurationError("invalid_settings")
        if file_stat.st_mode & (
            stat.S_IRWXG
            | stat.S_IRWXO
            | stat.S_IWUSR
        ):
            raise SecretStoreConfigurationError("invalid_settings")
        with os.fdopen(descriptor, "rb", closefd=False) as key_file:
            key = key_file.read(_MASTER_KEY_BYTES + 1)
    except SecretStoreConfigurationError:
        raise
    except (OSError, ValueError):
        raise SecretStoreConfigurationError("backend_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(key) != _MASTER_KEY_BYTES:
        raise SecretStoreConfigurationError("invalid_settings")
    return key


class AesGcmSecretCipher:
    """Authenticated encryption bound to the opaque reference and key version."""

    def __init__(self, master_key: bytes, *, key_version: int = 1) -> None:
        if type(master_key) is not bytes or len(master_key) != _MASTER_KEY_BYTES:
            raise SecretStoreConfigurationError("invalid_settings")
        if type(key_version) is not int or not 1 <= key_version <= 2_147_483_647:
            raise SecretStoreConfigurationError("invalid_settings")
        self._aesgcm = AESGCM(master_key)
        self.key_version = key_version

    @classmethod
    def from_file(
        cls,
        path: str | os.PathLike[str],
        *,
        key_version: int = 1,
    ) -> AesGcmSecretCipher:
        return cls(load_master_key(path), key_version=key_version)

    @staticmethod
    def _aad(secret_ref: SecretReference, key_version: int) -> bytes:
        return (
            _AAD_PREFIX
            + str(key_version).encode("ascii")
            + b"\x00"
            + secret_ref.value.encode("utf-8")
        )

    def encrypt(
        self,
        secret_ref: SecretReference,
        plaintext: bytes,
    ) -> EncryptedSecretRecord:
        if type(plaintext) is not bytes or not plaintext:
            raise SecretStoreOperationError("invalid_secret_material")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(
            nonce,
            plaintext,
            self._aad(secret_ref, self.key_version),
        )
        return EncryptedSecretRecord(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self.key_version,
        )

    def decrypt(
        self,
        secret_ref: SecretReference,
        record: EncryptedSecretRecord,
    ) -> bytes:
        if record.key_version != self.key_version:
            raise SecretStoreOperationError("invalid_secret_material")
        if len(record.nonce) != _NONCE_BYTES or not record.ciphertext:
            raise SecretStoreOperationError("invalid_secret_material")
        try:
            return self._aesgcm.decrypt(
                record.nonce,
                record.ciphertext,
                self._aad(secret_ref, record.key_version),
            )
        except (InvalidTag, ValueError):
            raise SecretStoreOperationError(
                "invalid_secret_material"
            ) from None


class PostgresEncryptedSecretVault:
    """Least-privilege adapter over the migration's bounded SQL functions."""

    def __init__(
        self,
        database_url: str,
        *,
        role: _Role,
        pool: Any | None = None,
    ) -> None:
        if not database_url and pool is None:
            raise SecretStoreConfigurationError("invalid_settings")
        if role not in {
            "adx_arena_api",
            "adx_hosted_worker",
            "adx_credential_controller",
        }:
            raise SecretStoreConfigurationError("invalid_settings")
        self._database_url = database_url
        self._role = role
        self._pool = pool
        self._owns_pool = pool is None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError:
            raise SecretStoreConfigurationError(
                "backend_unavailable"
            ) from None

        role = self._role

        async def initialize_connection(connection: Any) -> None:
            await connection.execute(f"SET ROLE {role}")
            await connection.execute("SET search_path TO pg_catalog, public")

        try:
            self._pool = await asyncpg.create_pool(
                self._database_url,
                min_size=1,
                max_size=3,
                command_timeout=30,
                init=initialize_connection,
            )
        except Exception:  # noqa: BLE001 - redact driver/backend details
            raise SecretStoreConfigurationError(
                "backend_unavailable"
            ) from None
        self._owns_pool = True

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise SecretStoreOperationError("backend_unavailable")
        return self._pool

    @staticmethod
    def _sqlstate(error: Exception) -> str | None:
        value = getattr(error, "sqlstate", None)
        return value if isinstance(value, str) else None

    async def create(
        self,
        secret_ref: SecretReference,
        record: EncryptedSecretRecord,
    ) -> None:
        try:
            await self._require_pool().fetchval(
                "SELECT store_hosted_encrypted_secret($1, $2, $3, $4)",
                secret_ref.value,
                record.ciphertext,
                record.nonce,
                record.key_version,
            )
        except Exception as error:  # noqa: BLE001 - map safe SQLSTATE only
            code = (
                "secret_already_exists"
                if self._sqlstate(error) == "23505"
                else "backend_unavailable"
            )
            raise SecretStoreOperationError(code) from None

    async def resolve(
        self,
        secret_ref: SecretReference,
    ) -> EncryptedSecretRecord:
        try:
            row = await self._require_pool().fetchrow(
                "SELECT * FROM read_hosted_encrypted_secret($1)",
                secret_ref.value,
            )
        except Exception as error:  # noqa: BLE001 - map safe SQLSTATE only
            code = {
                "P0002": "secret_not_found",
                "55000": "secret_revoked",
            }.get(self._sqlstate(error), "backend_unavailable")
            raise SecretStoreOperationError(code) from None
        if row is None:
            raise SecretStoreOperationError("secret_not_found")
        return EncryptedSecretRecord(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            key_version=int(row["key_version"]),
        )

    async def revoke(self, secret_ref: SecretReference) -> None:
        try:
            await self._require_pool().fetchval(
                "SELECT revoke_hosted_encrypted_secret($1)",
                secret_ref.value,
            )
        except Exception as error:  # noqa: BLE001 - map safe SQLSTATE only
            code = (
                "secret_not_found"
                if self._sqlstate(error) == "P0002"
                else "backend_unavailable"
            )
            raise SecretStoreOperationError(code) from None

    async def delete_after_retention(
        self,
        secret_ref: SecretReference,
    ) -> None:
        try:
            await self._require_pool().fetchval(
                "SELECT delete_hosted_encrypted_secret($1)",
                secret_ref.value,
            )
        except Exception as error:  # noqa: BLE001 - map safe SQLSTATE only
            code = {
                "P0002": "secret_not_found",
                "55000": "secret_must_be_revoked",
            }.get(self._sqlstate(error), "backend_unavailable")
            raise SecretStoreOperationError(code) from None


class PostgresEncryptedSecretWriter:
    """API-side write-only adapter."""

    def __init__(
        self,
        vault: PostgresEncryptedSecretVault,
        cipher: AesGcmSecretCipher,
    ) -> None:
        self._vault = vault
        self._cipher = cipher

    async def initialize(self) -> None:
        await self._vault.initialize()

    async def close(self) -> None:
        await self._vault.close()

    async def create(
        self,
        secret_ref: SecretReference,
        secret: SecretWrite,
    ) -> SecretReference:
        if not isinstance(secret, SecretWrite):
            raise SecretStoreOperationError("invalid_secret_material")
        plaintext = secret._copy_bytes()
        try:
            record = self._cipher.encrypt(secret_ref, plaintext)
        finally:
            plaintext = b""
        await self._vault.create(secret_ref, record)
        return secret_ref


class PostgresEncryptedSecretReader:
    """Hosted Worker read-only adapter."""

    def __init__(
        self,
        vault: PostgresEncryptedSecretVault,
        cipher: AesGcmSecretCipher,
    ) -> None:
        self._vault = vault
        self._cipher = cipher

    async def initialize(self) -> None:
        await self._vault.initialize()

    async def close(self) -> None:
        await self._vault.close()

    async def resolve_for_worker(
        self,
        secret_ref: SecretReference,
    ) -> WorkerSecret:
        record = await self._vault.resolve(secret_ref)
        plaintext = self._cipher.decrypt(secret_ref, record)
        try:
            return WorkerSecret(plaintext)
        finally:
            plaintext = b""


class PostgresEncryptedSecretController:
    """Lifecycle-only adapter; deliberately receives no encryption key."""

    def __init__(self, vault: PostgresEncryptedSecretVault) -> None:
        self._vault = vault

    async def initialize(self) -> None:
        await self._vault.initialize()

    async def close(self) -> None:
        await self._vault.close()

    async def revoke(self, secret_ref: SecretReference) -> None:
        await self._vault.revoke(secret_ref)

    async def delete_after_retention(
        self,
        secret_ref: SecretReference,
    ) -> None:
        await self._vault.delete_after_retention(secret_ref)


__all__ = [
    "AesGcmSecretCipher",
    "EncryptedSecretRecord",
    "PostgresEncryptedSecretController",
    "PostgresEncryptedSecretReader",
    "PostgresEncryptedSecretVault",
    "PostgresEncryptedSecretWriter",
    "load_master_key",
]
