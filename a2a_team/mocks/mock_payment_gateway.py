"""
A2A 组开发用 Mock — 模拟 X402 支付模块。

不发起真实链上交易。行为可配置，覆盖所有支付终态。
"""

from __future__ import annotations

from shared.payment_gateway import IPaymentGateway
from shared.interfaces import (
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
    PaymentProof,
    X402PaymentRequired,
    Token,
    Chain,
)
from typing import Optional


class MockPaymentGateway(IPaymentGateway):
    """
    A2A 组独立开发时使用。

    behavior 参数：
        "always_success"        — 每次都成功
        "always_timeout"        — 每次都超时
        "insufficient_funds"    — 每次都余额不足
        "always_failed"         — 每次都链上失败 (reverted)
        "fail_then_success"     — 第一次失败，第二次成功（测重试）
    """

    def __init__(self, behavior: str = "always_success"):
        valid = {
            "always_success",
            "always_timeout",
            "insufficient_funds",
            "always_failed",
            "fail_then_success",
        }
        if behavior not in valid:
            raise ValueError(f"behavior must be one of {valid}, got: {behavior}")
        self.behavior = behavior
        self._attempts = 0
        self._verified: list[str] = []  # 记录所有被验证的 tx_hash

    # ---- IPaymentGateway 实现 ----

    async def create_payment_from_402(self, x402_response: dict) -> PaymentRequest:
        return PaymentRequest(
            request_id=x402_response.get("payment_id", "mock-req-001"),
            amount=float(x402_response.get("amount", 1.0)),
            token=Token(x402_response.get("token", "USDC")),
            recipient_address=x402_response.get(
                "recipient_address", "inj1mockrecipient000000000000"
            ),
            chain=Chain(x402_response.get("chain", "injective-1")),
            description=x402_response.get("description", "Mock payment"),
        )

    async def execute_payment(self, request: PaymentRequest) -> PaymentResult:
        self._attempts += 1

        if self.behavior == "always_success":
            return self._success(request)
        elif self.behavior == "always_timeout":
            return self._timeout(request)
        elif self.behavior == "insufficient_funds":
            return self._insufficient_funds(request)
        elif self.behavior == "always_failed":
            return self._failed(request)
        elif self.behavior == "fail_then_success":
            if self._attempts == 1:
                return self._failed(request)
            return self._success(request)

    async def verify_payment_proof(self, proof: PaymentProof) -> bool:
        self._verified.append(proof.tx_hash)
        # Mock: 链上"查到"了这笔交易 → True
        return True

    def attach_payment_proof_to_message(
        self, a2a_message: dict, proof: PaymentProof
    ) -> dict:
        import copy
        enriched = copy.deepcopy(a2a_message)
        enriched["x_payment_proof"] = proof.to_dict()
        return enriched

    def extract_payment_proof_from_message(
        self, a2a_message: dict
    ) -> Optional[PaymentProof]:
        data = a2a_message.get("x_payment_proof")
        if data is None:
            return None
        try:
            return PaymentProof.from_dict(data)
        except Exception:
            return None

    # ---- 内部 ----

    def _success(self, req: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            request_id=req.request_id,
            status=PaymentStatus.SUCCESS,
            tx_hash=f"0x{'a'*64}",
            block_number=12345678 + self._attempts,
            amount_paid=req.amount,
            fee_paid=0.001,
            confirmed_at="2026-07-23T00:00:00Z",
            metadata=req.metadata,
        )

    def _timeout(self, req: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            request_id=req.request_id,
            status=PaymentStatus.TIMEOUT,
            error_code="TX_TIMEOUT",
            error_message="Mock timeout",
            metadata=req.metadata,
        )

    def _insufficient_funds(self, req: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            request_id=req.request_id,
            status=PaymentStatus.INSUFFICIENT_FUNDS,
            error_code="INSUFFICIENT_BALANCE",
            error_message="Mock: not enough balance",
            metadata=req.metadata,
        )

    def _failed(self, req: PaymentRequest) -> PaymentResult:
        return PaymentResult(
            request_id=req.request_id,
            status=PaymentStatus.FAILED,
            error_code="TX_REVERTED",
            error_message="Mock: transaction reverted",
            metadata=req.metadata,
        )
