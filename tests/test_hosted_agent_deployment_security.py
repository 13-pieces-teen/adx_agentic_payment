"""Static production logging boundaries for secret-bearing Hosted ingress."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_edge_access_logs_never_emit_request_uri_or_query_values() -> None:
    for name in (
        "Caddyfile.domain",
        "Caddyfile.ip",
        "Caddyfile.ip-bootstrap",
    ):
        config = (ROOT / "deploy" / "caddy" / name).read_text(
            encoding="utf-8"
        )
        assert "format filter {" in config
        assert "request>uri replace REDACTED" in config
        assert "wrap json" in config


def test_uvicorn_access_log_is_disabled_behind_redacting_edge() -> None:
    entrypoint = (
        ROOT / "deploy" / "scripts" / "api-entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "--no-access-log" in entrypoint
    assert "\n  --access-log" not in entrypoint
