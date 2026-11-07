import hashlib
import secrets


def new_token() -> str:
    """Create a random, unguessable session token.

    Returns:
        A URL-safe token with 256 bits of entropy.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash a session token for storage and lookup.

    A fast hash is enough: the token is random, not a guessable password.

    Args:
        token: The secret token.

    Returns:
        The hex SHA-256 digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()
