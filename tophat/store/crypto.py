"""At-rest encryption for stored secrets (stdlib only: hmac + sha256).

ProjectX API keys are live trading credentials, so they're encrypted before they
touch disk. We reuse the same per-machine secret that signs session cookies
(`server.auth.get_secret` → `data/.secret`, chmod 600) as the master key, derive
separate encryption and MAC subkeys from it, and use an HMAC-SHA256 keystream
(CTR-style) with encrypt-then-MAC authentication. No third-party crypto
dependency — this matches the stdlib-only posture of `server/auth.py`.

Token format:  v1.<nonce>.<ciphertext>.<tag>   (each field urlsafe-base64).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_VERSION = "v1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _master() -> bytes:
    # Imported lazily so this module has no import-time dependency on the auth DB.
    from tophat.server.auth import get_secret
    return get_secret()


def _subkey(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(enc_key, nonce + counter.to_bytes(8, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def encrypt(plaintext: str, *, master: bytes | None = None) -> str:
    master = master or _master()
    enc_key = _subkey(master, b"tophat-enc")
    mac_key = _subkey(master, b"tophat-mac")
    nonce = secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    ct = _xor(data, _keystream(enc_key, nonce, len(data)))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return ".".join([_VERSION, _b64(nonce), _b64(ct), _b64(tag)])


def decrypt(token: str, *, master: bytes | None = None) -> str:
    master = master or _master()
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _VERSION:
        raise ValueError("bad ciphertext token")
    nonce, ct, tag = _unb64(parts[1]), _unb64(parts[2]), _unb64(parts[3])
    mac_key = _subkey(master, b"tophat-mac")
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("ciphertext authentication failed")
    enc_key = _subkey(master, b"tophat-enc")
    return _xor(ct, _keystream(enc_key, nonce, len(ct))).decode("utf-8")
