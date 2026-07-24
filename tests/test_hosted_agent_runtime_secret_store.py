"""Security-boundary tests for Hosted Agent BYOK secret storage."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any, Coroutine, TypeVar

import pytest

from hosted_agent_runtime.secret_store import (
    DeploymentEnvironment,
    MemorySecretStore,
    SecretBackend,
    SecretReference,
    SecretStoreConfigurationError,
    SecretStoreOperationError,
    SecretStoreSettings,
    SecretWrite,
    TencentSecretController,
    TencentSecretReader,
    TencentSecretWriter,
    TencentSsmSettings,
    build_secret_store_ports,
    tencent_sdk_is_importable,
)


_T = TypeVar("_T")


def _run(coroutine: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coroutine)


def test_memory_store_is_explicitly_test_only() -> None:
    with pytest.raises(SecretStoreConfigurationError) as exc:
        MemorySecretStore()
    assert exc.value.code == "memory_backend_not_explicitly_test_only"

    with pytest.raises(SecretStoreConfigurationError) as exc:
        build_secret_store_ports(
            SecretStoreSettings(
                environment=DeploymentEnvironment.TEST,
                backend=SecretBackend.MEMORY,
            )
        )
    assert exc.value.code == "memory_backend_not_explicitly_test_only"

    ports = build_secret_store_ports(
        SecretStoreSettings(
            environment=DeploymentEnvironment.TEST,
            backend=SecretBackend.MEMORY,
            allow_memory_for_tests=True,
        )
    )
    assert ports.writer is not ports.reader
    assert ports.reader is not ports.controller


@pytest.mark.parametrize("backend", [None, SecretBackend.MEMORY])
def test_production_secret_store_fails_closed(
    backend: SecretBackend | None,
) -> None:
    with pytest.raises(SecretStoreConfigurationError) as exc:
        build_secret_store_ports(
            SecretStoreSettings(
                environment=DeploymentEnvironment.PRODUCTION,
                backend=backend,
                allow_memory_for_tests=True,
            )
        )
    expected = (
        "backend_required"
        if backend is None
        else "memory_backend_forbidden_in_production"
    )
    assert exc.value.code == expected


def test_role_ports_expose_only_their_authorized_operation() -> None:
    ports = MemorySecretStore.for_testing().ports

    assert hasattr(ports.writer, "create")
    assert not hasattr(ports.writer, "resolve_for_worker")
    assert not hasattr(ports.writer, "revoke")
    assert not hasattr(ports.writer, "delete_after_retention")

    assert hasattr(ports.reader, "resolve_for_worker")
    assert not hasattr(ports.reader, "create")
    assert not hasattr(ports.reader, "revoke")
    assert not hasattr(ports.reader, "delete_after_retention")

    assert hasattr(ports.controller, "revoke")
    assert hasattr(ports.controller, "delete_after_retention")
    assert not hasattr(ports.controller, "create")
    assert not hasattr(ports.controller, "resolve_for_worker")


def test_write_once_returns_only_reference_and_worker_handle_is_redacted() -> None:
    raw_secret = "provider-key-super-sensitive-123"
    secret_ref = SecretReference("memory/hosted-model/credential-001")
    ports = MemorySecretStore.for_testing().ports

    with SecretWrite.from_text(raw_secret) as write:
        result = _run(ports.writer.create(secret_ref, write))
        assert result == secret_ref
        assert raw_secret not in repr(write)
        assert raw_secret not in str(write)

    worker_secret = _run(ports.reader.resolve_for_worker(secret_ref))
    assert raw_secret not in repr(worker_secret)
    assert raw_secret not in str(worker_secret)
    assert worker_secret.reveal_for_worker() == raw_secret
    worker_secret.close()

    with pytest.raises(SecretStoreOperationError) as exc:
        worker_secret.reveal_for_worker()
    assert exc.value.code == "secret_value_closed"
    assert raw_secret not in str(exc.value)


def test_secret_write_hmac_does_not_expose_plaintext_and_honors_close() -> None:
    raw_secret = "provider-key-hmac-sensitive-123"
    pepper = b"p" * 32
    write = SecretWrite.from_text(raw_secret)
    try:
        assert write.hmac_sha256(pepper) == hmac.new(
            pepper,
            raw_secret.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert raw_secret not in repr(write)
    finally:
        write.close()

    with pytest.raises(SecretStoreOperationError) as exc:
        write.hmac_sha256(pepper)
    assert exc.value.code == "secret_value_closed"
    assert raw_secret not in str(exc.value)


def test_duplicate_write_revoke_and_delete_lifecycle_is_ordered() -> None:
    raw_secret = "provider-key-never-in-errors"
    secret_ref = SecretReference("memory/hosted-model/credential-002")
    ports = MemorySecretStore.for_testing().ports

    with SecretWrite.from_text(raw_secret) as write:
        _run(ports.writer.create(secret_ref, write))

    with SecretWrite.from_text("replacement-must-not-win") as duplicate:
        with pytest.raises(SecretStoreOperationError) as exc:
            _run(ports.writer.create(secret_ref, duplicate))
    assert exc.value.code == "secret_already_exists"
    assert raw_secret not in str(exc.value)
    assert "replacement-must-not-win" not in str(exc.value)

    with pytest.raises(SecretStoreOperationError) as exc:
        _run(ports.controller.delete_after_retention(secret_ref))
    assert exc.value.code == "secret_must_be_revoked"
    assert raw_secret not in str(exc.value)

    still_active = _run(ports.reader.resolve_for_worker(secret_ref))
    try:
        assert still_active.reveal_for_worker() == raw_secret
    finally:
        still_active.close()

    _run(ports.controller.revoke(secret_ref))
    _run(ports.controller.revoke(secret_ref))
    with pytest.raises(SecretStoreOperationError) as exc:
        _run(ports.reader.resolve_for_worker(secret_ref))
    assert exc.value.code == "secret_revoked"
    assert raw_secret not in str(exc.value)

    _run(ports.controller.delete_after_retention(secret_ref))
    with pytest.raises(SecretStoreOperationError) as exc:
        _run(ports.reader.resolve_for_worker(secret_ref))
    assert exc.value.code == "secret_not_found"
    assert raw_secret not in str(exc.value)


def test_tencent_ssm_roles_use_safe_name_and_keep_plaintext_reader_only() -> None:
    raw_secret = "provider-key-sent-only-to-secret-manager"
    settings = TencentSsmSettings(region="ap-guangzhou")
    secret_ref = SecretReference(
        "arena402/hosted-model/credential-003"
    )
    events: list[tuple[object, ...]] = []

    class WriterApi:
        def create_secret(self, name: str, value: str) -> None:
            events.append(("create", name, value))

    class ReaderApi:
        def get_secret_value(self, name: str) -> str:
            events.append(("read", name))
            return raw_secret

    class ControllerApi:
        def disable_secret(self, name: str) -> None:
            events.append(("disable", name))

        def delete_secret(self, name: str, recovery_window_days: int) -> None:
            events.append(("delete", name, recovery_window_days))

    writer = TencentSecretWriter(settings, api=WriterApi())
    reader = TencentSecretReader(settings, api=ReaderApi())
    controller = TencentSecretController(settings, api=ControllerApi())

    with SecretWrite.from_text(raw_secret) as write:
        assert _run(writer.create(secret_ref, write)) == secret_ref
    resolved = _run(reader.resolve_for_worker(secret_ref))
    try:
        assert resolved.reveal_for_worker() == raw_secret
    finally:
        resolved.close()
    _run(controller.revoke(secret_ref))
    _run(controller.delete_after_retention(secret_ref))

    expected_name = "arena402_hosted_model_credential_003"
    assert events == [
        ("create", expected_name, raw_secret),
        ("read", expected_name),
        ("disable", expected_name),
        ("delete", expected_name, 0),
    ]
    assert not hasattr(writer, "resolve_for_worker")
    assert not hasattr(reader, "create")
    assert not hasattr(controller, "resolve_for_worker")

    with pytest.raises(SecretStoreConfigurationError) as exc:
        build_secret_store_ports(
            SecretStoreSettings(
                environment=DeploymentEnvironment.PRODUCTION,
                backend=SecretBackend.TENCENT_SSM,
                tencent_ssm=settings,
            )
        )
    assert exc.value.code == "tencent_ssm_adapter_unverified"


def test_tencent_sdk_probe_is_lazy_and_reports_importability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def missing_sdk(module_name: str) -> None:
        calls.append(module_name)
        raise ModuleNotFoundError(module_name)

    monkeypatch.setattr(
        "hosted_agent_runtime.secret_store.importlib.import_module",
        missing_sdk,
    )
    assert tencent_sdk_is_importable() is False
    assert calls == ["tencentcloud.ssm.v20190923.ssm_client"]


@pytest.mark.parametrize(
    ("region", "prefix"),
    [
        ("", "arena402/hosted-model/"),
        ("AP-GUANGZHOU", "arena402/hosted-model/"),
        ("ap-guangzhou", "another-product/"),
        ("ap-guangzhou", "arena402/hosted-model/../"),
    ],
)
def test_tencent_settings_reject_unsafe_scope(
    region: str,
    prefix: str,
) -> None:
    with pytest.raises(SecretStoreConfigurationError) as exc:
        TencentSsmSettings(region=region, secret_prefix=prefix)
    assert exc.value.code == "invalid_settings"
