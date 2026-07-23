"""
共享测试数据。两组都从这里 import 标准 fixture。

用法：
    from shared.test_fixtures import VALID_PAYMENT_REQUEST
"""

from shared.interfaces import (
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
    PaymentProof,
    Token,
    Chain,
)

# ---- 标准 PaymentRequest (买方发起支付时) ----

VALID_PAYMENT_REQUEST = PaymentRequest(
    request_id="550e8400-e29b-41d4-a716-446655440000",
    amount=1.6,
    token=Token.USDC,
    recipient_address="inj1testrecipient0000000000000000",
    chain=Chain.INJECTIVE,
    payer_address="inj1testpayer00000000000000000000",
    description="GPU compute, 2h, 4x A100",
)

# ---- 标准 PaymentResult (支付成功) ----

VALID_PAYMENT_RESULT_SUCCESS = PaymentResult(
    request_id="550e8400-e29b-41d4-a716-446655440000",
    status=PaymentStatus.SUCCESS,
    tx_hash=(
        "0xabcdef1234567890abcdef1234567890"
        "abcdef1234567890abcdef1234567890"
    ),
    block_number=12345678,
    amount_paid=1.6,
    fee_paid=0.001,
    confirmed_at="2026-07-23T00:00:00Z",
)

# ---- 各种失败 PaymentResult ----

VALID_PAYMENT_RESULT_TIMEOUT = PaymentResult(
    request_id="550e8400-e29b-41d4-a716-446655440000",
    status=PaymentStatus.TIMEOUT,
    error_code="TX_TIMEOUT",
    error_message="Transaction not confirmed within expiry window",
)

VALID_PAYMENT_RESULT_INSUFFICIENT_FUNDS = PaymentResult(
    request_id="550e8400-e29b-41d4-a716-446655440000",
    status=PaymentStatus.INSUFFICIENT_FUNDS,
    error_code="INSUFFICIENT_BALANCE",
    error_message="Payer balance 0.5 USDC < required 1.6 USDC",
)

VALID_PAYMENT_RESULT_FAILED = PaymentResult(
    request_id="550e8400-e29b-41d4-a716-446655440000",
    status=PaymentStatus.FAILED,
    error_code="TX_REVERTED",
    error_message="Transaction reverted on-chain",
)

# ---- 标准 PaymentProof (附加到 A2A 消息) ----

VALID_PAYMENT_PROOF = PaymentProof(
    tx_hash=(
        "0xabcdef1234567890abcdef1234567890"
        "abcdef1234567890abcdef1234567890"
    ),
    payer_address="inj1testpayer00000000000000000000",
    amount=1.6,
    token=Token.USDC,
    block_number=12345678,
    chain=Chain.INJECTIVE,
    confirmed_at="2026-07-23T00:00:00Z",
)

# ---- A2A 消息示例 (带和不带 payment proof) ----

A2A_MESSAGE_WITHOUT_PROOF = {
    "id": "msg-001",
    "jsonrpc": "2.0",
    "method": "message/send",
    "params": {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "I need 2h of A100 GPU compute"}],
            "messageId": "msg-001",
        }
    },
}

# 买方附加 proof 后的消息
A2A_MESSAGE_WITH_PROOF = {
    **A2A_MESSAGE_WITHOUT_PROOF,
    "x_payment_proof": VALID_PAYMENT_PROOF.to_dict(),
}
