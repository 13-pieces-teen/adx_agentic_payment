"""Role-separated Secret Store ports for Hosted Arena Agent BYOK.

The application API may receive a raw provider key in memory, but it must only
receive a reference back from the writer.  Only the Hosted Worker receives a
redacted ``WorkerSecret`` handle, and only the Credential Controller receives
the revoke and post-retention delete ports.

There is deliberately no list operation and no production fallback to memory.
Production composition can select the single-host encrypted PostgreSQL vault
or Tencent SSM; both remain fail-closed until deployment verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, Protocol, Self, TypeAlias


SecretStoreErrorCode: TypeAlias = Literal[
    "backend_required",
    "backend_unavailable",
    "invalid_secret_material",
    "invalid_secret_reference",
    "invalid_settings",
    "memory_backend_forbidden_in_production",
    "memory_backend_not_explicitly_test_only",
    "secret_already_exists",
    "secret_must_be_revoked",
    "secret_not_found",
    "secret_revoked",
    "secret_value_closed",
    "credential_backend_unverified",
    "tencent_ssm_adapter_unverified",
]

# Tencent SSM CreateSecret accepts at most 32 KiB of SecretString material.
# Applying the same ceiling at the port boundary keeps test and production
# behavior aligned. Provider API keys are expected to be far smaller.
_MAX_SECRET_BYTES: Final[int] = 32 * 1024
_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$"
)
_REGION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9-]{1,62}$"
)
TENCENT_HOSTED_MODEL_SECRET_PREFIX: Final[str] = "arena402/hosted-model/"
_TENCENT_SECRET_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)


class SecretStoreError(RuntimeError):
    """Safe-to-log error that never retains a secret or backend exception."""

    def __init__(self, code: SecretStoreErrorCode) -> None:
        self.code = code
        super().__init__(f"Hosted Secret Store operation failed ({code})")


class SecretStoreConfigurationError(SecretStoreError):
    """Fail-closed composition error."""


class SecretStoreOperationError(SecretStoreError):
    """Safe operational failure."""


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Opaque, non-secret reference persisted by the business database."""

    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not _REFERENCE_PATTERN.fullmatch(self.value)
            or ".." in self.value
            or "//" in self.value
        ):
            raise SecretStoreConfigurationError("invalid_secret_reference")

    def __str__(self) -> str:
        return self.value


class _RedactedSecretBuffer:
    """Best-effort mutable secret buffer with redacted string representations."""

    __slots__ = ("_buffer", "_closed")

    _label = "SecretBuffer"

    def __init__(self, encoded_value: bytes) -> None:
        if (
            not encoded_value
            or len(encoded_value) > _MAX_SECRET_BYTES
            or b"\x00" in encoded_value
        ):
            raise SecretStoreOperationError("invalid_secret_material")
        self._buffer = bytearray(encoded_value)
        self._closed = False

    def __repr__(self) -> str:
        return f"<{self._label} redacted>"

    def __str__(self) -> str:
        return "[REDACTED]"

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._closed = True

    def _copy_bytes(self) -> bytes:
        if self._closed:
            raise SecretStoreOperationError("secret_value_closed")
        return bytes(self._buffer)

    def __enter__(self) -> Self:
        if self._closed:
            raise SecretStoreOperationError("secret_value_closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True) is False:
            self.close()


class SecretWrite(_RedactedSecretBuffer):
    """One in-memory write value accepted by the API-side writer port."""

    __slots__ = ()
    _label = "SecretWrite"

    @classmethod
    def from_text(cls, raw_value: str) -> "SecretWrite":
        if type(raw_value) is not str:
            raise SecretStoreOperationError("invalid_secret_material")
        try:
            encoded = raw_value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise SecretStoreOperationError(
                "invalid_secret_material"
            ) from None
        return cls(encoded)

    def hmac_sha256(self, pepper: bytes) -> str:
        """Return a keyed fingerprint without exposing or copying plaintext.

        The caller remains responsible for supplying a deployment secret
        pepper.  Only the one-way hexadecimal digest leaves this object.
        """

        if type(pepper) is not bytes or not pepper:
            raise SecretStoreOperationError("invalid_secret_material")
        if self._closed:
            raise SecretStoreOperationError("secret_value_closed")
        return hmac.new(pepper, self._buffer, hashlib.sha256).hexdigest()


class WorkerSecret(_RedactedSecretBuffer):
    """Redacted handle returned only through the Worker reader port.

    Provider code must call ``reveal_for_worker`` in the smallest possible
    scope and must never log, persist, trace, or return that value.
    """

    __slots__ = ()
    _label = "WorkerSecret"

    def reveal_for_worker(self) -> str:
        try:
            return self._copy_bytes().decode("utf-8", errors="strict")
        except UnicodeError:
            raise SecretStoreOperationError(
                "invalid_secret_material"
            ) from None


class SecretWriter(Protocol):
    """API-side write-only port."""

    async def create(
        self, secret_ref: SecretReference, secret: SecretWrite
    ) -> SecretReference: ...


class SecretReader(Protocol):
    """Hosted Worker read-only port."""

    async def resolve_for_worker(
        self, secret_ref: SecretReference
    ) -> WorkerSecret: ...


class SecretController(Protocol):
    """Credential Controller lifecycle port without read access."""

    async def revoke(self, secret_ref: SecretReference) -> None: ...

    async def delete_after_retention(
        self, secret_ref: SecretReference
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SecretStorePorts:
    """Ports must be injected into separate processes, never as one identity."""

    writer: SecretWriter
    reader: SecretReader
    controller: SecretController


@dataclass(slots=True)
class _MemoryEntry:
    value: bytearray
    revoked: bool = False

    def zeroize(self) -> None:
        for index in range(len(self.value)):
            self.value[index] = 0
        self.value.clear()


class _MemoryBackend:
    def __init__(self) -> None:
        self.entries: dict[str, _MemoryEntry] = {}
        self.lock = asyncio.Lock()

    async def create(
        self, secret_ref: SecretReference, secret: SecretWrite
    ) -> SecretReference:
        secret_copy = bytearray(secret._copy_bytes())
        async with self.lock:
            if secret_ref.value in self.entries:
                for index in range(len(secret_copy)):
                    secret_copy[index] = 0
                secret_copy.clear()
                raise SecretStoreOperationError("secret_already_exists")
            self.entries[secret_ref.value] = _MemoryEntry(secret_copy)
        return secret_ref

    async def resolve(self, secret_ref: SecretReference) -> WorkerSecret:
        async with self.lock:
            entry = self.entries.get(secret_ref.value)
            if entry is None:
                raise SecretStoreOperationError("secret_not_found")
            if entry.revoked:
                raise SecretStoreOperationError("secret_revoked")
            return WorkerSecret(bytes(entry.value))

    async def revoke(self, secret_ref: SecretReference) -> None:
        async with self.lock:
            entry = self.entries.get(secret_ref.value)
            if entry is None:
                raise SecretStoreOperationError("secret_not_found")
            if entry.revoked:
                return
            entry.zeroize()
            entry.revoked = True

    async def delete_after_retention(
        self, secret_ref: SecretReference
    ) -> None:
        async with self.lock:
            entry = self.entries.get(secret_ref.value)
            if entry is None:
                raise SecretStoreOperationError("secret_not_found")
            if not entry.revoked:
                raise SecretStoreOperationError(
                    "secret_must_be_revoked"
                )
            entry.zeroize()
            del self.entries[secret_ref.value]


class _MemorySecretWriter:
    def __init__(self, backend: _MemoryBackend) -> None:
        self._backend = backend

    async def create(
        self, secret_ref: SecretReference, secret: SecretWrite
    ) -> SecretReference:
        return await self._backend.create(secret_ref, secret)


class _MemorySecretReader:
    def __init__(self, backend: _MemoryBackend) -> None:
        self._backend = backend

    async def resolve_for_worker(
        self, secret_ref: SecretReference
    ) -> WorkerSecret:
        return await self._backend.resolve(secret_ref)


class _MemorySecretController:
    def __init__(self, backend: _MemoryBackend) -> None:
        self._backend = backend

    async def revoke(self, secret_ref: SecretReference) -> None:
        await self._backend.revoke(secret_ref)

    async def delete_after_retention(
        self, secret_ref: SecretReference
    ) -> None:
        await self._backend.delete_after_retention(secret_ref)


class MemorySecretStore:
    """Ephemeral store available only behind an explicit non-production gate."""

    def __init__(self, *, allow_for_testing: bool = False) -> None:
        if allow_for_testing is not True:
            raise SecretStoreConfigurationError(
                "memory_backend_not_explicitly_test_only"
            )
        backend = _MemoryBackend()
        self._ports = SecretStorePorts(
            writer=_MemorySecretWriter(backend),
            reader=_MemorySecretReader(backend),
            controller=_MemorySecretController(backend),
        )

    @classmethod
    def for_testing(cls) -> "MemorySecretStore":
        return cls(allow_for_testing=True)

    @classmethod
    def for_local_development(cls) -> "MemorySecretStore":
        """Build the explicit local-only store used by the one-process demo."""

        return cls(allow_for_testing=True)

    @property
    def ports(self) -> SecretStorePorts:
        return self._ports


@dataclass(frozen=True, slots=True)
class TencentSsmSettings:
    """Non-secret Tencent SSM adapter settings."""

    region: str
    secret_prefix: str = TENCENT_HOSTED_MODEL_SECRET_PREFIX
    recovery_window_days: int = 0
    deployment_iam_verified: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.region) is not str
            or not _REGION_PATTERN.fullmatch(self.region)
            or type(self.secret_prefix) is not str
            or not self.secret_prefix.startswith(
                TENCENT_HOSTED_MODEL_SECRET_PREFIX
            )
            or not self.secret_prefix.endswith("/")
            or ".." in self.secret_prefix
            or "//" in self.secret_prefix
            or not _REFERENCE_PATTERN.fullmatch(self.secret_prefix[:-1])
            or type(self.recovery_window_days) is not int
            or self.recovery_window_days not in {0, *range(7, 31)}
            or type(self.deployment_iam_verified) is not bool
        ):
            raise SecretStoreConfigurationError("invalid_settings")


class _TencentWriterApi(Protocol):
    def create_secret(self, name: str, value: str) -> None: ...


class _TencentReaderApi(Protocol):
    def get_secret_value(self, name: str) -> str: ...


class _TencentControllerApi(Protocol):
    def disable_secret(self, name: str) -> None: ...

    def delete_secret(self, name: str, recovery_window_days: int) -> None: ...


def _secret_name(
    settings: TencentSsmSettings,
    secret_ref: SecretReference,
) -> str:
    if not secret_ref.value.startswith(settings.secret_prefix):
        raise SecretStoreOperationError("invalid_secret_reference")
    suffix = secret_ref.value[len(settings.secret_prefix) :]
    name = "arena402_hosted_model_" + suffix.replace("-", "_")
    if not _TENCENT_SECRET_NAME_PATTERN.fullmatch(name):
        raise SecretStoreOperationError("invalid_secret_reference")
    return name


def _tencent_sdk_client(settings: TencentSsmSettings) -> object:
    """Build the official SDK client using its default credential chain.

    The chain supports environment credentials, local profile, CVM role and
    TKE workload identity. No Tencent credential is accepted through the Arena
    HTTP API or stored in the Arena database.
    """

    try:
        credential_module = importlib.import_module(
            "tencentcloud.common.credential"
        )
        client_module = importlib.import_module(
            "tencentcloud.ssm.v20190923.ssm_client"
        )
        profile_module = importlib.import_module(
            "tencentcloud.common.profile.client_profile"
        )
        http_module = importlib.import_module(
            "tencentcloud.common.profile.http_profile"
        )
        credential = (
            credential_module.DefaultCredentialProvider().get_credential()
        )
        http_profile = http_module.HttpProfile()
        http_profile.protocol = "https"
        http_profile.endpoint = "ssm.tencentcloudapi.com"
        http_profile.reqTimeout = 15
        client_profile = profile_module.ClientProfile()
        client_profile.httpProfile = http_profile
        return client_module.SsmClient(
            credential,
            settings.region,
            client_profile,
        )
    except Exception:
        raise SecretStoreConfigurationError("backend_unavailable") from None


class _TencentSdkApi:
    """Private synchronous official-SDK wrapper used behind ``to_thread``."""

    def __init__(self, settings: TencentSsmSettings) -> None:
        self._client = _tencent_sdk_client(settings)
        try:
            self._models = importlib.import_module(
                "tencentcloud.ssm.v20190923.models"
            )
        except Exception:
            raise SecretStoreConfigurationError(
                "backend_unavailable"
            ) from None

    def create_secret(self, name: str, value: str) -> None:
        request = self._models.CreateSecretRequest()
        request.SecretName = name
        request.SecretString = value
        request.Description = "Arena 402 hosted model credential"
        self._client.CreateSecret(request)

    def get_secret_value(self, name: str) -> str:
        request = self._models.GetSecretValueRequest()
        request.SecretName = name
        response = self._client.GetSecretValue(request)
        value = getattr(response, "SecretString", None)
        if not isinstance(value, str) or not value:
            raise SecretStoreOperationError("invalid_secret_material")
        return value

    def disable_secret(self, name: str) -> None:
        request = self._models.DisableSecretRequest()
        request.SecretName = name
        self._client.DisableSecret(request)

    def delete_secret(self, name: str, recovery_window_days: int) -> None:
        request = self._models.DeleteSecretRequest()
        request.SecretName = name
        request.RecoveryWindowInDays = recovery_window_days
        self._client.DeleteSecret(request)


class TencentSecretWriter:
    """API-side Tencent SSM writer with no reader/controller operations."""

    def __init__(
        self,
        settings: TencentSsmSettings,
        *,
        api: _TencentWriterApi | None = None,
    ) -> None:
        self._settings = settings
        self._api = api or _TencentSdkApi(settings)

    async def create(
        self, secret_ref: SecretReference, secret: SecretWrite
    ) -> SecretReference:
        if not isinstance(secret, SecretWrite):
            raise SecretStoreOperationError("invalid_secret_material")
        name = _secret_name(self._settings, secret_ref)
        value = secret._copy_bytes().decode("utf-8", errors="strict")
        try:
            await asyncio.to_thread(self._api.create_secret, name, value)
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreOperationError("backend_unavailable") from None
        finally:
            value = ""
        return secret_ref


class TencentSecretReader:
    """Worker-side Tencent SSM reader with no writer/controller operations."""

    def __init__(
        self,
        settings: TencentSsmSettings,
        *,
        api: _TencentReaderApi | None = None,
    ) -> None:
        self._settings = settings
        self._api = api or _TencentSdkApi(settings)

    async def resolve_for_worker(
        self, secret_ref: SecretReference
    ) -> WorkerSecret:
        name = _secret_name(self._settings, secret_ref)
        try:
            value = await asyncio.to_thread(
                self._api.get_secret_value,
                name,
            )
            return WorkerSecret(value.encode("utf-8", errors="strict"))
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreOperationError("backend_unavailable") from None
        finally:
            if "value" in locals():
                value = ""


class TencentSecretController:
    """Lifecycle-only Tencent SSM controller with no plaintext read method."""

    def __init__(
        self,
        settings: TencentSsmSettings,
        *,
        api: _TencentControllerApi | None = None,
    ) -> None:
        self._settings = settings
        self._api = api or _TencentSdkApi(settings)

    async def revoke(self, secret_ref: SecretReference) -> None:
        name = _secret_name(self._settings, secret_ref)
        try:
            await asyncio.to_thread(self._api.disable_secret, name)
        except Exception:
            raise SecretStoreOperationError("backend_unavailable") from None

    async def delete_after_retention(
        self, secret_ref: SecretReference
    ) -> None:
        name = _secret_name(self._settings, secret_ref)
        try:
            await asyncio.to_thread(
                self._api.delete_secret,
                name,
                self._settings.recovery_window_days,
            )
        except Exception:
            raise SecretStoreOperationError("backend_unavailable") from None


def tencent_sdk_is_importable() -> bool:
    """Lazy SDK probe only; importability is not deployment verification."""

    try:
        importlib.import_module("tencentcloud.ssm.v20190923.ssm_client")
    except (ImportError, ModuleNotFoundError):
        return False
    return True


class DeploymentEnvironment(str, Enum):
    TEST = "test"
    PRODUCTION = "production"


class SecretBackend(str, Enum):
    MEMORY = "memory"
    TENCENT_SSM = "tencent_ssm"


@dataclass(frozen=True, slots=True)
class SecretStoreSettings:
    environment: DeploymentEnvironment
    backend: SecretBackend | None
    allow_memory_for_tests: bool = False
    tencent_ssm: TencentSsmSettings | None = None


def build_secret_store_ports(
    settings: SecretStoreSettings,
) -> SecretStorePorts:
    """Compose Secret Store roles without an unsafe production fallback."""

    if not isinstance(settings, SecretStoreSettings):
        raise SecretStoreConfigurationError("invalid_settings")
    if not isinstance(settings.environment, DeploymentEnvironment):
        raise SecretStoreConfigurationError("invalid_settings")
    if settings.backend is None:
        raise SecretStoreConfigurationError("backend_required")
    if not isinstance(settings.backend, SecretBackend):
        raise SecretStoreConfigurationError("invalid_settings")

    if settings.backend is SecretBackend.MEMORY:
        if settings.environment is DeploymentEnvironment.PRODUCTION:
            raise SecretStoreConfigurationError(
                "memory_backend_forbidden_in_production"
            )
        if settings.allow_memory_for_tests is not True:
            raise SecretStoreConfigurationError(
                "memory_backend_not_explicitly_test_only"
            )
        return MemorySecretStore.for_testing().ports

    if settings.tencent_ssm is None:
        raise SecretStoreConfigurationError("invalid_settings")
    if not settings.tencent_ssm.deployment_iam_verified:
        raise SecretStoreConfigurationError(
            "tencent_ssm_adapter_unverified"
        )
    return SecretStorePorts(
        writer=TencentSecretWriter(settings.tencent_ssm),
        reader=TencentSecretReader(settings.tencent_ssm),
        controller=TencentSecretController(settings.tencent_ssm),
    )


__all__ = [
    "DeploymentEnvironment",
    "MemorySecretStore",
    "SecretBackend",
    "SecretController",
    "SecretReader",
    "SecretReference",
    "SecretStoreConfigurationError",
    "SecretStoreError",
    "SecretStoreOperationError",
    "SecretStorePorts",
    "SecretStoreSettings",
    "SecretWrite",
    "SecretWriter",
    "TENCENT_HOSTED_MODEL_SECRET_PREFIX",
    "TencentSecretController",
    "TencentSecretReader",
    "TencentSecretWriter",
    "TencentSsmSettings",
    "WorkerSecret",
    "build_secret_store_ports",
    "tencent_sdk_is_importable",
]
