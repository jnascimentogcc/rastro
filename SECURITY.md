# Security

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting instead: on the
repository page choose *Security* > *Report a vulnerability*. Include what you found, how to reproduce it and
which version or commit you tested. You will get an answer as soon as the maintainer can read it; this is a small
project, so there is no guaranteed response time.

## What Rastro handles

Rastro reads parts of coding-agent conversations, which can contain private information. What it does about that:

- **On the developer machine**, text is scrubbed of secrets (own patterns plus the `detect-secrets` detectors)
  before upload. Redaction is pattern based, not a guarantee: treat it as a safety net, not as permission to put
  secrets in a conversation.
- **On the server**, conversation excerpts are encrypted at rest with Fernet when `EXCERPTS_ENCRYPTION_KEYS` is set.
  Without a key they are stored in plain text, which is acceptable only for local trials.
- **Original quotes** from conversations are never shown in pull request comments, to other users or to the agent.
- **Tokens**: each repository has its own bearer token, stored hashed on the server and in the system keychain on
  the developer machine.
- **Pull request comments** of Rastro show rule statements and reasons. In a public repository anyone can read them.

How these parts work is described in [docs/reference.md](docs/reference.md).
