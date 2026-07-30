from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

import scripts.provision_official_litellm as provision
from scripts.run_official_litellm import (
    _build_litellm_config,
    _child_environment,
    _load_manifest,
)


def _valid_manifest() -> dict[str, object]:
    return {
        "schemaVersion": "arena.official-litellm-secrets.v1",
        "configVersion": "v1",
        "gatewaySecretRef": "arena402/hosted-model/litellm-gateway",
        "deployments": [
            {
                "alias": "ARENA_OFFICIAL_DEEPSEEK_001",
                "modelAlias": "deepseek-v4-flash",
                "secretRef": "arena402/hosted-model/deepseek-001",
                "upstreamModel": "deepseek-chat",
            },
            {
                "alias": "ARENA_OFFICIAL_DEEPSEEK_002",
                "modelAlias": "deepseek-v4-flash",
                "secretRef": "arena402/hosted-model/deepseek-002",
                "upstreamModel": "deepseek-chat",
            },
        ],
    }


def test_key_directory_is_sorted_by_filename(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    (key_dir / "20-secondary.key").write_text(
        "sk-secondary\n",
        encoding="utf-8",
    )
    (key_dir / "10-primary.key").write_text(
        "sk-primary\n",
        encoding="utf-8",
    )
    (key_dir / "README.txt").write_text("ignored", encoding="utf-8")

    paths = provision._key_paths_from_source(key_dir)
    keys = provision._load_distinct_provider_keys(paths)

    assert [path.name for path in paths] == [
        "10-primary.key",
        "20-secondary.key",
    ]
    assert [key.get_secret_value() for key in keys] == [
        "sk-primary",
        "sk-secondary",
    ]


def test_provider_key_source_rejects_duplicate_values(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    (key_dir / "01.key").write_text("sk-duplicate", encoding="utf-8")
    (key_dir / "02.key").write_text("sk-duplicate\n", encoding="utf-8")

    paths = provision._key_paths_from_source(key_dir)

    with pytest.raises(RuntimeError, match="distinct"):
        provision._load_distinct_provider_keys(paths)


def test_provider_key_source_rejects_empty_material_and_directory(
    tmp_path: Path,
) -> None:
    empty_key_dir = tmp_path / "empty-key"
    empty_key_dir.mkdir()
    (empty_key_dir / "01.key").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid"):
        provision._load_distinct_provider_keys(
            provision._key_paths_from_source(empty_key_dir)
        )

    empty_dir = tmp_path / "empty-directory"
    empty_dir.mkdir()
    with pytest.raises(RuntimeError, match=r"at least one \*\.key"):
        provision._key_paths_from_source(empty_dir)


def test_provider_key_source_rejects_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.key"
    source.write_text("sk-source", encoding="utf-8")
    path_type = type(source)
    original_is_symlink = path_type.is_symlink
    monkeypatch.setattr(
        path_type,
        "is_symlink",
        lambda self: self == source or original_is_symlink(self),
    )

    with pytest.raises(RuntimeError, match="source must not be a symlink"):
        provision._key_paths_from_source(source)


def test_provider_key_directory_rejects_symlink_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    linked_entry = key_dir / "01.key"
    linked_entry.write_text("sk-linked", encoding="utf-8")
    path_type = type(linked_entry)
    original_is_symlink = path_type.is_symlink
    monkeypatch.setattr(
        path_type,
        "is_symlink",
        lambda self: self == linked_entry or original_is_symlink(self),
    )

    with pytest.raises(RuntimeError, match="non-symlink files"):
        provision._key_paths_from_source(key_dir)


def test_provisioned_manifest_never_contains_raw_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_secrets = ("sk-upstream-first", "sk-upstream-second")
    gateway_secret = "sk-litellm-gateway"
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    (key_dir / "02.key").write_text(
        upstream_secrets[1],
        encoding="utf-8",
    )
    (key_dir / "01.key").write_text(
        upstream_secrets[0],
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"

    expected_values = iter(
        (gateway_secret, upstream_secrets[0], upstream_secrets[1])
    )
    written_refs: list[str] = []

    class _SecretWriter:
        async def create(self, secret_ref: object, secret: object) -> object:
            assert getattr(secret_ref, "value") not in written_refs
            assert (
                secret._copy_bytes().decode("utf-8")  # type: ignore[attr-defined]
                == next(expected_values)
            )
            written_refs.append(getattr(secret_ref, "value"))
            return secret_ref

    secret_writer = _SecretWriter()

    async def _noop(_: object) -> None:
        return None

    monkeypatch.setattr(
        provision,
        "_required_environment",
        lambda name: {
            "ADX_HOSTED_CONTROL_DATABASE_URL": "postgresql://control",
        }[name],
    )
    monkeypatch.setattr(
        provision,
        "_load_litellm_token",
        lambda _: SecretStr(gateway_secret),
    )
    monkeypatch.setattr(
        provision,
        "build_production_secret_writer",
        lambda _: secret_writer,
    )
    monkeypatch.setattr(provision, "initialize_secret_port", _noop)
    monkeypatch.setattr(provision, "close_secret_port", _noop)

    result = asyncio.run(
        provision._provision(
            argparse.Namespace(
                deepseek_key_source=key_dir,
                litellm_token_file=tmp_path / "gateway.key",
                manifest_file=manifest_path,
                model_alias="deepseek-v4-flash",
                upstream_model="deepseek-chat",
                config_version="v1",
            )
        )
    )

    serialized_manifest = manifest_path.read_text(encoding="utf-8")
    assert result["deploymentCount"] == 2
    assert gateway_secret not in serialized_manifest
    assert all(
        upstream_secret not in serialized_manifest
        for upstream_secret in upstream_secrets
    )
    assert json.loads(serialized_manifest) == {
        "configVersion": "v1",
        "deployments": [
            {
                "alias": "ARENA_OFFICIAL_DEEPSEEK_001",
                "modelAlias": "deepseek-v4-flash",
                "secretRef": (
                    "arena402/hosted-model/"
                    "official-litellm-v1-deepseek-001"
                ),
                "upstreamModel": "deepseek-chat",
            },
            {
                "alias": "ARENA_OFFICIAL_DEEPSEEK_002",
                "modelAlias": "deepseek-v4-flash",
                "secretRef": (
                    "arena402/hosted-model/"
                    "official-litellm-v1-deepseek-002"
                ),
                "upstreamModel": "deepseek-chat",
            },
        ],
        "gatewaySecretRef": (
            "arena402/hosted-model/official-litellm-v1-gateway"
        ),
        "schemaVersion": "arena.official-litellm-secrets.v1",
    }


def test_load_manifest_accepts_only_the_strict_reference_contract(
    tmp_path: Path,
) -> None:
    valid = _valid_manifest()
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(valid), encoding="utf-8")

    assert _load_manifest(valid_path) == valid

    invalid_payloads: list[dict[str, object]] = []

    payload = copy.deepcopy(valid)
    payload["apiKey"] = "sk-raw-key"
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    deployments = payload["deployments"]
    assert isinstance(deployments, list)
    deployments[0]["apiKey"] = "sk-raw-key"
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    payload["schemaVersion"] = "arena.official-litellm-secrets.v2"
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    payload["deployments"] = []
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    deployments = payload["deployments"]
    assert isinstance(deployments, list)
    deployments[1]["alias"] = deployments[0]["alias"]
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    deployments = payload["deployments"]
    assert isinstance(deployments, list)
    deployments[1]["secretRef"] = deployments[0]["secretRef"]
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    deployments = payload["deployments"]
    assert isinstance(deployments, list)
    deployments[0]["alias"] = "DEEPSEEK_001"
    invalid_payloads.append(payload)

    payload = copy.deepcopy(valid)
    deployments = payload["deployments"]
    assert isinstance(deployments, list)
    deployments[0]["secretRef"] = "x"
    invalid_payloads.append(payload)

    for index, invalid in enumerate(invalid_payloads):
        invalid_path = tmp_path / f"invalid-{index}.json"
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(RuntimeError):
            _load_manifest(invalid_path)


def test_build_litellm_config_load_balances_same_alias_without_retries() -> None:
    config = _build_litellm_config(_valid_manifest())

    model_list = config["model_list"]
    assert isinstance(model_list, list)
    assert [model["model_name"] for model in model_list] == [
        "deepseek-v4-flash",
        "deepseek-v4-flash",
    ]
    assert [
        model["litellm_params"]["api_key"] for model in model_list
    ] == [
        "os.environ/ARENA_OFFICIAL_DEEPSEEK_001",
        "os.environ/ARENA_OFFICIAL_DEEPSEEK_002",
    ]
    assert [
        model["litellm_params"]["model"] for model in model_list
    ] == [
        "deepseek/deepseek-chat",
        "deepseek/deepseek-chat",
    ]
    assert config["router_settings"] == {
        "routing_strategy": "simple-shuffle",
        "num_retries": 0,
    }
    assert config["litellm_settings"] == {
        "num_retries": 0,
        "fallbacks": [],
        "context_window_fallbacks": [],
        "set_verbose": False,
    }
    assert config["general_settings"] == {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "health_check_details": False,
    }


def test_child_environment_drops_arena_database_and_vault_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_environment = {
        "ADX_HOSTED_CONTROL_DATABASE_URL": "postgresql://control",
        "ADX_LITELLM_SECRET_DATABASE_URL": "postgresql://secrets",
        "ADX_HOSTED_MASTER_KEY_FILE": "C:/secrets/master.key",
        "ADX_TENCENT_SSM_SECRET_ID": "secret-id",
        "DATABASE_URL": "postgresql://default",
        "POSTGRES_URL": "postgresql://postgres",
        "VAULT_ADDR": "https://vault.internal",
        "VAULT_TOKEN": "vault-token",
        "UNRELATED_APPLICATION_SECRET": "must-not-pass",
    }
    for name, value in blocked_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "C:/runtime/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    secrets = {
        "LITELLM_MASTER_KEY": "sk-litellm-gateway",
        "ARENA_OFFICIAL_DEEPSEEK_001": "sk-upstream",
    }

    child = _child_environment(secrets)

    assert child["PATH"] == "C:/runtime/bin"
    assert child["LANG"] == "C.UTF-8"
    assert child["LITELLM_MASTER_KEY"] == "sk-litellm-gateway"
    assert child["ARENA_OFFICIAL_DEEPSEEK_001"] == "sk-upstream"
    assert not set(blocked_environment).intersection(child)
