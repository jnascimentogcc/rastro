# Rastro

**Rastro keeps the *why* behind AI-generated code.**

When a coding agent writes the code, the reason for a business rule ("implants are never booked on Friday
afternoons, because the surgeon is away") lives in a chat that nobody reads again. A few weeks later someone, or
another agent, "simplifies" the code and the rule silently disappears. Rastro captures that intent at commit time,
turns it into rules anchored to the code, and brings it back when it matters: on the pull request that is about to
break a rule, and in front of the agent before it edits a file.

## What it does

1. **Captures** the part of your Claude Code conversation that produced each commit (a git `post-commit` hook;
   secrets are removed on your machine before anything is sent, and it never blocks or fails a commit).
2. **Extracts rules** when a pull request is merged: a statement, the reason (only if the conversation actually
   gave one, never invented), and the functions it applies to. People confirm, correct or reject them.
3. **Warns on pull requests** with one comment, edited in place, that says which recorded rule a change may break
   and proposes the rules the PR itself introduces, ready to confirm with a tick or `/rastro ok`.
4. **Tells the agent** which rules exist for a file before it reads or edits it (a hook and a read-only MCP
   server), so it can ask you first.
5. **Reports its own health** (`rastro health`): how much of the knowledge is recorded, explained and confirmed.

## Using it

Once the CLI is connected to a repository (see [Installation](#installation)):

```bash
rastro rules                                 # active rules of this repository, with the reason behind each
rastro rules --file src/agenda.ts            # rules anchored in a file or folder
rastro rules --search "can implants be booked on fridays"    # semantic search
rastro rule 4f3e2d1c                         # one rule: its code, its history and who confirmed it
```

Review what was extracted:

```bash
rastro rules --unvalidated                   # rules nobody has confirmed yet
rastro rule 4f3e2d1c --confirm               # "this is right as written"
rastro rule 4f3e2d1c --correct "New statement" --why "Because ..."
rastro rule 4f3e2d1c --why "Because ..."     # add the reason the conversation never gave
rastro rule 4f3e2d1c --reject --note "not a real rule"
```

On a pull request, write `/rastro ok <id>`, `/rastro reject <id>`, `/rastro correct <id>: <text>` or
`/rastro why <id> <reason>` in a comment (or tick the box next to a proposal). Only people with write access to the
repository are heard.

Let the agent see the rules before it edits:

```bash
rastro agent install            # hooks for Claude Code (local, not committed) + the read-only MCP server
rastro agent uninstall
```

Look at the state of the knowledge base:

```bash
rastro health                   # recorded, explained and confirmed, with numerator and denominator
rastro health --zones --queue 10    # per directory, and what to confirm first
rastro health --history         # how it changed day by day
```

Every figure in the report shows what it is out of, says `n/a` with the reason when it cannot be known, and says
nothing about individual people. See [docs/reference.md](docs/reference.md) for how each part works.

## Installation

Rastro has two parts, installed by two different people:

- **The CLI** (`rastro-cli`, on PyPI), for every developer: `pipx install rastro-cli` or
  `uv tool install rastro-cli`.
- **The server** (API, worker and PostgreSQL with pgvector, plus a GitHub App), for one person per team, with
  Docker Compose.

The step-by-step, for both, is in **[INSTALLATION.md](INSTALLATION.md)**.

## What it needs and what it does not do

- Languages: **TypeScript and JavaScript** (the code is analysed with tree-sitter).
- Coding agent: **Claude Code**. Source hosting: **GitHub**.
- The server needs an **OpenAI** API key for extraction, impact analysis and embeddings; without one those jobs are
  skipped. Model access goes through one interface (`packages/llm`), so other providers can be added.
- There is no web interface: you use the CLI and the pull request comments.
- The conversation excerpts that Rastro stores are encrypted at rest when you set `EXCERPTS_ENCRYPTION_KEYS`, and
  the original quotes are never shown to other people or to the agent. See [SECURITY.md](SECURITY.md).

## Status

Early (alpha). The capture, the GitHub App, rule extraction, pull request analysis, human review, the agent hooks
and the health report are built and tested, and evaluated on one real repository and on a demonstration project
(`fixtures/clinica-demo`). Results of each stage are in [docs/](docs/). Expect rough edges and breaking changes.

## Documentation

- [INSTALLATION.md](INSTALLATION.md): install the CLI and the server.
- [docs/reference.md](docs/reference.md): how the server and the CLI behave, part by part.
- [docs/spec-rastro-mvp.md](docs/spec-rastro-mvp.md) and the other specs and evaluation results in [docs/](docs/)
  (some are in Portuguese).
- [CONTRIBUTING.md](CONTRIBUTING.md): working on the code.

## License

[MIT](LICENSE).
