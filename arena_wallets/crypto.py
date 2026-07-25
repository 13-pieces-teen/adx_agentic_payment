"""Small dependency-free Ethereum personal-sign verifier.

The API only accepts EIP-191 ``personal_sign`` signatures.  Keeping recovery
here avoids adding a wallet SDK to the web process; no private key material is
ever handled by this module.
"""

from __future__ import annotations

import re


class WalletSignatureError(ValueError):
    """Raised when an Ethereum message signature cannot be recovered."""


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_G = (
    55066263022277343669578718895168534326250603453777594175500187360389116729240,
    32670510020758816978083085130507043184471273380659243275938904335757337482424,
)
_KECCAK_MASK = (1 << 64) - 1
_KECCAK_ROUNDS = (
    1,
    0x8082,
    0x800000000000808A,
    0x8000000080008000,
    0x808B,
    0x80000001,
    0x8000000080008081,
    0x8000000000008009,
    0x8A,
    0x88,
    0x80008009,
    0x8000000A,
    0x8000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x80000001,
    0x8000000080008008,
)
_KECCAK_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl(value: int, amount: int) -> int:
    if amount == 0:
        return value & _KECCAK_MASK
    return ((value << amount) | (value >> (64 - amount))) & _KECCAK_MASK


def _keccak_f(state: list[int]) -> None:
    for round_constant in _KECCAK_ROUNDS:
        column_parity = [
            state[x]
            ^ state[x + 5]
            ^ state[x + 10]
            ^ state[x + 15]
            ^ state[x + 20]
            for x in range(5)
        ]
        theta = [
            column_parity[(x - 1) % 5]
            ^ _rotl(column_parity[(x + 1) % 5], 1)
            for x in range(5)
        ]
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] ^= theta[x]

        rotated = [0] * 25
        for y in range(5):
            for x in range(5):
                rotated[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(
                    state[x + 5 * y], _KECCAK_ROTATIONS[x][y]
                )
        for y in range(5):
            row = [rotated[x + 5 * y] for x in range(5)]
            for x in range(5):
                state[x + 5 * y] = row[x] ^ ((~row[(x + 1) % 5]) & row[(x + 2) % 5])
                state[x + 5 * y] &= _KECCAK_MASK
        state[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    """Return legacy Keccak-256, not the FIPS SHA3-256 variant."""

    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0)
    padded.append(0x80)
    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for lane in range(rate // 8):
            state[lane] ^= int.from_bytes(block[lane * 8 : lane * 8 + 8], "little")
        _keccak_f(state)
    return b"".join(value.to_bytes(8, "little") for value in state)[:32]


def _inverse(value: int, modulus: int) -> int:
    return pow(value, modulus - 2, modulus)


def _point_add(left: tuple[int, int] | None, right: tuple[int, int] | None):
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _SECP256K1_P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1) * _inverse(2 * y1 % _SECP256K1_P, _SECP256K1_P)
    else:
        slope = (y2 - y1) * _inverse((x2 - x1) % _SECP256K1_P, _SECP256K1_P)
    slope %= _SECP256K1_P
    x3 = (slope * slope - x1 - x2) % _SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % _SECP256K1_P
    return x3, y3


def _point_mul(scalar: int, point: tuple[int, int] | None):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _signature_parts(signature: str) -> tuple[int, int, int]:
    if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", signature):
        raise WalletSignatureError("invalid_signature_encoding")
    raw = bytes.fromhex(signature[2:])
    if len(raw) == 65:
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        if v in (27, 28):
            recovery_id = v - 27
        elif v in (0, 1):
            recovery_id = v
        else:
            raise WalletSignatureError("invalid_signature_recovery_id")
    elif len(raw) == 64:
        r = int.from_bytes(raw[:32], "big")
        compact_s = int.from_bytes(raw[32:], "big")
        recovery_id = compact_s >> 255
        s = compact_s & ((1 << 255) - 1)
    else:
        raise WalletSignatureError("invalid_signature_length")
    if not 0 < r < _SECP256K1_N or not 0 < s <= _SECP256K1_N // 2:
        raise WalletSignatureError("invalid_signature_values")
    return r, s, recovery_id


def recover_personal_signer(message: str, signature: str) -> str:
    """Recover the lower-case Ethereum address that signed ``message``."""

    if not isinstance(message, str) or not message or len(message.encode("utf-8")) > 4096:
        raise WalletSignatureError("invalid_signed_message")
    r, s, recovery_id = _signature_parts(signature)
    message_bytes = message.encode("utf-8")
    prefixed = (
        b"\x19Ethereum Signed Message:\n"
        + str(len(message_bytes)).encode("ascii")
        + message_bytes
    )
    digest = int.from_bytes(keccak256(prefixed), "big")
    x = r
    if x >= _SECP256K1_P:
        raise WalletSignatureError("invalid_signature_point")
    alpha = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(alpha, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if (y * y - alpha) % _SECP256K1_P != 0:
        raise WalletSignatureError("invalid_signature_point")
    if y & 1 != recovery_id:
        y = _SECP256K1_P - y
    recovered = _point_mul(
        _inverse(r, _SECP256K1_N),
        _point_add(_point_mul(s, (x, y)), _point_mul((-digest) % _SECP256K1_N, _SECP256K1_G)),
    )
    if recovered is None:
        raise WalletSignatureError("invalid_signature_point")
    public_key = recovered[0].to_bytes(32, "big") + recovered[1].to_bytes(32, "big")
    return "0x" + keccak256(public_key)[-20:].hex()


def normalize_address(value: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise WalletSignatureError("invalid_wallet_address")
    return value.lower()
