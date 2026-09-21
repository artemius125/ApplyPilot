"""Local admin web UI for ApplyPilot.

A dependency-free control panel served on localhost with the standard library.
It reads local artifacts (screen reports, the SQLite journal, emitted snapshots)
and launches ``applypilot`` subcommands as subprocesses so the operator can scan,
screen, sync, dry-run and — behind an explicit confirmation — send applications,
watching live logs in the browser.

Nothing is exposed beyond 127.0.0.1 and real sending stays gated on both an
explicit confirm and ``reviewed = true`` in the profile.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .balance import fetch_balance
from .config import AppConfig, ConfigError, effective_search
from .letters import LettersError, generate_letter
from .storage import Store

# Human-readable HH experience tiers (the candidate has ~1.3 years hands-on).
EXPERIENCE_LABELS = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "6+ лет",
}
# Tiers that exceed the candidate's real experience — flagged in the UI.
OVER_EXPERIENCE = {"between3And6", "moreThan6"}

# track -> the profile/search/snapshot artifacts that our two-track setup uses.
TRACKS = {
    "ai": {
        "label": "AI / LLM",
        "profile": "private/config/profile.toml",
        "search": "private/config/search.toml",
        "screen_report": "private/reports/screen-ai.json",
        "accepted": "private/data/snapshots/accepted-ai.json",
    },
    "infra": {
        "label": "DevOps / инфраструктура",
        "profile": "private/config/profile-infra.toml",
        "search": "private/config/search-infra.toml",
        "screen_report": "private/reports/screen-infra.json",
        "accepted": "private/data/snapshots/accepted-infra.json",
    },
}


# Model choices offered in the admin (the first is the stock default).
MODEL_CHOICES = [
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5",
    "deepseek-v4-flash-0731",
    "qwen3-7-flash",
]
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_BASE_URL = "https://api.aitunnel.ru/v1/chat/completions"


@dataclass
class Job:
    """A single background subprocess whose output is streamed to the browser."""

    argv: list[str] = field(default_factory=list)
    label: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    returncode: int | None = None
    lines: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "argv": self.argv,
            "running": self.process is not None and self.returncode is None,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lines": self.lines[-500:],
        }


class JobRunner:
    """Runs at most one job at a time and captures its output."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.current: Job | None = None

    def start(self, argv: list[str], label: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
        with self.lock:
            if self.current is not None and self.current.returncode is None:
                return False, "another job is running"
            job = Job(argv=argv, label=label, started_at=time.time())
            run_env = {**os.environ, **(env or {})}
            try:
                job.process = subprocess.Popen(
                    argv, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=run_env,
                )
            except OSError as exc:
                return False, f"failed to start: {exc}"
            self.current = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return True, "started"

    def _pump(self, job: Job) -> None:
        assert job.process is not None and job.process.stdout is not None
        for line in job.process.stdout:
            job.lines.append(line.rstrip("\n"))
        job.process.wait()
        job.returncode = job.process.returncode
        job.finished_at = time.time()

    def status(self) -> dict[str, Any] | None:
        return self.current.snapshot() if self.current else None

    def stop(self) -> bool:
        with self.lock:
            if self.current and self.current.process and self.current.returncode is None:
                self.current.process.terminate()
                return True
        return False


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _profile_flag(root: Path, track: str) -> dict[str, Any]:
    path = root / TRACKS[track]["profile"]
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _exp_label(value: str) -> str:
    return EXPERIENCE_LABELS.get(str(value or ""), "—")


def _salary_label(salary: Any) -> str:
    """Format an HH salary dict into a compact human string, '—' when absent."""
    if not isinstance(salary, dict):
        return "—"
    lo, hi = salary.get("from"), salary.get("to")
    cur = str(salary.get("currency") or "").upper()
    sign = "₽" if cur in {"RUR", "RUB", ""} else cur
    if lo and hi:
        body = f"{int(lo):,}–{int(hi):,}".replace(",", " ")
    elif lo:
        body = f"от {int(lo):,}".replace(",", " ")
    elif hi:
        body = f"до {int(hi):,}".replace(",", " ")
    else:
        return "—"
    return f"{body} {sign}".strip()


def _is_fresh(published: str, *, days: int = 3) -> bool:
    """True when the vacancy was published within the last ``days`` days."""
    if not published:
        return False
    try:
        from datetime import UTC, datetime
        text = str(published).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() <= days * 86400
    except (ValueError, TypeError):
        return False


def _norm_title(name: str) -> str:
    """Normalise a vacancy title for near-duplicate collapsing."""
    text = str(name or "").lower().strip()
    return re.sub(r"[\s\W]+", " ", text).strip()


_VERDICT_ORDER = {"FIT": 0, "MAYBE": 1, "SKIP": 2, "ERROR": 3}


def _dedup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse HH reposts (same company + title) into their strongest row.

    HH lets employers repost the same vacancy under fresh sequential ids to stay
    on top of search; those arrive as distinct ids the mechanical id-dedup cannot
    catch.  We keep the best verdict / highest fit_score and record how many
    duplicates were folded in so the operator sees one line, not three.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (str(row.get("company", "")).lower().strip(), _norm_title(row.get("name", "")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    def rank(r: dict[str, Any]) -> tuple[int, int]:
        return (_VERDICT_ORDER.get(r.get("verdict", ""), 4), -int(r.get("fit_score", 0) or 0))

    collapsed: list[dict[str, Any]] = []
    for key in order:
        members = sorted(groups[key], key=rank)
        best = dict(members[0])
        best["dupes"] = len(members) - 1
        best["dupe_ids"] = [str(m.get("id", "")) for m in members[1:]]
        collapsed.append(best)
    return collapsed


class AdminApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.root
        self.runner = JobRunner(self.root)
        self.settings_path = config.data_dir / "admin-settings.json"
        self._balance_cache: tuple[float, float | None] = (0.0, None)  # (fetched_at, rub)

    # ---- settings (model + per-user API key) ---------------------------
    def load_settings(self) -> dict[str, Any]:
        data = _read_json(self.settings_path) or {}
        return data if isinstance(data, dict) else {}

    def settings_public(self) -> dict[str, Any]:
        """Settings for the browser — the API key is NEVER returned, only whether it is set."""
        s = self.load_settings()
        return {
            "model": s.get("model") or DEFAULT_MODEL,
            "base_url": s.get("base_url") or DEFAULT_BASE_URL,
            "key_set": bool(s.get("api_key") or os.getenv("AITUNNEL_API_KEY")),
            "models": MODEL_CHOICES,
            "constraints": s.get("constraints") or "",
            "salary_expectation": s.get("salary_expectation") or "",
            "criteria_ai": s.get("criteria_ai") or "",
            "criteria_infra": s.get("criteria_infra") or "",
        }

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        s = self.load_settings()
        if patch.get("model"):
            s["model"] = str(patch["model"]).strip()
        if patch.get("base_url"):
            s["base_url"] = str(patch["base_url"]).strip()
        # Free-text prompt/candidate overrides ("" clears them).
        for fld in ("constraints", "salary_expectation", "criteria_ai", "criteria_infra"):
            if fld in patch and isinstance(patch[fld], str):
                s[fld] = patch[fld].strip()
        # Only overwrite the key when a non-empty value is supplied; "" leaves it as is.
        api_key = patch.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            s["api_key"] = api_key.strip()
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        try:
            self.settings_path.chmod(0o600)
        except OSError:
            pass
        return self.settings_public()

    def _screen_env(self) -> dict[str, str]:
        key = str(self.load_settings().get("api_key") or "").strip()
        return {"AITUNNEL_API_KEY": key} if key else {}

    def _api_key(self) -> str:
        return str(self.load_settings().get("api_key") or "").strip() or os.getenv("AITUNNEL_API_KEY", "")

    def live_balance(self, *, max_age: float = 60.0) -> float | None:
        """Balance in rubles from aitunnel, cached briefly to avoid per-request calls."""
        now = time.time()
        fetched_at, cached = self._balance_cache
        if cached is not None and now - fetched_at < max_age:
            return cached
        key = self._api_key()
        if not key:
            return cached
        base = str(self.load_settings().get("base_url") or DEFAULT_BASE_URL)
        # fetch_balance wants the API origin, not the chat-completions path.
        origin = base.split("/v1/")[0] if "/v1/" in base else base
        value = fetch_balance(key, origin)
        if value is not None:
            self._balance_cache = (now, value)
        return value if value is not None else cached

    # ---- data endpoints -------------------------------------------------
    def overview(self) -> dict[str, Any]:
        store = Store(self.config.db_path)
        account = "hh-primary"
        session_file = self.config.data_dir / "hh_session.json"
        watch_log = self.config.data_dir / "watch.log"
        watch_tail = ""
        if watch_log.exists():
            try:
                watch_tail = "\n".join(watch_log.read_text(encoding="utf-8").splitlines()[-4:])
            except OSError:
                watch_tail = ""
        result: dict[str, Any] = {
            "session_present": session_file.exists(),
            "blocked": len(store.blocked_ids(account)) if self.config.db_path.exists() else 0,
            "balance": self.live_balance(),
            "watch_tail": watch_tail,
            "tracks": {},
            "job": self.runner.status(),
        }
        for track, cfg in TRACKS.items():
            profile = _profile_flag(self.root, track)
            report = _read_json(self.root / cfg["screen_report"]) or {}
            results = report.get("results", []) if isinstance(report, dict) else []
            # Top-fit preview (deduplicated) for the dashboard.
            enriched = [{**r, "fresh": _is_fresh(r.get("published", "")),
                         "exp_label": _exp_label(r.get("experience", ""))} for r in results]
            top = sorted(_dedup_rows(enriched),
                         key=lambda r: (_VERDICT_ORDER.get(r.get("verdict", ""), 4),
                                        -int(r.get("fit_score", 0) or 0)))
            top_fit = [{"id": r.get("id"), "name": r.get("name"), "company": r.get("company"),
                        "url": r.get("url"), "fit_score": r.get("fit_score"),
                        "verdict": r.get("verdict"), "exp_label": r.get("exp_label"),
                        "fresh": r.get("fresh")}
                       for r in top if r.get("verdict") == "FIT"][:6]
            fresh_count = sum(1 for r in top if r.get("fresh")
                              and r.get("verdict") in ("FIT", "MAYBE"))
            accepted = _read_json(self.root / cfg["accepted"]) or {}
            result["tracks"][track] = {
                "label": cfg["label"],
                "reviewed": bool(profile.get("reviewed", False)),
                "counts": report.get("counts") if isinstance(report, dict) else None,
                "accepted": len(accepted.get("items", [])) if isinstance(accepted, dict) else 0,
                "has_report": bool(report),
                "top_fit": top_fit,
                "fresh_count": fresh_count,
            }
        return result

    def spend(self) -> dict[str, Any]:
        """Aggregate the LLM spend ledger for the stats panel."""
        path = self.config.data_dir / "spend.jsonl"
        today = time.strftime("%Y-%m-%d")
        by_day: dict[str, float] = {}
        total = 0.0
        calls = 0
        balance: float | None = None
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = float(row.get("cost_rub") or 0)
                day = str(row.get("date") or "")
                by_day[day] = round(by_day.get(day, 0.0) + cost, 4)
                total += cost
                calls += 1
                if row.get("balance") is not None:
                    balance = float(row["balance"])
        days = sorted(by_day.items())[-14:]
        live = self.live_balance()
        return {
            "balance": live if live is not None else balance,
            "balance_live": live is not None,
            "today_rub": round(by_day.get(today, 0.0), 2),
            "total_rub": round(total, 2),
            "calls": calls,
            "by_day": [{"date": d, "rub": round(v, 2)} for d, v in days],
        }

    def verdict_stats(self) -> dict[str, Any]:
        out = {}
        for track, cfg in TRACKS.items():
            report = _read_json(self.root / cfg["screen_report"]) or {}
            out[track] = {"label": cfg["label"],
                          "counts": report.get("counts") if isinstance(report, dict) else None}
        return out

    def vacancies(self, track: str) -> dict[str, Any]:
        if track not in TRACKS:
            return {"error": "unknown track"}
        report = _read_json(self.root / TRACKS[track]["screen_report"]) or {}
        results = report.get("results", []) if isinstance(report, dict) else []
        store = Store(self.config.db_path)
        statuses = store.read_statuses("hh-primary") if self.config.db_path.exists() else {}
        blocked = store.blocked_ids("hh-primary") if self.config.db_path.exists() else set()
        rows = []
        for row in results:
            vid = str(row.get("id", ""))
            rows.append({
                **row,
                "db_status": statuses.get(vid, ""),
                "blocked": vid in blocked,
                "exp_label": _exp_label(row.get("experience", "")),
                "over_experience": str(row.get("experience", "")) in OVER_EXPERIENCE,
                "salary_label": _salary_label(row.get("salary")),
                "fresh": _is_fresh(row.get("published", "")),
            })
        rows = _dedup_rows(rows)
        rows.sort(key=lambda r: (_VERDICT_ORDER.get(r.get("verdict", ""), 4),
                                 -int(r.get("fit_score", 0) or 0)))
        folded = sum(int(r.get("dupes", 0)) for r in rows)
        return {"track": track, "model": report.get("model"), "count": len(rows),
                "folded_duplicates": folded, "rows": rows}

    # ---- job endpoints --------------------------------------------------
    def start_job(self, body: dict[str, Any]) -> tuple[bool, str]:
        action = str(body.get("action", ""))
        track = str(body.get("track", "ai"))
        if track not in TRACKS:
            return False, "unknown track"
        cfg = TRACKS[track]
        base = [sys.executable, "-m", "applypilot"]
        # Global flags (profile/search) go before the subcommand.
        gflags = ["--profile", cfg["profile"], "--search", cfg["search"]]
        if action == "scan":
            argv = base + gflags + ["scan"]
        elif action == "sync":
            argv = base + ["sync"]
        elif action == "screen":
            argv = base + gflags + [
                "screen", "--input", body.get("input") or _track_snapshot(self.root, track),
                "--track", track,
                "--output", cfg["screen_report"], "--emit-snapshot", cfg["accepted"],
            ]
            s = self.load_settings()
            if str(s.get("model") or "").strip():
                argv += ["--model", str(s["model"]).strip()]
            if s.get("constraints"):
                argv += ["--constraints", str(s["constraints"])]
            if s.get("salary_expectation"):
                argv += ["--salary-expectation", str(s["salary_expectation"])]
            if s.get(f"criteria_{track}"):
                argv += ["--criteria", str(s[f"criteria_{track}"])]
            if not self._screen_env() and not os.getenv("AITUNNEL_API_KEY"):
                return False, "no AITUNNEL_API_KEY: задайте ключ во вкладке «Настройки»"
        elif action == "fresh":
            # Manual version of the watch timer: scan the freshest vacancies for
            # this track, then screen them, in one streamed job.  Never applies.
            if not self._api_key():
                return False, "no AITUNNEL_API_KEY: задайте ключ во вкладке «Настройки»"
            import shlex
            days = max(1, int(body.get("days") or 3))
            s = self.load_settings()
            screen_extra = ["--track", track, "--output", cfg["screen_report"],
                            "--emit-snapshot", cfg["accepted"]]
            if str(s.get("model") or "").strip():
                screen_extra += ["--model", str(s["model"]).strip()]
            if s.get("constraints"):
                screen_extra += ["--constraints", str(s["constraints"])]
            if s.get("salary_expectation"):
                screen_extra += ["--salary-expectation", str(s["salary_expectation"])]
            if s.get(f"criteria_{track}"):
                screen_extra += ["--criteria", str(s[f"criteria_{track}"])]
            py = shlex.quote(sys.executable)
            g = " ".join(shlex.quote(x) for x in gflags)
            scan_cmd = f"{py} -m applypilot {g} scan --days {days}"
            snap = "$(ls -t private/data/snapshots/hh_vacancies_*.json | head -1)"
            screen_cmd = (f"{py} -m applypilot {g} screen --input {snap} "
                          + " ".join(shlex.quote(x) for x in screen_extra))
            argv = ["bash", "-lc", f"set -e; {scan_cmd}; {screen_cmd}"]
            return self.runner.start(argv, f"fresh:{track}", env=self._screen_env())
        elif action == "analytics":
            argv = base + ["analytics"]
        elif action in {"apply_dry", "apply_run"}:
            accepted = self.root / cfg["accepted"]
            if not accepted.exists():
                return False, "no accepted snapshot yet; run screen first"
            argv = base + gflags + ["apply", "--input", cfg["accepted"]]
            limit = int(body.get("limit") or 10)
            argv += ["--limit", str(max(1, limit))]
            if action == "apply_dry":
                argv += ["--dry-run"]
            else:
                if not body.get("confirm"):
                    return False, "real sending requires explicit confirmation"
                if not _profile_flag(self.root, track).get("reviewed", False):
                    return False, "profile is not reviewed=true; cannot send"
                argv += ["--run"]
                if body.get("target"):
                    argv += ["--target-success", str(int(body["target"]))]
        else:
            return False, f"unknown action: {action}"
        return self.runner.start(argv, f"{action}:{track}", env=self._screen_env())

    def letter(self, track: str, vid: str) -> dict[str, Any]:
        """Generate an individual cover letter for one vacancy (in-process)."""
        if track not in TRACKS:
            return {"error": "unknown track"}
        item = None
        for source in (Path(_track_snapshot(self.root, track)), self.root / TRACKS[track]["accepted"]):
            data = _read_json(source) or {}
            items = data.get("items", []) if isinstance(data, dict) else []
            item = next((it for it in items if str(it.get("id", "")) == str(vid)), None)
            if item is not None:
                break
        if item is None:
            return {"error": "vacancy not found"}
        s = self.load_settings()
        key = str(s.get("api_key") or "").strip() or os.getenv("AITUNNEL_API_KEY", "")
        if not key:
            return {"error": "no AITUNNEL_API_KEY: задайте ключ в «Настройки»"}
        try:
            res = generate_letter(
                item, _profile_flag(self.root, track), self.config.data_dir / "letter-cache",
                model=str(s.get("model") or DEFAULT_MODEL).strip(),
                base_url=str(s.get("base_url") or DEFAULT_BASE_URL).strip(), api_key=key,
            )
        except LettersError as exc:
            return {"error": str(exc)[:200]}
        return {"text": res.get("text", ""), "source": res.get("source", ""),
                "url": item.get("url", ""), "name": item.get("name", "")}


def _track_queries(root: Path, track: str) -> set[str]:
    cfg = TRACKS[track]
    conf = AppConfig.discover(root=root, profile=cfg["profile"], search=cfg["search"])
    try:
        queries = effective_search(conf.load_search(), None).get("queries", [])
        return {str(q).strip().lower() for q in queries}
    except (ConfigError, OSError, ValueError):
        return set()


def _track_snapshot(root: Path, track: str) -> str:
    """Newest scan snapshot whose queries belong to this track (not just newest overall)."""
    directory = root / "private/data/snapshots"
    files = sorted(directory.glob("hh_vacancies_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    queries = _track_queries(root, track)
    for path in files:
        data = _read_json(path) or {}
        segment_queries = {str(seg.get("query", "")).strip().lower() for seg in data.get("segments", [])}
        if queries and (segment_queries & queries):
            return str(path)
    return str(files[0]) if files else str(root / TRACKS[track]["accepted"])


def _handler(app: AdminApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # keep the console quiet
            return

        def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html")
            elif parsed.path == "/api/overview":
                self._send(200, app.overview())
            elif parsed.path == "/api/vacancies":
                track = parse_qs(parsed.query).get("track", ["ai"])[0]
                self._send(200, app.vacancies(track))
            elif parsed.path == "/api/job":
                self._send(200, app.runner.status() or {})
            elif parsed.path == "/api/settings":
                self._send(200, app.settings_public())
            elif parsed.path == "/api/stats":
                self._send(200, {"spend": app.spend(), "verdicts": app.verdict_stats()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = _read_json_bytes(self.rfile.read(length)) if length else {}
            if parsed.path == "/api/job":
                ok, message = app.start_job(body if isinstance(body, dict) else {})
                self._send(200 if ok else 409, {"ok": ok, "message": message})
            elif parsed.path == "/api/stop":
                self._send(200, {"ok": app.runner.stop()})
            elif parsed.path == "/api/settings":
                self._send(200, app.save_settings(body if isinstance(body, dict) else {}))
            elif parsed.path == "/api/letter":
                b = body if isinstance(body, dict) else {}
                self._send(200, app.letter(str(b.get("track", "ai")), str(b.get("id", ""))))
            else:
                self._send(404, {"error": "not found"})

    return Handler


def _read_json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def serve(config: AppConfig, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    app = AdminApp(config)
    # A larger accept backlog: the live-polling UI opens several short-lived
    # connections, and the stdlib default of 5 can refuse bursts.
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer((host, port), _handler(app))
    url = f"http://{host}:{port}"
    print(f"ApplyPilot admin: {url}  (Ctrl-C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001,S110 - opening a browser is best-effort
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping admin")
    finally:
        server.server_close()



INDEX_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ApplyPilot admin</title>
<style>
:root{--bg:#0f1216;--card:#1a1f27;--card2:#20262f;--fg:#e7ecf3;--mut:#93a1b3;--line:#2b333f;--fit:#2fbf71;--maybe:#e2b13c;--skip:#e15c5c;--accent:#4c8dff;--warn:#f0883e;--star:#ffd23f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:20}
h1{font-size:16px;margin:0;font-weight:650}
.tabs{display:flex;gap:6px;flex-wrap:wrap}.tab{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;color:var(--fg)}
.tab.active{border-color:var(--accent);color:#fff;background:#223049}
.bal{margin-left:auto;display:flex;gap:14px;align-items:center}
.bal b{color:var(--fit)}
main{padding:18px;max-width:1220px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h3{margin:0 0 8px;font-size:13px;color:var(--mut);font-weight:600}
.big{font-size:22px;font-weight:700}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px}
button.ghost{background:var(--card2);border:1px solid var(--line);color:var(--fg)}
button.mini{padding:4px 9px;font-size:12px}
button.danger{background:var(--skip)}button:disabled{opacity:.5;cursor:not-allowed}
label{color:var(--mut);font-size:12px}
input,select,textarea{background:#11151b;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px;font-family:inherit}
table{width:100%;border-collapse:collapse;margin-top:10px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;position:sticky;top:57px;background:var(--bg)}
tr:hover td{background:#151b22}
.pill{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.FIT{background:rgba(47,191,113,.16);color:var(--fit)}.MAYBE{background:rgba(226,177,60,.16);color:var(--maybe)}
.SKIP{background:rgba(225,92,92,.16);color:var(--skip)}.ERROR{background:rgba(147,161,179,.16);color:var(--mut)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}
pre{background:#0a0d11;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:420px;overflow:auto;white-space:pre-wrap}
.muted{color:var(--mut)}.hide{display:none}.hl{color:var(--warn)}
.badge{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--card2);border:1px solid var(--line);color:var(--mut);margin-left:6px}
.fresh{color:var(--fit);border-color:rgba(47,191,113,.4)}
.star{cursor:pointer;font-size:16px;color:#3a434f;user-select:none}.star.on{color:var(--star)}
.tf{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)}
.tf:last-child{border-bottom:0}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.62);display:flex;align-items:center;justify-content:center;z-index:60;padding:16px}
.modal.hide{display:none}
.modal .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;max-width:700px;width:100%;max-height:88vh;overflow:auto}
.modal h3{margin:0 0 6px}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>
<header><h1>ApplyPilot</h1>
<div class="tabs">
<div class="tab active" data-t="overview">Обзор</div>
<div class="tab" data-t="vac">Вакансии</div>
<div class="tab" data-t="apply">Отклики</div>
<div class="tab" data-t="stats">Статистика</div>
<div class="tab" data-t="log">Лог</div>
<div class="tab" data-t="settings">Настройки</div>
</div>
<div class="bal"><span id="hbal" class="muted"></span><span id="jobstate" class="muted"></span></div>
</header>
<main>
<section id="overview"></section>

<section id="vac" class="hide">
  <div class="row"><label>Трек</label>
    <select id="vtrack"><option value="ai">AI / LLM</option><option value="infra">DevOps / инфраструктура</option></select>
    <label>Вердикт</label>
    <select id="vfilter"><option value="">все</option><option>FIT</option><option>MAYBE</option><option>SKIP</option><option>ERROR</option></select>
    <label><input type="checkbox" id="vshowskip"> показывать SKIP</label>
    <label><input type="checkbox" id="vfresh"> только свежие</label>
    <label><input type="checkbox" id="vmarked"> только отмеченные ★</label>
    <button class="ghost mini" onclick="loadVac()">Обновить</button>
    <span id="vmeta" class="muted"></span></div>
  <div id="vtable"></div>
</section>

<section id="apply" class="hide">
  <div class="card" style="max-width:660px">
    <h3>Запуск откликов</h3>
    <div class="row"><label>Трек</label>
      <select id="atrack"><option value="ai">AI / LLM</option><option value="infra">DevOps / инфраструктура</option></select>
      <label>Лимит</label><input id="alimit" type="number" value="10" style="width:80px">
      <label>Target success</label><input id="atarget" type="number" value="" placeholder="—" style="width:80px"></div>
    <div class="row"><button class="ghost" onclick="job('apply_dry')">Dry-run (безопасно)</button></div>
    <hr style="border-color:var(--line)">
    <div class="row"><input type="checkbox" id="aconfirm"><label for="aconfirm">Подтверждаю реальную отправку откликов работодателям</label></div>
    <div class="row"><button class="danger" onclick="job('apply_run')">Отправить реальные отклики</button></div>
    <p class="muted">Реальная отправка требует галки и <code>reviewed = true</code> в профиле трека. Отклики необратимы.</p>
  </div>
</section>

<section id="stats" class="hide">
  <div class="grid" id="spendcards"></div>
  <div class="card" style="margin-top:12px"><h3>Расход по дням, ₽</h3><div id="spendbars"></div></div>
  <div class="card" style="margin-top:12px"><h3>Вердикты по трекам</h3><div id="verdictbars"></div></div>
</section>

<section id="log" class="hide"><div class="row"><button class="ghost mini" onclick="refreshJob()">Обновить</button><button class="danger mini" onclick="stopJob()">Стоп</button></div><pre id="logbox">—</pre></section>

<section id="settings" class="hide">
  <div class="card" style="max-width:680px">
    <h3>Модель и ключ (LLM-скрининг и письма)</h3>
    <div class="row"><label>Модель</label><select id="smodel" style="min-width:220px"></select></div>
    <div class="row"><label>Base URL</label><input id="sbase" style="min-width:360px"></div>
    <div class="row"><label>API-ключ</label><input id="skey" type="password" placeholder="sk-aitunnel-... (пусто — не менять)" style="min-width:360px"></div>
    <p class="muted">Ключ хранится локально (права 600), в интерфейс не возвращается. У каждого пользователя — свой ключ.</p>
    <h3 style="margin-top:14px">Кто кандидат и как оценивать</h3>
    <div class="row"><label style="width:170px">Ограничения кандидата</label></div>
    <textarea id="sconstraints" rows="4" style="width:100%" placeholder="опыт, метод работы, интервью, английский, формат"></textarea>
    <div class="row" style="margin-top:8px"><label style="width:170px">Зарплатный ориентир</label></div>
    <textarea id="ssalary" rows="2" style="width:100%" placeholder="напр.: ориентир от 100 000 ₽; вилки заметно ниже неинтересны"></textarea>
    <div class="row" style="margin-top:8px"><label style="width:170px">Доп. правила скрининга — AI</label></div>
    <textarea id="scritai" rows="4" style="width:100%" placeholder="пусто — только встроенная рубрика AI"></textarea>
    <div class="row" style="margin-top:8px"><label style="width:170px">Доп. правила скрининга — DevOps</label></div>
    <textarea id="scritinfra" rows="4" style="width:100%" placeholder="пусто — только встроенная рубрика DevOps"></textarea>
    <div class="row" style="margin-top:10px"><button onclick="saveSettings()">Сохранить</button><span id="skeystate" class="muted"></span></div>
  </div>
</section>
</main>

<div id="modal" class="modal hide" onclick="if(event.target===this)closeModal()">
  <div class="box">
    <h3>Сопроводительное письмо</h3>
    <div class="muted" id="lettertitle" style="margin-bottom:8px"></div>
    <textarea id="lettertext" rows="10" style="width:100%"></textarea>
    <div class="row"><button onclick="copyLetter()">Копировать</button>
      <button class="ghost" onclick="openHH()">Открыть вакансию на HH</button>
      <button class="ghost" onclick="closeModal()">Закрыть</button>
      <span id="letterhint" class="muted"></span></div>
    <p class="muted">Проверь и при необходимости поправь письмо. Отклик и отправку письма делаешь на HH сам (ассистированный режим).</p>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const SECTIONS=["overview","vac","apply","stats","log","settings"];
let tab="overview";
function show(t){tab=t;document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.t===t));
  SECTIONS.forEach(id=>$("#"+id).classList.toggle("hide",id!==t));
  if(t==="overview")loadOverview();if(t==="vac")loadVac();if(t==="settings")loadSettings();if(t==="stats")loadStats();}
document.querySelectorAll(".tab").forEach(el=>el.onclick=()=>show(el.dataset.t));
async function api(p,opt){const r=await fetch(p,opt);return r.json();}
function esc(s){return (s||"").toString().replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function rub(v){return v==null?"—":Number(v).toLocaleString("ru-RU");}

/* ---- marks (localStorage) ---- */
function marks(){try{return new Set(JSON.parse(localStorage.getItem("ap_marks")||"[]"))}catch(e){return new Set()}}
function saveMarks(s){try{localStorage.setItem("ap_marks",JSON.stringify([...s]))}catch(e){}}
function toggleMark(id){const s=marks();s.has(id)?s.delete(id):s.add(id);saveMarks(s);loadVac();}

/* ---- overview ---- */
async function loadOverview(){const d=await api("/api/overview");
  let h='<div class="grid">';
  h+=card("Баланс, ₽",rub(d.balance),d.balance!=null?"aitunnel":"нет ключа");
  h+=card("Сессия HH",d.session_present?"есть":"нет");
  h+=card("В блок-листе",d.blocked,"уже откликался");
  h+='</div>';
  h+='<div class="grid2" style="margin-top:12px">';
  for(const k in d.tracks){const t=d.tracks[k];const c=t.counts||{};
    h+=`<div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">${esc(t.label)}</h3>`
      +(t.fresh_count?`<span class="badge fresh">🟢 свежих ${t.fresh_count}</span>`:"")+`</div>`
      +`<div class="big">${t.accepted||0} <span class="muted" style="font-size:13px">в работе</span></div>`
      +`<div class="row" style="gap:6px;margin:6px 0"><span class="pill FIT">FIT ${c.FIT??"–"}</span><span class="pill MAYBE">MAYBE ${c.MAYBE??"–"}</span><span class="pill SKIP">SKIP ${c.SKIP??"–"}</span>${c.ERROR?`<span class="pill ERROR">ERR ${c.ERROR}</span>`:""}</div>`
      +`<div class="muted" style="margin-bottom:8px">reviewed: ${t.reviewed?"✅":"—"}</div>`
      +`<div class="row"><button class="mini" onclick="gotoVac('${k}','FIT')">Показать FIT →</button>`
      +`<button class="ghost mini" onclick="gotoVac('${k}','')">Все вакансии</button>`
      +`<button class="ghost mini" onclick="job('fresh','${k}')">Проверить свежие</button>`
      +`<button class="ghost mini" onclick="job('screen','${k}')">Пере-скрин</button></div>`;
    if((t.top_fit||[]).length){h+='<div style="margin-top:10px">';
      for(const f of t.top_fit){h+=`<div class="tf"><div><a href="${f.url}" target="_blank">${esc(f.name)}</a>`
        +(f.fresh?'<span class="badge fresh">свежая</span>':"")+`<div class="muted">${esc(f.company||"")} · ${esc(f.exp_label||"")}</div></div>`
        +`<div style="text-align:right;white-space:nowrap"><span class="pill FIT">${f.fit_score}</span><br>`
        +`<button class="ghost mini" style="margin-top:4px" onclick="genLetter('${k}','${f.id}','${encodeURIComponent(f.url||"")}')">Письмо</button></div></div>`;}
      h+='</div>';}
    h+='</div>';}
  h+='</div>';
  h+='<div class="card" style="margin-top:12px"><h3>Автопоиск свежих вакансий</h3>'
    +'<p class="muted" style="margin:0 0 8px">«Проверить свежие» = скан за последние дни + скрининг для трека (отклики остаются ручными). Для автоматики есть таймер systemd / cron — см. packaging/README.</p>'
    +'<div class="row"><button class="ghost mini" onclick="job(\\'fresh\\',\\'ai\\')">Свежие: AI</button>'
    +'<button class="ghost mini" onclick="job(\\'fresh\\',\\'infra\\')">Свежие: DevOps</button></div>'
    +(d.watch_tail?`<pre style="margin-top:8px;max-height:120px">${esc(d.watch_tail)}</pre>`:'<div class="muted" style="margin-top:6px">лог автопоиска пуст (таймер ещё не запускался)</div>')
    +'</div>';
  h+='<div class="card" style="margin-top:12px"><h3>Прочие действия</h3><div class="row">'
    +btn("scan","ai","Scan AI")+btn("scan","infra","Scan DevOps")
    +btn("sync","ai","Sync отклики")+btn("analytics","ai","Analytics")+'</div></div>';
  $("#overview").innerHTML=h;
  $("#hbal").innerHTML=d.balance!=null?`баланс <b>${rub(d.balance)} ₽</b>`:"";}
function card(t,b,sub){return `<div class="card"><h3>${t}</h3><div class="big">${b}</div>${sub?`<div class="muted">${sub}</div>`:""}</div>`;}
function btn(a,tr,l){return `<button class="ghost mini" onclick="job('${a}','${tr}')">${l}</button>`;}
function gotoVac(track,filter){$("#vtrack").value=track;$("#vfilter").value=filter;
  if(filter==="SKIP")$("#vshowskip").checked=true;show("vac");}

/* ---- jobs ---- */
async function job(action,track){const body={action,track:track||$("#atrack")?.value||"ai"};
  if(action.startsWith("apply")){body.track=$("#atrack").value;body.limit=+$("#alimit").value;
    if($("#atarget").value)body.target=+$("#atarget").value;if(action==="apply_run")body.confirm=$("#aconfirm").checked;}
  const r=await api("/api/job",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok){alert("Не запущено: "+r.message);return;}show("log");refreshJob();}

/* ---- settings ---- */
async function loadSettings(){const s=await api("/api/settings");
  const sel=$("#smodel");sel.innerHTML="";(s.models||[]).forEach(m=>{const o=document.createElement("option");o.value=m;o.textContent=m;if(m===s.model)o.selected=true;sel.appendChild(o);});
  $("#sbase").value=s.base_url||"";$("#skeystate").textContent=s.key_set?"ключ задан ✓":"ключ не задан";
  $("#sconstraints").value=s.constraints||"";$("#ssalary").value=s.salary_expectation||"";
  $("#scritai").value=s.criteria_ai||"";$("#scritinfra").value=s.criteria_infra||"";}
async function saveSettings(){const body={model:$("#smodel").value,base_url:$("#sbase").value,
  constraints:$("#sconstraints").value,salary_expectation:$("#ssalary").value,
  criteria_ai:$("#scritai").value,criteria_infra:$("#scritinfra").value};
  const k=$("#skey").value.trim();if(k)body.api_key=k;
  const s=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  $("#skey").value="";$("#skeystate").textContent=(s.key_set?"ключ задан ✓":"ключ не задан")+" · сохранено";}

/* ---- stats ---- */
async function loadStats(){const s=await api("/api/stats");const sp=s.spend||{};
  $("#spendcards").innerHTML=card("Баланс, ₽",rub(sp.balance),sp.balance_live?"aitunnel (live)":"из леджера")
    +card("Сегодня, ₽",sp.today_rub??0)+card("Всего потрачено, ₽",sp.total_rub??0)+card("Запросов к LLM",sp.calls??0);
  const days=sp.by_day||[];const mx=Math.max(1,...days.map(d=>d.rub));
  $("#spendbars").innerHTML=days.map(d=>bar(d.date,d.rub,mx,'var(--accent)','₽')).join("")||'<span class="muted">нет данных (скрининг ещё не тратил или брал из кэша)</span>';
  let vh="";const vd=s.verdicts||{};for(const k in vd){const t=vd[k],c=t.counts;
    if(!c){vh+=`<div class="muted" style="margin:8px 0">${t.label}: нет отчёта</div>`;continue;}
    const tot=Math.max(1,(c.FIT||0)+(c.MAYBE||0)+(c.SKIP||0)+(c.ERROR||0));
    vh+=`<div style="margin:10px 0"><b>${t.label}</b>`+seg('FIT',c.FIT||0,tot,'var(--fit)')+seg('MAYBE',c.MAYBE||0,tot,'var(--maybe)')+seg('SKIP',c.SKIP||0,tot,'var(--skip)')+seg('ERROR',c.ERROR||0,tot,'var(--mut)')+`</div>`;}
  $("#verdictbars").innerHTML=vh;}
function bar(label,val,mx,color,unit){const w=Math.round(100*val/mx);return `<div class="row" style="gap:8px"><span class="muted" style="width:96px">${label}</span><div style="flex:1;background:#11151b;border-radius:6px"><div style="width:${w}%;background:${color};height:14px;border-radius:6px"></div></div><span style="width:70px;text-align:right">${val}${unit||''}</span></div>`;}
function seg(label,val,tot,color){const w=Math.round(100*val/tot);return `<div class="row" style="gap:8px"><span class="muted" style="width:70px">${label}</span><div style="flex:1;background:#11151b;border-radius:6px"><div style="width:${w}%;background:${color};height:12px;border-radius:6px"></div></div><span style="width:40px;text-align:right">${val}</span></div>`;}

/* ---- job log ---- */
async function refreshJob(){const j=await api("/api/job");
  $("#logbox").textContent=(j.lines||[]).join("\\n")||"—";
  const st=j.label?(j.label+(j.running?' <span class="spin"></span> идёт':(" ✓ код "+j.returncode))):"";
  $("#jobstate").innerHTML=st;return !!j.running;}
async function stopJob(){await api("/api/stop",{method:"POST"});refreshJob();}

/* ---- vacancies ---- */
async function loadVac(){const tr=$("#vtrack").value,f=$("#vfilter").value,showskip=$("#vshowskip").checked;
  const onlyFresh=$("#vfresh").checked,onlyMarked=$("#vmarked").checked,mk=marks();
  const d=await api("/api/vacancies?track="+tr);
  let rows=(d.rows||[]).filter(r=> f ? r.verdict===f : (showskip || r.verdict!=="SKIP"));
  if(onlyFresh)rows=rows.filter(r=>r.fresh);
  if(onlyMarked)rows=rows.filter(r=>mk.has(String(r.id)));
  const dup=d.folded_duplicates?` · свернуто дублей: ${d.folded_duplicates}`:"";
  $("#vmeta").innerHTML=`модель ${esc(d.model||"—")} · показано ${rows.length} из ${d.count||0}${dup}`;
  let h='<table><tr><th>★</th><th>Вердикт</th><th>fit</th><th>Опыт</th><th>Зарплата</th><th>Вакансия</th><th>Причина</th><th>Статус</th><th></th></tr>';
  for(const r of rows){const id=String(r.id);const on=mk.has(id);
    const expc=r.over_experience?' class="hl"':"";
    const dupb=r.dupes?`<span class="badge">повторов: ${r.dupes}</span>`:"";
    const freshb=r.fresh?'<span class="badge fresh">свежая</span>':"";
    h+=`<tr><td><span class="star ${on?"on":""}" onclick="toggleMark('${id}')">${on?"★":"☆"}</span></td>
      <td><span class="pill ${r.verdict}">${r.verdict||"?"}</span></td>
      <td>${r.fit_score??""}</td>
      <td${expc}>${esc(r.exp_label||"—")}</td>
      <td class="muted">${esc(r.salary_label||"—")}</td>
      <td><a href="${r.url}" target="_blank">${esc(r.name)}</a>${freshb}${dupb}<div class="muted">${esc(r.company||"")}</div></td>
      <td class="muted">${esc(r.reason||"")}</td>
      <td>${r.blocked?'<span class="muted">откликался</span>':esc(r.db_status||"")}</td>
      <td><button class="ghost mini" onclick="genLetter('${tr}','${id}','${encodeURIComponent(r.url||"")}')">Письмо</button></td></tr>`;}
  $("#vtable").innerHTML=h+"</table>";}
["vtrack","vfilter","vshowskip","vfresh","vmarked"].forEach(id=>{const el=$("#"+id);if(el)el.onchange=loadVac;});

/* ---- letter modal ---- */
function closeModal(){$("#modal").classList.add("hide");}
function openHH(){const u=$("#modal").dataset.url;if(u)window.open(u,"_blank");}
function copyLetter(){const t=$("#lettertext").value;if(navigator.clipboard)navigator.clipboard.writeText(t);$("#letterhint").textContent="скопировано";}
async function genLetter(track,id,url){const m=$("#modal");m.classList.remove("hide");
  m.dataset.url=decodeURIComponent(url||"");
  $("#lettertitle").textContent="";$("#letterhint").innerHTML='<span class="spin"></span> генерирую…';
  $("#lettertext").value="";
  const r=await api("/api/letter",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({track,id})});
  if(r.error){$("#letterhint").textContent="";$("#lettertext").value="Ошибка: "+r.error;return;}
  $("#lettertitle").textContent=r.name||"";$("#lettertext").value=r.text||"";
  if(!m.dataset.url&&r.url)m.dataset.url=r.url;
  try{await navigator.clipboard.writeText(r.text||"");$("#letterhint").textContent="письмо скопировано в буфер";}
  catch(e){$("#letterhint").textContent="скопируй письмо кнопкой (буфер недоступен)";}}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});

/* ---- live refresh ---- */
let prevRunning=false;
setInterval(async()=>{const running=await refreshJob();
  if(running||prevRunning){if(tab==="vac")loadVac();if(tab==="overview")loadOverview();}
  prevRunning=running;},3000);
loadOverview();
</script>
</body></html>"""
