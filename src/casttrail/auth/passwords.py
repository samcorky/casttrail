from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_hasher = PasswordHash((Argon2Hasher(),))

# Verified against when an email is unknown, so a missing account costs the same time
# as a wrong password and the response does not reveal which accounts exist.
DUMMY_HASH = _hasher.hash("casttrail-dummy-password")


def hash_password(password: str) -> str:
    """Hash a password with Argon2id.

    Args:
        password: The plain-text password.

    Returns:
        The encoded hash, including its salt and parameters.
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash.

    Args:
        password: The plain-text password offered.
        password_hash: The stored encoded hash.

    Returns:
        Whether the password matches.
    """
    return _hasher.verify(password, password_hash)
