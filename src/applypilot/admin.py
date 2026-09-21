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

from .config import AppConfig, ConfigError, effective_search
from .storage import Store

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

    def start(self, argv: list[str], label: str) -> tuple[bool, str]:
        with self.lock:
            if self.current is not None and self.current.returncode is None:
                return False, "another job is running"
            job = Job(argv=argv, label=label, started_at=time.time())
            try:
                job.process = subprocess.Popen(
                    argv, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
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


class AdminApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.root
        self.runner = JobRunner(self.root)

    # ---- data endpoints -------------------------------------------------
    def overview(self) -> dict[str, Any]:
        store = Store(self.config.db_path)
        account = "hh-primary"
        session_file = self.config.data_dir / "hh_session.json"
        result: dict[str, Any] = {
            "session_present": session_file.exists(),
            "blocked": len(store.blocked_ids(account)) if self.config.db_path.exists() else 0,
            "tracks": {},
            "job": self.runner.status(),
        }
        for track, cfg in TRACKS.items():
            profile = _profile_flag(self.root, track)
            report = _read_json(self.root / cfg["screen_report"]) or {}
            accepted = _read_json(self.root / cfg["accepted"]) or {}
            result["tracks"][track] = {
                "label": cfg["label"],
                "reviewed": bool(profile.get("reviewed", False)),
                "counts": report.get("counts") if isinstance(report, dict) else None,
                "accepted": len(accepted.get("items", [])) if isinstance(accepted, dict) else 0,
                "has_report": bool(report),
            }
        return result

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
            rows.append({**row, "db_status": statuses.get(vid, ""), "blocked": vid in blocked})
        order = {"FIT": 0, "MAYBE": 1, "SKIP": 2}
        rows.sort(key=lambda r: (order.get(r.get("verdict", ""), 3), -int(r.get("fit_score", 0) or 0)))
        return {"track": track, "model": report.get("model"), "count": len(rows), "rows": rows}

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
        return self.runner.start(argv, f"{action}:{track}")


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
:root{--bg:#0f1216;--card:#1a1f27;--fg:#e7ecf3;--mut:#93a1b3;--line:#2b333f;--fit:#2fbf71;--maybe:#e2b13c;--skip:#e15c5c;--accent:#4c8dff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:650}
.tabs{display:flex;gap:6px}.tab{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;color:var(--fg)}
.tab.active{border-color:var(--accent);color:#fff}
main{padding:18px;max-width:1200px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h3{margin:0 0 8px;font-size:13px;color:var(--mut);font-weight:600}
.big{font-size:22px;font-weight:700}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px}
button.ghost{background:var(--card);border:1px solid var(--line);color:var(--fg)}
button.danger{background:var(--skip)}button:disabled{opacity:.5;cursor:not-allowed}
label{color:var(--mut);font-size:12px}input,select{background:#11151b;border:1px solid var(--line);color:var(--fg);border-radius:6px;padding:6px}
table{width:100%;border-collapse:collapse;margin-top:10px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px}
.pill{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.FIT{background:rgba(47,191,113,.16);color:var(--fit)}.MAYBE{background:rgba(226,177,60,.16);color:var(--maybe)}.SKIP{background:rgba(225,92,92,.16);color:var(--skip)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}
pre{background:#0a0d11;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:360px;overflow:auto;white-space:pre-wrap}
.muted{color:var(--mut)}.hide{display:none}
</style></head><body>
<header><h1>ApplyPilot admin</h1>
<div class="tabs">
<div class="tab active" data-t="overview">Обзор</div>
<div class="tab" data-t="vac">Вакансии</div>
<div class="tab" data-t="apply">Отклики</div>
<div class="tab" data-t="log">Лог</div>
</div>
<span id="jobstate" class="muted" style="margin-left:auto"></span>
</header>
<main>
<section id="overview"></section>
<section id="vac" class="hide">
  <div class="row"><label>Трек</label>
    <select id="vtrack"><option value="ai">AI / LLM</option><option value="infra">DevOps / инфраструктура</option></select>
    <label>Вердикт</label>
    <select id="vfilter"><option value="">все</option><option>FIT</option><option>MAYBE</option><option>SKIP</option></select>
    <button class="ghost" onclick="loadVac()">Обновить</button>
    <span id="vmeta" class="muted"></span></div>
  <div id="vtable"></div>
</section>
<section id="apply" class="hide">
  <div class="card" style="max-width:640px">
    <h3>Запуск откликов</h3>
    <div class="row"><label>Трек</label>
      <select id="atrack"><option value="ai">AI / LLM</option><option value="infra">DevOps / инфраструктура</option></select>
      <label>Лимит</label><input id="alimit" type="number" value="10" style="width:80px">
      <label>Target success</label><input id="atarget" type="number" value="" placeholder="—" style="width:80px"></div>
    <div class="row"><button class="ghost" onclick="job('apply_dry')">Dry-run (безопасно)</button></div>
    <hr style="border-color:var(--line)">
    <div class="row"><input type="checkbox" id="aconfirm"><label for="aconfirm">Подтверждаю реальную отправку откликов работодателям</label></div>
    <div class="row"><button class="danger" onclick="job('apply_run')">Отправить реальные отклики</button></div>
    <p class="muted">Реальная отправка требует галки и <code>reviewed = true</code> в профиле трека.</p>
  </div>
</section>
<section id="log" class="hide"><div class="row"><button class="ghost" onclick="refreshJob()">Обновить</button><button class="danger" onclick="stopJob()">Стоп</button></div><pre id="logbox">—</pre></section>
</main>
<script>
const $=s=>document.querySelector(s);
let tab="overview";
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{tab=t.dataset.t;
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x===t));
  ["overview","vac","apply","log"].forEach(id=>$("#"+id).classList.toggle("hide",id!==tab));
  if(tab==="overview")loadOverview(); if(tab==="vac")loadVac();});
async function api(p,opt){const r=await fetch(p,opt);return r.json();}
async function loadOverview(){const d=await api("/api/overview");
  let h='<div class="grid">';
  h+=card("Сессия HH",d.session_present?"есть":"нет");
  h+=card("В блок-листе",d.blocked);
  for(const k in d.tracks){const t=d.tracks[k];const c=t.counts||{};
    h+=card(t.label,(t.accepted||0)+" принято",
      `FIT ${c.FIT??"–"} · MAYBE ${c.MAYBE??"–"} · SKIP ${c.SKIP??"–"}<br>reviewed: ${t.reviewed?"✅":"—"}`);}
  h+='</div><div class="card" style="margin-top:12px"><h3>Действия</h3><div class="row">'
    +btn("scan","ai","Scan AI")+btn("scan","infra","Scan DevOps")
    +btn("screen","ai","Screen AI")+btn("screen","infra","Screen DevOps")
    +btn("sync","ai","Sync отклики")+btn("analytics","ai","Analytics")+'</div></div>';
  $("#overview").innerHTML=h;}
function card(t,b,sub){return `<div class="card"><h3>${t}</h3><div class="big">${b}</div>${sub?`<div class="muted">${sub}</div>`:""}</div>`;}
function btn(a,tr,l){return `<button class="ghost" onclick="job('${a}','${tr}')">${l}</button>`;}
async function job(action,track){const body={action,track:track||$("#atrack")?.value||"ai"};
  if(action.startsWith("apply")){body.track=$("#atrack").value;body.limit=+$("#alimit").value;
    if($("#atarget").value)body.target=+$("#atarget").value; if(action==="apply_run")body.confirm=$("#aconfirm").checked;}
  const r=await api("/api/job",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!r.ok){alert("Не запущено: "+r.message);return;} tab="log";
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.t==="log"));
  ["overview","vac","apply","log"].forEach(id=>$("#"+id).classList.toggle("hide",id!=="log"));refreshJob();}
async function refreshJob(){const j=await api("/api/job");
  $("#logbox").textContent=(j.lines||[]).join("\\n")||"—";
  $("#jobstate").textContent=j.label?(j.label+(j.running?" ▶ идёт":(" ✓ код "+j.returncode))):"";
  return !!j.running;}
async function stopJob(){await api("/api/stop",{method:"POST"});refreshJob();}
async function loadVac(){const tr=$("#vtrack").value,f=$("#vfilter").value;const d=await api("/api/vacancies?track="+tr);
  $("#vmeta").textContent=`модель ${d.model||"—"} · ${d.count||0} вакансий`;
  let rows=(d.rows||[]).filter(r=>!f||r.verdict===f);
  let h='<table><tr><th>Вердикт</th><th>fit</th><th>score</th><th>Вакансия</th><th>Причина</th><th>Статус</th></tr>';
  for(const r of rows){h+=`<tr><td><span class="pill ${r.verdict}">${r.verdict||"?"}</span></td>
    <td>${r.fit_score??""}</td><td>${r.score??""}</td>
    <td><a href="${r.url}" target="_blank">${esc(r.name)}</a><div class="muted">${esc(r.company||"")}</div></td>
    <td class="muted">${esc(r.reason||"")}</td>
    <td>${r.blocked?'<span class="muted">откликался</span>':(r.db_status||"")}</td></tr>`;}
  $("#vtable").innerHTML=h+"</table>";}
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
let prevRunning=false;
setInterval(async()=>{const running=await refreshJob();
  if(running||prevRunning){ if(tab==="vac")loadVac(); if(tab==="overview")loadOverview(); }
  prevRunning=running;},3000);
loadOverview();
</script>
</body></html>"""
