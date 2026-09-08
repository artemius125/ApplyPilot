# Architecture

`cli` owns command parsing and orchestration. `config` resolves paths and TOML profiles. `parser` fetches and normalizes public vacancy pages. `scoring` is pure and shared by `plan` and `apply`. `storage` owns SQLite state and idempotent imports. `llm` is opt-in and lazy. `session` and `autoapply` import Playwright only inside browser commands. `analytics` reads local history without network access.

The normal read-only flow is:

```text
scan -> immutable JSON snapshot -> plan/dry-run (read-only) -> explicit apply --run -> SQLite history -> analytics
```

The JSON snapshot is retained as an immutable input for a run. A failed scan does not replace the last successful snapshot. The SQLite journal is the source of truth for deduplication and ambiguous outcomes.

`inspect` is a separate read-only Playwright branch: it opens a private context, reads the resume page and at most three vacancy pages, and never calls `click`, `fill`, `submit` or page-evaluated JavaScript. `sync` reads negotiation statuses without opening chats or fetching messages; an error records a failed sync snapshot while preserving the last successful statuses.
