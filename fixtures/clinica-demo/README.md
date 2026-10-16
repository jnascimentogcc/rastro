# clinica-demo

A small TypeScript appointment-booking app, told as 15 commits with simulated Claude Code
sessions. It is the ground truth for measuring rule extraction (spec sections 2 and 12).

Each `commits/NN-slug/` holds:

- `files/` - the new full content of every file the commit adds or changes (the tree is cumulative);
- `commit.json` - the commit message, the user requests of the session (`session`) and the
  business rules the commit plants (`expected`, empty for technical-only commits).

What is planted:

| Commit | Planted |
|---|---|
| 01 | Implants not on Friday afternoon, **with** a reason (create) |
| 02 | 65+ discount, **no** reason (create, `rationale` must be null) |
| 03 | Same logic copy-pasted into another file (must **update** rule 01, not create a new one) |
| 04 | Saturday added to the rule (update) |
| 05, 08, 09, 10 | Cancellation window, appointment durations, guardian for minors, insurance copay - all with a reason |
| 06, 11 | Daily limit, reminder time - no reason |
| 07 | Emergencies bypass the daily limit (update of 06) |
| 12-15 | Technical only: refactor, formatting, logging/error handling, rename. **Must not** produce rules |

Expected entries: `match` is a list of keyword groups (every group needs one of its words in the
extracted statement); `rationale` is `null` (the extractor must not invent one) or
`{"any": [...]}`; `updates` names the earlier rule an update must reuse; `symbols` and `quote`
are only used by the offline "oracle" run that checks the fixture itself.

Run the evaluation with a real model:

```bash
uv run --env-file .env python -m rastro_worker.evaluate fixtures/clinica-demo
```

It needs `OPENAI_API_KEY`, `LLM_MODEL` and `EMBEDDING_MODEL`, the compose Postgres with migrations
applied, and prints recall, rationale accuracy, update accuracy, false-positive commits and the
estimated cost. The success criterion is at least 80% of the planted rules extracted correctly.
`--oracle` replays the ground truth instead of calling a model, to check the harness.
