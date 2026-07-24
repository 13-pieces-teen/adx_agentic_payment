from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hosted_agent_control_plane.credential_controller import (
    CredentialLifecycleJob,
    DurableCredentialController,
)


class _Repository:
    def __init__(self, jobs: list[CredentialLifecycleJob]) -> None:
        self.jobs = jobs
        self.completions: list[dict[str, object]] = []

    async def claim(self, **_: object) -> list[CredentialLifecycleJob]:
        jobs, self.jobs = self.jobs, []
        return jobs

    async def complete(self, **values: object) -> str:
        self.completions.append(values)
        return "succeeded" if values["succeeded"] else "queued"


class _Controller:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.revoked: list[str] = []
        self.deleted: list[str] = []

    async def revoke(self, secret_ref: object) -> None:
        if self.fail:
            raise RuntimeError("backend details must not persist")
        self.revoked.append(str(secret_ref))

    async def delete_after_retention(self, secret_ref: object) -> None:
        if self.fail:
            raise RuntimeError("backend details must not persist")
        self.deleted.append(str(secret_ref))


def _job(kind: str = "revoke") -> CredentialLifecycleJob:
    return CredentialLifecycleJob(
        lifecycle_job_id=f"lifecycle-{kind}",
        credential_id="credential-1",
        job_kind=kind,
        secret_ref="arena402/hosted-model/credential-1",
        attempt_no=1,
        max_attempts=3,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def test_controller_executes_revoke_and_delete_without_secret_read() -> None:
    repository = _Repository([_job("revoke"), _job("delete")])
    secret_controller = _Controller()
    controller = DurableCredentialController(
        repository=repository,
        secret_controller=secret_controller,
        controller_id="controller-test",
    )

    assert asyncio.run(controller.run_once()) == 2
    assert secret_controller.revoked == [
        "arena402/hosted-model/credential-1"
    ]
    assert secret_controller.deleted == [
        "arena402/hosted-model/credential-1"
    ]
    assert all(
        completion["succeeded"] is True
        and completion["error_class"] is None
        and completion["retry_at"] is None
        for completion in repository.completions
    )


def test_controller_persists_only_safe_failure_class_and_retry_time() -> None:
    repository = _Repository([_job()])
    controller = DurableCredentialController(
        repository=repository,
        secret_controller=_Controller(fail=True),
        controller_id="controller-test",
        retry_seconds=1,
    )

    assert asyncio.run(controller.run_once()) == 1
    completion = repository.completions[0]
    assert completion["succeeded"] is False
    assert completion["error_class"] == "credential_controller_unavailable"
    assert isinstance(completion["retry_at"], datetime)
    assert "backend details" not in str(completion)


def test_controller_rejects_collapsed_secret_store_port() -> None:
    class CollapsedPort(_Controller):
        async def create(self, *_: object) -> None:
            return None

    with pytest.raises(TypeError, match="must not receive"):
        DurableCredentialController(
            repository=_Repository([]),
            secret_controller=CollapsedPort(),
        )
