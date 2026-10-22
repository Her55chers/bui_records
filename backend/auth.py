"""
Password hashing for customer accounts.

Standard library only (PBKDF2-HMAC-SHA256), so requirements.txt does not change.
A stored hash looks like:  pbkdf2_sha256$<iterations>$<salt hex>$<digest hex>
The iteration count is stored with each hash, so it can be raised later without
breaking existing accounts.
"""

import hashlib
import hmac
import secrets
from functools import lru_cache

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000  # OWASP's recommended minimum for PBKDF2-HMAC-SHA256


def _digest(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = _digest(password, salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


@lru_cache(maxsize=1)
def _dummy_hash():
    return hash_password(secrets.token_hex(16))


def verify_password(password, stored):
    """
    True if `password` matches the stored hash.

    Pass stored=None when the account does not exist: the same amount of work is
    still done, so response time does not reveal whether an email is registered.
    """
    if stored is None:
        verify_password(password, _dummy_hash())
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = _digest(password, bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)
