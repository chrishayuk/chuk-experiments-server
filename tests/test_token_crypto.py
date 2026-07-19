"""token_crypto.py — Fernet encrypt/decrypt for per-user GitHub/HF tokens."""

import pytest
from cryptography.fernet import Fernet

from chuk_experiments_server import token_crypto
from chuk_experiments_server.config import settings


@pytest.fixture(autouse=True)
def _token_encryption_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(type(settings), "token_encryption_key", property(lambda self: key))
    return key


def test_encrypt_decrypt_round_trip():
    ciphertext = token_crypto.encrypt_token("ghp_realsecret")
    assert ciphertext != "ghp_realsecret"
    assert token_crypto.decrypt_token(ciphertext) == "ghp_realsecret"


def test_decrypt_with_wrong_key_fails(monkeypatch):
    ciphertext = token_crypto.encrypt_token("ghp_realsecret")
    monkeypatch.setattr(
        type(settings), "token_encryption_key", property(lambda self: Fernet.generate_key().decode("utf-8"))
    )
    with pytest.raises(ValueError):
        token_crypto.decrypt_token(ciphertext)


def test_encrypt_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(type(settings), "token_encryption_key", property(lambda self: None))
    with pytest.raises(token_crypto.TokenEncryptionNotConfigured):
        token_crypto.encrypt_token("x")
