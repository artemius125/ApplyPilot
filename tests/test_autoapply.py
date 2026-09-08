from applypilot.autoapply import apply_one
from applypilot.cli import _run_apply, _submission_preflight
from applypilot.config import AppConfig
from applypilot.session import SessionCheck
from applypilot.storage import Store


class _Locator:
    def __init__(self, page, selector, scope="page", index=None):
        self.page = page
        self.selector = selector
        self.scope = scope
        self.index = index

    @property
    def first(self):
        return self

    def filter(self, has_text=None):
        self.page.resume_filter = has_text or ""
        return self

    def locator(self, selector):
        if selector.startswith("xpath=ancestor::"):
            return _Locator(self.page, selector, "dialog_root")
        if self.scope == "dialog_root":
            return _Locator(self.page, selector, "dialog")
        return _Locator(self.page, selector, self.scope)

    def nth(self, index):
        return _Locator(self.page, self.selector, self.scope, index)

    def count(self):
        selector = self.selector
        if self.scope == "dialog_root":
            return 1 if self.page.dialog else 0
        if "vacancy-response-link" in selector:
            return 1 if self.page.state == "initial" else 0
        if "vacancy-response-letter-input" in selector:
            return 1 if self.page.dialog else 0
        if "vacancy-response-submit-popup" in selector:
            return 1 if self.page.dialog else 0
        if "resume-title" in selector:
            if self.scope == "dialog":
                return self.page.resume_count if self.page.dialog else 0
            return self.page.global_resume_count
        if "task-body" in selector or "vacancy-response-popup__screening" in selector:
            return self.page.question_count if self.page.dialog else 0
        return 1

    def is_visible(self, timeout=None):
        if self.scope == "dialog_root":
            return self.page.dialog
        if "resume-title" in self.selector and self.page.resume_filter:
            return self.page.resume_visible
        return self.count() > 0

    def inner_text(self, timeout=None):
        if "resume-title" in self.selector:
            return self.page.resume_titles[self.index or 0]
        if self.selector == "body" and self.page.state == "submitted":
            self.page.confirmation_reads += 1
            if self.page.confirmation_reads >= self.page.confirm_after_reads:
                self.page.body = "Отклик отправлен"
        return self.page.body

    def get_attribute(self, name):
        if name == "href" and "vacancy-response-link" in self.selector:
            return self.page.response_href
        return None

    def click(self, timeout=None):
        self.page.clicks.append(self.selector)
        if "vacancy-response-link" in self.selector:
            self.page.state = "after_response"
            self.page.dialog = self.page.dialog_after_response
            self.page.body = self.page.body_after_response
        elif "vacancy-response-submit-popup" in self.selector:
            self.page.state = "submitted"
            self.page.body = "Pending"

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
        self.global_resume_count = 3
        self.resume_titles = ["Python", "Python", "Python"]
        self.resume_visible = True
        self.question_count = 0
        self.resume_filter = ""
        self.response_href = ""
        self.clicks = []
        self.fills = []
        self.confirm_after_reads = 1
        self.confirmation_reads = 0

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


def test_global_resume_duplicates_do_not_make_popup_selection_ambiguous():
    page = _Page()
    page.global_resume_count = 4
    page.resume_count = 1

    result = apply_one(page, _item(), "Python")

    assert result.status == "success"


def test_single_preselected_resume_needs_no_resume_option():
    page = _Page()
    page.resume_count = 0

    result = apply_one(page, _item(), "Python")

    assert result.status == "success"


def test_single_visible_preselected_resume_may_differ_from_planned_title():
    page = _Page()
    page.resume_titles = ["AI Agent Engineer"]

    result = apply_one(page, _item(), "Backend Developer")

    assert result.status == "success"


def test_delayed_confirmation_is_polled_before_unknown():
    page = _Page()
    page.confirm_after_reads = 4

    result = apply_one(page, _item(), "Python", confirmation_timeout_seconds=2)

    assert result.status == "success"
    assert page.confirmation_reads == 4


def test_screening_form_in_the_response_dialog_stops_before_submit():
    page = _Page()
    page.question_count = 1

    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert result.note == "screening questions require manual review"
    assert sum("vacancy-response-link" in selector for selector in page.clicks) == 1
    assert not any("submit-popup" in selector for selector in page.clicks)


def test_disabled_popup_requirements_are_manual_before_submit():
    page = _Page(body_after_response=(
        "Чтобы откликнуться, поменяйте видимость резюме. "
        "Сопроводительное письмо обязательное для этой вакансии"
    ))

    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert result.note == "resume visibility must be changed manually"
    assert not any("submit-popup" in selector for selector in page.clicks)


def test_external_navigation_is_manual_not_a_substring_match():
    page = _Page()

    def goto(url, **kwargs):
        page.url = "https://evil.example/hh.ru/vacancy/1"

    page.goto = goto
    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert page.clicks == []


def test_external_response_link_is_manual_before_the_first_click():
    page = _Page()
    page.response_href = "https://ats.example/apply"

    result = apply_one(page, _item(), "Python")

    assert result.status == "needs_manual"
    assert result.note == "application action points to an external ATS"
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


def test_unknown_result_stops_the_entire_run_before_the_next_vacancy(tmp_path, monkeypatch):
    class _Context:
        def new_page(self):
            return object()

        def storage_state(self):
            return {"cookies": []}

        def close(self):
            return None

    class _Browser:
        def new_context(self, **_kwargs):
            return _Context()

        def close(self):
            return None

    class _Playwright:
        class chromium:
            @staticmethod
            def launch(**_kwargs):
                return _Browser()

        def stop(self):
            return None

    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    store = Store(config.db_path)
    selected = [
        {"id": "one", "url": "https://hh.ru/vacancy/1", "resume": "Resume"},
        {"id": "two", "url": "https://hh.ru/vacancy/2", "resume": "Resume"},
    ]
    calls = []

    monkeypatch.setattr("applypilot.cli.effective_search", lambda *_args: {})
    monkeypatch.setattr("applypilot.cli._profile", lambda *_args: {
        "reviewed": True, "account": "account", "limits": {"per_run": 10, "per_day": 10},
        "apply": {"delay_min_seconds": 0, "delay_max_seconds": 0}, "llm": {"enabled": False},
    })
    monkeypatch.setattr("applypilot.cli.check_session", lambda _path: SessionCheck("confirmed", "ok"))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: type("_Starter", (), {
        "start": staticmethod(lambda: _Playwright()),
    })())
    monkeypatch.setattr("applypilot.autoapply.apply_one", lambda _page, item, *_args, **_kwargs: (
        calls.append(item["id"]) or type("_Result", (), {"status": "unknown", "note": "ambiguous"})()
    ))
    monkeypatch.setattr("applypilot.negotiations.sync_statuses", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("applypilot.cli.save_state", lambda *_args: None)

    result = _run_apply(config, store, selected, "run", "account", tmp_path / "input.json", 2)

    assert result == 3
    assert calls == ["one"]
    summary = store.run_summary("run")
    assert summary is not None
    assert summary["run"]["status"] == "stopped_unknown"
    assert summary["counts"] == {"unknown": 1, "prepared": 1}


def test_negotiation_confirmation_turns_unknown_into_success_and_continues(tmp_path, monkeypatch):
    class _Context:
        def new_page(self):
            return object()

        def storage_state(self):
            return {"cookies": []}

        def close(self):
            return None

    class _Browser:
        def new_context(self, **_kwargs):
            return _Context()

        def close(self):
            return None

    class _Playwright:
        class chromium:
            @staticmethod
            def launch(**_kwargs):
                return _Browser()

        def stop(self):
            return None

    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    store = Store(config.db_path)
    selected = [
        {"id": "one", "url": "https://hh.ru/vacancy/1", "resume": "Resume"},
        {"id": "two", "url": "https://hh.ru/vacancy/2", "resume": "Resume"},
    ]
    calls = []

    monkeypatch.setattr("applypilot.cli.effective_search", lambda *_args: {})
    monkeypatch.setattr("applypilot.cli._profile", lambda *_args: {
        "reviewed": True, "account": "account", "limits": {"per_run": 10, "per_day": 10},
        "apply": {"delay_min_seconds": 0, "delay_max_seconds": 0}, "llm": {"enabled": False},
    })
    monkeypatch.setattr("applypilot.cli.check_session", lambda _path: SessionCheck("confirmed", "ok"))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: type("_Starter", (), {
        "start": staticmethod(lambda: _Playwright()),
    })())
    monkeypatch.setattr("applypilot.autoapply.apply_one", lambda _page, item, *_args, **_kwargs: (
        calls.append(item["id"]) or type("_Result", (), {"status": "unknown", "note": "pending"})()
    ))

    def confirm_latest(_state_path, sync_store, account="default", **_kwargs):
        rows = [{"vacancy_id": calls[-1], "status": "not_viewed"}]
        sync_store.replace_negotiation_statuses(rows, account)
        return rows

    monkeypatch.setattr("applypilot.negotiations.sync_statuses", confirm_latest)
    monkeypatch.setattr("applypilot.cli.save_state", lambda *_args: None)

    result = _run_apply(config, store, selected, "run", "account", tmp_path / "input.json", 2)

    assert result == 0
    assert calls == ["one", "two"]
    summary = store.run_summary("run")
    assert summary is not None
    assert summary["run"]["status"] == "completed"
    assert summary["counts"] == {"success": 2}


def test_success_target_replaces_manual_and_leaves_unused_candidate(tmp_path, monkeypatch):
    class _Context:
        def new_page(self): return object()
        def storage_state(self): return {"cookies": []}
        def close(self): return None

    class _Browser:
        def new_context(self, **_kwargs): return _Context()
        def close(self): return None

    class _Playwright:
        class chromium:
            @staticmethod
            def launch(**_kwargs): return _Browser()
        def stop(self): return None

    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    store = Store(config.db_path)
    selected = [
        {"id": value, "url": f"https://hh.ru/vacancy/{value}", "resume": "Resume"}
        for value in ("manual", "one", "two", "unused")
    ]
    calls = []
    statuses = iter(("needs_manual", "success", "success"))
    monkeypatch.setattr("applypilot.cli.effective_search", lambda *_args: {})
    monkeypatch.setattr("applypilot.cli._profile", lambda *_args: {
        "reviewed": True, "account": "account", "limits": {"per_run": 10, "per_day": 10},
        "apply": {"delay_min_seconds": 0, "delay_max_seconds": 0}, "llm": {"enabled": False},
    })
    monkeypatch.setattr("applypilot.cli.check_session", lambda _path: SessionCheck("confirmed", "ok"))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: type("_Starter", (), {
        "start": staticmethod(lambda: _Playwright()),
    })())

    def apply(_page, item, *_args, **_kwargs):
        calls.append(item["id"])
        status = next(statuses)
        return type("_Result", (), {"status": status, "note": status})()

    monkeypatch.setattr("applypilot.autoapply.apply_one", apply)
    monkeypatch.setattr("applypilot.cli.save_state", lambda *_args: None)
    result = _run_apply(
        config, store, selected, "run", "account", tmp_path / "input.json", 4,
        target_success=2,
    )

    assert result == 0
    assert calls == ["manual", "one", "two"]
    summary = store.run_summary("run")
    assert summary is not None
    assert summary["run"]["status"] == "target_reached"
    assert summary["counts"] == {"needs_manual": 1, "success": 2, "prepared": 1}
