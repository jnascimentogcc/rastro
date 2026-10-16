import pytest

from rastro_session_parsers import REDACTED, redact


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 40,
        "sk-ant-" + "c" * 30,
        "xoxb-1234567890-abcdefghij",
        "AIza" + "d" * 35,
        "eyJhbGciOiJI.eyJzdWIiOiIx.abcdefghijkl",
        "Bearer abcdefghijklmnopqrstuvwxyz0123",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_known_token_formats_are_removed(secret: str) -> None:
    assert secret not in redact(f"before {secret} after")


def test_url_password_is_removed_but_host_is_kept() -> None:
    assert redact("postgres://admin:s3cretpass@db.internal:5432/x") == (
        f"postgres://admin:{REDACTED}@db.internal:5432/x"
    )


def test_sensitive_assignments_keep_the_name() -> None:
    assert redact("DATABASE_PASSWORD=hunter22") == f"DATABASE_PASSWORD={REDACTED}"
    assert redact('{"api_key": "abc123xyz"}') == f'{{"api_key": "{REDACTED}"}}'


def test_normal_text_is_untouched() -> None:
    text = (
        "Implants cannot be booked on Friday afternoon. The author is Dr. Mendes (token limit 3)."
    )
    assert redact(text) == text
