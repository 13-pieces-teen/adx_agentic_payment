"""Static safety contracts for the production CI/CD release path."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (
    ROOT / ".github" / "workflows" / "ci-cd.yml"
).read_text(encoding="utf-8")
RELEASE = (
    ROOT / "deploy" / "scripts" / "release.sh"
).read_text(encoding="utf-8")
GENERATE_ENV = (
    ROOT / "deploy" / "scripts" / "generate-env.sh"
).read_text(encoding="utf-8")
BUILD_CONNECTOR_ARTIFACTS = (
    ROOT / "deploy" / "scripts" / "build-connector-artifacts.sh"
).read_text(encoding="utf-8")


def test_workflow_deploys_only_main_after_all_ci_gates() -> None:
    assert "github.ref == 'refs/heads/main'" in WORKFLOW
    assert "needs:\n      - python\n      - connector\n      - settlement" in WORKFLOW
    assert "production-images" in WORKFLOW
    assert "group: arena402-production" in WORKFLOW
    assert "cancel-in-progress: false" in WORKFLOW
    assert "environment:\n      name: production" in WORKFLOW
    assert "Run contract tests against an isolated Hardhat node" in WORKFLOW
    assert '"method":"eth_chainId"' in WORKFLOW


def test_workflow_packages_the_exact_revision_without_runtime_secrets() -> None:
    assert 'git archive --format=tar --output="${archive}" "${GITHUB_SHA}"' in WORKFLOW
    assert 'test "$(git rev-parse HEAD)" = "${GITHUB_SHA}"' in WORKFLOW
    assert "sha256sum" in WORKFLOW
    assert "StrictHostKeyChecking=yes" in WORKFLOW
    assert "PROD_SSH_KNOWN_HOSTS" in WORKFLOW
    assert "deploy/.env" not in WORKFLOW.split(
        "- name: Create the exact source archive", 1
    )[1].split("- name: Configure pinned SSH trust", 1)[0]
    for forbidden in (
        "ADX_WALLET_MASTER",
        "ADX_X402_FACILITATOR_1_BEARER_TOKEN",
        "TENCENTCLOUD_SECRET_KEY",
        "ssh-keyscan",
    ):
        assert forbidden not in WORKFLOW


def test_release_verifies_identity_before_activation_and_marks_after_health() -> None:
    checksum_index = RELEASE.index(
        'actual_checksum="$(sha256sum "${archive}"'
    )
    backup_index = RELEASE.index("sh deploy/scripts/backup.sh")
    activation_index = RELEASE.index(
        'mv -- "${release_dir}" "${rollback_dir}"'
    )
    deploy_index = RELEASE.index("sh deploy/scripts/deploy.sh")
    health_index = RELEASE.index('health_url="${public_api_url%/}/api/health"')
    marker_index = RELEASE.index(
        'write_marker "${release_dir}/DEPLOYED_GIT_SHA"'
    )
    assert checksum_index < backup_index < activation_index < deploy_index
    assert deploy_index < health_index < marker_index


def test_release_preserves_server_only_state_and_refuses_automatic_db_rollback() -> None:
    assert 'cp -p -- "${release_dir}/deploy/.env"' in RELEASE
    assert "for runtime_dir in artifacts secrets official-litellm" in RELEASE
    assert '"${release_dir}/deploy/${runtime_dir}"' in RELEASE
    assert '"${release_dir}/${runtime_dir}"' in RELEASE
    assert '"${staging_dir}/${runtime_dir}/"' in RELEASE
    assert "Automatic rollback is disabled because migrations may have started." in RELEASE
    assert "compose run --rm migrate" in RELEASE
    assert 'build-connector-artifacts.sh" --verify-only' in RELEASE
    assert "END { exit 0 }" not in RELEASE
    assert 'compose_file="${repo_dir}/docker-compose.production.yml"' in RELEASE


def test_release_allows_only_the_tracked_environment_templates() -> None:
    for template in (
        '".env.example"',
        '"agent-arena/settlement/.env.example"',
    ):
        assert template in RELEASE
    assert "/(^|\\/)\\.env($|\\.)/" in RELEASE
    assert "/\\.pem$/ || /\\.key$/" in RELEASE


def test_release_checks_runtime_and_public_boundaries() -> None:
    for evidence in (
        "require_running_service",
        "/api/health",
        "/api/connectors/devices",
        "expected 401",
        "/api/v1/games/current",
        "current_game_not_found",
        "text/event-stream",
        "DEPLOYED_ARCHIVE_SHA256",
    ):
        assert evidence in RELEASE
    assert 'body.get("gameId")' in RELEASE
    assert 'game.get("gameId")' in RELEASE
    assert '-C\n            -o BatchMode=yes' in WORKFLOW
    assert (
        "require_running_service official-agents official-litellm"
        in RELEASE
    )


def test_release_applies_the_frozen_current_game_round_count() -> None:
    assert "PROD_CURRENT_GAME_ROUND_COUNT" in WORKFLOW
    assert "--current-game-round-count" in WORKFLOW
    assert "--current-game-round-count" in RELEASE
    assert 'set_env_value ADX_CURRENT_GAME_ROUND_COUNT "${current_game_round_count}"' in (
        RELEASE
    )
    preserve_index = RELEASE.index(
        'cp -p -- "${release_dir}/deploy/.env"'
    )
    configure_index = RELEASE.index(
        'set_env_value ADX_CURRENT_GAME_ROUND_COUNT'
    )
    backup_index = RELEASE.index("sh deploy/scripts/backup.sh")
    assert preserve_index < configure_index < backup_index


def test_release_refreshes_official_strategies_after_runtime_deploy() -> None:
    assert "--refresh-official-strategies" in WORKFLOW
    assert "--refresh-official-strategies" in RELEASE
    deploy_index = RELEASE.index("sh deploy/scripts/deploy.sh")
    refresh_index = RELEASE.index("official-agent-strategy-refresh")
    health_index = RELEASE.index('health_url="${public_api_url%/}/api/health"')
    assert deploy_index < refresh_index < health_index


def test_generated_environment_keeps_public_app_and_api_urls_separate() -> None:
    assert 'public_api_url="https://${public_host}"' in GENERATE_ENV
    assert 'public_url="${public_api_url}"' in GENERATE_ENV
    assert "printf 'ADX_PUBLIC_APP_URL=%s\\n' \"${public_url}\"" in GENERATE_ENV
    assert "printf 'ADX_PUBLIC_API_URL=%s\\n' \"${public_api_url}\"" in GENERATE_ENV


def test_docker_connector_build_preserves_the_final_artifact_name() -> None:
    docker_builder = BUILD_CONNECTOR_ARTIFACTS.split(
        "build_in_docker() {", 1
    )[1].split("\n}", 1)[0]
    assert 'docker_output_name="$3"' in docker_builder
    assert '-o "/out/${docker_output_name}"' in docker_builder
    assert '\n  output_name="$3"\n' not in docker_builder
    assert (
        'printf \'%s  %s\\n\' "${checksum}" "${output_name}"'
        in BUILD_CONNECTOR_ARTIFACTS
    )
