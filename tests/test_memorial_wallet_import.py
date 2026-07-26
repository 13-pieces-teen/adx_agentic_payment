from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "import_memorial_wallet_inventory.py"
SPEC = importlib.util.spec_from_file_location("memorial_wallet_import", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
RECORD_SCRIPT = (
    ROOT / "deploy" / "scripts" / "record_memorial_mint_batch.py"
)
RECORD_SPEC = importlib.util.spec_from_file_location(
    "memorial_mint_record", RECORD_SCRIPT
)
assert RECORD_SPEC is not None and RECORD_SPEC.loader is not None
RECORD_MODULE = importlib.util.module_from_spec(RECORD_SPEC)
sys.modules[RECORD_SPEC.name] = RECORD_MODULE
RECORD_SPEC.loader.exec_module(RECORD_MODULE)


def test_importer_returns_public_fields_only(tmp_path: Path) -> None:
    csv_path = (tmp_path / "memorial.csv").resolve()
    fields = [
        "index",
        "token_id",
        "wallet_id",
        "ethereum_address",
        "public_key",
        "private_key",
        "mnemonic",
        "derivation_path",
        "chain_id",
        "network",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for token_id in range(402):
            writer.writerow(
                {
                    "index": token_id,
                    "token_id": token_id,
                    "wallet_id": f"memorial-wallet-{token_id:04d}",
                    "ethereum_address": f"0x{token_id + 1:040x}",
                    "public_key": "public-material",
                    "private_key": "private-material",
                    "mnemonic": "twelve words stay outside the database",
                    "derivation_path": "m/44'/60'/0'/0/0",
                    "chain_id": 1439,
                    "network": "injective-evm-testnet",
                }
            )

    wallets, checksum = MODULE.load_public_memorial_inventory(csv_path)

    assert len(wallets) == 402
    assert len(checksum) == 64
    assert wallets[0].token_id == 0
    assert wallets[-1].token_id == 401
    public_record = wallets[0]
    assert not hasattr(public_record, "private_key")
    assert not hasattr(public_record, "mnemonic")


def test_confirmed_manifest_loader_requires_contiguous_chain_evidence(
    tmp_path: Path,
) -> None:
    import json

    manifest_path = (tmp_path / "batch.json").resolve()
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "campaign": "arena402-genesis",
                "batchId": "memorial-000-001-test",
                "addressDigest": "sha256:" + "00" * 32,
                "records": [
                    {
                        "tokenId": token_id,
                        "walletId": f"memorial-wallet-{token_id:04d}",
                        "address": f"0x{token_id + 1:040x}",
                        "status": "confirmed",
                        "txHash": "0x" + "ab" * 32,
                        "blockNumber": 123,
                    }
                    for token_id in (0, 1)
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = RECORD_MODULE.load_confirmed_manifest(manifest_path)

    assert len(loaded["records"]) == 2
