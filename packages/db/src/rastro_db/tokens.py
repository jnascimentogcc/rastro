import hashlib
import secrets

TOKEN_PREFIX = "rst_"


def generate_token() -> str:
    """New repository API token. Only its hash is stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
