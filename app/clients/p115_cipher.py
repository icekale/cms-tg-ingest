"""115 lixianssp request encryption."""

from __future__ import annotations

import base64


_RSA_N = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b8602136477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683",
    16,
)
_RSA_E = 0x10001
_RSA_KEY = bytes((0x8D, 0xA5, 0xA5, 0x8D))
_G_KEY_L = bytes((0x78, 0x06, 0xAD, 0x4C, 0x33, 0x86, 0x5D, 0x18, 0x4C, 0x01, 0x3F, 0x46))


def _xor115(source: bytes, key: bytes) -> bytes:
    output = bytearray(len(source))
    head = len(source) % 4
    for index in range(head):
        output[index] = source[index] ^ key[index]
    for offset in range(head, len(source), len(key)):
        size = min(len(key), len(source) - offset)
        for index in range(size):
            output[offset + index] = source[offset + index] ^ key[index]
    return bytes(output)


def _rsa_block(message: bytes) -> bytes:
    block = bytearray(128)
    fill = 126 - len(message)
    block[1 : 1 + fill] = b"\x02" * fill
    block[2 + fill :] = message
    encrypted = pow(int.from_bytes(block, "big"), _RSA_E, _RSA_N)
    return encrypted.to_bytes(128, "big")


def lixian_rsa_encrypt(data: bytes) -> str:
    """Encrypt a JSON body for 115's ``lixianssp`` endpoint."""

    reversed_data = _xor115(data, _RSA_KEY)[::-1]
    wrapped = b"\x00" * 16 + _xor115(reversed_data, _G_KEY_L)
    encrypted = b"".join(_rsa_block(wrapped[offset : offset + 117]) for offset in range(0, len(wrapped), 117))
    return base64.b64encode(encrypted).decode("ascii")
