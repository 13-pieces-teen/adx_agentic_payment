"""Provision LiteLLM's official DeepSeek key pool through write-only ingress.

The output manifest contains only opaque Secret Store references and model
aliases. Raw DeepSeek keys and the LiteLLM gateway token are never written to
the manifest, command output, Arena business payloads, or logs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from pydantic import SecretStr

from hosted_agent_runtime.production_secrets import (
    build_production_secret_writer,
    close_secret_port,
    initialize_secret_port,
)
from hosted_agent_runtime.secret_store import (
    SecretReference,
    SecretWrite,
    SecretWriter,
)
from scripts.bootstrap_official_agent_pool import (
    _load_litellm_token,
    _required_environment,
)


_MANIFEST_SCHEMA = "arena.official-litellm-secrets.v1"
_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]{0,5}$")
_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _load_provider_key(path: Path) -> SecretStr:
    descriptor = -1
    try:
        path_status = path.lstat()
        if stat.S_ISLNK(path_status.st_mode):
            raise RuntimeError("DeepSeek key file must not be a symlink")
        if not stat.S_ISREG(path_status.st_mode):
            raise RuntimeError("DeepSeek key path must be a regular file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or not os.path.samestat(path_status, opened_status)
        ):
            raise RuntimeError("DeepSeek key path changed while it was opened")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(16_385)
    except RuntimeError:
        raise
    except OSError:
        raise RuntimeError("DeepSeek key file is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if not raw or len(raw) > 16_384 or b"\x00" in raw:
        raise RuntimeError("DeepSeek key file is invalid")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError("DeepSeek key file must be UTF-8") from None
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("DeepSeek key file must contain one key")
    return SecretStr(value)


def _key_paths_from_source(path: Path) -> tuple[Path, ...]:
    if path.is_symlink():
        raise RuntimeError("DeepSeek key source must not be a symlink")
    resolved = path.resolve(strict=True)
    if resolved.is_file():
        return (resolved,)
    if not resolved.is_dir():
        raise RuntimeError(
            "DeepSeek key source must be a regular file or directory"
        )
    candidates = tuple(
        sorted(
            (
                candidate
                for candidate in resolved.iterdir()
                if candidate.name.endswith(".key")
            ),
            key=lambda candidate: candidate.name,
        )
    )
    if not candidates:
        raise RuntimeError(
            "DeepSeek key directory must contain at least one *.key file"
        )
    if any(
        candidate.is_symlink() or not candidate.is_file()
        for candidate in candidates
    ):
        raise RuntimeError(
            "DeepSeek key entries must be regular, non-symlink files"
        )
    return candidates


def _load_distinct_provider_keys(
    paths: tuple[Path, ...],
) -> tuple[SecretStr, ...]:
    keys: list[SecretStr] = []
    digests: set[bytes] = set()
    for path in paths:
        key = _load_provider_key(path)
        digest = hashlib.sha256(
            key.get_secret_value().encode("utf-8")
        ).digest()
        if digest in digests:
            raise RuntimeError("DeepSeek key files must contain distinct keys")
        digests.add(digest)
        keys.append(key)
    return tuple(keys)


def _secret_reference(
    *,
    config_version: str,
    name: str,
) -> SecretReference:
    return SecretReference(
        "arena402/hosted-model/"
        f"official-litellm-{config_version}-{name}"
    )


async def _write_secret(
    writer: SecretWriter,
    *,
    secret_ref: SecretReference,
    value: SecretStr,
) -> SecretReference:
    secret = SecretWrite.from_text(value.get_secret_value())
    try:
        written = await writer.create(secret_ref, secret)
    finally:
        secret.close()
    if written != secret_ref:
        raise RuntimeError("Official LiteLLM secret reference changed")
    return written


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    resolved_parent = path.parent.resolve(strict=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=resolved_parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def _provision(args: argparse.Namespace) -> dict[str, object]:
    key_paths = _key_paths_from_source(args.deepseek_key_source)
    if len(key_paths) > 100:
        raise RuntimeError("DeepSeek key source cannot contain more than 100 keys")
    upstream_keys = _load_distinct_provider_keys(key_paths)
    gateway_token = _load_litellm_token(args.litellm_token_file)
    control_database_url = _required_environment(
        "ADX_HOSTED_CONTROL_DATABASE_URL"
    )
    secret_writer = build_production_secret_writer(control_database_url)
    try:
        await initialize_secret_port(secret_writer)
        gateway_ref = await _write_secret(
            secret_writer,
            secret_ref=_secret_reference(
                config_version=args.config_version,
                name="gateway",
            ),
            value=gateway_token,
        )

        deployments: list[dict[str, str]] = []
        for index, upstream_key in enumerate(upstream_keys, start=1):
            secret_ref = await _write_secret(
                secret_writer,
                secret_ref=_secret_reference(
                    config_version=args.config_version,
                    name=f"deepseek-{index:03d}",
                ),
                value=upstream_key,
            )
            deployments.append(
                {
                    "alias": f"ARENA_OFFICIAL_DEEPSEEK_{index:03d}",
                    "modelAlias": args.model_alias,
                    "secretRef": secret_ref.value,
                    "upstreamModel": args.upstream_model,
                }
            )

        manifest: dict[str, object] = {
            "schemaVersion": _MANIFEST_SCHEMA,
            "configVersion": args.config_version,
            "gatewaySecretRef": gateway_ref.value,
            "deployments": deployments,
        }
        _write_manifest(args.manifest_file, manifest)
        return {
            "status": "ready",
            "deploymentCount": len(deployments),
            "modelAlias": args.model_alias,
            "upstreamModel": args.upstream_model,
            "manifestFile": str(args.manifest_file),
        }
    finally:
        upstream_keys = tuple(SecretStr("") for _ in upstream_keys)
        gateway_token = SecretStr("")
        await close_secret_port(secret_writer)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Provision LiteLLM's DeepSeek deployments through Arena's "
            "write-only production Secret Store."
        )
    )
    parser.add_argument(
        "--deepseek-key-source",
        type=Path,
        required=True,
        help="One key file or a directory of immediate *.key files.",
    )
    parser.add_argument(
        "--litellm-token-file",
        type=Path,
        required=True,
        help="File containing the internal LiteLLM master token.",
    )
    parser.add_argument("--manifest-file", type=Path, required=True)
    parser.add_argument("--model-alias", default="deepseek-v4-flash")
    parser.add_argument("--upstream-model", default="deepseek-v4-flash")
    parser.add_argument("--config-version", default="v1")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not _VERSION_PATTERN.fullmatch(args.config_version):
        raise SystemExit("--config-version must match v<positive integer>")
    if (
        not _MODEL_PATTERN.fullmatch(args.model_alias)
        or not _MODEL_PATTERN.fullmatch(args.upstream_model)
    ):
        raise SystemExit("model aliases contain invalid characters")
    try:
        result = asyncio.run(_provision(args))
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
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
