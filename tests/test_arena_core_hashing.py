import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from arena_core.hashing import (
    canonical_json_bytes,
    sha256_identifier,
    sha256_text_identifier,
)


def test_snapshot_hash_is_order_independent_and_prefixed():
    first = {
        "price": Decimal("12.500000"),
        "deadline": datetime(2026, 7, 24, tzinfo=timezone.utc),
        "nested": {"b": 2, "a": 1},
    }
    second = {
        "nested": {"a": 1, "b": 2},
        "deadline": datetime(2026, 7, 24, tzinfo=timezone.utc),
        "price": Decimal("12.500000"),
    }

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_identifier(first) == sha256_identifier(second)
    assert sha256_identifier(first).startswith("sha256:")
    assert len(sha256_identifier(first)) == len("sha256:") + 64


def test_snapshot_hash_rejects_unsupported_values():
    with pytest.raises(TypeError, match="Unsupported snapshot value"):
        sha256_identifier({"unsafe": object()})


def test_opaque_identifier_digest_hashes_raw_utf8_text():
    value = "runtime-result_01J123"

    assert sha256_text_identifier(value) == (
        "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    )
