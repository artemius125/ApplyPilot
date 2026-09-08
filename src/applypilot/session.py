from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionCheck:
    status: str
    detail: str


def classify_session_page(url: str, body: str) -> SessionCheck:
    """Classify an HH applicant page using host, URL and account markers."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    low_body = body.lower()
    if parsed.scheme != "https" or not (hostname == "hh.ru" or hostname.endswith(".hh.ru")):
        return SessionCheck("unknown", f"unexpected page: {url}")
    if "/account/login" in path or "войти" in low_body or "login" in path:
        return SessionCheck("expired", f"redirected to {url}")
    if "captcha" in path or "капч" in low_body:
        return SessionCheck("unknown", f"captcha at {url}")
    if path.startswith("/applicant/"):
        account_markers = ("выйти", "выход", "резюме", "мои отклики", "my resumes", "log out")
        if any(marker in low_body for marker in account_markers):
            return SessionCheck("confirmed", url)
        return SessionCheck("unknown", f"account markers not found: {url}")
    return SessionCheck("unknown", f"unexpected page: {url}")


def validate_state(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid:{exc}"
    if not isinstance(data, dict) or not isinstance(data.get("cookies", []), list):
        return False, "invalid:storage-state"
    return True, f"cookies={len(data['cookies'])}"


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def login(path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install browser support with: pip install -e '.[browser]'") from exc
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = None
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://hh.ru/account/login", wait_until="domcontentloaded", timeout=60000)
            input("Log in in the browser, then press Enter here: ")
            save_state(path, context.storage_state())
        finally:
            if context is not None:
                context.close()
            browser.close()


def check_session(path: Path) -> SessionCheck:
    """Classify the real HH session without using the user's existing browser."""
    ok, detail = validate_state(path)
    if not ok:
        return SessionCheck("missing" if detail == "missing" else "invalid", detail)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return SessionCheck("valid-format", f"{detail}; Playwright is not installed")
    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(path))
            page = context.new_page()
            page.goto("https://hh.ru/applicant/resumes", wait_until="domcontentloaded", timeout=30000)
            body = page.locator("body").inner_text(timeout=5000)
            return classify_session_page(page.url, body)
    except Exception as exc:  # noqa: BLE001 - browser/network errors are classified together
        return SessionCheck("network_error", str(exc))
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.debug("failed to close session context", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                LOGGER.debug("failed to close session browser", exc_info=True)
