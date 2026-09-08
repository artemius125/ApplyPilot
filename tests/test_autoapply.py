from applypilot.autoapply import apply_one
from applypilot.cli import _submission_preflight


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def filter(self, has_text=None):
        self.page.resume_filter = has_text or ""
        return self

    def count(self):
        selector = self.selector
        if "vacancy-response-link" in selector:
            return 1 if self.page.state == "initial" else 0
        if "resume-title" in selector:
            return self.page.resume_count if self.page.dialog else 0
        if "vacancy-response-letter-input" in selector:
            return 1 if self.page.dialog else 0
        if "vacancy-response-submit-popup" in selector:
            return 1 if self.page.dialog else 0
        if "screening" in selector or "question" in selector:
            return self.page.question_count
        return 1

    def is_visible(self, timeout=None):
        if "resume-title" in self.selector and self.page.resume_filter:
            return self.page.resume_visible
        return self.count() > 0

    def inner_text(self, timeout=None):
        return self.page.body

    def click(self, timeout=None):
        self.page.clicks.append(self.selector)
        if "vacancy-response-link" in self.selector:
            self.page.state = "after_response"
            self.page.dialog = self.page.dialog_after_response
            self.page.body = self.page.body_after_response
        elif "vacancy-response-submit-popup" in self.selector:
            self.page.body = "Отклик отправлен"

    def fill(self, value):
        self.page.fills.append(value)


class _Page:
    def __init__(self, *, dialog_after_response=True, body_after_response="Form"):
        self.url = ""
        self.body = "Vacancy"
        self.state = "initial"
        self.dialog = False
        self.dialog_after_response = dialog_after_response
        self.body_after_response = body_after_response
        self.resume_count = 1
        self.resume_visible = True
        self.question_count = 0
        self.resume_filter = ""
        self.clicks = []
        self.fills = []

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        return _Locator(self, selector)

    def wait_for_timeout(self, milliseconds):
        return None


def _item(**extra):
    item = {"id": "1", "url": "https://hh.ru/vacancy/1"}
    item.update(extra)
    return item


def test_missing_resume_is_manual_before_first_action():
    page = _Page()

    result = apply_one(page, _item(), "")

    assert result.status == "needs_manual"
    assert page.clicks == []


def test_required_letter_is_manual_before_first_action():
    page = _Page()

    result = apply_one(page, _item(cover_letter_required=True), "Python")

    assert result.status == "needs_manual"
    assert page.clicks == []


def test_first_action_direct_success_is_not_repeated():
    page = _Page(dialog_after_response=False, body_after_response="Отклик отправлен")

    result = apply_one(page, _item(), "Python")

    assert result.status == "success"
    assert len(page.clicks) == 1


def test_first_action_without_follow_up_is_unknown_and_not_repeated():
    page = _Page(dialog_after_response=False, body_after_response="Vacancy")

    result = apply_one(page, _item(), "Python")

    assert result.status == "unknown"
    assert len(page.clicks) == 1


def test_ambiguous_resume_stops_before_submit():
    page = _Page()
    page.resume_count = 2

    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert len(page.clicks) == 1
    assert not any("submit-popup" in selector for selector in page.clicks)


def test_confirmed_two_step_submission_has_one_submit():
    page = _Page()

    result = apply_one(page, _item(), "Python", "Здравствуйте")

    assert result.status == "success"
    assert sum("vacancy-response-link" in selector for selector in page.clicks) == 1
    assert sum("submit-popup" in selector for selector in page.clicks) == 1
    assert page.fills == ["Здравствуйте"]


def test_external_navigation_is_manual_not_a_substring_match():
    page = _Page()

    def goto(url, **kwargs):
        page.url = "https://evil.example/hh.ru/vacancy/1"

    page.goto = goto
    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert page.clicks == []


def test_submission_preflight_stops_before_a_budget_reservation():
    assert _submission_preflight(_item(), False) == "resume is not selected"
    assert _submission_preflight(_item(resume="Python", url="https://evil.example/hh.ru"), False) == (
        "URL is not an allowed HH hostname"
    )
    assert _submission_preflight(_item(resume="Python", cover_letter_required=True), False) == (
        "required cover letter is missing"
    )
    assert _submission_preflight(_item(resume="Python"), False) == ""
