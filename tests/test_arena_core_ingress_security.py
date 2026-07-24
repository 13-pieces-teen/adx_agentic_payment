import pytest

from arena_core.ingress_security import (
    ArenaIngressSecurityError,
    secure_config_snapshot,
    validate_runtime_controlled_text,
)


def test_secure_config_snapshot_allows_opaque_reference_and_returns_detached_copy():
    source = {
        "provider": "fake",
        "model": "fake-structured-v1",
        "credential_id": "credential_01J123",
        "secret_ref": "qcs::ssm:ap-guangzhou:account:secret/arena-model-key",
        "thinking": {"enabled": True},
        "strategy_instructions": "Prefer preserving cash.",
    }

    secured = secure_config_snapshot(source)
    source["thinking"]["enabled"] = False

    assert secured["credential_id"] == "credential_01J123"
    assert secured["secret_ref"].startswith("qcs::ssm:")
    assert secured["thinking"]["enabled"] is True


@pytest.mark.parametrize(
    "secret_key",
    [
        "api_key",
        "apiKey",
        "provider_api_secret",
        "Authorization",
        "client_secret",
        "password",
        "private_key",
        "access_token",
    ],
)
def test_secure_config_snapshot_rejects_secret_bearing_keys(secret_key):
    with pytest.raises(ArenaIngressSecurityError) as captured:
        secure_config_snapshot({secret_key: None})

    assert captured.value.code == "secret_bearing_key"
    assert "None" not in repr(captured.value)


def test_secure_config_snapshot_rejects_nested_secret_value_without_echoing_it():
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(ArenaIngressSecurityError) as captured:
        secure_config_snapshot(
            {
                "provider": "fake",
                "nested": [{"model": "safe"}, {"value": raw_secret}],
            }
        )

    assert captured.value.code == "secret_or_pii"
    assert raw_secret not in str(captured.value)
    assert raw_secret not in repr(captured.value)


def test_safe_credential_reference_field_cannot_hide_raw_secret():
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(ArenaIngressSecurityError) as captured:
        secure_config_snapshot({"credential_id": raw_secret})

    assert captured.value.code == "secret_or_pii"
    assert raw_secret not in repr(captured.value)


@pytest.mark.parametrize(
    ("unsafe_value", "expected_code"),
    [
        (b"opaque-secret-bytes", "unsafe_binary"),
        (object(), "unsafe_config_value"),
    ],
)
def test_secure_config_snapshot_rejects_opaque_values(
    unsafe_value, expected_code
):
    with pytest.raises(ArenaIngressSecurityError) as captured:
        secure_config_snapshot({"value": unsafe_value})

    assert captured.value.code == expected_code
    assert "opaque-secret-bytes" not in repr(captured.value)


@pytest.mark.parametrize(
    "runtime_value",
    [
        "sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer abcdefghijklmnop",
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "user@example.com",
    ],
)
def test_runtime_controlled_text_rejects_secret_or_pii_without_echo(runtime_value):
    with pytest.raises(ArenaIngressSecurityError) as captured:
        validate_runtime_controlled_text(runtime_value)

    assert captured.value.code == "secret_or_pii"
    assert runtime_value not in str(captured.value)
    assert runtime_value not in repr(captured.value)


@pytest.mark.parametrize(
    "runtime_value",
    [
        "result\u202e-hidden",
        "good\u200b-hidden",
        "quote\u0000hidden",
    ],
)
def test_runtime_controlled_text_rejects_invisible_or_control_unicode(runtime_value):
    with pytest.raises(ArenaIngressSecurityError) as captured:
        validate_runtime_controlled_text(runtime_value)

    assert captured.value.code in {
        "unsafe_unicode_control",
        "unsafe_unicode_format",
    }
    assert runtime_value not in repr(captured.value)


def test_runtime_controlled_text_preserves_safe_identifier():
    assert (
        validate_runtime_controlled_text("runtime-result_01J123")
        == "runtime-result_01J123"
    )
