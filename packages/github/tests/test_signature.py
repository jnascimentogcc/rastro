from rastro_github import sign, verify_signature

SECRET = "s3cret"
BODY = b'{"zen": "Keep it logically awesome."}'


def test_valid_signature_is_accepted() -> None:
    assert verify_signature(SECRET, BODY, sign(SECRET, BODY))


def test_known_github_example() -> None:
    # Example from GitHub's documentation for validating webhook deliveries.
    secret = "It's a Secret to Everybody"
    body = b"Hello, World!"
    header = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"

    assert verify_signature(secret, body, header)


def test_tampered_body_is_rejected() -> None:
    assert not verify_signature(SECRET, BODY + b" ", sign(SECRET, BODY))


def test_wrong_secret_is_rejected() -> None:
    assert not verify_signature("other", BODY, sign(SECRET, BODY))


def test_missing_or_malformed_header_is_rejected() -> None:
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, "")
    assert not verify_signature(SECRET, BODY, "sha1=abc")
    assert not verify_signature(SECRET, BODY, sign(SECRET, BODY).removeprefix("sha256="))
