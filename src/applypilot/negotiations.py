from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from .storage import Store


class SyncError(RuntimeError):
    """Read-only status sync could not produce a trustworthy snapshot."""


STATUS_MAP = {
    "INVITATION": "invitation",
    "DISCARD": "discard",
    "PHONE_INTERVIEW": "phone_interview",
    "INTERVIEW": "interview",
}


def _initial_state(html: str) -> dict[str, Any] | None:
    tag = BeautifulSoup(html, "html.parser").find("template", id="HH-Lux-InitialState")
    if not tag:
        return None
    try:
        return json.loads(tag.decode_contents())
    except json.JSONDecodeError:
        return None


def _cookies(state_path: Path) -> list[dict[str, Any]]:
    data = json.loads(state_path.read_text(encoding="utf-8"))
    return [cookie for cookie in data.get("cookies", []) if cookie.get("name") and cookie.get("value")]


def _parse_topics(state: dict[str, Any]) -> list[dict[str, Any]]:
    topics = (state.get("applicantNegotiations") or {}).get("topicList") or []
    rows = []
    for topic in topics:
        last_state = str(topic.get("lastState") or "")
        status = STATUS_MAP.get(last_state)
        if not status and last_state == "RESPONSE":
            status = "viewed" if topic.get("viewedByOpponent") else "not_viewed"
        rows.append({
            "vacancy_id": str(topic.get("vacancyId") or ""),
            "id": str(topic.get("id") or ""),
            "status": status or last_state or "unknown",
            "name": str(topic.get("vacancyName") or ""),
            "company": str(topic.get("companyName") or ""),
            "updated_at": str(topic.get("lastModified") or ""),
        })
    return [row for row in rows if row["vacancy_id"]]


def sync_statuses(state_path: Path, store: Store, account: str = "default",
                  max_pages: int | None = None, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Read negotiation statuses only; messages and chat endpoints are excluded."""
    try:
        client = requests.Session()
        for cookie in _cookies(state_path):
            client.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ".hh.ru"),
                              path=cookie.get("path", "/"))
        rows: list[dict[str, Any]] = []
        page = 0
        seen_pages: set[tuple[str, ...]] = set()
        while max_pages is None or page < max_pages:
            response = client.get("https://hh.ru/applicant/negotiations", params={"page": page},
                                  timeout=timeout, headers={"User-Agent": "ApplyPilot/0.1"})
            if response.status_code in {403, 429}:
                raise SyncError(f"HTTP {response.status_code}: access limited")
            if response.status_code != 200:
                raise SyncError(f"HTTP {response.status_code}: {response.reason}")
            state = _initial_state(response.text)
            if state is None:
                if "captcha" in response.text.lower():
                    raise SyncError("CAPTCHA detected")
                raise SyncError("HH-Lux-InitialState not found")
            page_rows = _parse_topics(state)
            page_ids = tuple(row["vacancy_id"] for row in page_rows)
            if page_ids and page_ids in seen_pages:
                raise SyncError("repeated negotiation page; stopping without guessing pagination")
            seen_pages.add(page_ids)
            rows.extend(page_rows)
            if not page_rows:
                break
            page += 1
        store.replace_negotiation_statuses(rows, account)
        store.save_sync_snapshot("hh.ru", "ok" if rows else "empty", len(rows), account=account)
        return rows
    except (requests.RequestException, OSError, json.JSONDecodeError) as exc:
        store.save_sync_snapshot("hh.ru", "network_error", 0, str(exc)[:240], account=account)
        raise SyncError(f"network error: {exc}") from exc
    except SyncError as exc:
        store.save_sync_snapshot("hh.ru", "error", 0, str(exc), account=account)
        raise


def sync(*_args, **_kwargs) -> str:
    return "use sync_statuses with an authenticated storage state; chat/messages are disabled"
