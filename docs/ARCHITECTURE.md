# Architecture

`cli` owns command parsing and orchestration. `config` resolves paths and TOML profiles. `parser` fetches and normalizes public vacancy pages. `scoring` is pure and shared by `plan` and `apply`. `storage` owns SQLite state and idempotent imports. `llm` is opt-in and lazy. `session` and `autoapply` import Playwright only inside browser commands. `analytics` reads local history without network access.

## Optional LLM + admin layer

`screen` is an opt-in second pass after the mechanical `scan`→score filter: it sends each candidate's description to an OpenAI-compatible model (aitunnel, default `gpt-5-mini`) and returns a structured FIT/MAYBE/SKIP verdict with `fit_score` and reason. The system prompt is a two-step check (hard stop-factors, then a track rubric); the per-vacancy cache key includes the rubric so editing criteria invalidates it. It never applies and never touches the session; it writes a verdict report and, optionally, an accepted snapshot for `apply`. `letters` drafts an individual cover letter per vacancy from allowlisted profile facts. `pacing` computes humanized apply delays. `balance` reads the aitunnel balance. The API key comes from `AITUNNEL_API_KEY`, `private/config/aitunnel.key`, or the admin settings file — never the repository.

`admin` serves a dependency-free localhost web UI (`http.server`) that reads local artifacts and launches `applypilot` subcommands as subprocesses. Tracks are config-driven from `private/config/tracks.toml` (a track = resume + search config + rubric type), so the number of tracks is not hard-coded. Small JSON sets under the data dir hold per-vacancy user state — `manual-applied.json`, `viewed.json`, `bad.json` — which filter the pool and the apply queue; `bad` vacancies export to Markdown for manual prompt tuning. Real sending stays gated on `reviewed = true` plus an explicit confirmation. `packaging/applypilot-watch.*` provides a systemd user timer for periodic scan+screen (applying stays manual).

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
