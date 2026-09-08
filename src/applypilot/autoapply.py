from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

LOGGER = logging.getLogger(__name__)
SUBMIT_SELECTOR = '[data-qa="vacancy-response-submit-popup"]'
RESUME_SELECTOR = '[data-qa="resume-title"]'
LETTER_SELECTOR = (
    '[data-qa="vacancy-response-letter-input"], '
    '[data-qa="vacancy-response-popup-form-letter-input"]'
)


@dataclass(frozen=True)
class ApplyResult:
    status: str
    note: str


def allowed_hh_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))


def _response_dialog(page, submit):
    """Return the visible response popup containing the final submit control."""
    for selector in (
        "xpath=ancestor::*[@role='dialog'][1]",
        "xpath=ancestor::*[contains(@class,'vacancy-response-popup')][1]",
    ):
        try:
            dialog = submit.locator(selector).first
            if dialog.count() == 1 and dialog.is_visible(timeout=1000):
                return dialog
        except Exception:
            LOGGER.debug("Could not inspect response-dialog root %s", selector, exc_info=True)
    try:
        dialogs = page.locator("[role='dialog']")
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if dialog.is_visible(timeout=1000) and dialog.locator(SUBMIT_SELECTOR).count() == 1:
                return dialog
    except Exception:
        LOGGER.debug("Could not locate the response dialog from page roots", exc_info=True)
    return None


def has_screening_form(page_or_dialog) -> bool:
    """Detect the response-dialog form, not the public vacancy FAQ."""
    for selector in ("[data-qa='task-body']", ".vacancy-response-popup__screening"):
        try:
            if page_or_dialog.locator(selector).first.is_visible(timeout=2000):
                return True
        except Exception:
            LOGGER.debug("Could not inspect response-dialog selector %s", selector, exc_info=True)
            continue
    return False


def _select_resume(page, dialog, submit, resume: str) -> str:
    """Select one exact visible title inside the popup, or accept HH's sole preselection."""
    root = dialog or page
    options = root.locator(RESUME_SELECTOR)
    visible = []
    exact = []
    for index in range(options.count()):
        option = options.nth(index)
        try:
            if not option.is_visible(timeout=1000):
                continue
            lines = [" ".join(line.split()) for line in option.inner_text(timeout=1000).splitlines() if line.strip()]
            visible.append(option)
            if lines and lines[0].casefold() == resume.casefold():
                exact.append(option)
        except Exception:
            LOGGER.debug("Could not inspect a resume option", exc_info=True)
    if len(exact) == 1:
        exact[0].click(timeout=8000)
        return ""
    if len(visible) <= 1 and submit.is_visible(timeout=2000):
        return ""  # HH preselected the only available resume.
    return "resume selection is missing or ambiguous"


def _wait_for_confirmation(page, timeout_seconds: float) -> ApplyResult | None:
    checks = max(1, round(max(0.0, timeout_seconds) * 2) + 1)
    for check in range(checks):
        body = page.locator("body").inner_text().lower()
        if "отклик отправлен" in body or "ваш отклик отправлен" in body or "вы откликнулись" in body:
            return ApplyResult("success", "confirmed by HH page after submission")
        if "вы уже откликались" in body or "уже откликались" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if "captcha" in body or "капч" in body:
            return ApplyResult("needs_manual", "CAPTCHA detected after submission")
        if check + 1 < checks:
            page.wait_for_timeout(500)
    return None


def apply_one(page, item: dict, resume: str, cover_letter: str = "", dry_run: bool = False,
              confirmation_timeout_seconds: float = 15.0) -> ApplyResult:
    if not allowed_hh_url(str(item.get("url", ""))):
        return ApplyResult("needs_manual", "URL is not an allowed HH hostname")
    if not resume.strip():
        return ApplyResult("needs_manual", "resume is not selected")
    if ((item.get("cover_letter_required") or item.get("requires_cover_letter") or item.get("letter_required"))
            and not cover_letter.strip()):
        return ApplyResult("needs_manual", "required cover letter is missing")
    if dry_run:
        return ApplyResult("prepared", f"would apply with resume={resume}")
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "initial navigation left HH hostname")
        body = page.locator("body").inner_text().lower()
        if "вы откликнулись" in body or "отклик отправлен" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if "captcha" in body:
            return ApplyResult("needs_manual", "CAPTCHA detected")
        links = page.locator('[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link"]')
        if links.count() == 0:
            return ApplyResult("needs_manual", "application button not found")
        try:
            href = str(links.first.get_attribute("href") or "")
        except Exception:  # noqa: BLE001 - an unavailable attribute is not a submission outcome
            href = ""
        if href and not allowed_hh_url(urljoin(str(page.url), href)):
            return ApplyResult("needs_manual", "application action points to an external ATS")
        links.first.click(timeout=15000)
        page.wait_for_timeout(1500)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "external ATS")
        body = page.locator("body").inner_text().lower()
        if "отклик отправлен" in body:
            return ApplyResult("success", "confirmed after first response action")
        if "вы уже откликались" in body or "уже откликались" in body:
            return ApplyResult("already_applied", "HH reports an existing application")
        if "captcha" in body:
            return ApplyResult("needs_manual", "CAPTCHA detected")
        submit = page.locator(SUBMIT_SELECTOR).first
        dialog = _response_dialog(page, submit)
        if dialog is None and not submit.is_visible(timeout=2000):
            return ApplyResult("unknown", "first response action had no follow-up dialog")
        response_root = dialog or page
        response_text = response_root.inner_text(timeout=2000).lower() if dialog is not None else body
        if "поменяйте видимость резюме" in response_text:
            return ApplyResult("needs_manual", "resume visibility must be changed manually")
        if "сопроводительное письмо обязательное" in response_text and not cover_letter.strip():
            return ApplyResult("needs_manual", "required cover letter is missing")
        if has_screening_form(response_root):
            return ApplyResult("needs_manual", "screening questions require manual review")
        dialog_controls = response_root.locator(
            f"{RESUME_SELECTOR}, {LETTER_SELECTOR}, {SUBMIT_SELECTOR}"
        )
        if dialog_controls.count() == 0:
            return ApplyResult("unknown", "first response action had no follow-up dialog")
        resume_error = _select_resume(page, dialog, submit, resume)
        if resume_error:
            return ApplyResult("needs_manual", resume_error)
        if cover_letter:
            field = response_root.locator(LETTER_SELECTOR).first
            if field.count() != 1 or not field.is_visible(timeout=2000):
                return ApplyResult("needs_manual", "cover letter field is missing")
            field.fill(cover_letter)
        if has_screening_form(response_root):
            return ApplyResult("needs_manual", "screening questions require manual review")
        if not submit.is_visible(timeout=8000):
            return ApplyResult("needs_manual", "confirmation button not found")
        # HH may keep the popup button briefly non-actionable while its form state settles.
        # Waiting here is still a single click attempt; never force-click or replay it.
        submit.click(timeout=15000)
        page.wait_for_timeout(1500)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "submission navigated to external ATS")
        confirmed = _wait_for_confirmation(page, confirmation_timeout_seconds)
        return confirmed or ApplyResult("unknown", "submission not confirmed by HH page")
    except Exception as exc:  # noqa: BLE001 - an unclear browser outcome is always unknown
        return ApplyResult("unknown", f"browser operation ended ambiguously: {str(exc)[:160]}")
