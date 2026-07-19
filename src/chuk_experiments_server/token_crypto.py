"""Encrypt/decrypt per-user GitHub/HF tokens at rest (app_user.*_token_encrypted).

The only thing in this schema that needs to be *retrieved and used*, unlike
api_key's one-way hash (only ever compared, never reversed). Fernet
(symmetric, authenticated) is already available transitively via
google-auth's own dependency on `cryptography` — no new dependency.
"""

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class TokenEncryptionNotConfigured(Exception):
    pass


def _fernet() -> Fernet:
    if not settings.token_encryption_configured:
        raise TokenEncryptionNotConfigured("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(settings.token_encryption_key.encode("utf-8"))


def encrypt_token(raw: str) -> str:
    return _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("token could not be decrypted — wrong key or corrupted value") from exc
