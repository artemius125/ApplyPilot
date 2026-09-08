from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .analytics import report
from .config import (
    AppConfig,
    ConfigError,
    effective_search,
    ensure_data_dirs,
    search_groups,
    search_origins,
    validate_search,
)
from .cover_letters import letter_mode, load_letter_profile, render_template
from .llm import generate
from .parser import ScanSegment, enrich_items, load_items, save_snapshot, scan_many
from .presets import ROLE_PRESETS
from .quality import run_benchmark
from .review import write_review
from .scoring import (
    choose_resume,
    evaluate_search_filter,
    filter_candidates,
    prioritize_for_enrichment,
)
from .session import check_session, login, save_state, validate_state
from .storage import Store
from .templates import create_template, list_templates

PRESET_CHOICES = tuple(ROLE_PRESETS)
LOGGER = logging.getLogger(__name__)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", help="runtime data directory")
    parser.add_argument("--profile", help="private TOML profile")
    parser.add_argument("--search", help="TOML search configuration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="applypilot", description="Local HH.ru application workflow")
    parser.add_argument("--version", action="version", version=__version__)
    _common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check local setup")
    doctor.add_argument("--online", action="store_true", help="perform a small public network check")

    sub.add_parser("login", help="open an isolated browser for manual login")
    session = sub.add_parser("session", help="inspect the saved session")
    session.add_argument("action", choices=["check"])

    scan_cmd = sub.add_parser("scan", help="fetch a vacancy snapshot")
    scan_cmd.add_argument("--query")
    scan_cmd.add_argument("--add-query", action="append",
                          help="append a query without replacing preset queries")
    scan_cmd.add_argument("--area", type=int, action="append")
    scan_cmd.add_argument("--page", type=int)
    scan_cmd.add_argument("--pages", type=int)
    scan_cmd.add_argument("--request-budget", type=int,
                          help="maximum search HTTP requests; overrides TOML")
    scan_cmd.add_argument("--days", type=int)
    scan_cmd.add_argument("--remote", action=argparse.BooleanOptionalAction, default=None)
    scan_cmd.add_argument("--preset", choices=PRESET_CHOICES)
    scan_cmd.add_argument("--details-limit", type=int)
    scan_cmd.add_argument("--salary-from", type=int)
    scan_cmd.add_argument("--salary-currency")
    scan_cmd.add_argument("--salary-missing", choices=("include", "exclude", "only"))
    scan_cmd.add_argument("--experience", action="append")
    scan_cmd.add_argument("--work-format", action="append")

    for name, help_text in (("plan", "select candidates offline"), ("apply", "prepare or send applications")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--input", required=True, type=Path)
        cmd.add_argument("--limit", type=int, help="maximum selected vacancies")
        cmd.add_argument("--min-score", type=int)
        cmd.add_argument("--rescore", action="store_true",
                         help="recalculate legacy snapshots instead of preserving historical score")
        cmd.add_argument("--skip-security", action="store_true")
        if name == "apply":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--run", action="store_true")
            cmd.add_argument(
                "--target-success", type=int,
                help="stop after this many confirmed successes; --limit is the candidate ceiling",
            )
        else:
            cmd.add_argument("--output", type=Path, help="private JSON plan output")
        cmd.add_argument("--preset", choices=PRESET_CHOICES)

    inspect_cmd = sub.add_parser("inspect", help="read vacancy pages without actions")
    inspect_cmd.add_argument("--input", type=Path)
    inspect_cmd.add_argument("--limit", type=int, default=3)
    inspect_cmd.add_argument("--page-timeout", type=float, default=10.0,
                             help="per-page read-only deadline in seconds")
    inspect_cmd.add_argument("--selected", action="store_true", help="inspect top confirmed candidates only")
    inspect_cmd.add_argument("--resumes", action="store_true", help="read available HH resume titles only")
    inspect_cmd.add_argument("--preset", choices=PRESET_CHOICES)
    inspect_cmd.add_argument("--min-score", type=int)

    llm = sub.add_parser("llm", help="LLM utilities")
    llm_sub = llm.add_subparsers(dest="llm_action", required=True)
    preview = llm_sub.add_parser("preview")
    preview.add_argument("--input", required=True, type=Path)
    preview.add_argument("--id", required=True)
    letter = sub.add_parser("letter", help="preview a template or LLM cover letter")
    letter_sub = letter.add_subparsers(dest="letter_action", required=True)
    letter_preview = letter_sub.add_parser("preview")
    letter_preview.add_argument("--input", required=True, type=Path)
    letter_preview.add_argument("--id", required=True)
    rerank = llm_sub.add_parser("rerank", help="explicitly run bounded optional LLM reranking")
    rerank.add_argument("--input", required=True, type=Path)
    rerank.add_argument("--model", required=True)
    rerank.add_argument("--limit", type=int, default=20)
    rerank.add_argument("--enable", action="store_true", help="confirm the external LLM request")

    history = sub.add_parser("history", help="legacy history utilities")
    history_sub = history.add_subparsers(dest="history_action", required=True)
    imp = history_sub.add_parser("import")
    imp.add_argument("--source", required=True, type=Path)
    history_sub.add_parser("reconcile").add_argument("--input", required=True, type=Path)
    sync_cmd = sub.add_parser("sync", help="read negotiation statuses without messages")
    sync_cmd.add_argument("--pages", type=int,
                          help="maximum negotiation pages; omitted means fetch until HH returns an empty page")
    sync_cmd.add_argument("--output", type=Path, help="private JSON sync report")
    analytics_cmd = sub.add_parser("analytics", help="show local application counts")
    analytics_cmd.add_argument("--output", type=Path, help="private analytics report")
    benchmark = sub.add_parser("benchmark", help="run offline scanner quality benchmark")
    benchmark.add_argument("--suite", default="tech-roles")
    benchmark.add_argument("--control-only", action="store_true")
    review = sub.add_parser("review", help="write a local HTML vacancy review")
    review.add_argument("--input", required=True, type=Path)
    review.add_argument("--top", type=int, default=20)
    review.add_argument("--output", type=Path)
    review.add_argument("--preset", choices=PRESET_CHOICES)
    config_cmd = sub.add_parser("config", help="inspect effective configuration")
    config_sub = config_cmd.add_subparsers(dest="config_action", required=True)
    config_show = config_sub.add_parser("show", help="show effective search settings")
    config_show.add_argument("--preset", choices=PRESET_CHOICES)
    templates = sub.add_parser("templates", help="show or create public search templates")
    templates_sub = templates.add_subparsers(dest="templates_action", required=True)
    templates_sub.add_parser("list", help="list available templates")
    template_init = templates_sub.add_parser("init", help="create a new template without overwriting")
    template_init.add_argument("--name", required=True, choices=list_templates())
    template_init.add_argument("--output", required=True, type=Path)
    return parser


def _config(args: argparse.Namespace) -> AppConfig:
    config = AppConfig.discover(data_dir=args.data_dir, profile=args.profile, search=args.search)
    ensure_data_dirs(config)
    # Keep Playwright downloads private by default and use an existing private browser install.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.root / "private" / "browsers"))
    return config


def _profile(config: AppConfig, search: dict | None = None) -> dict:
    profile = config.load_profile()
    if search:
        profile = {**profile, "search": search}
        for key in ("role_terms", "required_role_terms", "title_role_terms", "exclude_titles", "salary", "experience",
                    "only_remote", "work_formats"):
            if key in search:
                profile[key] = search[key]
    return profile


def _account(profile: dict) -> str:
    """Use an explicit private account key, retaining legacy compatibility."""
    return str(profile.get("account") or "default").strip() or "default"


def _default_limit(profile: dict) -> int:
    return int((profile.get("limits") or {}).get("per_run", 5))


def _write_private_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _default_artifact(config: AppConfig, category: str, suffix: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return config.data_dir / category / f"{category}-{stamp}-{uuid.uuid4().hex[:8]}.{suffix}"


def _print_run_summary(store: Store, run_id: str) -> None:
    summary = store.run_summary(run_id)
    if summary is None:
        return
    run = summary["run"]
    counts = summary["counts"]
    print(f"run_id: {run_id}; run_status: {run['status']}")
    for status in ("success", "already_applied", "needs_manual", "unknown", "failed_before_submit",
                   "skipped", "prepared"):
        print(f"{status}: {counts.get(status, 0)}")
    if run["stop_reason"]:
        print(f"stop_reason: {run['stop_reason']}")


def _print_candidates(items: list[dict], profile: dict, limit: int, min_score: int,
                      skip_security: bool, blocked_ids: set[str] | None = None,
                      rescore: bool = False) -> list[dict]:
    candidates = filter_candidates(items, profile, limit, min_score, skip_security, blocked_ids, rescore)
    for index, candidate in enumerate(candidates, 1):
        print(f"{index:>2}. [{candidate.score:>3}] {candidate.name} — {candidate.company}")
        print(f"    id={candidate.id} resume={candidate.resume or '(profile not configured)'}")
        if candidate.reasons:
            print(f"    reasons={', '.join(candidate.reasons)}")
    return [candidate.to_dict() for candidate in candidates]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config(args)

    if args.command == "doctor":
        print(f"root: {config.root}")
        print(f"data: {config.data_dir}")
        print(f"profile: {'present' if config.profile_path.exists() else 'missing'}")
        print("playwright: lazy (install optional browser extra)")
        if args.online:
            try:
                import requests
                response = requests.get("https://hh.ru", timeout=10, headers={"User-Agent": "ApplyPilot/0.1"})
                print(f"network: HTTP {response.status_code}")
            except requests.RequestException as exc:
                print(f"network: error ({exc})")
        return 0

    session_path = config.data_dir / "hh_session.json"
    if args.command == "login":
        login(session_path)
        print(f"saved: {session_path}")
        return 0
    if args.command == "session":
        from .session import check_session
        result = check_session(session_path)
        print(f"session: {result.status} ({result.detail})")
        return 0 if result.status in {"confirmed", "valid-format"} else 1
    if args.command == "scan":
        raw_search = config.load_search()
        try:
            search = effective_search(raw_search, args.preset)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        if args.salary_from is not None or args.salary_currency or args.salary_missing:
            search["salary"] = {**search.get("salary", {})}
            if args.salary_from is not None:
                search["salary"]["from"] = args.salary_from
            if args.salary_currency:
                search["salary"]["currency"] = args.salary_currency
            if args.salary_missing:
                search["salary"]["missing"] = args.salary_missing
        if args.experience is not None:
            search["experience"] = {**search.get("experience", {}), "allowed": args.experience}
        if args.work_format is not None:
            search["work_formats"] = args.work_format
        if args.request_budget is not None:
            search["request_budget"] = args.request_budget
        if args.add_query:
            additions = [value.strip() for value in args.add_query if value.strip()]
            if not additions:
                print("at least one --add-query value must be non-empty", file=sys.stderr)
                return 2
            search["additional_queries"] = list(dict.fromkeys([
                *search.get("additional_queries", []), *additions,
            ]))
        try:
            search = validate_search(search)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        try:
            groups = search_groups(search)
        except ConfigError as exc:
            print(f"invalid search group configuration: {exc}", file=sys.stderr)
            return 2
        all_items: list[dict] = []
        segments = []
        group_queries: list[str] = []
        if args.pages is not None and args.pages < 1:
            print("--pages must be positive", file=sys.stderr)
            return 2
        if args.request_budget is not None and args.request_budget < 1:
            print("--request-budget must be positive", file=sys.stderr)
            return 2
        remaining_requests = int(search["request_budget"])
        page = args.page if args.page is not None else 0
        for group in groups:
            queries = [args.query] if args.query else list(group.get("queries", []))
            if args.add_query and not args.query:
                queries = list(dict.fromkeys([*queries, *search["additional_queries"]]))
            group_queries.extend(queries)
            if not queries:
                continue
            areas = args.area if args.area is not None else [int(value) for value in (group.get("areas") or [113])]
            if remaining_requests <= 0:
                segments.append(ScanSegment(queries[0], areas[0], 0, "truncated", 0,
                                            "search request budget reached"))
                break
            pages = args.pages if args.pages is not None else int(group["max_pages"])
            remote = args.remote if args.remote is not None else bool(group.get("only_remote", False))
            days = args.days if args.days is not None else group.get("days")
            date_from = (datetime.now(UTC) - timedelta(days=int(days))).date().isoformat() if days else None
            group_items, group_segments = scan_many(queries, areas, pages, remote, date_from=date_from,
                                                    start_page=page, pause_seconds=1.0,
                                                    request_budget=remaining_requests)
            remaining_requests -= sum(segment.requests for segment in group_segments)
            group_name = str(group.get("group_name") or "default")
            for item in group_items:
                item["search_group"] = group_name
            all_items.extend(group_items)
            segments.extend(group_segments)
        if not group_queries:
            print("search query is required (or configure private/config/search.toml)", file=sys.stderr)
            return 2
        deduplicated: dict[str, dict] = {}
        for item in all_items:
            vacancy_id = str(item.get("id", ""))
            if not vacancy_id or vacancy_id not in deduplicated:
                deduplicated[vacancy_id] = item
                continue
            existing = deduplicated[vacancy_id]
            existing["query_sources"] = sorted(set(existing.get("query_sources", []))
                                                 | set(item.get("query_sources", [])))
            existing["area_sources"] = sorted(set(existing.get("area_sources", []))
                                                | set(item.get("area_sources", [])))
            existing["search_groups"] = sorted(set(existing.get("search_groups", [existing.get("search_group", "")]))
                                               | {item.get("search_group", "")})
        all_items = list(deduplicated.values())
        group_by_name = {str(group.get("group_name") or "default"): group for group in groups}
        details_limit = (args.details_limit if args.details_limit is not None
                         else int(search.get("details_limit", 100)))
        ranking_profile = _profile(config, search)
        enrichment_candidates = prioritize_for_enrichment(all_items, ranking_profile, details_limit)
        candidate_ids = {candidate.id for candidate in enrichment_candidates}
        enrichment_items = [item for item in all_items if str(item.get("id", "")) in candidate_ids]
        for item in all_items:
            if str(item.get("id", "")) not in candidate_ids:
                item["description_status"] = "provisional"
        enriched, detail_errors = enrich_items(enrichment_items, details_limit, pause_seconds=1.0)
        enriched_ids = {str(item.get("id", "")) for item in enriched}
        all_items = [item for item in all_items if str(item.get("id", "")) in enriched_ids or
                     str(item.get("id", "")) not in candidate_ids]
        for item in all_items:
            group_search = group_by_name.get(str(item.get("search_group", "default")), search)
            decision = evaluate_search_filter(item, group_search)
            item["filter_status"] = decision.status
            item["filter_reasons"] = list(decision.reasons)
            item["filter_fields"] = list(decision.fields)
        errors = [f"{segment.query} area {segment.area}: {segment.error}"
                  for segment in segments if segment.error]
        errors.extend(detail_errors)
        failed = [segment for segment in segments if segment.status in {"failed", "captcha"}]
        truncated = any(segment.status == "truncated" for segment in segments)
        if failed:
            status = "partial"
        elif truncated:
            status = "truncated"
        else:
            status = "ok" if all_items else "empty"
        path = save_snapshot(all_items, config.snapshots_dir, " | ".join(queries), status,
                             "\n".join(errors), segments, args.preset or search.get("preset"))
        print(f"status: {status}; items: {len(all_items)}; segments: {len(segments)}; snapshot: {path}")
        for segment in segments:
            print(f"segment: query={segment.query!r} area={segment.area} pages={segment.pages} "
                  f"requests={segment.requests} status={segment.status} items={segment.items}")
        return 0 if status in {"ok", "empty", "truncated"} else 2
    if args.command in {"plan", "apply"}:
        items = load_items(args.input)
        try:
            search = effective_search(config.load_search(), args.preset)
        except ConfigError as exc:
            print(f"invalid search configuration: {exc}", file=sys.stderr)
            return 2
        store = Store(config.db_path)
        profile = _profile(config, search)
        account = _account(profile)
        limit = args.limit if args.limit is not None else _default_limit(profile)
        if limit < 1:
            print("--limit must be positive", file=sys.stderr)
            return 2
        min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
        selected = _print_candidates(items, profile, limit, min_score,
                                      args.skip_security, store.blocked_ids(account), args.rescore)
        if args.command == "plan":
            output = args.output or _default_artifact(config, "plans", "json")
            _write_private_json(output, {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "account": account,
                "input": str(args.input.resolve()),
                "requested_limit": limit,
                "selected": selected,
            })
            print(f"plan: {output}")
            return 0
        if not args.dry_run and not args.run:
            print("Choose --dry-run or --run", file=sys.stderr)
            return 2
        if args.target_success is not None:
            if args.dry_run:
                print("--target-success requires --run", file=sys.stderr)
                return 2
            if args.target_success < 1 or args.target_success > limit:
                print("--target-success must be positive and no greater than --limit", file=sys.stderr)
                return 2
        run_id = uuid.uuid4().hex
        if args.dry_run:
            return _run_dry(store, selected, run_id, account, args.input, limit)
        return _run_apply(
            config, store, selected, run_id, account, args.input, limit, args.target_success,
        )
    if args.command == "inspect":
        ok, detail = validate_state(session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .inspection import inspect_items, inspect_resumes
        try:
            if args.resumes:
                if args.page_timeout <= 0:
                    print("--page-timeout must be positive", file=sys.stderr)
                    return 2
                print(json.dumps(inspect_resumes(
                    session_path, timeout_ms=round(args.page_timeout * 1000)
                ), ensure_ascii=False, sort_keys=True))
                if args.input is None:
                    return 0
            if args.input is None:
                print("--input is required unless --resumes is used", file=sys.stderr)
                return 2
            if args.limit < 1:
                print("--limit must be positive", file=sys.stderr)
                return 2
            items = load_items(args.input)
            if args.selected:
                try:
                    search = effective_search(config.load_search(), args.preset)
                except ConfigError as exc:
                    print(f"invalid search configuration: {exc}", file=sys.stderr)
                    return 2
                min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
                profile = _profile(config, search)
                items = [candidate.to_dict() for candidate in filter_candidates(
                    items, profile, args.limit, min_score,
                    blocked_ids=Store(config.db_path).blocked_ids(_account(profile)),
                )]
            results = inspect_items(session_path, items, args.limit, round(args.page_timeout * 1000))
        except Exception as exc:  # noqa: BLE001 - a read-only browser failure is reported, never retried
            print(f"inspect: error ({str(exc)[:240]})", file=sys.stderr)
            return 3
        for result in results:
            print(result)
        return 0
    if args.command in {"llm", "letter"}:
        items = load_items(args.input)
        if getattr(args, "llm_action", None) == "rerank":
            if not args.enable:
                print("rerank is disabled by default; pass --enable explicitly", file=sys.stderr)
                return 2
            from .llm import rerank
            try:
                search = effective_search(config.load_search(), None)
            except ConfigError as exc:
                print(f"invalid search configuration: {exc}", file=sys.stderr)
                return 2
            try:
                profile = _profile(config, search)
            except ConfigError as exc:
                print(f"invalid profile: {exc}", file=sys.stderr)
                return 2
            ranked = filter_candidates(items, profile, min(args.limit, 20), int(search.get("min_score", 0)))
            try:
                result, source = rerank([item.to_dict() for item in ranked], profile,
                                        config.data_dir / "llm-cache", args.model,
                                        enabled=True, limit=min(args.limit, 20))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 3
            print(json.dumps({"source": source, "model": args.model, "items": result},
                             ensure_ascii=False, indent=2))
            return 0
        item = next((x for x in items if str(x.get("id", "")) == args.id), None)
        if not item:
            print(f"vacancy id not found: {args.id}", file=sys.stderr)
            return 1
        try:
            text, source = _prepare_cover_letter(config, item, _profile(config))
        except (ConfigError, RuntimeError) as exc:
            print(f"cover letter unavailable: {exc}", file=sys.stderr)
            return 2 if isinstance(exc, ConfigError) else 3
        print(f"source: {source}\n{text}")
        return 0
    if args.command == "history":
        store = Store(config.db_path)
        account = _account(_profile(config))
        if args.history_action == "import":
            paths = [args.source] if args.source.is_file() else sorted(args.source.rglob("apply_log.csv"))
            reports = [store.import_csv(path, account) for path in paths]
            for source, result in zip(paths, reports, strict=True):
                print(
                    f"import: {source}; logical_rows={result.logical_rows}; "
                    f"physical_data_lines={result.physical_data_lines}; events_added={result.events_added}; "
                    f"already_imported={result.already_imported}; sha256={result.source_sha256}"
                )
        else:
            changed = store.reconcile(args.input, account)
            print(f"reconciled: {changed}; unknown attempts were not retried")
        return 0
    if args.command == "sync":
        ok, detail = validate_state(session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .negotiations import SyncError, sync_statuses
        try:
            account = _account(_profile(config))
            if args.pages is not None and args.pages < 1:
                print("--pages must be positive", file=sys.stderr)
                return 2
            sync_store = Store(config.db_path)
            with sync_store.run_lock():
                sync_store.recover_interrupted_runs(account)
                rows = sync_statuses(session_path, sync_store, account=account, max_pages=args.pages)
                reconciled = sync_store.reconcile_unknowns_from_negotiations(account)
                snapshot = sync_store.latest_sync(account)
        except SyncError as exc:
            print(f"sync: error ({exc})", file=sys.stderr)
            return 3
        output = args.output or _default_artifact(config, "reports", "json")
        status = snapshot["status"] if snapshot else "unknown"
        _write_private_json(output, {"account": account, "status": status, "rows": rows,
                                    "reconciled_unknown": reconciled})
        print(f"sync: {status}; {len(rows)} statuses; messages=disabled")
        print(f"reconciled_unknown: {len(reconciled)}"
              + (f" ({','.join(reconciled)})" if reconciled else ""))
        print(f"sync_report: {output}")
        return 0
    if args.command == "analytics":
        account = _account(_profile(config))
        result = report(Store(config.db_path), account)
        output = args.output or _default_artifact(config, "reports", "txt")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result + "\n", encoding="utf-8")
        print(result)
        print(f"analytics_report: {output}")
        return 0
    if args.command == "benchmark":
        try:
            result = run_benchmark(args.suite, args.control_only)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["precision"] >= 0.9 and result["recall"] >= 0.85 else 1
    if args.command == "review":
        search = effective_search(config.load_search(), args.preset)
        output = args.output or config.data_dir / "reports" / "review.html"
        print(f"review: {write_review(load_items(args.input), _profile(config, search), output, args.top)}")
        return 0
    if args.command == "config" and args.config_action == "show":
        raw = config.load_search()
        values = effective_search(raw, args.preset)
        values.pop("profile", None)
        print(json.dumps({"values": values, "origins": search_origins(raw, args.preset)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "templates":
        if args.templates_action == "list":
            print("\n".join(list_templates()))
            return 0
        try:
            print(f"created: {create_template(args.name, args.output)}")
        except (FileExistsError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    return 2


def _run_dry(store: Store, selected: list[dict], run_id: str, account: str,
             input_path: Path, requested_limit: int) -> int:
    """Record an offline, immutable candidate list without creating attempts."""
    with store.run_lock():
        store.start_run(run_id, account, "dry-run", input_path, requested_limit, selected)
        for item in selected:
            note = f"would apply with resume={item.get('resume', '')}"
            store.mark_run_item(run_id, str(item.get("id", "")), "prepared", note)
            print(f"{item.get('id')}: prepared — {note}")
        store.finish_run(run_id, "completed")
    _print_run_summary(store, run_id)
    return 0


def _run_apply(config: AppConfig, store: Store, selected: list[dict], run_id: str,
               account: str, input_path: Path, requested_limit: int,
               target_success: int | None = None) -> int:
    """Execute explicitly requested browser submissions with auditable stop conditions."""
    try:
        profile = _profile(config, effective_search(config.load_search(), None))
        letter_enabled = letter_mode(profile) != "off"
    except ConfigError as exc:
        print(f"invalid search configuration: {exc}", file=sys.stderr)
        return 2
    if profile.get("reviewed") is not True:
        print("profile is not reviewed; real submissions require reviewed = true", file=sys.stderr)
        return 2
    if _account(profile) != account:
        print("profile account changed after candidate selection; rerun dry-run", file=sys.stderr)
        return 2
    state_path = config.data_dir / "hh_session.json"
    session = check_session(state_path)
    if session.status != "confirmed":
        print(f"session unavailable: {session.status} ({session.detail}); run `applypilot login` first",
              file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright

        from .autoapply import apply_one
        from .negotiations import SyncError, sync_statuses
    except ImportError:
        print("browser support missing; install with: pip install -e '.[browser]'", file=sys.stderr)
        return 2
    limits = profile.get("limits", {})
    per_run = int(limits.get("per_run", 5))
    per_day = int(limits.get("per_day", 20))
    if per_run < 1 or per_day < 1:
        print("profile limits must be positive", file=sys.stderr)
        return 2
    timing = profile.get("apply", {}) or {}
    delay_min = max(0.0, float(timing.get("delay_min_seconds", 1)))
    delay_max = max(delay_min, float(timing.get("delay_max_seconds", delay_min)))
    confirmation_timeout = max(0.0, float(timing.get("confirmation_timeout_seconds", 15)))
    run_status = "completed"
    stop_reason = ""
    exit_code = 0
    confirmed_successes = 0
    with store.run_lock():
        store.recover_interrupted_runs(account)
        store.start_run(run_id, account, "apply", input_path, requested_limit, selected)
        browser = None
        context = None
        playwright = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                storage_state=str(state_path), viewport={"width": 1280, "height": 900}
            )
            page = context.new_page()
            for index, item in enumerate(selected, 1):
                preflight_error = _submission_preflight(item, letter_enabled)
                if preflight_error:
                    store.record(item, "needs_manual", preflight_error, run_id, account)
                    print(f"{item.get('id')}: needs_manual — {preflight_error}")
                    continue
                allowed, reason = store.reserve(item, run_id, per_run, per_day, account)
                if not allowed:
                    store.mark_run_item(run_id, str(item.get("id", "")), "skipped", reason)
                    print(f"{item.get('id')}: skipped — {reason}")
                    if reason.startswith(("run budget", "daily budget")):
                        run_status = "budget_exhausted"
                        stop_reason = reason
                        break
                    continue
                cover_letter = ""
                if letter_enabled:
                    try:
                        cover_letter, source = _prepare_cover_letter(config, item, profile)
                    except (Exception, KeyboardInterrupt):
                        store.record(item, "failed_before_submit", "cover letter generation failed",
                                     run_id, account)
                        raise
                    item["cover_letter_source"] = source
                    if source in {"generated", "cache"}:
                        item["llm_source"] = source
                result = apply_one(
                    page, item, str(item.get("resume", "")).strip(), cover_letter,
                    confirmation_timeout_seconds=confirmation_timeout,
                )
                if result.status == "unknown":
                    store.record(item, result.status, result.note, run_id, account)
                    try:
                        save_state(state_path, context.storage_state())
                        sync_statuses(state_path, store, account=account)
                        reconciled = store.reconcile_unknowns_from_negotiations(account)
                    except SyncError as exc:
                        LOGGER.warning("Could not confirm vacancy %s through sync: %s", item.get("id"), exc)
                        reconciled = []
                    vacancy_id = str(item.get("id", ""))
                    if vacancy_id in reconciled:
                        print(f"{vacancy_id}: success — confirmed by HH negotiations")
                        confirmed_successes += 1
                    else:
                        print(f"{vacancy_id}: unknown — {result.note}")
                        run_status = "stopped_unknown"
                        stop_reason = f"unknown result for vacancy {vacancy_id}"
                        exit_code = 3
                        break
                else:
                    store.record(item, result.status, result.note, run_id, account)
                    print(f"{item.get('id')}: {result.status} — {result.note}")
                    if result.status == "success":
                        confirmed_successes += 1
                if target_success is not None and confirmed_successes >= target_success:
                    run_status = "target_reached"
                    stop_reason = f"confirmed success target reached ({confirmed_successes})"
                    break
                if index < len(selected) and delay_max:
                    time.sleep(random.uniform(delay_min, delay_max))
        except KeyboardInterrupt:
            run_status = "interrupted"
            stop_reason = "run interrupted"
            exit_code = 130
        except Exception as exc:  # noqa: BLE001 - journal the run even if browser setup fails
            run_status = "failed"
            stop_reason = f"runner error: {str(exc)[:160]}"
            exit_code = 3
            print(stop_reason, file=sys.stderr)
        finally:
            try:
                if context is not None:
                    save_state(state_path, context.storage_state())
            except Exception as exc:  # noqa: BLE001 - session persistence must not leave a run unfinished
                if run_status == "completed":
                    run_status = "failed"
                    stop_reason = f"session persistence error: {str(exc)[:160]}"
                    exit_code = 3
                    print(stop_reason, file=sys.stderr)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        LOGGER.debug("Could not close browser context", exc_info=True)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        LOGGER.debug("Could not close browser", exc_info=True)
                if playwright is not None:
                    try:
                        playwright.stop()
                    except Exception:
                        LOGGER.debug("Could not stop Playwright", exc_info=True)
            if target_success is not None and run_status == "completed":
                run_status = "completed_shortfall"
                stop_reason = (
                    f"confirmed success target not reached ({confirmed_successes}/{target_success})"
                )
                exit_code = 4
            store.finish_run(run_id, run_status, stop_reason)
            store.recover_interrupted_runs(account)
    _print_run_summary(store, run_id)
    return exit_code


def _prepare_cover_letter(config: AppConfig, item: dict, profile: dict) -> tuple[str, str]:
    """Prepare the same letter for preview and apply without silently enabling a fallback."""
    mode = letter_mode(profile)
    if mode == "off":
        return "", "disabled"
    prepared = load_letter_profile(profile, config.profile_path)
    vacancy = dict(item)
    if not vacancy.get("resume"):
        vacancy["resume"] = choose_resume(str(vacancy.get("name", "")), prepared)
    if mode == "template":
        return render_template(vacancy, prepared), "template"
    settings = prepared.get("cover_letter", {})
    fallback = render_template(vacancy, prepared) if settings.get("fallback_to_template", False) else None
    llm = prepared.get("llm", {})
    if not isinstance(llm, dict) or not isinstance(llm.get("model", ""), str):
        raise ConfigError("llm.model must be a string")
    try:
        text, source = generate(vacancy, prepared, config.data_dir / "llm-cache",
                                str(llm.get("model", "")), enabled=True, required=True)
        if not text.strip():
            raise RuntimeError("provider returned an empty cover letter")
        return text, source
    except RuntimeError:
        if fallback is not None:
            return fallback, "template_fallback"
        raise


def _submission_preflight(item: dict, letter_enabled: bool) -> str:
    """Return a manual-review reason before consuming an apply reservation."""
    from .autoapply import allowed_hh_url

    if not str(item.get("resume", "")).strip():
        return "resume is not selected"
    if not allowed_hh_url(str(item.get("url", ""))):
        return "URL is not an allowed HH hostname"
    requires_letter = any(item.get(key) for key in (
        "cover_letter_required", "requires_cover_letter", "letter_required",
    ))
    if requires_letter and not letter_enabled:
        return "required cover letter is missing"
    return ""
