from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


@dataclass(frozen=True)
class ApplyResult:
    status: str
    note: str


def allowed_hh_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "hh.ru" or host.endswith(".hh.ru"))


def apply_one(page, item: dict, resume: str, cover_letter: str = "", dry_run: bool = False) -> ApplyResult:
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
        if page.locator('[data-qa*="screening"], [data-qa*="question"]').count() > 0:
            return ApplyResult("needs_manual", "screening questions require manual review")
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
        if page.locator('[data-qa*="screening"], [data-qa*="question"]').count() > 0:
            return ApplyResult("needs_manual", "screening questions require manual review")
        dialog_controls = page.locator(
            '[data-qa="resume-title"], [data-qa="vacancy-response-letter-input"], '
            '[data-qa="vacancy-response-submit-popup"]'
        )
        if dialog_controls.count() == 0:
            return ApplyResult("unknown", "first response action had no follow-up dialog")
        resume_option = page.locator('[data-qa="resume-title"]').filter(has_text=resume)
        if resume_option.count() != 1 or not resume_option.is_visible(timeout=2000):
            return ApplyResult("needs_manual", "resume selection is missing or ambiguous")
        resume_option.click(timeout=8000)
        if cover_letter:
            field = page.locator('[data-qa="vacancy-response-letter-input"]').first
            if field.count() != 1 or not field.is_visible(timeout=2000):
                return ApplyResult("needs_manual", "cover letter field is missing")
            field.fill(cover_letter)
        if page.locator('[data-qa*="screening"], [data-qa*="question"]').count() > 0:
            return ApplyResult("needs_manual", "screening questions require manual review")
        submit = page.locator('[data-qa="vacancy-response-submit-popup"]').first
        if not submit.is_visible(timeout=8000):
            return ApplyResult("needs_manual", "confirmation button not found")
        submit.click(timeout=8000)
        page.wait_for_timeout(1500)
        if not allowed_hh_url(str(page.url)):
            return ApplyResult("needs_manual", "submission navigated to external ATS")
        confirmed = "отклик отправлен" in page.locator("body").inner_text().lower()
        return ApplyResult("success" if confirmed else "unknown", "confirmed" if confirmed else "submission not confirmed")
    except Exception as exc:  # noqa: BLE001 - an unclear browser outcome is always unknown
        return ApplyResult("unknown", f"browser operation ended ambiguously: {str(exc)[:160]}")
