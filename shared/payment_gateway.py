"""
支付网关抽象接口 — A2A 组和 X402 组之间的唯一代码依赖。

A2A 组：只 import 这个抽象类。不要 import X402 的任何具体实现。
X402 组：提供 X402PaymentGateway 的具体实现。

地位：CONTRACT — 修改需要两组双方 Approve。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from shared.interfaces import (
        PaymentRequest,
        PaymentResult,
        PaymentProof,
        X402PaymentRequired,
    )


class IPaymentGateway(ABC):
    """
    A2A ↔ X402 之间的唯一接口层。

    生命周期内的典型调用顺序：

    买方 Agent:
        1. create_payment_from_402(x402_response) → PaymentRequest
        2. execute_payment(request) → PaymentResult
        3. attach_payment_proof_to_message(msg, proof) → enriched msg

    卖方 Agent:
        1. extract_payment_proof_from_message(msg) → PaymentProof | None
        2. verify_payment_proof(proof) → bool  # 绝对不能跳过！
    """

    @abstractmethod
    async def create_payment_from_402(
        self, x402_response: dict
    ) -> "PaymentRequest":
        """
        解析卖方 Agent 返回的 HTTP 402 响应，生成 PaymentRequest。

        输入：HTTP 响应的 body（JSON dict），应包含 X402 头部信息
        输出：标准化的 PaymentRequest

        X402 组实现时：从 body 中提取 X-402-* 信息，映射到 PaymentRequest 字段。
        """
        ...

    @abstractmethod
    async def execute_payment(
        self, request: "PaymentRequest"
    ) -> "PaymentResult":
        """
        执行链上支付。阻塞直到支付完成。

        X402 组实现时：
        1. 校验 request（尤其 expires_at）
        2. 在 TEE 内构造并签名交易
        3. 广播交易
        4. 等待区块确认
        5. 返回 PaymentResult

        A2A 组调用时注意事项：
        - 不要假设即时返回（可能需要几秒）
        - 必须处理所有 PaymentStatus 终态
        - 不要基于 PaymentResult 做业务决策后再发起新支付（竞态）
        """
        ...

    @abstractmethod
    async def verify_payment_proof(
        self, proof: "PaymentProof"
    ) -> bool:
        """
        链上验证支付证明。

        X402 组实现时：用 proof.tx_hash 去链上查交易收据，
        验证 to/amount/token 是否与 proof 一致。
        如果任何一项不匹配，返回 False。

        卖方 A2A 组调用时：必须调用此方法验证，绝对不能信任未验证的 proof。
        参见红线 #1。
        """
        ...

    @abstractmethod
    def attach_payment_proof_to_message(
        self, a2a_message: dict, proof: "PaymentProof"
    ) -> dict:
        """
        将支付证明附加到 A2A 消息中。

        不修改 a2a_message 原有内容，仅在最外层增加：
            "x_payment_proof": { proof.to_dict() }

        X402 组实现时：注意 deep copy，不要修改原消息。
        """
        ...

    @abstractmethod
    def extract_payment_proof_from_message(
        self, a2a_message: dict
    ) -> Optional["PaymentProof"]:
        """
        从 A2A 消息中提取支付证明。

        如果消息中没有 x_payment_proof 字段，返回 None。
        如果有但格式非法，也返回 None（不抛异常，让上层决定如何响应）。
        """
        ...
