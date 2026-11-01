"""Secrets at rest and bearer secrets of the hosted bridge.

- Keyring: AES-256-GCM envelope for values the bridge must read back (Google refresh tokens, webhook signing
  secrets). Each ciphertext is bound to its context (e.g. the user's Google sub) through the AEAD associated
  data, so a value copied to another document doesn't decrypt. Key ids allow rotation (KMS can replace the
  key source later without changing the format).
- new_secret / digest: bearer secrets (access/refresh tokens, codes, agent keys, client secrets) are random,
  and only their SHA-256 is stored; lookups go by digest, so no stored secret is ever compared char by char.
"""

import base64
import binascii
import hashlib
import hmac
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = "v1"


class CryptoError(Exception):
    pass


def _b64decode(text: str) -> bytes:
    text = text.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError):
        raise CryptoError("encryption key is not valid base64") from None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class Keyring:
    """`spec`: base64 of a 32-byte key, or "kid:base64,kid2:base64" — the first key encrypts, all decrypt."""

    def __init__(self, spec: str):
        self._keys: dict[str, AESGCM] = {}
        self.current = ""
        for index, part in enumerate(p.strip() for p in spec.split(",") if p.strip()):
            kid, sep, material = part.partition(":")
            if not sep:
                kid, material = "k1", part
            kid = kid.strip()
            if not kid.isalnum() or kid in self._keys:
                raise CryptoError("encryption key ids must be unique and alphanumeric")
            key = _b64decode(material)
            if len(key) != 32:
                raise CryptoError(f"encryption key {kid} must be 32 bytes (got {len(key)})")
            self._keys[kid] = AESGCM(key)
            if index == 0:
                self.current = kid
        if not self._keys:
            raise CryptoError("no encryption key given")

    def encrypt(self, plaintext: str, context: str) -> str:
        nonce = os.urandom(12)
        sealed = self._keys[self.current].encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
        return f"{FORMAT}.{self.current}.{_b64(nonce + sealed)}"

    def decrypt(self, token: str, context: str) -> str:
        try:
            version, kid, body = token.split(".", 2)
        except (AttributeError, ValueError):
            raise CryptoError("not an encrypted value") from None
        if version != FORMAT or kid not in self._keys:
            raise CryptoError(f"unknown encryption key {kid!r}")
        raw = _b64decode(body)
        try:
            return self._keys[kid].decrypt(raw[:12], raw[12:], context.encode("utf-8")).decode("utf-8")
        except (InvalidTag, ValueError):
            raise CryptoError("encrypted value does not match its key or context") from None


def new_secret(prefix: str = "", nbytes: int = 32) -> str:
    """A random bearer secret (256 bits by default), URL-safe, with a recognisable prefix (qta_, qtr_, qtk_)."""
    return prefix + secrets.token_urlsafe(nbytes)


def digest(secret: str) -> str:
    """The stored form (and document key) of a bearer secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def same(a: str, b: str) -> bool:
    """Constant-time comparison of two strings (compared as SHA-256 digests, so lengths don't leak)."""
    return hmac.compare_digest(hashlib.sha256(a.encode("utf-8")).digest(), hashlib.sha256(b.encode("utf-8")).digest())
