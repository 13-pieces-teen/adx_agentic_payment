"""
X402 组开发用 Mock — 模拟 A2A Agent 通信层。

模拟一个会返回 HTTP 402 的卖方 Agent，以及接受带 payment proof 请求的买方交互。
"""

from __future__ import annotations

from shared.interfaces import PaymentProof, Token, Chain


class MockA2ASellerAgent:
    """
    模拟卖方 Agent。
    总是对请求返回 HTTP 402 Payment Required。
    """

    def __init__(
        self,
        amount: float = 1.6,
        token: Token = Token.USDC,
        chain: Chain = Chain.INJECTIVE,
        expires_in_seconds: int = 300,
    ):
        self.amount = amount
        self.token = token
        self.chain = chain
        self.expires_in_seconds = expires_in_seconds

    async def handle_request(self, a2a_message: dict) -> dict:
        """
        收到 A2A 请求 → 检查有无支付证明 → 无则返回 402。

        返回格式模拟 HTTP 响应：{status_code, headers, body}
        """
        proof_data = a2a_message.get("x_payment_proof")

        if proof_data:
            # 有支付证明，但卖方还得验证（交给 verify_payment_proof）
            return {
                "status_code": 200,
                "headers": {},
                "body": {
                    "result": "Service delivered (payment proof present, pending verification)",
                    "payment_proof_received": True,
                },
            }

        # 无支付证明 → 返回 402
        return {
            "status_code": 402,
            "headers": {
                "X-402-Payment-Id": "mock-payment-session-001",
                "X-402-Amount": str(self.amount),
                "X-402-Token": self.token.value,
                "X-402-Recipient": "inj1mockseller000000000000000000",
                "X-402-Chain": self.chain.value,
                "X-402-Expires-In": str(self.expires_in_seconds),
            },
            "body": {
                "error": "Payment Required",
                "message": (
                    f"This service requires payment of "
                    f"{self.amount} {self.token.value}"
                ),
            },
        }


class MockA2ABuyerClient:
    """
    模拟买方 Agent 的 A2A 通信能力。
    """

    async def send_message(self, agent_url: str, message: dict) -> dict:
        """模拟向卖方 Agent 发送消息。"""
        # 简易：调用 MockA2ASellerAgent
        seller = MockA2ASellerAgent()
        return await seller.handle_request(message)

    async def send_message_with_proof(
        self, agent_url: str, message: dict, proof: PaymentProof
    ) -> dict:
        """发送带支付证明的消息。"""
        enriched = {**message, "x_payment_proof": proof.to_dict()}
        seller = MockA2ASellerAgent()
        return await seller.handle_request(enriched)
