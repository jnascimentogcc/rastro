"""Column-level encryption of session excerpts (spec section 11).

`agent_sessions.excerpts` holds fragments of private conversations, so it is encrypted by the
application before it reaches PostgreSQL. The column stays `jsonb`: an encrypted value is the
envelope `{"v": 1, "ct": "<fernet token>"}`; a plain JSON list is a legacy (pre-encryption)
row and is still readable. Readers see a list of dicts either way (`EncryptedExcerpts`).

Keys come from `EXCERPTS_ENCRYPTION_KEYS`: comma-separated Fernet keys, the first one encrypts
and all of them decrypt. To rotate: prepend a new key, deploy, run
`python -m rastro_db.crypto reencrypt`, then drop the old key.
"""

import json
import logging
import os
import sys
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

KEYS_ENV = "EXCERPTS_ENCRYPTION_KEYS"
ENVELOPE_VERSION = 1


class DecryptionError(RuntimeError):
    """The stored excerpts cannot be decrypted (missing or wrong key, corrupted value)."""


class ExcerptCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError(f"at least one key is required ({KEYS_ENV})")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except ValueError as error:
            raise ValueError(f"{KEYS_ENV} must hold Fernet keys (see generate_key)") from error

    def encrypt(self, excerpts: list[dict[str, Any]]) -> dict[str, Any]:
        token = self._fernet.encrypt(json.dumps(excerpts, separators=(",", ":")).encode())
        return {"v": ENVELOPE_VERSION, "ct": token.decode()}

    def decrypt(self, value: Any) -> list[dict[str, Any]]:
        if not is_envelope(value):
            return value  # type: ignore[no-any-return]  # legacy plaintext row
        try:
            return json.loads(self._fernet.decrypt(value["ct"].encode()))  # type: ignore[no-any-return]
        except InvalidToken as error:
            raise DecryptionError(
                f"cannot decrypt excerpts: none of the keys in {KEYS_ENV} matches"
            ) from error

    def rotate(self, value: dict[str, Any]) -> dict[str, Any]:
        """Re-encrypt an envelope with the primary key (same plaintext)."""
        try:
            token = self._fernet.rotate(value["ct"].encode())
        except InvalidToken as error:
            raise DecryptionError(
                f"cannot rotate excerpts: no key in {KEYS_ENV} matches"
            ) from error
        return {"v": ENVELOPE_VERSION, "ct": token.decode()}


def generate_key() -> str:
    return Fernet.generate_key().decode()


def is_envelope(value: Any) -> bool:
    return isinstance(value, dict) and "ct" in value and "v" in value


def cipher_from_env() -> ExcerptCipher | None:
    keys = [k.strip() for k in os.environ.get(KEYS_ENV, "").split(",") if k.strip()]
    return ExcerptCipher(keys) if keys else None


_cipher: ExcerptCipher | None = None
_cipher_loaded = False
_warned = False


def get_cipher() -> ExcerptCipher | None:
    global _cipher, _cipher_loaded
    if not _cipher_loaded:
        _cipher = cipher_from_env()
        _cipher_loaded = True
    return _cipher


def set_cipher(cipher: ExcerptCipher | None) -> None:
    """Override the cipher (tests, tools). `None` returns to reading the environment."""
    global _cipher, _cipher_loaded
    _cipher = cipher
    _cipher_loaded = cipher is not None


class EncryptedExcerpts(TypeDecorator[list[dict[str, Any]]]):
    """`jsonb` column that stores the excerpts encrypted. Without a configured key it falls
    back to plaintext (development) and says so once; with a key, reading plaintext legacy rows
    still works, and reading encrypted rows with a wrong or missing key raises DecryptionError."""

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        global _warned
        if value is None:
            return None
        cipher = get_cipher()
        if cipher is None:
            if not _warned:
                logger.warning("%s is not set: session excerpts are stored in plaintext", KEYS_ENV)
                _warned = True
            return value
        return cipher.encrypt(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None or not is_envelope(value):
            return value
        cipher = get_cipher()
        if cipher is None:
            raise DecryptionError(f"excerpts are encrypted but {KEYS_ENV} is not set")
        return cipher.decrypt(value)


def encrypt_existing_rows(connection: Connection, *, cipher: ExcerptCipher | None = None) -> int:
    """Encrypt plaintext rows and re-encrypt envelopes under the primary key. Idempotent.
    Raw SQL on purpose: it must see the stored value, not the decrypted one. Returns the number
    of rows written; 0 (and a warning) when no key is configured."""
    cipher = cipher or get_cipher()
    if cipher is None:
        logger.warning("%s is not set: existing session excerpts were not encrypted", KEYS_ENV)
        return 0
    rows = connection.execute(text("SELECT id, excerpts FROM agent_sessions")).all()
    written = 0
    for row_id, stored in rows:
        updated = cipher.rotate(stored) if is_envelope(stored) else cipher.encrypt(stored)
        connection.execute(
            text("UPDATE agent_sessions SET excerpts = CAST(:v AS jsonb) WHERE id = :id"),
            {"v": json.dumps(updated), "id": row_id},
        )
        written += 1
    return written


def main(argv: list[str]) -> int:
    if argv[:1] == ["generate-key"]:
        print(generate_key())
        return 0
    if argv[:1] == ["reencrypt"]:
        import asyncio

        from rastro_db.session import create_engine

        async def run() -> int:
            engine = create_engine()
            async with engine.begin() as connection:
                count = await connection.run_sync(encrypt_existing_rows)
            await engine.dispose()
            return count

        print(f"re-encrypted {asyncio.run(run())} rows")
        return 0
    print("usage: python -m rastro_db.crypto generate-key | reencrypt", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
