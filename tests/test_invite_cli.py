from __future__ import annotations

import hashlib
import json

from connector_gateway import invite_cli


def test_invite_cli_generates_a_unique_machine_readable_batch(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["invite_cli", "--count", "12", "--json"],
    )

    assert invite_cli.main() == 0
    generated = json.loads(capsys.readouterr().out)

    assert len(generated) == 12
    assert len({item["invite"] for item in generated}) == 12
    assert all(
        item["tokenHash"]
        == hashlib.sha256(item["invite"].encode("utf-8")).hexdigest()
        for item in generated
    )
