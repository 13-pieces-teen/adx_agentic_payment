import pytest

from arena_payments.facilitator import HttpX402FacilitatorClient
from arena_payments.signer import HttpWalletSignerClient


def test_cloud_payment_clients_allow_only_https_or_named_internal_services() -> None:
    HttpX402FacilitatorClient(
        "http://arena-facilitator:4021",
        facilitator_id="internal-testnet",
    )
    HttpWalletSignerClient(
        "http://wallet-signer:8787",
        bearer_token="x" * 32,
    )

    with pytest.raises(ValueError, match="https_or_internal"):
        HttpX402FacilitatorClient(
            "http://example.com",
            facilitator_id="unsafe",
        )
    with pytest.raises(ValueError, match="https_or_internal"):
        HttpWalletSignerClient(
            "http://example.com",
            bearer_token="x" * 32,
        )
