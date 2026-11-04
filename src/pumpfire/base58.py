"""Minimal base58 codec (Bitcoin alphabet) for Solana public keys.

Decoded events carry mints and wallets as raw 32-byte keys. Encoding them
locally keeps the hot path network-free.
"""

from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(ALPHABET)}


def b58encode(raw: bytes) -> str:
    """Encode bytes to base58. Leading zero bytes become leading ``1``."""
    number = int.from_bytes(raw, "big")
    out: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        out.append(ALPHABET[remainder])
    padding = 0
    for byte in raw:
        if byte == 0:
            padding += 1
        else:
            break
    return "1" * padding + ("".join(reversed(out)) or "")


def b58decode(text: str) -> bytes:
    """Decode base58 to bytes. Raises ``ValueError`` on an invalid character."""
    number = 0
    for char in text:
        try:
            number = number * 58 + _INDEX[char]
        except KeyError as exc:
            raise ValueError(f"invalid base58 character: {char!r}") from exc
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    padding = 0
    for char in text:
        if char == "1":
            padding += 1
        else:
            break
    return b"\x00" * padding + body
