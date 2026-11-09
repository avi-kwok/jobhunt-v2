"""Secrets: one master key (JOBHUNT_SECRET_KEY) -> Fernet key + HMAC key via HKDF-SHA256."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENV_VAR = "JOBHUNT_SECRET_KEY"
_INFO_ENC = b"jobhunt-v2/webhook-encryption"
_INFO_MAC = b"jobhunt-v2/webhook-hmac"


class CryptoError(RuntimeError):
    """Missing/malformed key, or ciphertext that cannot be decrypted."""


def generate_secret_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _master(key: str | None) -> bytes:
    key = (key if key is not None else os.environ.get(ENV_VAR, "")).strip()
    if not key:
        raise CryptoError(f"{ENV_VAR} is not set. Generate one with `jobhunt keygen`.")
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except (binascii.Error, ValueError):
        raise CryptoError(f"{ENV_VAR} is not valid urlsafe base64.") from None
    if len(raw) != 32:
        raise CryptoError(f"{ENV_VAR} must decode to exactly 32 bytes.")
    return raw


def require_key(key: str | None = None) -> None:
    """Raise CryptoError now if the secret key is missing or malformed."""
    _master(key)


def _derive(key: str | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_master(key))


def _fernet(key: str | None) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive(key, _INFO_ENC)))


def encrypt_url(url: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(url.encode()).decode()


def decrypt_url(token: str, key: str | None = None) -> str:
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken:
        raise CryptoError("cannot decrypt webhook (wrong JOBHUNT_SECRET_KEY?)") from None


def webhook_hash(normalized_url: str, key: str | None = None) -> str:
    """Stable HMAC-SHA256 hex of the normalized URL (dedupe without storing plaintext)."""
    return hmac.new(_derive(key, _INFO_MAC), normalized_url.encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
