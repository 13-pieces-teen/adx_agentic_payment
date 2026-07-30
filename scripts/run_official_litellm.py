"""Resolve encrypted official secrets, then exec the stock LiteLLM Proxy.

Only the short bootstrap process can reach the Arena credential vault. Before
``exec``, it removes every Arena database/vault setting from the child
environment. LiteLLM receives the gateway and upstream keys only in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Final

import yaml

from hosted_agent_runtime.production_secrets import (
    build_production_secret_reader,
    close_secret_port,
    initialize_secret_port,
)
from hosted_agent_runtime.secret_store import SecretReference


_MANIFEST_SCHEMA: Final[str] = "arena.official-litellm-secrets.v1"
_ALIAS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ARENA_OFFICIAL_DEEPSEEK_[0-9]{3}$"
)
_MODEL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,127}$"
)
_PASSTHROUGH_ENV: Final[frozenset[str]] = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LITELLM_NON_ROOT",
        "PATH",
        "PRISMA_BINARY_CACHE_DIR",
        "PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING",
        "PRISMA_HIDE_UPDATE_MESSAGE",
        "PRISMA_OFFLINE_MODE",
        "PRISMA_SKIP_POSTINSTALL_GENERATE",
        "SSL_CERT_FILE",
        "TZ",
        "XDG_CACHE_HOME",
    }
)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("official LiteLLM manifest is unavailable") from None
    if not isinstance(payload, dict) or set(payload) != {
        "configVersion",
        "deployments",
        "gatewaySecretRef",
        "schemaVersion",
    }:
        raise RuntimeError("official LiteLLM manifest is invalid")
    if payload["schemaVersion"] != _MANIFEST_SCHEMA:
        raise RuntimeError("official LiteLLM manifest schema is unsupported")
    if not isinstance(payload["configVersion"], str):
        raise RuntimeError("official LiteLLM manifest is invalid")
    SecretReference(payload["gatewaySecretRef"])
    deployments = payload["deployments"]
    if (
        not isinstance(deployments, list)
        or not deployments
        or len(deployments) > 100
    ):
        raise RuntimeError("official LiteLLM manifest has no deployments")

    aliases: set[str] = set()
    refs: set[str] = set()
    for deployment in deployments:
        if not isinstance(deployment, dict) or set(deployment) != {
            "alias",
            "modelAlias",
            "secretRef",
            "upstreamModel",
        }:
            raise RuntimeError("official LiteLLM deployment is invalid")
        alias = deployment["alias"]
        model_alias = deployment["modelAlias"]
        upstream_model = deployment["upstreamModel"]
        secret_ref = deployment["secretRef"]
        if (
            not isinstance(alias, str)
            or not _ALIAS_PATTERN.fullmatch(alias)
            or not isinstance(model_alias, str)
            or not _MODEL_PATTERN.fullmatch(model_alias)
            or not isinstance(upstream_model, str)
            or not _MODEL_PATTERN.fullmatch(upstream_model)
            or not isinstance(secret_ref, str)
        ):
            raise RuntimeError("official LiteLLM deployment is invalid")
        SecretReference(secret_ref)
        if alias in aliases or secret_ref in refs:
            raise RuntimeError("official LiteLLM deployments must be unique")
        aliases.add(alias)
        refs.add(secret_ref)
    return payload


def _build_litellm_config(
    manifest: dict[str, object],
) -> dict[str, object]:
    deployments = manifest["deployments"]
    assert isinstance(deployments, list)
    model_list = []
    for deployment in deployments:
        assert isinstance(deployment, dict)
        alias = str(deployment["alias"])
        model_list.append(
            {
                "model_name": str(deployment["modelAlias"]),
                "litellm_params": {
                    "model": f"deepseek/{deployment['upstreamModel']}",
                    "api_key": f"os.environ/{alias}",
                },
                "model_info": {
                    "id": alias.lower().replace("_", "-"),
                    "mode": "chat",
                },
            }
        )
    return {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "num_retries": 0,
        },
        "litellm_settings": {
            "num_retries": 0,
            "fallbacks": [],
            "context_window_fallbacks": [],
            "set_verbose": False,
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "health_check_details": False,
        },
    }


async def _resolve_secrets(
    manifest: dict[str, object],
) -> dict[str, str]:
    database_url = os.getenv(
        "ADX_LITELLM_SECRET_DATABASE_URL",
        "",
    ).strip()
    if not database_url:
        raise RuntimeError("ADX_LITELLM_SECRET_DATABASE_URL is required")
    reader = build_production_secret_reader(database_url)
    resolved: dict[str, str] = {}
    try:
        await initialize_secret_port(reader)
        gateway = await reader.resolve_for_worker(
            SecretReference(str(manifest["gatewaySecretRef"]))
        )
        try:
            resolved["LITELLM_MASTER_KEY"] = gateway.reveal_for_worker()
        finally:
            gateway.close()

        deployments = manifest["deployments"]
        assert isinstance(deployments, list)
        for deployment in deployments:
            assert isinstance(deployment, dict)
            secret = await reader.resolve_for_worker(
                SecretReference(str(deployment["secretRef"]))
            )
            try:
                resolved[str(deployment["alias"])] = (
                    secret.reveal_for_worker()
                )
            finally:
                secret.close()
    finally:
        await close_secret_port(reader)
    return resolved


def _write_runtime_config(config: dict[str, object]) -> Path:
    runtime_dir = Path(
        os.getenv("ADX_LITELLM_RUNTIME_DIR", "/tmp/arena-official-litellm")
    )
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix="config-",
        suffix=".yaml",
        dir=runtime_dir,
        text=True,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(
                config,
                stream,
                allow_unicode=False,
                default_flow_style=False,
                sort_keys=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _child_environment(secrets: dict[str, str]) -> dict[str, str]:
    child = {
        name: value
        for name, value in os.environ.items()
        if name in _PASSTHROUGH_ENV
    }
    child.update(secrets)
    return child


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest-file",
        type=Path,
        default=Path("/run/official-litellm/manifest.json"),
    )
    parser.add_argument("--port", type=int, default=4000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")
    try:
        manifest = _load_manifest(args.manifest_file)
        config = _build_litellm_config(manifest)
        secrets = asyncio.run(_resolve_secrets(manifest))
        config_path = _write_runtime_config(config)
        child_environment = _child_environment(secrets)
    except Exception as exc:
        safe_code = getattr(exc, "code", exc.__class__.__name__)
        print(
            json.dumps(
                {"status": "failed", "error": str(safe_code)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    os.execvpe(
        "litellm",
        [
            "litellm",
            "--config",
            str(config_path),
            "--host",
            "0.0.0.0",
            "--port",
            str(args.port),
        ],
        child_environment,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
