from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ScanResult:
    items: list[dict[str, Any]]
    status: str
    source: str
    error: str = ""
    query: str = ""
    area: int | None = None
    page: int | None = None
    total: int | None = None
    attempts: int = 1
    requests: int = 1


@dataclass(frozen=True)
class ScanSegment:
    query: str
    area: int
    pages: int
    status: str
    items: int
    error: str = ""
    requests: int = 0


class _BudgetExhausted(Exception):
    pass


@dataclass
class _RequestBudget:
    limit: int | None
    used: int = 0

    def consume(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise _BudgetExhausted
        self.used += 1


def _state(html: str) -> dict[str, Any] | None:
    template = BeautifulSoup(html, "html.parser").find("template", id="HH-Lux-InitialState")
    if not template:
        return None
    try:
        state = json.loads(template.decode_contents())
        return state if isinstance(state, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    company = raw.get("company") or raw.get("employer") or {}
    company_name = company.get("visibleName") or company.get("name", "") if isinstance(company, dict) else str(company)
    snippet = raw.get("snippet") or {}
    vacancy_id = str(raw.get("vacancyId") or raw.get("id") or raw.get("vacancy_id") or "")
    compensation = raw.get("compensation") or raw.get("salary")
    salary = None
    if isinstance(compensation, dict) and (compensation.get("from") is not None or compensation.get("to") is not None):
        salary = {"from": compensation.get("from"), "to": compensation.get("to"),
                  "currency": compensation.get("currencyCode") or compensation.get("currency", "RUR")}
    schedule_id = raw.get("@workSchedule") or raw.get("schedule") or ""
    if isinstance(schedule_id, dict):
        schedule_id = schedule_id.get("id") or schedule_id.get("name", "")
    experience = raw.get("workExperience") or raw.get("experience") or ""
    if isinstance(experience, dict):
        experience = experience.get("id") or experience.get("name", "")
    area = raw.get("area") or ""
    area_name = area.get("name", "") if isinstance(area, dict) else str(area)
    formats: list[str] = []
    for block in raw.get("workFormats") or []:
        if isinstance(block, dict):
            values = block.get("workFormatsElement") or [block.get("id", "")]
            formats.extend(str(value).lower() for value in values if value)
        elif block:
            formats.append(str(block).lower())
    publication_time = raw.get("publicationTime", "")
    if isinstance(publication_time, dict):
        publication_time = publication_time.get("$", "")
    return {
        "id": vacancy_id,
        "name": raw.get("name", ""),
        "company": company_name,
        "url": raw.get("alternate_url") or raw.get("url") or f"https://hh.ru/vacancy/{vacancy_id}",
        "description": " ".join(str(value) for value in (
            raw.get("description", ""),
            snippet.get("requirement", "") if isinstance(snippet, dict) else "",
            snippet.get("responsibility", "") if isinstance(snippet, dict) else "",
        ) if value),
        "salary": salary,
        "experience": experience,
        "area": area_name,
        "schedule": str(schedule_id),
        "work_format": formats,
        "is_remote": str(schedule_id).lower() == "remote" or "remote" in formats,
        "published": str(publication_time),
        "source": "hh.ru/search/vacancy",
    }


def scan(query: str, area: int = 113, page: int = 0, only_remote: bool = False,
         timeout: float = 20.0, session: requests.Session | None = None,
         date_from: str | None = None, max_attempts: int = 2,
         request_budget: int | None = None) -> ScanResult:
    params: dict[str, Any] = {"text": query, "area": area, "page": page,
                              "items_on_page": 50, "search_field": "vacancy_name",
                              "order_by": "relevance"}
    if only_remote:
        params["schedule"] = "remote"
    if date_from:
        params["date_from"] = date_from
    client = session or requests.Session()
    budget = _RequestBudget(request_budget)
    attempts = max(1, min(int(max_attempts), 2))
    for attempt in range(1, attempts + 1):
        try:
            response, redirect_error = _get_hh(client, "https://hh.ru/search/vacancy", params=params,
                                                timeout=timeout, budget=budget)
        except _BudgetExhausted:
            return ScanResult([], "truncated", "hh.ru", "search request budget reached", query, area,
                              page, attempts=attempt, requests=budget.used)
        except requests.RequestException:
            if attempt < attempts and (budget.limit is None or budget.used < budget.limit):
                time.sleep(1)
                continue
            return ScanResult([], "failed", "hh.ru", "network error", query, area, page,
                              attempts=attempt, requests=budget.used)
        if redirect_error:
            return ScanResult([], "failed", "hh.ru", redirect_error, query, area, page,
                              attempts=attempt, requests=budget.used)
        if response.status_code == 403:
            return ScanResult([], "failed", "hh.ru", "HTTP 403 access limited", query, area, page,
                              attempts=attempt, requests=budget.used)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < attempts and (budget.limit is None or budget.used < budget.limit):
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay = min(10.0, max(1.0, float(retry_after)))
                except ValueError:
                    delay = 1.0
                time.sleep(delay)
                continue
            return ScanResult([], "failed", "hh.ru", f"HTTP {response.status_code}", query, area, page,
                              attempts=attempt, requests=budget.used)
        if response.status_code != 200:
            return ScanResult([], "failed", "hh.ru", f"HTTP {response.status_code}", query, area, page,
                              attempts=attempt, requests=budget.used)
        state = _state(response.text)
        if state is None:
            status = "captcha" if any(marker in response.text.lower() for marker in ("captcha", "капч")) else "failed"
            return ScanResult([], status, "hh.ru", "HH-Lux-InitialState not found", query, area, page,
                              attempts=attempt, requests=budget.used)
        result = state.get("vacancySearchResult")
        if (not isinstance(result, dict) or not isinstance(result.get("vacancies"), list)
                or any(not isinstance(item, dict) for item in result["vacancies"])):
            return ScanResult([], "failed", "hh.ru", "unrecognized vacancy search structure", query,
                              area, page, attempts=attempt, requests=budget.used)
        items = [_normalize(item) for item in result["vacancies"]]
        return ScanResult(items, "ok" if items else "empty", "hh.ru", "", query, area, page,
                          int(result.get("totalResults") or 0), attempt, budget.used)
    return ScanResult([], "failed", "hh.ru", "request budget exhausted", query, area, page,
                      attempts=attempts, requests=budget.used)


def scan_many(queries: Iterable[str], areas: Iterable[int], max_pages: int = 1,
              only_remote: bool = False, date_from: str | None = None,
              session: requests.Session | None = None,
              page_limit: int | None = None, start_page: int = 0,
              pause_seconds: float = 1.0,
              request_budget: int | None = None) -> tuple[list[dict[str, Any]], list[ScanSegment]]:
    """Scan a bounded query/area/page matrix and retain segment-level diagnostics.

    ``request_budget`` limits actual search HTTP requests, rather than merely
    the number of query strings.  This keeps a multi-region scan predictable.
    """
    client = session or requests.Session()
    unique: dict[str, dict[str, Any]] = {}
    segments: list[ScanSegment] = []
    queries = [str(query).strip() for query in queries if str(query).strip()]
    areas = [int(area) for area in areas]
    pages = max(1, int(max_pages))
    if page_limit is not None:
        pages = min(pages, max(1, int(page_limit)))
    aborted = False
    remaining_requests = None if request_budget is None else max(0, int(request_budget))
    last_request_at: float | None = None
    for query in queries:
        if aborted:
            break
        for area in areas:
            if aborted:
                break
            if remaining_requests is not None and remaining_requests <= 0:
                segments.append(ScanSegment(query, area, 0, "truncated", 0,
                                            "search request budget reached"))
                aborted = True
                break
            fetched = 0
            segment_status = "empty"
            segment_error = ""
            pages_used = 0
            requests_used = 0
            for page in range(start_page, start_page + pages):
                if remaining_requests is not None and remaining_requests <= 0:
                    segment_status = "truncated"
                    segment_error = "search request budget reached"
                    break
                if last_request_at is not None and pause_seconds:
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < pause_seconds:
                        time.sleep(pause_seconds - elapsed)
                result = scan(query, area, page, only_remote, session=client, date_from=date_from,
                              request_budget=remaining_requests)
                last_request_at = time.monotonic()
                pages_used += 1
                requests_used += result.requests
                if remaining_requests is not None:
                    remaining_requests -= result.requests
                fetched += len(result.items)
                segment_status = result.status
                segment_error = result.error
                for item in result.items:
                    item["query_sources"] = sorted(set(item.get("query_sources", [])) | {query})
                    item["area_sources"] = sorted(set(item.get("area_sources", [])) | {area})
                    key = str(item.get("id") or "")
                    if key:
                        unique.setdefault(key, item)
                        unique[key]["query_sources"] = sorted(set(unique[key].get("query_sources", [])) | {query})
                        unique[key]["area_sources"] = sorted(set(unique[key].get("area_sources", [])) | {area})
                if result.status != "ok" or len(result.items) < 50 or (
                    result.total is not None and (page + 1) * 50 >= result.total
                ):
                    break
            if (segment_status == "ok" and pages_used >= pages and
                    result.total is not None and pages_used * 50 < result.total):
                segment_status = "truncated"
            segments.append(ScanSegment(query, area, pages_used, segment_status,
                                        fetched, segment_error, requests_used))
            if segment_status in {"failed", "captcha"}:
                aborted = True
    return list(unique.values()), segments


def _is_hh_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))


def _get_hh(client: requests.Session, url: str, *, params: dict[str, Any] | None = None,
            timeout: float = 20.0, max_redirects: int = 3,
            budget: _RequestBudget | None = None) -> tuple[Any, str]:
    """Request an HH page while refusing to contact an external redirect target."""
    current_url = url
    current_params = params
    if not _is_hh_url(current_url):
        return None, "invalid HH URL"
    for _ in range(max_redirects + 1):
        if budget is not None:
            budget.consume()
        response = client.get(current_url, params=current_params, timeout=timeout,
                              headers={"User-Agent": "ApplyPilot/0.1"}, allow_redirects=False)
        current_params = None
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, ""
        location = response.headers.get("Location", "")
        if not location:
            return response, "redirect without location"
        next_url = urljoin(current_url, location)
        if not _is_hh_url(next_url):
            return response, "external redirect rejected"
        current_url = next_url
    return response, "too many redirects"


def enrich_items(items: list[dict[str, Any]], limit: int = 0,
                 session: requests.Session | None = None,
                 pause_seconds: float = 0.0) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch full descriptions for a bounded number of normalized HH vacancies."""
    client = session or requests.Session()
    errors: list[str] = []
    selected = items[:max(0, limit)]
    for index, item in enumerate(selected):
        url = str(item.get("url") or "")
        if not _is_hh_url(url):
            item["description_status"] = "invalid_url"
            errors.append(f"{item.get('id', '')}: invalid vacancy URL")
            continue
        response = None
        error = ""
        for attempt in range(1, 3):
            try:
                response, error = _get_hh(client, url)
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(1)
                    continue
                error = "network error"
                break
            if error:
                break
            assert response is not None
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                try:
                    delay = min(10.0, max(1.0, float(response.headers.get("Retry-After", "1"))))
                except ValueError:
                    delay = 1.0
                time.sleep(delay)
                continue
            break
        if error:
            item["description_status"] = "network_error" if error == "network error" else "invalid_redirect"
            errors.append(f"{item.get('id', '')}: {error}")
            continue
        assert response is not None
        if response.status_code in (403, 429):
            item["description_status"] = f"http_{response.status_code}"
            errors.append(f"{item.get('id', '')}: HTTP {response.status_code}")
            continue
        if response.status_code != 200:
            item["description_status"] = f"http_{response.status_code}"
            errors.append(f"{item.get('id', '')}: HTTP {response.status_code}")
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        element = soup.select_one("[data-qa='vacancy-description']") or soup.select_one(".vacancy-description")
        description = element.get_text(" ", strip=True) if element else ""
        if description:
            item["description"] = description
            item["description_status"] = "ok"
            item["description_source"] = "vacancy_page"
        else:
            item["description_status"] = "missing"
            errors.append(f"{item.get('id', '')}: description not found")
        if pause_seconds and index + 1 < len(selected):
            time.sleep(pause_seconds)
    return items, errors


def save_snapshot(items: list[dict[str, Any]], directory: Path, query: str,
                  status: str = "ok", error: str = "",
                  segments: list[ScanSegment] | None = None,
                  preset: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"hh_vacancies_{stamp}_{time.time_ns()}.json"
    payload = {"schema_version": 3, "fetched_at": stamp, "query": query,
               "source": "hh.ru", "status": status, "error": error, "items": items,
               "segments": [segment.__dict__ for segment in (segments or [])]}
    if preset:
        payload["preset"] = preset
    fd, temp_name = tempfile.mkstemp(prefix=".snapshot-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
        if status == "ok" or (status == "truncated" and items):
            pointer = directory / "last_successful.json"
            pointer_temp = directory / ".last_successful.tmp"
            pointer_temp.write_text(path.name, encoding="utf-8")
            os.replace(pointer_temp, pointer)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def stamp_first_seen(items: list[dict[str, Any]], registry_path: Path,
                     today: str | None = None) -> list[dict[str, Any]]:
    """Attach and persist the date each vacancy id was first seen by a scan.

    A small ``id -> YYYY-MM-DD`` registry is kept next to the data so a vacancy
    keeps its original discovery date across scans, while genuinely new ids get
    today's date.  Each item gains ``first_seen`` (the recorded date) and
    ``is_new`` (True when that date is today), which lets the UI tell an
    unseen-before vacancy from one that was already in a previous scan.
    """
    today = today or datetime.now(UTC).date().isoformat()
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        if not isinstance(registry, dict):
            registry = {}
    except (OSError, json.JSONDecodeError):
        registry = {}
    changed = False
    for item in items:
        vacancy_id = str(item.get("id") or "")
        if not vacancy_id:
            continue
        seen = str(registry.get(vacancy_id) or "")
        if not seen:
            seen = today
            registry[vacancy_id] = seen
            changed = True
        item["first_seen"] = seen
        item["is_new"] = seen == today
    if changed:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".seen-", dir=registry_path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(registry, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, registry_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return items


def load_items(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else list(payload.get("items", []))
