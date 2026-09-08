# Architecture

`cli` owns command parsing and orchestration. `config` resolves paths and TOML profiles. `parser` fetches and normalizes public vacancy pages. `scoring` is pure and shared by `plan` and `apply`. `storage` owns SQLite state and idempotent imports. `llm` is opt-in and lazy. `session` and `autoapply` import Playwright only inside browser commands. `analytics` reads local history without network access.

The normal read-only flow is:

```text
scan -> immutable JSON snapshot -> private plan/dry-run run -> explicit apply --run -> SQLite history -> analytics
```

The JSON snapshot is retained as an immutable input for a run. A failed scan does not replace the last successful snapshot. The SQLite journal is the source of truth for deduplication and ambiguous outcomes.

`inspect` is a separate read-only Playwright branch: it opens a private context, reads the resume page and a requested bounded vacancy list, and never calls `click`, `fill`, `submit` or page-evaluated JavaScript. `sync` reads negotiation statuses without opening chats or fetching messages; an error records a failed sync snapshot while preserving the last successful statuses.

Search request accounting is performed immediately before each HTTP call, including retries
and HH redirects. Per-segment request counts carry the remaining budget across search groups.
Description enrichment has its own separate candidate limit.

The journal run lock protects application execution and explicit synchronization. On acquiring
it, a real run or sync recovers abandoned `submitting` attempts as `unknown`; recovery never
releases an ambiguous vacancy for automatic retry. A known failure before browser submission
is recorded separately as `failed_before_submit`. Full negotiation snapshots replace only the
current account's status rows, while truncated snapshots upsert the pages that were fetched.
Reconciliation accepts only known HH statuses fetched at or after the ambiguous attempt.

`cover_letters` resolves the off/template/llm modes, loads explicitly configured UTF-8
TXT/Markdown files relative to the profile, and renders strict offline templates.
`config.professional_context` validates and allowlists resume text, experience and projects.
The CLI shares `_prepare_cover_letter` between preview and real submission; dry runs skip it.
Only cover-letter generation adds the professional context to the OpenRouter prompt;
reranking retains its existing minimal profile. The generated-letter cache includes loaded
resume content and the selected resume name. Empty completions fail; an explicitly enabled
template fallback handles provider failures after validating the template before any request.
