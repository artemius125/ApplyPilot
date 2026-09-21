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

import html
import json
import os
import re
import shutil
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

TRACKS_CONFIG = "private/config/tracks.toml"
RUBRIC_TYPES = ("ai", "infra", "general")
WATCH_TIMER = "applypilot-watch.timer"

# Fallback used only when private/config/tracks.toml is absent; it is written to
# disk on first load so the operator can edit / add tracks there.
DEFAULT_TRACKS = [
    {"key": "ai", "label": "AI / LLM", "type": "ai",
     "profile": "private/config/profile.toml", "search": "private/config/search.toml",
     "resume": "AI/LLM Engineer"},
    {"key": "infra", "label": "DevOps / инфраструктура", "type": "infra",
     "profile": "private/config/profile-infra.toml", "search": "private/config/search-infra.toml",
     "resume": "DevOps / Infrastructure Engineer"},
]

# track key -> normalised track config; populated by load_tracks() so the number
# of tracks (and their resumes) is driven by config, not hard-coded.
TRACKS: dict[str, dict[str, Any]] = {}


def _normalise_track(entry: dict[str, Any]) -> dict[str, Any] | None:
    key = re.sub(r"[^a-z0-9_-]", "", str(entry.get("key", "")).strip().lower())
    if not key:
        return None
    rubric = str(entry.get("type", "general")).strip().lower()
    if rubric not in RUBRIC_TYPES:
        rubric = "general"
    return {
        "key": key,
        "label": str(entry.get("label") or key),
        "type": rubric,
        "profile": str(entry.get("profile") or f"private/config/profile-{key}.toml"),
        "search": str(entry.get("search") or f"private/config/search-{key}.toml"),
        "resume": str(entry.get("resume") or ""),
        "screen_report": str(entry.get("screen_report") or f"private/reports/screen-{key}.json"),
        "accepted": str(entry.get("accepted") or f"private/data/snapshots/accepted-{key}.json"),
    }


def load_tracks(root: Path) -> dict[str, dict[str, Any]]:
    """Load track definitions from tracks.toml into the module TRACKS mapping.

    Missing config falls back to the built-in two tracks and is written out so
    the file becomes the single, editable source of truth.
    """
    path = root / TRACKS_CONFIG
    entries: list[dict[str, Any]] = []
    if path.exists():
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            raw = data.get("track", [])
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
        except (OSError, tomllib.TOMLDecodeError):
            entries = []
    if not entries:
        entries = [dict(e) for e in DEFAULT_TRACKS]
        try:
            _write_tracks_config(path, entries)
        except OSError:
            pass
    TRACKS.clear()
    for entry in entries:
        norm = _normalise_track(entry)
        if norm is not None:
            TRACKS[norm["key"]] = norm
    return TRACKS


def _write_tracks_config(path: Path, entries: list[dict[str, Any]]) -> None:
    """Persist track definitions to tracks.toml (append-safe, minimal writer)."""
    lines = ["# Треки поиска для админки. Число треков динамическое.",
             "# key/type(ai|infra|general)/profile/search/resume — см. админку «Резюме и треки».", ""]
    for e in entries:
        n = _normalise_track(e)
        if n is None:
            continue
        lines.append("[[track]]")
        for field_name in ("key", "label", "type", "profile", "search", "resume"):
            lines.append(f'{field_name} = {json.dumps(n[field_name], ensure_ascii=False)}')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


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


_RESUME_STOP = {"мои резюме", "создать резюме", "резюме", "показать ещё", "показать еще"}
_RESUME_SKIP_PREFIX = ("уровень дохода", "постоянная работа", "проектная работа", "стажировка",
                       "частичная занятость", "удалённо", "удаленно", "гибрид", "обновлено")


def _clean_resume_titles(raw: list[str]) -> list[str]:
    """Reduce HH's noisy resume labels (card blocks, headings) to clean titles."""
    out: list[str] = []
    for entry in raw:
        for line in str(entry).split("\n"):
            s = line.strip()
            low = s.lower()
            if not s or low in _RESUME_STOP or low.startswith(_RESUME_SKIP_PREFIX):
                continue
            out.append(s)
            break  # first meaningful line of a block is the resume title
    seen: set[str] = set()
    result: list[str] = []
    for title in out:
        key = title.lower()
        if key not in seen:
            seen.add(key)
            result.append(title)
    return result


def _disp(value: Any) -> str:
    """Decode HTML entities in scanned text so the UI doesn't double-escape them."""
    return html.unescape(str(value or ""))


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
        self._resume_cache: dict[str, Any] = {}  # last-known HH resume titles
        load_tracks(self.root)

    # ---- settings (model + per-user API key) ---------------------------
    def load_settings(self) -> dict[str, Any]:
        data = _read_json(self.settings_path) or {}
        return data if isinstance(data, dict) else {}

    def settings_public(self) -> dict[str, Any]:
        """Settings for the browser — the API key is NEVER returned, only whether it is set."""
        s = self.load_settings()
        criteria = {key: s.get(f"criteria_{key}") or "" for key in TRACKS}
        return {
            "model": s.get("model") or DEFAULT_MODEL,
            "base_url": s.get("base_url") or DEFAULT_BASE_URL,
            "key_set": bool(s.get("api_key") or os.getenv("AITUNNEL_API_KEY")),
            "models": MODEL_CHOICES,
            "constraints": s.get("constraints") or "",
            "salary_expectation": s.get("salary_expectation") or "",
            # Per-track screening overrides, plus a list of tracks for the UI.
            "criteria": criteria,
            "tracks": [{"key": k, "label": v["label"], "type": v["type"], "resume": v["resume"]}
                       for k, v in TRACKS.items()],
        }

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        s = self.load_settings()
        if patch.get("model"):
            s["model"] = str(patch["model"]).strip()
        if patch.get("base_url"):
            s["base_url"] = str(patch["base_url"]).strip()
        # Free-text prompt/candidate overrides ("" clears them).
        fields = ["constraints", "salary_expectation"] + [f"criteria_{key}" for key in TRACKS]
        for fld in fields:
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
            # Unique (deduplicated) counts so overview matches the vacancies view.
            uniq: dict[str, int] = {"FIT": 0, "MAYBE": 0, "SKIP": 0, "ERROR": 0}
            for r in top:
                uniq[r.get("verdict", "")] = uniq.get(r.get("verdict", ""), 0) + 1
            accepted = _read_json(self.root / cfg["accepted"]) or {}
            result["tracks"][track] = {
                "label": cfg["label"],
                "reviewed": bool(profile.get("reviewed", False)),
                "counts": report.get("counts") if isinstance(report, dict) else None,
                "counts_unique": uniq,
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
        applied = self._manual_applied()
        viewed = self._viewed()
        bad = self._bad()
        rows = []
        for row in results:
            vid = str(row.get("id", ""))
            rows.append({
                **row,
                "name": _disp(row.get("name", "")),
                "company": _disp(row.get("company", "")),
                "db_status": statuses.get(vid, ""),
                "blocked": vid in blocked,
                "applied": vid in applied,
                "viewed": vid in viewed,
                "bad": vid in bad,
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
                "--track", cfg["type"],
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
            screen_extra = ["--track", cfg["type"], "--output", cfg["screen_report"],
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
            if not (self.root / cfg["accepted"]).exists():
                return False, "нет отобранных вакансий — сначала запусти скрининг"
            mode = str(body.get("mode", "all"))
            input_path = self._build_apply_input(track, mode, body.get("marked"))
            if input_path is None:
                return False, "очередь пуста: нет вакансий под выбранный фильтр"
            argv = base + gflags + ["apply", "--input", str(input_path)]
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

    # ---- resumes & tracks ----------------------------------------------
    def fetch_resumes(self, *, refresh: bool = False) -> dict[str, Any]:
        """Read active HH resume titles.

        The live HH read (Playwright, ~10s) runs only on ``refresh``; otherwise
        the last-known result is returned so the tab opens instantly.
        """
        if not refresh:
            return self._resume_cache or {"auth_status": "unknown", "resume_titles": [], "error": ""}
        session_path = self.config.data_dir / "hh_session.json"
        result: dict[str, Any] = {"auth_status": "unknown", "resume_titles": [], "error": ""}
        if not session_path.exists():
            result["error"] = "нет сессии HH (сделай login)"
            return result
        try:
            from .inspection import inspect_resumes
            data = inspect_resumes(session_path)
            result["auth_status"] = data.get("auth_status", "unknown")
            result["resume_titles"] = _clean_resume_titles(list(data.get("resume_titles", [])))
        except RuntimeError as exc:  # playwright missing / read-only failure
            result["error"] = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001 - a browser read failure is reported, not fatal
            result["error"] = f"не удалось прочитать резюме: {str(exc)[:160]}"
        if result["resume_titles"] or not self._resume_cache:
            self._resume_cache = result
        return result

    def tracks_overview(self, *, refresh: bool = False) -> dict[str, Any]:
        """Tracks joined with active HH resumes, flagging mismatches both ways."""
        resumes = self.fetch_resumes(refresh=refresh)
        titles = [str(t).strip() for t in resumes.get("resume_titles", [])]
        title_set = {t.lower() for t in titles}
        used = set()
        tracks = []
        for key, cfg in TRACKS.items():
            wanted = str(cfg.get("resume") or "").strip()
            present = wanted.lower() in title_set if wanted else None
            if present:
                used.add(wanted.lower())
            tracks.append({"key": key, "label": cfg["label"], "type": cfg["type"],
                           "resume": wanted, "profile": cfg["profile"], "search": cfg["search"],
                           "resume_present": present})
        unassigned = [t for t in titles if t.lower() not in {str(c.get("resume", "")).strip().lower()
                                                              for c in TRACKS.values()}]
        return {"tracks": tracks, "hh_resumes": titles, "unassigned_resumes": unassigned,
                "auth_status": resumes.get("auth_status"), "error": resumes.get("error", ""),
                "rubric_types": list(RUBRIC_TYPES)}

    def add_track(self, body: dict[str, Any]) -> dict[str, Any]:
        """Scaffold a new track (config entry + profile/search skeletons)."""
        key = re.sub(r"[^a-z0-9_-]", "", str(body.get("key", "")).strip().lower())
        if not key:
            return {"error": "ключ трека обязателен (латиница, цифры, _-)"}
        if key in TRACKS:
            return {"error": f"трек «{key}» уже существует"}
        rubric = str(body.get("type", "general")).strip().lower()
        if rubric not in RUBRIC_TYPES:
            rubric = "general"
        label = str(body.get("label") or key).strip()
        resume = str(body.get("resume") or "").strip()
        queries = [q.strip() for q in re.split(r"[\n,;]+", str(body.get("queries", ""))) if q.strip()]
        entry = {"key": key, "label": label, "type": rubric, "resume": resume,
                 "profile": f"private/config/profile-{key}.toml",
                 "search": f"private/config/search-{key}.toml"}
        try:
            self._scaffold_profile(self.root / entry["profile"], resume)
            self._scaffold_search(self.root / entry["search"], queries)
            entries = [dict(v) for v in TRACKS.values()] + [entry]
            _write_tracks_config(self.root / TRACKS_CONFIG, entries)
        except OSError as exc:
            return {"error": f"не удалось создать файлы трека: {str(exc)[:160]}"}
        # Reload first so the new key is a recognised settings field, then save
        # any per-track screening criteria supplied with the form.
        load_tracks(self.root)
        criteria = str(body.get("criteria") or "").strip()
        if criteria:
            self.save_settings({f"criteria_{key}": criteria})
        return {"ok": True, "key": key, "profile": entry["profile"], "search": entry["search"],
                "note": "Заполни [professional] в профиле данными из резюме (PDF) и проверь запросы."}

    def _scaffold_profile(self, path: Path, resume: str) -> None:
        if path.exists():
            return
        rt = json.dumps(resume or "ЗАПОЛНИ: точное название резюме на HH", ensure_ascii=False)
        text = (
            '# Профиль нового трека. Заполни [professional] и name данными из резюме (PDF),\n'
            '# проверь [resumes] (точное название резюме на HH) и лимиты. reviewed=false.\n'
            'name = "ЗАПОЛНИ: ФИО кандидата"\nlocation = "ЗАПОЛНИ: город"\nenglish_level = "B1"\n'
            'reviewed = false\naccount = "hh-primary"\n\n'
            '[limits]\nper_run = 15\nper_day = 40\n\n'
            '[apply]\ndelay_min_seconds = 20\ndelay_max_seconds = 45\n'
            'long_pause_every = 8\nlong_pause_min_seconds = 60\nlong_pause_max_seconds = 150\n\n'
            '[screen]\nmodel = "gpt-5-mini"\n'
            'base_url = "https://api.aitunnel.ru/v1/chat/completions"\nconcurrency = 6\n\n'
            '[cover_letter]\nmode = "off"\n\n'
            '[professional]\n# ЗАПОЛНИ из резюме/PDF: summary, skills, [[professional.experience]].\n'
            'summary = ""\nskills = []\n\n'
            f'[resumes]\ndefault = {rt}\n'
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _scaffold_search(self, path: Path, queries: list[str]) -> None:
        if path.exists():
            return
        q = queries or ["ЗАПОЛНИ запрос"]
        q_toml = "[\n" + "".join(f'  {json.dumps(x, ensure_ascii=False)},\n' for x in q) + "]"
        text = (
            '# Поисковая конфигурация нового трека. Проверь запросы, регион и фильтры.\n'
            f'queries = {q_toml}\n'
            'area = [1, 2]  # Москва + СПб; для всей России добавь регионы или используй remote\n'
            'only_remote = true\ndays = 14\nmin_score = 40\n\n'
            '[salary]\nfrom = 0\nmissing = "include"\n\n'
            '[experience]\nallowed = ["noExperience", "between1And3"]\n'
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ---- manual "applied" set (assisted-loop closure) ------------------
    def _applied_path(self) -> Path:
        return self.config.data_dir / "manual-applied.json"

    def _manual_applied(self) -> set[str]:
        data = _read_json(self._applied_path()) or []
        return {str(x) for x in data} if isinstance(data, list) else set()

    def mark_applied(self, vid: str, on: bool = True) -> dict[str, Any]:
        s = self._manual_applied()
        s.add(str(vid)) if on else s.discard(str(vid))
        path = self._applied_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "applied": len(s), "on": on}

    # ---- viewed set (opened on HH → hidden from the pool) --------------
    def _viewed_path(self) -> Path:
        return self.config.data_dir / "viewed.json"

    def _viewed(self) -> set[str]:
        data = _read_json(self._viewed_path()) or []
        return {str(x) for x in data} if isinstance(data, list) else set()

    def mark_viewed(self, vid: str, on: bool = True) -> dict[str, Any]:
        s = self._viewed()
        s.add(str(vid)) if on else s.discard(str(vid))
        path = self._viewed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "viewed": len(s), "on": on}

    # ---- bad set (user marked "не то" → out of the pool and the queue) --
    def _bad_path(self) -> Path:
        return self.config.data_dir / "bad.json"

    def _bad(self) -> set[str]:
        data = _read_json(self._bad_path()) or []
        return {str(x) for x in data} if isinstance(data, list) else set()

    def mark_bad(self, vid: str, on: bool = True) -> dict[str, Any]:
        s = self._bad()
        s.add(str(vid)) if on else s.discard(str(vid))
        path = self._bad_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "bad": len(s), "on": on}

    def export_bad(self) -> str:
        """Dump all vacancies marked 'bad' as Markdown for manual prompt tuning."""
        bad = self._bad()
        if not bad:
            return "# Плохие вакансии\n\nПока пусто — помечай неподходящие кнопкой «👎 плохая».\n"
        # id -> full item (from every scan/accepted snapshot)
        items: dict[str, dict[str, Any]] = {}
        snap_dir = self.root / "private/data/snapshots"
        for path in snap_dir.glob("*.json"):
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            lists = [data.get("items")] + [seg.get("items") for seg in data.get("segments", [])
                                           if isinstance(seg, dict)]
            for lst in lists:
                if isinstance(lst, list):
                    for it in lst:
                        if isinstance(it, dict) and str(it.get("id", "")):
                            items.setdefault(str(it["id"]), it)
        # id -> screener verdict/reason/track
        verdicts: dict[str, dict[str, Any]] = {}
        for track, cfg in TRACKS.items():
            report = _read_json(self.root / cfg["screen_report"]) or {}
            for r in report.get("results", []) if isinstance(report, dict) else []:
                verdicts.setdefault(str(r.get("id", "")), {**r, "track": track})
        lines = [f"# Плохие вакансии ({len(bad)}) — для ручной донастройки промпта скрининга", "",
                 ("Помечены оператором как «не то». Разбирай общие признаки и переноси их в "
                  "правила скрининга (вкладка «Настройки»)."), ""]
        for vid in sorted(bad):
            it = items.get(vid, {})
            v = verdicts.get(vid, {})
            name = _disp(it.get("name") or v.get("name") or f"id {vid}")
            company = _disp(it.get("company") or v.get("company") or "")
            lines.append(f"## {name} — {company}".rstrip(" —"))
            lines.append(f"- id: {vid}  ·  url: {it.get('url') or v.get('url') or ''}")
            lines.append(f"- опыт (HH): {it.get('experience', '—')}  ·  зарплата: "
                         f"{_salary_label(it.get('salary'))}  ·  формат: {it.get('schedule', '—')}")
            if v:
                lines.append(f"- вердикт скринера: {v.get('verdict', '?')} "
                             f"fit={v.get('fit_score', '?')} — {_disp(v.get('reason', ''))}")
            desc = _disp(it.get("description", "")).strip()
            if desc:
                lines.append("- описание:")
                lines.append("  " + desc[:1500].replace("\n", "\n  "))
            lines.append("")
        return "\n".join(lines)

    # ---- apply queue (what a run will actually send to) ----------------
    def apply_queue(self, track: str, mode: str = "all", limit: int = 10,
                    marked: list[str] | None = None) -> dict[str, Any]:
        if track not in TRACKS:
            return {"error": "unknown track"}
        rows = self.vacancies(track).get("rows", [])
        marks = {str(m) for m in (marked or [])}
        # A vacancy the user marked "bad" is out of the pool entirely.
        accepted = [r for r in rows if r.get("verdict") in ("FIT", "MAYBE") and not r.get("bad")]
        if mode == "fit":
            accepted = [r for r in accepted if r.get("verdict") == "FIT"]
        elif mode == "marked":
            accepted = [r for r in accepted if str(r.get("id")) in marks]
        queue = [{"id": str(r.get("id")), "name": r.get("name"), "company": r.get("company"),
                  "url": r.get("url"), "verdict": r.get("verdict"), "fit_score": r.get("fit_score"),
                  "exp_label": r.get("exp_label"), "over_experience": r.get("over_experience"),
                  "salary_label": r.get("salary_label"), "blocked": r.get("blocked"),
                  "applied": r.get("applied")} for r in accepted]
        sendable = [q for q in queue if not q["blocked"] and not q["applied"]]
        reviewed = bool(_profile_flag(self.root, track).get("reviewed", False))
        return {"track": track, "mode": mode, "limit": limit, "reviewed": reviewed,
                "total": len(queue), "sendable": len(sendable),
                "will_send": min(int(limit), len(sendable)), "rows": queue}

    def _build_apply_input(self, track: str, mode: str, marked: list[str] | None) -> Path | None:
        """Write a filtered snapshot (by mode) for `apply --input`; None on empty."""
        cfg = TRACKS[track]
        accepted = _read_json(self.root / cfg["accepted"]) or {}
        items = accepted.get("items", []) if isinstance(accepted, dict) else []
        report = _read_json(self.root / cfg["screen_report"]) or {}
        verdict_by = {str(r.get("id")): r.get("verdict") for r in report.get("results", [])}
        marks = {str(m) for m in (marked or [])}
        applied = self._manual_applied()
        bad = self._bad()
        keep = []
        for it in items:
            vid = str(it.get("id"))
            if vid in applied or vid in bad:
                continue
            if mode == "fit" and verdict_by.get(vid) != "FIT":
                continue
            if mode == "marked" and vid not in marks:
                continue
            keep.append(it)
        if not keep:
            return None
        path = self.config.data_dir / "snapshots" / f"apply-input-{track}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 3, "source": "hh.ru", "status": "ok",
                                    "query": f"apply:{track}:{mode}", "items": keep},
                                   ensure_ascii=False), encoding="utf-8")
        return path

    # ---- watch timer (systemd user unit) -------------------------------
    def watch_status(self) -> dict[str, Any]:
        def sc(*args: str) -> str:
            try:
                return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                      text=True, timeout=6, check=False).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""
        installed = (Path.home() / ".config/systemd/user" / WATCH_TIMER).exists()
        log = self.config.data_dir / "watch.log"
        tail = ""
        if log.exists():
            try:
                tail = "\n".join(log.read_text(encoding="utf-8").splitlines()[-6:])
            except OSError:
                tail = ""
        return {"installed": installed,
                "active": sc("is-active", WATCH_TIMER) if installed else "inactive",
                "enabled": sc("is-enabled", WATCH_TIMER) if installed else "disabled",
                "interval_min": self._watch_interval(installed),
                "next": sc("show", WATCH_TIMER, "-p", "NextElapseUSecRealtime", "--value")
                if installed else "",
                "systemctl": bool(shutil.which("systemctl")), "log_tail": tail}

    def _watch_interval(self, installed: bool = True) -> int:
        src = (Path.home() / ".config/systemd/user" / WATCH_TIMER) if installed else \
            (self.root / "packaging" / WATCH_TIMER)
        try:
            m = re.search(r"OnUnitActiveSec\s*=\s*(\d+)\s*(min|h|s)?", src.read_text(encoding="utf-8"))
            if m:
                n, unit = int(m.group(1)), (m.group(2) or "s")
                return n * 60 if unit == "h" else (n if unit == "min" else max(1, n // 60))
        except OSError:
            pass
        return 60

    def watch_control(self, action: str, minutes: int | None = None) -> dict[str, Any]:
        if not shutil.which("systemctl"):
            return {"error": "systemctl не найден — используйте cron (см. packaging/README)"}
        dst_dir = Path.home() / ".config/systemd/user"
        src_dir = self.root / "packaging"
        out: list[str] = []

        def sc(*args: str) -> tuple[int, str]:
            try:
                r = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                   text=True, timeout=15, check=False)
                return r.returncode, (r.stdout + r.stderr).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                return 1, str(exc)

        try:
            if action in ("install", "interval"):
                dst_dir.mkdir(parents=True, exist_ok=True)
                # service: point APPLYPILOT_HOME at this repo
                svc = (src_dir / "applypilot-watch.service").read_text(encoding="utf-8")
                svc = re.sub(r"APPLYPILOT_HOME=\S+", f"APPLYPILOT_HOME={self.root}", svc)
                if f"APPLYPILOT_HOME={self.root}" not in svc:
                    svc = svc.replace("[Service]", f"[Service]\nEnvironment=APPLYPILOT_HOME={self.root}", 1)
                (dst_dir / "applypilot-watch.service").write_text(svc, encoding="utf-8")
                tmr = (src_dir / WATCH_TIMER).read_text(encoding="utf-8")
                mins = max(5, int(minutes or self._watch_interval(False)))
                tmr = re.sub(r"OnUnitActiveSec\s*=\s*\S+", f"OnUnitActiveSec={mins}min", tmr)
                (dst_dir / WATCH_TIMER).write_text(tmr, encoding="utf-8")
                sc("daemon-reload")
                out.append(f"units → {dst_dir}, интервал {mins} мин")
            if action in ("install", "enable"):
                rc, msg = sc("enable", "--now", WATCH_TIMER)
                out.append(msg or ("включён" if rc == 0 else "не удалось включить"))
            elif action == "interval":
                sc("restart", WATCH_TIMER)
                out.append("интервал обновлён")
            elif action == "disable":
                rc, msg = sc("disable", "--now", WATCH_TIMER)
                out.append(msg or "выключен")
        except OSError as exc:
            return {"error": str(exc)[:200]}
        return {"ok": True, "message": "; ".join(o for o in out if o) or "готово",
                "status": self.watch_status()}


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
            elif parsed.path == "/api/resumes":
                refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] in ("1", "true", "yes")
                self._send(200, app.tracks_overview(refresh=refresh))
            elif parsed.path == "/api/watch":
                self._send(200, app.watch_status())
            elif parsed.path == "/api/bad-export":
                self._send(200, app.export_bad().encode("utf-8"), "text/markdown")
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
            elif parsed.path == "/api/track":
                res = app.add_track(body if isinstance(body, dict) else {})
                self._send(200 if res.get("ok") else 400, res)
            elif parsed.path == "/api/queue":
                b = body if isinstance(body, dict) else {}
                self._send(200, app.apply_queue(str(b.get("track", "ai")), str(b.get("mode", "all")),
                                                 int(b.get("limit") or 10), b.get("marked")))
            elif parsed.path == "/api/applied":
                b = body if isinstance(body, dict) else {}
                self._send(200, app.mark_applied(str(b.get("id", "")), bool(b.get("on", True))))
            elif parsed.path == "/api/viewed":
                b = body if isinstance(body, dict) else {}
                self._send(200, app.mark_viewed(str(b.get("id", "")), bool(b.get("on", True))))
            elif parsed.path == "/api/bad":
                b = body if isinstance(body, dict) else {}
                self._send(200, app.mark_bad(str(b.get("id", "")), bool(b.get("on", True))))
            elif parsed.path == "/api/watch":
                b = body if isinstance(body, dict) else {}
                res = app.watch_control(str(b.get("action", "")), b.get("minutes"))
                self._send(200 if res.get("ok") else 400, res)
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
:root{--bg:#0f1216;--card:#1a1f27;--card2:#20262f;--fg:#e7ecf3;--mut:#93a1b3;--line:#2b333f;
--fit:#2fbf71;--maybe:#e2b13c;--skip:#e15c5c;--accent:#4c8dff;--warn:#f0883e;--star:#ffd23f;--hdr:60px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:10px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;
flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:30}
h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.3px}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;color:var(--fg);font-size:13px}
.tab.active{border-color:var(--accent);color:#fff;background:#223049}
.bal{margin-left:auto;display:flex;gap:14px;align-items:center;font-size:13px}
.bal b{color:var(--fit)}
main{padding:18px;max-width:1480px;margin:0 auto}
.grid{display:grid;gap:12px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.two{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{margin:0 0 10px;font-size:12px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:.6px}
.big{font-size:24px;font-weight:750;line-height:1.1}
.sub{color:var(--mut);font-size:12px;margin-top:4px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 13px;cursor:pointer;font-size:13px}
button.ghost{background:var(--card2);border:1px solid var(--line);color:var(--fg)}
button.mini{padding:5px 10px;font-size:12px}
button.danger{background:var(--skip)}
button:disabled{opacity:.45;cursor:not-allowed}
label{color:var(--mut);font-size:12px}
input,select,textarea{background:#11151b;border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:7px 8px;font-family:inherit;font-size:13px}
textarea{resize:vertical;width:100%;line-height:1.5}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.field>label{font-weight:600}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-top:12px}
table{width:100%;border-collapse:collapse;min-width:900px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;background:#151a21}
tr:hover td{background:#151b22}
td.nowrap,th.nowrap{white-space:nowrap}
td.reason{color:var(--mut);max-width:44ch}
.pill{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap;display:inline-block}
.FIT{background:rgba(47,191,113,.16);color:var(--fit)}
.MAYBE{background:rgba(226,177,60,.16);color:var(--maybe)}
.SKIP{background:rgba(225,92,92,.16);color:var(--skip)}
.ERROR{background:rgba(147,161,179,.16);color:var(--mut)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}
pre{background:#0a0d11;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:70vh;overflow:auto;white-space:pre-wrap;margin:0}
.muted{color:var(--mut)}.hide{display:none}.hl{color:var(--warn)}
.badge{font-size:11px;padding:1px 7px;border-radius:6px;background:var(--card2);border:1px solid var(--line);color:var(--mut);margin-left:6px;white-space:nowrap;display:inline-block}
.fresh{color:var(--fit);border-color:rgba(47,191,113,.4)}
.applied{color:var(--accent);border-color:rgba(76,141,255,.4)}
.star{cursor:pointer;font-size:17px;color:#3a434f;user-select:none}.star.on{color:var(--star)}
.tf{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
.tf:last-child{border-bottom:0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{background:var(--card);border:0;border-right:1px solid var(--line);border-radius:0;color:var(--mut);font-weight:600}
.seg button:last-child{border-right:0}
.seg button.on{background:#223049;color:#fff}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--mut);font-size:12px;margin:8px 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:0}
.settings-grid{display:grid;grid-template-columns:minmax(320px,1fr) 2fr;gap:16px;align-items:start}
@media(max-width:1000px){.settings-grid{grid-template-columns:1fr}}
.qrow.send{background:rgba(47,191,113,.07)}
.chip{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--card2);border:1px solid var(--line);font-size:12px;margin-right:6px}
.stack>*+*{margin-top:14px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.62);display:flex;align-items:center;justify-content:center;z-index:60;padding:16px}
.modal.hide{display:none}
.modal .box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;max-width:720px;width:100%;max-height:90vh;overflow:auto}
.modal h3{margin:0 0 6px;text-transform:none;font-size:16px;color:var(--fg)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite;vertical-align:-2px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>
<header><h1>ApplyPilot</h1>
<div class="tabs" id="tabs">
<div class="tab active" data-t="overview">Обзор</div>
<div class="tab" data-t="vac">Вакансии</div>
<div class="tab" data-t="apply">Отклики</div>
<div class="tab" data-t="stats">Статистика</div>
<div class="tab" data-t="tracks">Резюме и треки</div>
<div class="tab" data-t="log">Лог</div>
<div class="tab" data-t="settings">Настройки</div>
</div>
<div class="bal"><span id="hbal" class="muted"></span><span id="jobstate" class="muted"></span></div>
</header>
<main>
<section id="overview"></section>

<section id="vac" class="hide">
  <div class="row"><label>Трек</label><select id="vtrack"></select>
    <span class="seg" id="vstatusseg"></span>
    <button class="ghost mini" onclick="loadVac()">Обновить</button>
    <span id="vmeta" class="muted"></span></div>
  <div class="row"><span class="seg" id="vseg"></span>
    <label><input type="checkbox" id="vfresh"> свежие</label>
    <label><input type="checkbox" id="vmarked"> отмеченные ★</label></div>
  <div class="legend">
    <span><span class="dot" style="background:var(--fit)"></span>FIT — подходит</span>
    <span><span class="dot" style="background:var(--maybe)"></span>MAYBE — на грани</span>
    <span><span class="dot" style="background:var(--skip)"></span>SKIP — мимо</span>
    <span><span class="dot" style="background:var(--warn)"></span>опыт выше твоего</span>
    <span><span class="dot" style="background:var(--fit)"></span>свежая · ★ пометить</span>
  </div>
  <div class="tablewrap"><div id="vtable"></div></div>
</section>

<section id="apply" class="hide"></section>

<section id="stats" class="hide">
  <div class="grid kpis" id="spendcards"></div>
  <div class="card" style="margin-top:12px"><h3>Расход по дням, ₽</h3><div id="spendbars"></div></div>
  <div class="card" style="margin-top:12px"><h3>Вердикты по трекам</h3><div id="verdictbars"></div></div>
</section>

<section id="tracks" class="hide">
  <div class="card"><div class="row" style="justify-content:space-between"><h3 style="margin:0">Активные резюме на HH и треки</h3>
    <button class="ghost mini" onclick="loadTracks(true)">Обновить с HH</button></div>
    <p class="muted" id="tracksmeta">Число треков задаётся в private/config/tracks.toml. «Обновить с HH» читает активные резюме через сессию (~10с).</p>
    <div class="tablewrap"><div id="trackstable"></div></div>
    <div id="unassigned"></div>
  </div>
  <div class="card" style="margin-top:12px"><div class="row" style="justify-content:space-between"><h3 style="margin:0">Автопоиск свежих вакансий (таймер)</h3>
    <button class="ghost mini" onclick="loadWatch()">Обновить статус</button></div>
    <p class="muted">Регулярный скан свежих + скрининг для всех треков (systemd-таймер). Отклики остаются ручными. «Проверить свежие сейчас» доступно на вкладке «Обзор».</p>
    <div id="watchbox" class="muted">загрузка…</div>
  </div>
  <div class="card" style="margin-top:12px;max-width:760px"><h3>Добавить трек</h3>
    <p class="muted">Создаст скелет profile-&lt;ключ&gt;.toml и search-&lt;ключ&gt;.toml. Дальше заполни в профиле блок [professional] и ФИО данными из резюме (PDF) и проверь запросы.</p>
    <div class="grid two">
      <div class="field"><label>Ключ (латиница)</label><input id="tkey" placeholder="напр. ml, backend"></div>
      <div class="field"><label>Название</label><input id="tlabel" placeholder="напр. ML Engineer"></div>
      <div class="field"><label>Рубрика скрининга</label><select id="ttype"></select></div>
      <div class="field"><label>Резюме на HH (точное название)</label><input id="tresume" placeholder="как в профиле HH"></div>
    </div>
    <div class="field"><label>Поисковые запросы (по одному в строке)</label>
      <textarea id="tqueries" rows="4" placeholder="LLM Engineer&#10;AI Agent Engineer&#10;RAG Engineer"></textarea></div>
    <div class="field"><label>Доп. правила скрининга (необязательно)</label><textarea id="tcriteria" rows="3"></textarea></div>
    <div class="row"><button onclick="addTrack()">Создать трек</button><span id="tmsg" class="muted"></span></div>
  </div>
</section>

<section id="log" class="hide">
  <div class="row"><span id="logmeta" class="muted"></span>
    <button class="ghost mini" id="logrefresh" onclick="refreshJob()">Обновить</button>
    <button class="danger mini" id="logstop" onclick="stopJob()">Стоп</button></div>
  <pre id="logbox"></pre>
</section>

<section id="settings" class="hide">
  <div class="settings-grid">
    <div class="stack">
      <div class="card"><h3>Модель и ключ (LLM: скрининг и письма)</h3>
        <div class="field"><label>Модель</label><select id="smodel"></select></div>
        <div class="field"><label>Base URL</label><input id="sbase"></div>
        <div class="field"><label>API-ключ (пусто — не менять)</label><input id="skey" type="password" placeholder="sk-aitunnel-..."></div>
        <p class="muted" style="margin:0">Ключ хранится локально (права 600), в интерфейс не возвращается. У каждого пользователя свой ключ.</p>
      </div>
      <div class="card"><h3>Статус</h3>
        <div class="sub" id="sstatus"></div></div>
    </div>
    <div class="stack">
      <div class="card"><h3>Кандидат</h3>
        <div class="field"><label>Ограничения кандидата (опыт, метод работы, интервью, формат)</label>
          <textarea id="sconstraints" style="min-height:180px"></textarea></div>
        <div class="field"><label>Зарплатный ориентир</label>
          <textarea id="ssalary" style="min-height:72px"></textarea></div>
      </div>
      <div class="card"><h3>Правила скрининга по трекам</h3>
        <div id="scrit" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(360px,1fr))"></div></div>
    </div>
  </div>
  <div class="row" style="margin-top:14px"><button onclick="saveSettings()">Сохранить настройки</button><span id="skeystate" class="muted"></span></div>
</section>
</main>

<div id="modal" class="modal hide" onclick="if(event.target===this)closeModal()">
  <div class="box">
    <h3 id="lettertitle">Сопроводительное письмо</h3>
    <div class="muted" id="lettersub" style="margin-bottom:8px"></div>
    <textarea id="lettertext" style="min-height:280px"></textarea>
    <div class="row"><button onclick="copyAndOpen()">Копировать и открыть на HH</button>
      <button class="ghost" onclick="copyLetter()">Копировать</button>
      <button class="ghost" onclick="markApplied()">✓ Отметить: откликнулся</button>
      <button class="ghost" onclick="closeModal()">Закрыть</button>
      <span id="letterhint" class="muted"></span></div>
    <p class="muted">Проверь и при необходимости поправь письмо. Отклик и отправку письма делаешь на HH сам (ассистированный режим). «Отметить» уберёт вакансию из очереди откликов.</p>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const SECTIONS=["overview","vac","apply","stats","tracks","log","settings"];
let tab="overview",TRACKS=[],VFILTER="";
function setHdr(){document.documentElement.style.setProperty('--hdr',(document.querySelector('header').offsetHeight)+'px');}
addEventListener('resize',setHdr);
function show(t){tab=t;location.hash=t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.t===t));
  SECTIONS.forEach(id=>$("#"+id).classList.toggle("hide",id!==t));setHdr();
  ({overview:loadOverview,vac:loadVac,apply:loadApply,settings:loadSettings,stats:loadStats,tracks:()=>loadTracks(false),log:refreshJob}[t]||(()=>{}))();}
document.querySelectorAll(".tab").forEach(el=>el.onclick=()=>show(el.dataset.t));
async function api(p,opt){const r=await fetch(p,opt);return r.json();}
function esc(s){return (s||"").toString().replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function rub(v){return v==null?"—":Number(v).toLocaleString("ru-RU");}
function marks(){try{return new Set(JSON.parse(localStorage.getItem("ap_marks")||"[]"))}catch(e){return new Set()}}
function saveMarks(s){try{localStorage.setItem("ap_marks",JSON.stringify([...s]))}catch(e){}}
function toggleMark(id){const s=marks();s.has(id)?s.delete(id):s.add(id);saveMarks(s);loadVac();}
function fillTrackSelects(tracks){TRACKS=tracks||[];
  for(const id of ["#vtrack","#atrack"]){const sel=$(id);if(!sel)continue;const cur=sel.value;
    sel.innerHTML=TRACKS.map(t=>`<option value="${t.key}">${esc(t.label)}</option>`).join("");
    if(cur&&TRACKS.some(t=>t.key===cur))sel.value=cur;}
  const tt=$("#ttype");if(tt&&!tt.dataset.filled){tt.innerHTML=["ai","infra","general"].map(x=>`<option>${x}</option>`).join("");tt.dataset.filled="1";}}

/* ---- overview ---- */
async function loadOverview(){const d=await api("/api/overview");
  fillTrackSelects(Object.keys(d.tracks||{}).map(k=>({key:k,label:d.tracks[k].label})));
  let h='<div class="grid kpis">';
  h+=card("Баланс, ₽",rub(d.balance),d.balance!=null?"aitunnel":"нет ключа");
  h+=card("Сессия HH",d.session_present?"есть":"нет");
  h+=card("В блок-листе",d.blocked,"уже откликался (исключены)");
  h+='</div><div class="grid two" style="margin-top:12px">';
  for(const k in d.tracks){const t=d.tracks[k];const c=t.counts_unique||t.counts||{};
    h+=`<div class="card"><div class="row" style="justify-content:space-between;margin:0"><h3 style="margin:0">${esc(t.label)}</h3>`
      +(t.fresh_count?`<span class="badge fresh">🟢 свежих ${t.fresh_count}</span>`:"")+`</div>`
      +`<div class="big" style="margin:8px 0">${(c.FIT||0)} <span class="muted" style="font-size:13px">подходящих (FIT)</span></div>`
      +`<div class="row" style="gap:6px;margin:0 0 8px"><span class="pill FIT">FIT ${c.FIT??0}</span><span class="pill MAYBE">MAYBE ${c.MAYBE??0}</span><span class="pill SKIP">SKIP ${c.SKIP??0}</span>${c.ERROR?`<span class="pill ERROR">ERR ${c.ERROR}</span>`:""}</div>`
      +(t.reviewed?'<div class="badge fresh" style="margin:0 0 8px">профиль проверен — реальные отклики разрешены</div>':'<div class="badge" style="margin:0 0 8px;color:var(--warn)">профиль не проверен → реальные отклики заблокированы</div>')
      +`<div class="row" style="margin:0"><button class="mini" onclick="gotoVac('${k}','FIT')">Показать FIT →</button>`
      +`<button class="ghost mini" onclick="gotoApply('${k}')">Откликнуться</button>`
      +`<button class="ghost mini" onclick="job('fresh','${k}')">Проверить свежие</button></div>`;
    if((t.top_fit||[]).length){h+='<div style="margin-top:12px">';
      for(const f of t.top_fit){h+=`<div class="tf"><div><a href="${f.url}" target="_blank">${esc(f.name)}</a>`
        +(f.fresh?'<span class="badge fresh">свежая</span>':"")+`<div class="muted">${esc(f.company||"")} · ${esc(f.exp_label||"")}</div></div>`
        +`<div style="text-align:right;white-space:nowrap"><span class="pill FIT">${f.fit_score}</span><br>`
        +`<button class="ghost mini" style="margin-top:4px" onclick="genLetter('${k}','${f.id}','${encodeURIComponent(f.url||"")}')">Письмо</button></div></div>`;}
      h+='</div>';}
    h+='</div>';}
  h+='</div>';
  h+='<div class="card" style="margin-top:12px"><h3>Прочие действия</h3><div class="row" style="margin:0">'
    +btn("scan","ai","Сканировать AI")+btn("scan","infra","Сканировать DevOps")
    +btn("sync","ai","Синхронизировать статусы с HH")+btn("analytics","ai","Пересчитать аналитику")+'</div></div>';
  $("#overview").innerHTML=h;
  $("#hbal").innerHTML=d.balance!=null?`баланс <b>${rub(d.balance)} ₽</b>`:"";}
function card(t,b,sub){return `<div class="card"><h3>${t}</h3><div class="big">${b}</div>${sub?`<div class="sub">${sub}</div>`:""}</div>`;}
function btn(a,tr,l){return `<button class="ghost mini" onclick="job('${a}','${tr}')">${l}</button>`;}
function gotoVac(track,filter){if(track)$("#vtrack").value=track;VFILTER=filter;show("vac");}
function gotoApply(track){pendingApplyTrack=track;show("apply");}

/* ---- jobs ---- */
async function job(action,track){const body={action,track:track||"ai"};
  const r=await api("/api/job",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok){alert("Не запущено: "+r.message);return;}show("log");refreshJob();}

/* ---- vacancies ---- */
let VSTATUS="active";
function bucket(r){return r.bad?"bad":(r.viewed?"viewed":"active");}
async function loadVac(){const tr=$("#vtrack").value;if(!tr)return;
  const onlyFresh=$("#vfresh").checked,onlyMarked=$("#vmarked").checked,mk=marks();
  const d=await api("/api/vacancies?track="+tr);
  const all=d.rows||[];
  const bc={active:0,viewed:0,bad:0};all.forEach(r=>bc[bucket(r)]++);
  $("#vstatusseg").innerHTML=[["active","Активные",bc.active],["viewed","Просмотренные",bc.viewed],["bad","Плохие",bc.bad]]
    .map(([v,l,n])=>`<button class="${VSTATUS===v?"on":""}" onclick="VSTATUS='${v}';loadVac()">${l} ${n}</button>`).join("");
  let scoped=all.filter(r=>bucket(r)===VSTATUS);
  const cnt={FIT:0,MAYBE:0,SKIP:0};scoped.forEach(r=>cnt[r.verdict]=(cnt[r.verdict]||0)+1);
  const allN=VSTATUS==="active"?((cnt.FIT||0)+(cnt.MAYBE||0)):scoped.length;
  $("#vseg").innerHTML=[["","все",allN],["FIT","FIT",cnt.FIT||0],["MAYBE","MAYBE",cnt.MAYBE||0],["SKIP","SKIP",cnt.SKIP||0]]
    .map(([v,l,n])=>`<button class="${VFILTER===v?"on":""}" onclick="VFILTER='${v}';loadVac()">${l} ${n}</button>`).join("");
  let rows=scoped.filter(r=> VFILTER? r.verdict===VFILTER : (VSTATUS!=="active"||r.verdict!=="SKIP"));
  if(onlyFresh)rows=rows.filter(r=>r.fresh);
  if(onlyMarked)rows=rows.filter(r=>mk.has(String(r.id)));
  const dup=d.folded_duplicates?` · свернуто дублей: ${d.folded_duplicates}`:"";
  const exportBtn=VSTATUS==="bad"?` <button class="ghost mini" onclick="exportBad()">Выгрузить в текст (.md)</button>`:"";
  $("#vmeta").innerHTML=`модель ${esc(d.model||"—")} · показано ${rows.length}${dup}${exportBtn}`;
  let h='<table><tr><th>★</th><th>Вердикт</th><th class="nowrap">fit</th><th class="nowrap">Опыт</th><th class="nowrap">Зарплата</th><th>Вакансия</th><th>Причина</th><th class="nowrap">Статус</th><th></th></tr>';
  for(const r of rows){const id=String(r.id);const on=mk.has(id);
    const expc=r.over_experience?' class="hl nowrap"':' class="nowrap"';
    const badges=(r.fresh?'<span class="badge fresh">свежая</span>':"")+(r.dupes?`<span class="badge">повторов: ${r.dupes}</span>`:"")
      +(r.applied?'<span class="badge applied">откликнулся</span>':"")+(r.viewed?'<span class="badge">просмотрено</span>':"")+(r.bad?'<span class="badge" style="color:var(--skip)">плохая</span>':"");
    const status=r.blocked?'<span class="muted">откликался</span>':(r.applied?'<span class="muted">отмечен</span>':esc(r.db_status||""));
    const badBtn=r.bad?`<button class="ghost mini" onclick="markBad('${id}',false)">вернуть</button>`
      :`<button class="ghost mini" title="пометить как не то" onclick="markBad('${id}',true)">👎 плохая</button>`;
    h+=`<tr><td><span class="star ${on?"on":""}" onclick="toggleMark('${id}')">${on?"★":"☆"}</span></td>
      <td><span class="pill ${r.verdict}">${r.verdict||"?"}</span></td>
      <td class="nowrap">${r.fit_score??""}</td>
      <td${expc} title="${r.over_experience?'требуемый опыт выше твоего':''}">${esc(r.exp_label||"—")}</td>
      <td class="muted nowrap">${esc(r.salary_label||"—")}</td>
      <td><a href="${r.url}" target="_blank" onclick="markViewed('${id}')">${esc(r.name)}</a>${badges}<div class="muted">${esc(r.company||"")}</div></td>
      <td class="reason">${esc(r.reason||"")}</td>
      <td class="nowrap">${status}</td>
      <td class="nowrap"><button class="ghost mini" onclick="genLetter('${tr}','${id}','${encodeURIComponent(r.url||"")}')">Письмо</button> ${badBtn}</td></tr>`;}
  $("#vtable").innerHTML=h+"</table>";}
async function markViewed(id){try{await api("/api/viewed",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})});}catch(e){}
  setTimeout(loadVac,600);}
async function markBad(id,on){await api("/api/bad",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,on})});loadVac();}
async function exportBad(){const r=await fetch("/api/bad-export");const text=await r.text();
  const blob=new Blob([text],{type:"text/markdown;charset=utf-8"});const url=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=url;a.download="bad-vacancies.md";document.body.appendChild(a);a.click();
  a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);}
["vtrack","vfresh","vmarked"].forEach(id=>{const el=$("#"+id);if(el)el.onchange=loadVac;});

/* ---- apply queue ---- */
let pendingApplyTrack=null,applyMode="all";
async function loadApply(){const sec=$("#apply");
  const track=pendingApplyTrack||($("#atrack")&&$("#atrack").value)||(TRACKS[0]&&TRACKS[0].key)||"ai";pendingApplyTrack=null;
  sec.innerHTML=`<div class="card"><div class="row" style="margin:0">
    <label>Трек</label><select id="atrack"></select>
    <span class="seg" id="aseg"></span>
    <label>Лимит</label><input id="alimit" type="number" value="10" style="width:74px">
    <button class="ghost mini" onclick="loadApply()">Обновить</button></div>
    <div id="asummary" class="row" style="margin-top:10px"></div>
    <div class="tablewrap"><div id="aqueue"></div></div>
    <div class="row" style="margin-top:12px"><button class="ghost" onclick="runApply('apply_dry')">Пробный запуск (ничего не отправит)</button></div>
    <div id="areal"></div>
  </div>`;
  $("#atrack").innerHTML=TRACKS.map(t=>`<option value="${t.key}">${esc(t.label)}</option>`).join("");
  $("#atrack").value=track;$("#alimit").value=lastLimit;
  $("#atrack").onchange=loadApply;$("#alimit").onchange=refreshQueue;
  $("#aseg").innerHTML=[["all","все accepted"],["fit","только FIT"],["marked","только ★"]]
    .map(([m,l])=>`<button class="${applyMode===m?"on":""}" onclick="applyMode='${m}';refreshQueue()">${l}</button>`).join("");
  refreshQueue();}
let lastLimit=10;
async function refreshQueue(){const track=$("#atrack").value;lastLimit=+$("#alimit").value||10;
  const body={track,mode:applyMode,limit:lastLimit,marked:[...marks()]};
  const d=await api("/api/queue",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const willSend=d.will_send||0;
  $("#asummary").innerHTML=`<span class="chip">Отправим <b>${willSend}</b> из ${d.sendable} готовых</span>`
    +`<span class="chip">в очереди ${d.total}</span>`
    +(d.reviewed?'<span class="chip" style="color:var(--fit)">профиль проверен</span>':'<span class="chip" style="color:var(--warn)">профиль не проверен — реальная отправка заблокирована</span>');
  let h='<table><tr><th>#</th><th>Вердикт</th><th class="nowrap">fit</th><th class="nowrap">Опыт</th><th class="nowrap">Зарплата</th><th>Вакансия</th><th class="nowrap">Статус</th><th></th></tr>';
  let n=0;
  for(const r of (d.rows||[])){const skip=r.blocked||r.applied;if(!skip)n++;
    const willrow=(!skip&&n<=willSend);
    const st=r.blocked?'откликался':(r.applied?'отмечен':(willrow?'в отправке':'ждёт'));
    h+=`<tr class="qrow ${willrow?'send':''}"><td>${skip?'—':n}</td>
      <td><span class="pill ${r.verdict}">${r.verdict}</span></td><td class="nowrap">${r.fit_score??''}</td>
      <td class="nowrap ${r.over_experience?'hl':''}">${esc(r.exp_label||'—')}</td>
      <td class="muted nowrap">${esc(r.salary_label||'—')}</td>
      <td><a href="${r.url}" target="_blank">${esc(r.name)}</a><div class="muted">${esc(r.company||'')}</div></td>
      <td class="nowrap muted">${st}</td>
      <td><button class="ghost mini" onclick="genLetter('${track}','${r.id}','${encodeURIComponent(r.url||'')}')">Письмо</button></td></tr>`;}
  $("#aqueue").innerHTML=h+"</table>";
  $("#areal").innerHTML=d.reviewed?
    `<hr style="border-color:var(--line)"><div class="row"><input type="checkbox" id="aconfirm"><label for="aconfirm">Подтверждаю реальную отправку ${willSend} откликов работодателям</label></div>
     <div class="row"><button class="danger" onclick="runApply('apply_run')">Отправить реальные отклики</button></div>`
    :`<p class="muted">Реальная отправка заблокирована: в профиле трека <code>reviewed = false</code>. Проверь отобранное пробным запуском; включение реальной отправки — отдельный осознанный шаг.</p>`;}
async function runApply(action){const track=$("#atrack").value;
  const body={action,track,mode:applyMode,limit:+$("#alimit").value||10,marked:[...marks()]};
  if(action==="apply_run"){if(!$("#aconfirm")||!$("#aconfirm").checked){alert("Отметь галку подтверждения");return;}body.confirm=true;}
  const r=await api("/api/job",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok){alert("Не запущено: "+r.message);return;}show("log");refreshJob();}

/* ---- stats ---- */
async function loadStats(){const s=await api("/api/stats");const sp=s.spend||{};
  $("#spendcards").innerHTML=card("Баланс, ₽",rub(sp.balance),sp.balance_live?"aitunnel (live)":"из леджера")
    +card("Сегодня, ₽",sp.today_rub??0)+card("Всего потрачено, ₽",sp.total_rub??0)+card("Запросов к LLM",sp.calls??0);
  const days=sp.by_day||[];const mx=Math.max(1,...days.map(d=>d.rub));
  $("#spendbars").innerHTML=days.map(d=>bar(d.date,d.rub,mx,'var(--accent)','₽')).join("")||'<span class="muted">нет данных</span>';
  let vh="";const vd=s.verdicts||{};for(const k in vd){const t=vd[k],c=t.counts;
    if(!c){vh+=`<div class="muted" style="margin:8px 0">${t.label}: нет отчёта</div>`;continue;}
    const tot=Math.max(1,(c.FIT||0)+(c.MAYBE||0)+(c.SKIP||0)+(c.ERROR||0));
    vh+=`<div style="margin:10px 0"><b>${t.label}</b>`+seg('FIT',c.FIT||0,tot,'var(--fit)')+seg('MAYBE',c.MAYBE||0,tot,'var(--maybe)')+seg('SKIP',c.SKIP||0,tot,'var(--skip)')+seg('ERROR',c.ERROR||0,tot,'var(--mut)')+`</div>`;}
  $("#verdictbars").innerHTML=vh;}
function bar(label,val,mx,color,unit){const w=Math.round(100*val/mx);return `<div class="row" style="gap:8px"><span class="muted" style="width:110px">${label}</span><div style="flex:1;background:#11151b;border-radius:6px"><div style="width:${w}%;background:${color};height:14px;border-radius:6px"></div></div><span style="width:80px;text-align:right">${val}${unit||''}</span></div>`;}
function seg(label,val,tot,color){const w=Math.round(100*val/tot);return `<div class="row" style="gap:8px"><span class="muted" style="width:72px">${label}</span><div style="flex:1;background:#11151b;border-radius:6px"><div style="width:${w}%;background:${color};height:12px;border-radius:6px"></div></div><span style="width:44px;text-align:right">${val}</span></div>`;}

/* ---- job log ---- */
async function refreshJob(){const j=await api("/api/job");const has=!!j.label;
  $("#logbox").textContent=(j.lines||[]).join("\\n")||(has?"":"Задач ещё не запускалось. Лог появится после «Проверить свежие», «Пересканировать», пробного запуска или отклика.");
  $("#logmeta").innerHTML=has?(j.label+(j.running?' <span class="spin"></span> идёт':(" — завершено, код "+j.returncode))):'<span class="muted">нет активных задач</span>';
  $("#logstop").disabled=!j.running;$("#logrefresh").disabled=!has;
  const st=has?(j.label+(j.running?' ▶':' ✓')):"";$("#jobstate").textContent=st;
  if(j.running){const b=$("#logbox");b.scrollTop=b.scrollHeight;}
  return !!j.running;}
async function stopJob(){await api("/api/stop",{method:"POST"});refreshJob();}

/* ---- tracks + watch ---- */
async function loadTracks(refresh){if(refresh)$("#tracksmeta").innerHTML='<span class="spin"></span> читаю резюме с HH…';
  const d=await api("/api/resumes"+(refresh?"?refresh=1":""));
  fillTrackSelects((d.tracks||[]).map(t=>({key:t.key,label:t.label})));
  const checked=d.auth_status==="confirmed";
  let h='<table><tr><th>Трек</th><th>Рубрика</th><th>Резюме на HH</th><th class="nowrap">Статус</th><th>Файлы</th></tr>';
  for(const t of (d.tracks||[])){let st;
    if(!checked)st='<span class="badge">не проверено</span>';
    else if(t.resume_present===true)st='<span class="pill FIT">на HH ✓</span>';
    else if(t.resume_present===false)st='<span class="pill SKIP">нет на HH</span>';
    else st='<span class="muted">резюме не указано</span>';
    h+=`<tr><td><b>${esc(t.label)}</b><div class="muted">${t.key}</div></td><td>${t.type}</td>
      <td>${esc(t.resume||"—")}</td><td class="nowrap">${st}</td>
      <td class="muted" style="font-size:12px">${esc(t.profile)}<br>${esc(t.search)}</td></tr>`;}
  $("#trackstable").innerHTML=h+"</table>";
  const un=(d.unassigned_resumes||[]);
  $("#unassigned").innerHTML=(d.error?`<div class="hl" style="margin-top:8px">${esc(d.error)}</div>`:"")
    +(un.length?`<div style="margin-top:10px" class="muted">Резюме на HH без трека: ${un.map(esc).join(", ")}. Заведи под них трек ниже.</div>`
    :(checked?'<div class="muted" style="margin-top:10px">Все активные резюме HH привязаны к трекам.</div>':""));
  $("#tracksmeta").textContent="Число треков задаётся в private/config/tracks.toml. «Обновить с HH» читает активные резюме через сессию (~10с).";
  loadWatch();}
async function loadWatch(){const w=await api("/api/watch");
  if(!w.systemctl){$("#watchbox").innerHTML='<span class="hl">systemctl недоступен — используй cron (см. packaging/README).</span>';return;}
  const active=w.active==="active";
  let h=`<div class="row" style="margin:0">
    <span class="chip">${w.installed?(active?'<b style="color:var(--fit)">включён</b>':'установлен, выключен'):'не установлен'}</span>
    <label>Интервал, мин</label><input id="winterval" type="number" value="${w.interval_min||60}" style="width:80px">`;
  if(!w.installed||!active)h+=`<button class="mini" onclick="watchCtl('install')">Установить и включить</button>`;
  else h+=`<button class="ghost mini" onclick="watchCtl('interval')">Применить интервал</button><button class="danger mini" onclick="watchCtl('disable')">Выключить</button>`;
  h+=`</div>`;
  if(w.next)h+=`<div class="muted" style="margin-top:6px">следующий запуск: ${esc(w.next)}</div>`;
  h+=w.log_tail?`<pre style="margin-top:8px;max-height:140px">${esc(w.log_tail)}</pre>`:'<div class="muted" style="margin-top:6px">лог автопоиска пуст</div>';
  $("#watchbox").innerHTML=h;}
async function watchCtl(action){const mins=+($("#winterval")&&$("#winterval").value)||60;
  $("#watchbox").innerHTML='<span class="spin"></span> применяю…';
  const r=await api("/api/watch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,minutes:mins})});
  if(r.error)alert("Ошибка: "+r.error);loadWatch();}

/* ---- settings ---- */
async function loadSettings(){const s=await api("/api/settings");fillTrackSelects(s.tracks);
  const sel=$("#smodel");sel.innerHTML="";(s.models||[]).forEach(m=>{const o=document.createElement("option");o.value=m;o.textContent=m;if(m===s.model)o.selected=true;sel.appendChild(o);});
  $("#sbase").value=s.base_url||"";
  $("#sstatus").innerHTML=`Ключ: ${s.key_set?'<b style="color:var(--fit)">задан ✓</b>':'<span class="hl">не задан</span>'}<br>Модель: ${esc(s.model)}<br>Треков: ${(s.tracks||[]).length}`;
  $("#skeystate").textContent=s.key_set?"ключ задан ✓":"ключ не задан";
  $("#sconstraints").value=s.constraints||"";$("#ssalary").value=s.salary_expectation||"";
  const cr=s.criteria||{};
  $("#scrit").innerHTML=(s.tracks||[]).map(t=>`<div class="field"><label>${esc(t.label)} <span class="muted">(${t.type})</span></label><textarea data-crit="${t.key}" style="min-height:150px">${esc(cr[t.key]||"")}</textarea></div>`).join("")||'<span class="muted">нет треков</span>';}
async function saveSettings(){const body={model:$("#smodel").value,base_url:$("#sbase").value,
  constraints:$("#sconstraints").value,salary_expectation:$("#ssalary").value};
  document.querySelectorAll("#scrit textarea[data-crit]").forEach(t=>{body["criteria_"+t.dataset.crit]=t.value;});
  const k=$("#skey").value.trim();if(k)body.api_key=k;
  const s=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  $("#skey").value="";$("#skeystate").textContent=(s.key_set?"ключ задан ✓":"ключ не задан")+" · сохранено ✓";loadSettings();}

/* ---- add track ---- */
async function addTrack(){const body={key:$("#tkey").value,label:$("#tlabel").value,type:$("#ttype").value,
  resume:$("#tresume").value,queries:$("#tqueries").value,criteria:$("#tcriteria").value};
  $("#tmsg").innerHTML='<span class="spin"></span> создаю…';
  const r=await api("/api/track",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(r.error){$("#tmsg").textContent="Ошибка: "+r.error;return;}
  $("#tmsg").textContent="Готово: "+r.profile+" — "+(r.note||"");
  $("#tkey").value=$("#tlabel").value=$("#tresume").value=$("#tqueries").value=$("#tcriteria").value="";loadTracks(false);}

/* ---- letter modal ---- */
let modalCtx={track:"",id:""};
function closeModal(){$("#modal").classList.add("hide");}
function openHH(){const u=$("#modal").dataset.url;if(u)window.open(u,"_blank");
  if(modalCtx.id){try{api("/api/viewed",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:modalCtx.id})});}catch(e){}}}
function copyLetter(){const t=$("#lettertext").value;if(navigator.clipboard)navigator.clipboard.writeText(t).then(()=>$("#letterhint").textContent="скопировано ✓").catch(()=>$("#letterhint").textContent="не удалось скопировать");}
function copyAndOpen(){copyLetter();openHH();}
async function markApplied(){if(!modalCtx.id)return;
  await api("/api/applied",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:modalCtx.id,on:true})});
  $("#letterhint").textContent="отмечено: откликнулся ✓";}
async function genLetter(track,id,url){const m=$("#modal");m.classList.remove("hide");modalCtx={track,id};
  m.dataset.url=decodeURIComponent(url||"");
  $("#lettertitle").textContent="Сопроводительное письмо";$("#lettersub").textContent="";
  $("#letterhint").innerHTML='<span class="spin"></span> генерирую…';$("#lettertext").value="";
  const r=await api("/api/letter",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({track,id})});
  if(r.error){$("#letterhint").textContent="";$("#lettertext").value="Ошибка: "+r.error;return;}
  $("#lettersub").textContent=r.name||"";$("#lettertext").value=r.text||"";
  if(!m.dataset.url&&r.url)m.dataset.url=r.url;
  const ta=$("#lettertext");ta.style.height="auto";ta.style.height=Math.min(500,ta.scrollHeight+8)+"px";
  $("#letterhint").textContent="готово — проверь и нажми «Копировать и открыть на HH»";}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});

/* ---- boot ---- */
let prevRunning=false;
setInterval(async()=>{const running=await refreshJob();
  if(running||prevRunning){if(tab==="vac")loadVac();if(tab==="overview")loadOverview();if(tab==="apply")refreshQueue();}
  prevRunning=running;},3000);
async function init(){setHdr();try{const s=await api("/api/settings");fillTrackSelects(s.tracks);}catch(e){}
  const h=(location.hash||"").replace("#","");show(SECTIONS.includes(h)?h:"overview");}
init();
</script>
</body></html>"""
