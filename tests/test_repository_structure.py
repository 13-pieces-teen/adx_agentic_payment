from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests" / "e2e"


def test_manual_e2e_assets_have_one_home() -> None:
    expected = {
        "connector_go_e2e.py",
        "connector_ui_smoke.py",
        "hosted_worker_process_recovery_e2e.py",
        "mcp_docker_e2e.py",
        "mixed_codex_fallback_docker_e2e.py",
        "real_runtimes_docker_e2e.py",
        "docker-compose.mcp-e2e.yml",
        "docker-compose.real-runtimes-e2e.yml",
    }

    assert expected <= {path.name for path in E2E.iterdir()}
    assert not any((ROOT / "tests" / name).exists() for name in expected)
    pytest_config = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "tests/e2e" in pytest_config


def test_repository_map_covers_maintained_top_level_packages() -> None:
    repository_map = (ROOT / "docs" / "repository-structure.md").read_text(
        encoding="utf-8"
    )
    maintained_packages = {
        "arena_agent_contracts",
        "arena_core",
        "arena_game",
        "arena_mcp",
        "arena_memorial",
        "arena_payments",
        "arena_wallets",
        "connector_gateway",
        "hosted_agent_control_plane",
        "hosted_agent_runtime",
        "web",
    }

    missing = sorted(
        package
        for package in maintained_packages
        if (ROOT / package / "__init__.py").is_file()
        and f"`{package}/`" not in repository_map
    )
    assert missing == []
