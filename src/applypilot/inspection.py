from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .autoapply import allowed_hh_url
from .session import classify_session_page

LOGGER = logging.getLogger(__name__)


def _page_state(page: Any) -> tuple[str, str]:
    url = str(page.url)
    body = page.locator("body").inner_text(timeout=5000)
    low_url = url.lower()
    low_body = body.lower()
    if not allowed_hh_url(url):
        return "redirect", body
    if "captcha" in low_url or "captcha" in low_body or "капч" in low_body:
        return "captcha", body
    if "/account/login" in low_url or "/login" in low_url:
        return "expired", body
    return "ok", body


def _same_vacancy(url: str, vacancy_id: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))
            and parsed.path.rstrip("/").endswith(f"/vacancy/{vacancy_id}"))


def inspect_page(page: Any, item: dict[str, Any], auth_status: str = "confirmed",
                 timeout_ms: int = 10_000) -> dict[str, Any]:
    """Read one vacancy page. This adapter deliberately has no click/fill/eval calls."""
    vacancy_id = str(item.get("id") or item.get("vacancyId") or "")
    url = str(item.get("url") or f"https://hh.ru/vacancy/{vacancy_id}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page_status, body = _page_state(page)
    except Exception as exc:  # noqa: BLE001 - a read-only timeout must not abort the whole review
        kind = "timeout" if "timeout" in str(exc).lower() else "unavailable"
        return {
            "id": vacancy_id,
            "url": url,
            "auth_status": auth_status,
            "page_status": kind,
            "available": False,
            "captcha_or_redirect": False,
            "apply_button_visible": False,
            "known_apply_status": "",
            "unknown_conditions": [f"page read {kind}; manual review required"],
        }
    low_body = body.lower()
    apply_button = page.locator(
        "[data-qa='vacancy-response-link-top'], [data-qa*='vacancy-response-button'], "
        "button:has-text('Откликнуться')"
    ).first
    status_text = page.locator(
        "[data-qa*='vacancy-response-status'], [data-qa*='negotiation-status']"
    ).first
    try:
        button_visible = bool(apply_button.is_visible(timeout=1500))
    except Exception:  # noqa: BLE001 - missing controls are an inspectable result
        button_visible = False
    try:
        known_status = status_text.inner_text(timeout=1000).strip()
    except Exception:  # noqa: BLE001 - missing status is not a browser failure
        known_status = ""
    if not known_status:
        if "вы уже откликались" in low_body or "уже откликались" in low_body:
            known_status = "already_applied"
        elif "отклик отправлен" in low_body:
            known_status = "success"
    unknown: list[str] = []
    if page_status == "ok" and not button_visible and not known_status:
        unknown.append("response control was not found")
    if "внешн" in low_body or "external" in low_body:
        unknown.append("external ATS or redirect may be required")
    return {
        "id": vacancy_id,
        "url": str(page.url),
        "auth_status": auth_status,
        "page_status": page_status,
        "available": page_status == "ok",
        "captcha_or_redirect": page_status in {"captcha", "expired"} or not _same_vacancy(page.url, vacancy_id),
        "apply_button_visible": button_visible,
        "known_apply_status": known_status,
        "unknown_conditions": unknown,
    }


def _resume_titles(page: Any) -> list[str]:
    """Read visible resume labels without interacting with the account page."""
    selectors = (
        "[data-qa='resume-title']",
        "[data-qa*='resume-title']",
        "a[href*='/resume/']",
    )
    titles: list[str] = []
    for selector in selectors:
        try:
            values = page.locator(selector).all_inner_texts()
        except Exception:  # noqa: BLE001 - HH markup differs by account/page version
            values = []
        titles.extend(value.strip() for value in values if value.strip())
    return list(dict.fromkeys(titles))


def inspect_resumes(state_path: Path, timeout_ms: int = 10_000) -> dict[str, Any]:
    """Inspect session and available HH resume titles in an isolated read-only context."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install browser support with: pip install -e '.[browser]'") from exc
    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(state_path))
            page = context.new_page()
            page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=timeout_ms)
            body = page.locator("body").inner_text(timeout=5000)
            auth = classify_session_page(page.url, body)
            return {
                "auth_status": auth.status,
                "detail": auth.detail,
                "resume_titles": _resume_titles(page) if auth.status == "confirmed" else [],
            }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.debug("failed to close resume inspection context", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                LOGGER.debug("failed to close resume inspection browser", exc_info=True)


def inspect_items(state_path: Path, items: list[dict[str, Any]], limit: int = 3,
                  timeout_ms: int = 10_000) -> list[str]:
    """Inspect a resume page and requested vacancies in a private read-only context."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install browser support with: pip install -e '.[browser]'") from exc
    results: list[dict[str, Any]] = []
    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(state_path))
            auth_page = context.new_page()
            try:
                auth_page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=timeout_ms)
                _page_status, body = _page_state(auth_page)
                auth_status = classify_session_page(auth_page.url, body).status
            except Exception:
                LOGGER.debug("failed to inspect authentication page", exc_info=True)
                auth_status = "network_error"
            for item in items[:max(0, limit)]:
                if auth_status != "confirmed":
                    results.append({"id": str(item.get("id", "")), "auth_status": auth_status,
                                    "available": False, "unknown_conditions": ["authentication not confirmed"]})
                    continue
                vacancy_page = context.new_page()
                try:
                    results.append(inspect_page(vacancy_page, item, auth_status, timeout_ms))
                finally:
                    vacancy_page.close()
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.debug("failed to close inspection context", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                LOGGER.debug("failed to close inspection browser", exc_info=True)
    return [json.dumps(result, ensure_ascii=False, sort_keys=True) for result in results]
