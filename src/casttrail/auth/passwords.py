from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from casttrail.auth.errors import AccountError

_hasher = PasswordHash((Argon2Hasher(),))

# Verified against when an email is unknown, so a missing account costs the same time
# as a wrong password and the response does not reveal which accounts exist.
DUMMY_HASH = _hasher.hash("casttrail-dummy-password")


MIN_PASSWORD_LENGTH = 12
# Argon2 would hash anything, but a huge body is only ever an attempt to burn CPU.
MAX_PASSWORD_LENGTH = 256


def check_new_password(password: str) -> None:
    """Refuse a password that is too short or too long to be set on an account.

    Sign-in does not call this: an existing password is never rejected for its length.

    Args:
        password: The plain-text password someone wants to set.

    Raises:
        AccountError: If the length is out of range.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        msg = f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
        raise AccountError(msg)
    if len(password) > MAX_PASSWORD_LENGTH:
        msg = f"The password must be at most {MAX_PASSWORD_LENGTH} characters."
        raise AccountError(msg)


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
