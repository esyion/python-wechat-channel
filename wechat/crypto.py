"""AES-128-ECB + PKCS7 + MD5 crypto utilities for CDN media."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Final

from Crypto.Cipher import AES

# PKCS7 padding block size
_BLOCK_SIZE: Final[int] = 16


def _pkcs7_pad(data: bytes) -> bytes:
    """Apply PKCS7 padding to align data to 16-byte boundary."""
    pad_len = _BLOCK_SIZE - (len(data) % _BLOCK_SIZE)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    """Remove PKCS7 padding, verifying integrity."""
    if not data:
        raise ValueError("empty ciphertext")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > _BLOCK_SIZE:
        raise ValueError(f"invalid PKCS7 pad byte: {pad_len}")
    if any(b != pad_len for b in data[-pad_len:]):
        raise ValueError("PKCS7 padding integrity check failed")
    return data[:-pad_len]


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt buffer with AES-128-ECB + PKCS7 padding."""
    if len(key) != 16:
        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
    cipher = AES.new(key, AES.MODE_ECB)
    padded = _pkcs7_pad(plaintext)
    return cipher.encrypt(padded)


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt buffer with AES-128-ECB + PKCS7 padding."""
    if len(key) != 16:
        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
    cipher = AES.new(key, AES.MODE_ECB)
    padded = cipher.decrypt(ciphertext)
    return _pkcs7_unpad(padded)


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Ciphertext size after PKCS7 padding (always multiple of 16)."""
    return ((plaintext_size + 1) // 16 + 1) * 16


def generate_aes_key() -> bytes:
    """Generate a fresh 16-byte AES key (random bytes)."""
    return os.urandom(16)


def generate_filekey() -> str:
    """Generate a 16-byte filekey as a 32-char hex string."""
    return os.urandom(16).hex()


def md5_hex(buf: bytes) -> str:
    """MD5 hex digest, uppercase (matching Node's createHash('md5'))."""
    return hashlib.md5(buf).hexdigest().upper()


def parse_aes_key(aes_key_base64: str) -> bytes:
    """
    Parse CDNMedia.aes_key into a raw 16-byte AES key.

    Supports both common encodings:
      - base64(raw 16 bytes)
      - base64(hex string of 16 bytes)  ← 32-char hex string encoded as base64
    """
    decoded = __import__("base64").b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.match(r"^[0-9a-fA-F]{32}$", decoded.decode("ascii")):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ValueError(
        f"aes_key must decode to 16 raw bytes or 32-char hex, got {len(decoded)} bytes"
    )


def aes_key_hex_to_base64(hex_key: str) -> str:
    """
    Convert a hex AES key (32-char string) to the ``media.aes_key`` encoding.

    Per weixin-channel-api.md §14.3: the value is ``base64(hex_key_ascii_bytes)``.
    The receiver does: base64 decode → ASCII string → hex decode → 16-byte key.

    Example:
      hex_key = "00112233445566778899aabbccddeeff"
      this returns: base64(b"00112233445566778899aabbccddeeff")
                  = "MDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVmZm=="

    Note: NOT base64(raw_16_bytes). That buggy form would produce
    "ABEiM0RVZneImaq7zN3u/w==" which the receiver cannot decrypt.
    """
    return __import__("base64").b64encode(hex_key.encode("ascii")).decode("ascii")


def aes_key_hex_to_buffer(hex_key: str) -> bytes:
    """Convert a hex AES key to raw 16-byte buffer."""
    buf = bytes.fromhex(hex_key)
    if len(buf) != 16:
        raise ValueError(f"Expected 16 bytes from hex key, got {len(buf)}")
    return buf
