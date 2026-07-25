from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_many_hosted_pawnhouse_demo import (  # noqa: E402
    _invites_from_environment,
    _join_body,
)


def test_many_hosted_demo_consumes_a_batch_without_printing_invites(
    monkeypatch,
) -> None:
    invites = [f"invite-{index:02d}-" + "x" * 24 for index in range(12)]
    monkeypatch.setenv(
        "ARENA_HOSTED_INVITES",
        json.dumps({"invites": invites}),
    )

    assert _invites_from_environment(12) == invites
    assert _join_body(seller=False)["cash"] == "20"
    assert _join_body(seller=True)["holdings"]["iron"] == 1
