"""Fail-closed production composition for Hosted credential backends."""

from __future__ import annotations

import os
from typing import Final

from .encrypted_secret_store import (
    AesGcmSecretCipher,
    PostgresEncryptedSecretController,
    PostgresEncryptedSecretReader,
    PostgresEncryptedSecretVault,
    PostgresEncryptedSecretWriter,
)
from .secret_store import (
    SecretController,
    SecretReader,
    SecretStoreConfigurationError,
    SecretWriter,
    TencentSecretController,
    TencentSecretReader,
    TencentSecretWriter,
    TencentSsmSettings,
)

POSTGRES_AESGCM_BACKEND: Final[str] = "postgres_aesgcm"
TENCENT_SSM_BACKEND: Final[str] = "tencent_ssm"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SecretStoreConfigurationError("invalid_settings")
    return value


def _true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def configured_backend() -> str:
    backend = os.getenv(
        "ADX_HOSTED_SECRET_BACKEND",
        TENCENT_SSM_BACKEND,
    ).strip()
    if backend not in {POSTGRES_AESGCM_BACKEND, TENCENT_SSM_BACKEND}:
        raise SecretStoreConfigurationError("invalid_settings")
    return backend


def verify_backend_configuration() -> str:
    backend = configured_backend()
    if backend == POSTGRES_AESGCM_BACKEND:
        if not _true("ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED"):
            raise SecretStoreConfigurationError(
                "credential_backend_unverified"
            )
        return backend
    if not _true("ADX_TENCENT_SSM_IAM_VERIFIED"):
        raise SecretStoreConfigurationError(
            "tencent_ssm_adapter_unverified"
        )
    return backend


def _cipher() -> AesGcmSecretCipher:
    try:
        key_version = int(
            os.getenv("ADX_HOSTED_MASTER_KEY_VERSION", "1")
        )
    except ValueError:
        raise SecretStoreConfigurationError("invalid_settings") from None
    return AesGcmSecretCipher.from_file(
        _required("ADX_HOSTED_MASTER_KEY_FILE"),
        key_version=key_version,
    )


def _tencent_settings() -> TencentSsmSettings:
    try:
        recovery_window_days = int(
            os.getenv("ADX_TENCENT_SSM_RECOVERY_WINDOW_DAYS", "0")
        )
    except ValueError:
        raise SecretStoreConfigurationError("invalid_settings") from None
    return TencentSsmSettings(
        region=os.getenv("ADX_TENCENT_SSM_REGION", "ap-guangzhou"),
        recovery_window_days=recovery_window_days,
        deployment_iam_verified=True,
    )


def build_production_secret_writer(
    database_url: str,
) -> SecretWriter:
    backend = verify_backend_configuration()
    if backend == TENCENT_SSM_BACKEND:
        return TencentSecretWriter(_tencent_settings())
    return PostgresEncryptedSecretWriter(
        PostgresEncryptedSecretVault(
            database_url,
            role="adx_arena_api",
        ),
        _cipher(),
    )


def build_production_secret_reader(
    database_url: str,
) -> SecretReader:
    backend = verify_backend_configuration()
    if backend == TENCENT_SSM_BACKEND:
        return TencentSecretReader(_tencent_settings())
    return PostgresEncryptedSecretReader(
        PostgresEncryptedSecretVault(
            database_url,
            role="adx_hosted_worker",
        ),
        _cipher(),
    )


def build_production_secret_controller(
    database_url: str,
) -> SecretController:
    backend = verify_backend_configuration()
    if backend == TENCENT_SSM_BACKEND:
        return TencentSecretController(_tencent_settings())
    return PostgresEncryptedSecretController(
        PostgresEncryptedSecretVault(
            database_url,
            role="adx_credential_controller",
        )
    )


async def initialize_secret_port(port: object) -> None:
    initialize = getattr(port, "initialize", None)
    if initialize is not None:
        await initialize()


async def close_secret_port(port: object) -> None:
    close = getattr(port, "close", None)
    if close is not None:
        await close()


__all__ = [
    "POSTGRES_AESGCM_BACKEND",
    "TENCENT_SSM_BACKEND",
    "build_production_secret_controller",
    "build_production_secret_reader",
    "build_production_secret_writer",
    "close_secret_port",
    "configured_backend",
    "initialize_secret_port",
    "verify_backend_configuration",
]
