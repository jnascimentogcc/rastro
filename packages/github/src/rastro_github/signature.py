import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    """Value GitHub sends in `X-Hub-Signature-256` for `body`."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of a webhook signature over the raw request body."""
    if not header:
        return False
    return hmac.compare_digest(sign(secret, body), header)
