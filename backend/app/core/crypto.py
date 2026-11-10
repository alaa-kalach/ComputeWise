"""Fernet encryption for provider secrets at rest.

Secrets are encrypted before they reach the database and are never returned by any API.
Run `python -m app.core.crypto` to print a new key for FERNET_KEY.
"""

from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY is not set. Generate one with: python -m app.core.crypto")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
