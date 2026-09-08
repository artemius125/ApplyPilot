from __future__ import annotations

import argparse
import json
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
from .llm import generate
from .parser import enrich_items, load_items, save_snapshot, scan_many
from .presets import ROLE_PRESETS
from .quality import run_benchmark
from .review import write_review
from .scoring import evaluate_search_filter, filter_candidates, prioritize_for_enrichment
from .session import check_session, login, save_state, validate_state
from .storage import Store
from .templates import create_template, list_templates

PRESET_CHOICES = tuple(ROLE_PRESETS)


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
        cmd.add_argument("--limit", type=int, default=5)
        cmd.add_argument("--min-score", type=int)
        cmd.add_argument("--rescore", action="store_true",
                         help="recalculate legacy snapshots instead of preserving historical score")
        cmd.add_argument("--skip-security", action="store_true")
        if name == "apply":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--run", action="store_true")
        cmd.add_argument("--preset", choices=PRESET_CHOICES)

    inspect_cmd = sub.add_parser("inspect", help="read vacancy pages without actions")
    inspect_cmd.add_argument("--input", required=True, type=Path)
    inspect_cmd.add_argument("--limit", type=int, default=3)
    inspect_cmd.add_argument("--selected", action="store_true", help="inspect top confirmed candidates only")
    inspect_cmd.add_argument("--preset", choices=PRESET_CHOICES)
    inspect_cmd.add_argument("--min-score", type=int)

    llm = sub.add_parser("llm", help="LLM utilities")
    llm_sub = llm.add_subparsers(dest="llm_action", required=True)
    preview = llm_sub.add_parser("preview")
    preview.add_argument("--input", required=True, type=Path)
    preview.add_argument("--id", required=True)
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
    sub.add_parser("sync", help="show status sync availability")
    sub.add_parser("analytics", help="show local application counts")
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
        remaining_requests = int(search.get("max_queries", 40))
        page = args.page if args.page is not None else 0
        for group in groups:
            if remaining_requests <= 0:
                break
            queries = [args.query] if args.query else list(group.get("queries", []))
            if args.add_query and not args.query:
                queries = list(dict.fromkeys([*queries, *search["additional_queries"]]))
            group_queries.extend(queries)
            if not queries:
                continue
            areas = args.area if args.area is not None else [int(value) for value in (group.get("areas") or [113])]
            pages = args.pages if args.pages is not None else int(group.get("max_pages", 2))
            pages = max(1, min(pages, 20))
            remote = args.remote if args.remote is not None else bool(group.get("only_remote", False))
            days = args.days if args.days is not None else group.get("days")
            date_from = (datetime.now(UTC) - timedelta(days=int(days))).date().isoformat() if days else None
            group_items, group_segments = scan_many(queries, areas, pages, remote, date_from=date_from,
                                                    start_page=page, pause_seconds=1.0,
                                                    request_budget=remaining_requests)
            remaining_requests -= sum(segment.pages for segment in group_segments)
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
                  f"status={segment.status} items={segment.items}")
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
        account = str(profile.get("account", "default"))
        min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
        selected = _print_candidates(items, profile, args.limit, min_score,
                                      args.skip_security, set(store.read_statuses(account)), args.rescore)
        if args.command == "plan":
            return 0
        if not args.dry_run and not args.run:
            print("Choose --dry-run or --run", file=sys.stderr)
            return 2
        run_id = uuid.uuid4().hex
        if args.dry_run:
            return 0
        return _run_apply(config, store, selected, run_id)
    if args.command == "inspect":
        ok, detail = validate_state(session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .inspection import inspect_items
        items = load_items(args.input)
        if args.selected:
            try:
                search = effective_search(config.load_search(), args.preset)
            except ConfigError as exc:
                print(f"invalid search configuration: {exc}", file=sys.stderr)
                return 2
            min_score = args.min_score if args.min_score is not None else int(search.get("min_score", 0))
            items = [candidate.to_dict() for candidate in filter_candidates(
                items, _profile(config, search), min(args.limit, 3), min_score
            )]
        results = inspect_items(session_path, items, min(args.limit, 3))
        for result in results:
            print(result)
        return 0
    if args.command == "llm":
        items = load_items(args.input)
        if args.llm_action == "rerank":
            if not args.enable:
                print("rerank is disabled by default; pass --enable explicitly", file=sys.stderr)
                return 2
            from .llm import rerank
            try:
                search = effective_search(config.load_search(), None)
            except ConfigError as exc:
                print(f"invalid search configuration: {exc}", file=sys.stderr)
                return 2
            profile = _profile(config, search)
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
        profile = _profile(config)
        model = str(profile.get("llm", {}).get("model", "openrouter/free"))
        text, source = generate(item, profile, config.data_dir / "llm-cache", model,
                                enabled=bool(profile.get("llm", {}).get("enabled", False)))
        print(f"source: {source}\n{text}")
        return 0
    if args.command == "history":
        store = Store(config.db_path)
        if args.history_action == "import":
            paths = [args.source] if args.source.is_file() else sorted(args.source.rglob("apply_log.csv"))
            total = sum(store.import_csv(path) for path in paths)
            print(f"imported: {total}")
        else:
            changed = store.reconcile(args.input)
            print(f"reconciled: {changed}; unknown attempts were not retried")
        return 0
    if args.command == "sync":
        ok, detail = validate_state(session_path)
        if not ok:
            print(f"session unavailable: {detail}; run `applypilot login` first", file=sys.stderr)
            return 2
        from .negotiations import SyncError, sync_statuses
        try:
            rows = sync_statuses(session_path, Store(config.db_path), max_pages=1)
        except SyncError as exc:
            print(f"sync: error ({exc})", file=sys.stderr)
            return 3
        print(f"sync: {len(rows)} statuses; messages=disabled")
        return 0
    if args.command == "analytics":
        print(report(Store(config.db_path)))
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


def _run_apply(config: AppConfig, store: Store, selected: list[dict], run_id: str) -> int:
    """Execute explicitly requested browser submissions with bounded state transitions."""
    state_path = config.data_dir / "hh_session.json"
    session = check_session(state_path)
    if session.status != "confirmed":
        print(f"session unavailable: {session.status} ({session.detail}); run `applypilot login` first",
              file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright

        from .autoapply import apply_one
    except ImportError:
        print("browser support missing; install with: pip install -e '.[browser]'", file=sys.stderr)
        return 2
    try:
        profile = _profile(config, effective_search(config.load_search(), None))
    except ConfigError as exc:
        print(f"invalid search configuration: {exc}", file=sys.stderr)
        return 2
    if profile.get("reviewed") is not True:
        print("profile is not reviewed; real submissions require reviewed = true", file=sys.stderr)
        return 2
    limits = profile.get("limits", {})
    per_run = int(limits.get("per_run", 5))
    per_day = int(limits.get("per_day", 20))
    account = str(profile.get("account", "default"))
    candidates = selected
    with store.run_lock(), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = None
        try:
            context = browser.new_context(storage_state=str(state_path), viewport={"width": 1280, "height": 900})
            page = context.new_page()
            for item in candidates:
                llm_cfg = profile.get("llm", {})
                preflight_error = _submission_preflight(item, bool(llm_cfg.get("enabled")))
                if preflight_error:
                    note = preflight_error
                    store.record(item, "needs_manual", note, run_id, account)
                    print(f"{item.get('id')}: needs_manual — {note}")
                    continue
                resume = str(item.get("resume", "")).strip()
                allowed, reason = store.reserve(item, run_id, per_run, per_day, account)
                if not allowed:
                    print(f"budget stop: {reason}", file=sys.stderr)
                    continue
                cover_letter = ""
                if llm_cfg.get("enabled"):
                    cover_letter, source = generate(item, profile, config.data_dir / "llm-cache",
                                                    str(llm_cfg.get("model", "")),
                                                    enabled=bool(llm_cfg.get("enabled", False)), required=True)
                    item["llm_source"] = source
                result = apply_one(page, item, resume, cover_letter)
                store.record(item, result.status, result.note, run_id, account)
                print(f"{item.get('id')}: {result.status} — {result.note}")
                time.sleep(1)
        finally:
            try:
                if context is not None:
                    try:
                        save_state(state_path, context.storage_state())
                    finally:
                        context.close()
            finally:
                browser.close()
    return 0


def _submission_preflight(item: dict, llm_enabled: bool) -> str:
    """Return a manual-review reason before consuming an apply reservation."""
    from .autoapply import allowed_hh_url

    if not str(item.get("resume", "")).strip():
        return "resume is not selected"
    if not allowed_hh_url(str(item.get("url", ""))):
        return "URL is not an allowed HH hostname"
    requires_letter = any(item.get(key) for key in (
        "cover_letter_required", "requires_cover_letter", "letter_required",
    ))
    if requires_letter and not llm_enabled:
        return "required cover letter is missing"
    return ""
