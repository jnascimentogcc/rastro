import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from rastro_db import create_engine, create_session_factory
from rastro_db.crypto import (
    DecryptionError,
    ExcerptCipher,
    encrypt_existing_rows,
    generate_key,
    get_cipher,
    is_envelope,
    set_cipher,
)
from rastro_db.models import AgentSession

EXCERPTS = [{"role": "user", "kind": "intent", "text": "Never book implants on Friday afternoon"}]


@pytest.fixture(autouse=True)
def restore_cipher() -> Iterator[None]:
    original = get_cipher()
    yield
    set_cipher(original)


def test_envelope_hides_the_plaintext_and_roundtrips() -> None:
    cipher = ExcerptCipher([generate_key()])
    stored = cipher.encrypt(EXCERPTS)

    assert is_envelope(stored) and stored["v"] == 1
    assert "implants" not in json.dumps(stored)
    assert cipher.decrypt(stored) == EXCERPTS


def test_wrong_key_cannot_decrypt() -> None:
    stored = ExcerptCipher([generate_key()]).encrypt(EXCERPTS)

    with pytest.raises(DecryptionError):
        ExcerptCipher([generate_key()]).decrypt(stored)


def test_plaintext_legacy_rows_are_still_readable() -> None:
    assert ExcerptCipher([generate_key()]).decrypt(EXCERPTS) == EXCERPTS


def test_key_rotation_keeps_old_data_readable_and_moves_it_to_the_new_key() -> None:
    old, new = generate_key(), generate_key()
    stored = ExcerptCipher([old]).encrypt(EXCERPTS)

    rotating = ExcerptCipher([new, old])
    assert rotating.decrypt(stored) == EXCERPTS  # old key still decrypts
    rotated = rotating.rotate(stored)

    assert ExcerptCipher([new]).decrypt(rotated) == EXCERPTS  # the old key is no longer needed
    with pytest.raises(DecryptionError):
        ExcerptCipher([old]).decrypt(rotated)


def test_invalid_key_is_rejected_with_a_clear_message() -> None:
    with pytest.raises(ValueError, match="EXCERPTS_ENCRYPTION_KEYS"):
        ExcerptCipher(["not-a-key"])


@pytest.mark.db
async def test_column_is_encrypted_in_postgres_and_transparent_to_the_orm() -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    repo_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO repositories (id, github_id, full_name, installation_id,"
                    " default_branch) VALUES (:id, :gid, 'o/crypto', 1, 'main')"
                ),
                {"id": repo_id, "gid": repo_id.int % 2**62},
            )
            session.add(AgentSession(repository_id=repo_id, commit_sha="a" * 40, excerpts=EXCERPTS))
        async with factory() as session:
            raw = await session.scalar(
                text("SELECT excerpts::text FROM agent_sessions WHERE repository_id = :r"),
                {"r": repo_id},
            )
            assert raw is not None and "implants" not in raw and '"ct"' in raw
            loaded = (
                await session.execute(
                    AgentSession.__table__.select().where(AgentSession.repository_id == repo_id)
                )
            ).one()
            assert loaded.excerpts == EXCERPTS

            set_cipher(ExcerptCipher([generate_key()]))  # wrong key
            with pytest.raises(DecryptionError):
                (
                    await session.execute(
                        AgentSession.__table__.select().where(AgentSession.repository_id == repo_id)
                    )
                ).one()
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM agent_sessions WHERE repository_id = :r"), {"r": repo_id}
            )
            await session.execute(text("DELETE FROM repositories WHERE id = :r"), {"r": repo_id})
        await engine.dispose()


@pytest.mark.db
async def test_data_migration_encrypts_plaintext_rows_and_is_idempotent() -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    repo_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO repositories (id, github_id, full_name, installation_id,"
                    " default_branch) VALUES (:id, :gid, 'o/legacy', 1, 'main')"
                ),
                {"id": repo_id, "gid": repo_id.int % 2**62},
            )
            await session.execute(
                text(
                    "INSERT INTO agent_sessions (repository_id, commit_sha, excerpts)"
                    " VALUES (:r, 'b', CAST(:e AS jsonb))"
                ),
                {"r": repo_id, "e": json.dumps(EXCERPTS)},
            )
        cipher = ExcerptCipher([generate_key()])
        async with engine.begin() as connection:
            await connection.run_sync(lambda c: encrypt_existing_rows(c, cipher=cipher))
        async with engine.begin() as connection:  # second run only rotates
            await connection.run_sync(lambda c: encrypt_existing_rows(c, cipher=cipher))
        async with factory() as session:
            raw = await session.scalar(
                text("SELECT excerpts FROM agent_sessions WHERE repository_id = :r"), {"r": repo_id}
            )
        assert is_envelope(raw) and cipher.decrypt(raw) == EXCERPTS
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM agent_sessions WHERE repository_id = :r"), {"r": repo_id}
            )
            await session.execute(text("DELETE FROM repositories WHERE id = :r"), {"r": repo_id})
        await engine.dispose()
