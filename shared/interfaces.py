"""
共享数据结构定义 — A2A 组和 X402 组之间的接口契约。

地位：CONTRACT
- 任何字段增删需要两组双方 Approve（参见共创协议 §8.2）
- A2A 组主导 PaymentRequest 的定义
- X402 组主导 PaymentResult / PaymentProof 的定义

版本：v1.0 | 日期：2026-07-23
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from datetime import datetime, timedelta, timezone
import uuid


# ============================================================
# 资产与链定义
# ============================================================

class Token(str, Enum):
    """支持的结算资产。v1.0 仅 USDC。"""
    USDC = "USDC"
    # USDT = "USDT"    # v1.1 候选
    # ETH  = "ETH"     # v1.1 候选


class Chain(str, Enum):
    """支持的区块链。v1.0 仅 Injective mainnet。"""
    INJECTIVE = "injective-1"


# ============================================================
# PaymentRequest — A2A 组构造，X402 组执行
# ============================================================

@dataclass
class PaymentRequest:
    """
    A2A 组告诉 X402 组：请帮我付这笔钱。

    必填字段：
        request_id: 唯一标识，格式 UUID4
        amount:     支付金额（人类可读，如 1.6 = 1.6 USDC）
        token:      结算资产
        recipient_address: 收款方链上地址
        chain:      目标链

    可选字段：
        payer_address: 指定付款地址（不填则由 X402 组自行选择）
        description:   交易描述
        expires_at:    ISO 8601 时间戳，超时后不再执行（默认 5 分钟）
        metadata:      A2A 组的自定义数据，X402 组原样带回
    """

    request_id: str
    amount: float
    token: Token
    recipient_address: str
    chain: Chain

    payer_address: Optional[str] = None
    description: str = ""
    expires_at: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        # 校验 request_id 格式
        try:
            uuid.UUID(self.request_id)
        except (ValueError, AttributeError):
            raise ValueError(f"request_id must be a valid UUID4, got: {self.request_id}")

        # 校验 amount
        if not isinstance(self.amount, (int, float)) or self.amount <= 0:
            raise ValueError(f"amount must be positive, got: {self.amount}")

        # 校验 recipient_address 非空
        if not self.recipient_address or not isinstance(self.recipient_address, str):
            raise ValueError("recipient_address must be a non-empty string")

        # 设置默认超时
        if self.expires_at is None:
            self.expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat()

    def to_dict(self) -> dict:
        """序列化为 JSON 字典。"""
        d = asdict(self)
        d["token"] = self.token.value
        d["chain"] = self.chain.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PaymentRequest":
        """从 JSON 字典反序列化。"""
        data = dict(data)  # shallow copy
        data["token"] = Token(data["token"])
        data["chain"] = Chain(data["chain"])
        return cls(**data)


# ============================================================
# PaymentResult — X402 组构造，返回给 A2A 组
# ============================================================

class PaymentStatus(str, Enum):
    """支付终态。"""
    SUCCESS = "SUCCESS"                         # 链上已确认
    FAILED = "FAILED"                           # 交易失败（revert / out of gas）
    TIMEOUT = "TIMEOUT"                         # 超时未确认
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"   # 余额不足


@dataclass
class PaymentResult:
    """
    X402 组告诉 A2A 组：支付结果。

    request_id: 对应 PaymentRequest.request_id
    status:     终态

    SUCCESS 时以下字段有意义：
        tx_hash, block_number, amount_paid, fee_paid, confirmed_at

    FAILED / TIMEOUT / INSUFFICIENT_FUNDS 时以下字段有意义：
        error_code, error_message
    """

    request_id: str
    status: PaymentStatus

    # SUCCESS 字段
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    amount_paid: Optional[float] = None
    fee_paid: Optional[float] = None
    confirmed_at: Optional[str] = None

    # 失败字段
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    # 原样带回
    metadata: dict = field(default_factory=dict)

    def is_success(self) -> bool:
        return self.status == PaymentStatus.SUCCESS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        # 移除 None 值，保持 JSON 干净
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "PaymentResult":
        data = dict(data)
        data["status"] = PaymentStatus(data["status"])
        return cls(**data)


# ============================================================
# PaymentProof — 买方 agent 附加到 A2A 消息，卖方 agent 验证
# ============================================================

@dataclass
class PaymentProof:
    """
    支付证明。买方 Agent 附加到 A2A 消息中，卖方 Agent 通过
    IPaymentGateway.verify_payment_proof() 链上验证。

    警告：PaymentProof 本身不提供任何安全保障。
    必须通过链上查询验证！参见红线 #1。
    """

    tx_hash: str
    payer_address: str
    amount: float
    token: Token
    block_number: int
    chain: Chain
    confirmed_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["token"] = self.token.value
        d["chain"] = self.chain.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PaymentProof":
        data = dict(data)
        data["token"] = Token(data["token"])
        data["chain"] = Chain(data["chain"])
        return cls(**data)


# ============================================================
# X402PaymentRequired — HTTP 402 响应的结构化表示
# ============================================================

@dataclass
class X402PaymentRequired:
    """
    当卖方 Agent 返回 HTTP 402 Payment Required 时，
    买方 A2A 模块将其解析为此结构，传给 X402 模块。
    """

    payment_id: str
    amount: float
    token: Token
    recipient_address: str
    chain: Chain
    expires_in_seconds: int
    description: str = ""

    def to_http_402_response(self) -> tuple[int, dict, dict]:
        """
        返回 (status_code, headers, body) 三元组。
        """
        headers = {
            "X-402-Payment-Id": self.payment_id,
            "X-402-Amount": str(self.amount),
            "X-402-Token": self.token.value,
            "X-402-Recipient": self.recipient_address,
            "X-402-Chain": self.chain.value,
            "X-402-Expires-In": str(self.expires_in_seconds),
        }
        body = {
            "error": "Payment Required",
            "message": self.description or f"This service requires payment of {self.amount} {self.token.value}",
        }
        return 402, headers, body

    @classmethod
    def from_dict(cls, data: dict) -> "X402PaymentRequired":
        data = dict(data)
        data["token"] = Token(data["token"])
        data["chain"] = Chain(data["chain"])
        return cls(**data)
