"""Static safety contracts for the Phase 4 HTTP idempotency migration."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "db" / "migrations" / "004_hosted_agent_api.sql"


def test_hosted_api_migration_is_digest_only_and_strictly_bounded():
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE arena_api_idempotency" in sql
    for column in (
        "owner_user_id TEXT NOT NULL",
        "route_key TEXT NOT NULL",
        "key_digest TEXT NOT NULL",
        "request_digest TEXT NOT NULL",
        "state TEXT NOT NULL",
        "resource_kind TEXT",
        "resource_id TEXT",
        "safe_response JSONB",
        "created_at TIMESTAMPTZ NOT NULL",
        "expires_at TIMESTAMPTZ NOT NULL",
    ):
        assert column in sql

    assert sql.count("'^sha256:[0-9a-f]{64}$'") >= 4
    assert "p_idempotency_key" not in sql
    assert "p_api_key" not in sql
    assert "p_secret" not in sql
    assert "p_safe_response" not in sql
    assert "BETWEEN 60 AND 86400" in sql
    assert "v_active_count >= 256" in sql
    assert "arena_api_idempotency_expiry_idx" in sql
    assert "arena-api-response.v1" in sql
    assert "safe_response\n                - 'httpStatus'" in sql


def test_hosted_api_functions_serialize_and_close_replay_states():
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "FUNCTION reserve_arena_api_idempotency(" in sql
    assert "FUNCTION attach_arena_api_idempotency_resource(" in sql
    assert "FUNCTION release_arena_api_idempotency_for_retry(" in sql
    assert "FUNCTION complete_arena_api_idempotency(" in sql
    assert sql.count("\nSECURITY DEFINER") == 4
    assert sql.count("SET search_path = pg_catalog, public") == 4
    assert sql.count("pg_catalog.pg_advisory_xact_lock(") == 4
    assert sql.count("FOR UPDATE;") == 4

    for disposition in (
        "attached",
        "reserved",
        "retry",
        "retryable_failure",
        "in_progress",
        "replay",
        "conflict",
        "completed",
        "expired",
        "not_found",
    ):
        assert f"'{disposition}'::TEXT" in sql

    assert "v_record.request_digest <> p_request_digest" in sql
    assert "v_record.expires_at <= v_now" in sql
    assert "v_record.safe_response = v_safe_response" in sql
    assert "jsonb_build_object(" in sql
    assert "v_resource_status = 'pending_write'" in sql
    assert "API idempotency resource is not ready" in sql


def test_hosted_api_role_has_function_only_idempotency_access():
    sql = SQL_PATH.read_text(encoding="utf-8")

    api_grants = re.findall(
        r"GRANT EXECUTE ON FUNCTION [^;]+ TO adx_arena_api;",
        sql,
        flags=re.DOTALL,
    )
    assert len(api_grants) == 4
    assert not re.search(
        r"GRANT\s+(?:SELECT|INSERT|UPDATE|DELETE)[^;]*"
        r"ON arena_api_idempotency[^;]*TO adx_arena_api;",
        sql,
        flags=re.DOTALL,
    )
    assert "REVOKE ALL ON arena_api_idempotency FROM\n    adx_arena_api" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON arena_api_idempotency\n" in sql
    assert "TO adx_arena_function_owner;" in sql


def test_hosted_api_completion_references_owned_business_resources():
    sql = SQL_PATH.read_text(encoding="utf-8")

    assert "credential.owner_user_id = p_owner_user_id" in sql
    assert "agent.owner_user_id = p_owner_user_id" in sql
    assert "participant.user_id = p_owner_user_id" in sql
    assert "p_resource_kind IS DISTINCT FROM v_expected_resource_kind" in sql
    assert "API idempotency resource not found" in sql
