# Rastro

Command line for [Rastro](https://github.com/myllmi/rastro): captures the intent behind
AI-generated commits from coding-agent sessions (Claude Code) and queries the business rules
extracted from them.

```bash
pipx install rastro        # or: uvx rastro init
cd your-repo
rastro init --api-url https://your-rastro-server
```

`rastro init` installs a git `post-commit` hook that runs `rastro capture`; make sure `rastro`
is on the `PATH` git uses (`pipx ensurepath` / `uv tool update-shell`).
