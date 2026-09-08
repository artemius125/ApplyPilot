from applypilot.inspection import inspect_page


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def inner_text(self, timeout=None):
        if self.selector == "body":
            return self.page.body
        return ""

    def is_visible(self, timeout=None):
        return False

    def __getattr__(self, name):
        if name in {"click", "fill", "evaluate", "press", "submit"}:
            raise AssertionError(f"inspect attempted forbidden action: {name}")
        raise AttributeError(name)


class _Page:
    def __init__(self):
        self.url = ""
        self.body = "Vacancy page"
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector):
        return _Locator(self, selector)

    def __getattr__(self, name):
        if name in {"click", "fill", "evaluate", "press", "submit"}:
            raise AssertionError(f"inspect attempted forbidden action: {name}")
        raise AttributeError(name)


def test_inspect_only_navigates_and_reads_dom():
    page = _Page()
    result = inspect_page(page, {"id": "42", "url": "https://hh.ru/vacancy/42"})
    assert page.goto_calls == ["https://hh.ru/vacancy/42"]
    assert result["id"] == "42"
    assert result["apply_button_visible"] is False
    assert result["unknown_conditions"] == ["response control was not found"]


def test_regional_hh_redirect_is_not_marked_as_external_redirect():
    page = _Page()
    page.url = "https://krasnoyarsk.hh.ru/vacancy/42"
    result = inspect_page(page, {"id": "42", "url": "https://hh.ru/vacancy/42"})

    assert result["captcha_or_redirect"] is False
