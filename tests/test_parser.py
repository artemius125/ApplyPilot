from applypilot.parser import (
    ScanResult,
    _normalize,
    _state,
    enrich_items,
    load_items,
    scan,
    scan_many,
)


def test_missing_hh_initial_state_is_detected():
    assert _state("<html><body>captcha</body></html>") is None


def test_load_legacy_list(tmp_path):
    path = tmp_path / "old.json"
    path.write_text('[{"id": 1, "name": "A"}]', encoding="utf-8")
    assert load_items(path)[0]["id"] == 1


def test_normalizes_hh_lux_vacancy_fields():
    item = _normalize({
        "vacancyId": 42,
        "name": "AI Engineer",
        "company": {"visibleName": "Acme"},
        "compensation": {"from": 100000, "to": 200000, "currencyCode": "RUR"},
        "workExperience": "between1And3",
        "area": {"name": "Красноярск"},
        "@workSchedule": "remote",
        "workFormats": [{"workFormatsElement": ["remote"]}],
        "snippet": {"requirement": "Python", "responsibility": "agents"},
    })
    assert item["id"] == "42"
    assert item["company"] == "Acme"
    assert item["url"] == "https://hh.ru/vacancy/42"
    assert item["salary"]["from"] == 100000
    assert item["is_remote"] is True
    assert "Python" in item["description"] and "agents" in item["description"]


def test_hh_html_fixture_has_expected_normalized_identity():
    from pathlib import Path

    html = Path(__file__).parent.joinpath("fixtures/hh_search.html").read_text(encoding="utf-8")
    state = _state(html)
    item = _normalize(state["vacancySearchResult"]["vacancies"][0])
    assert item["id"] == "fixture-1"
    assert item["url"] == "https://hh.ru/vacancy/fixture-1"


def test_empty_segment_does_not_hide_successful_segment(monkeypatch):
    results = iter([
        ScanResult([{"id": "1", "name": "Python"}], "ok", "hh.ru"),
        ScanResult([], "empty", "hh.ru"),
    ])
    monkeypatch.setattr("applypilot.parser.scan", lambda *args, **kwargs: next(results))

    items, segments = scan_many(["Python", "Go"], [113], max_pages=1)

    assert [item["id"] for item in items] == ["1"]
    assert [segment.status for segment in segments] == ["ok", "empty"]


def test_scan_many_deduplicates_but_keeps_sources(monkeypatch):
    def fake_scan(query, area, page, *args, **kwargs):
        return ScanResult([{"id": "1", "name": query}], "ok", "hh.ru")

    monkeypatch.setattr("applypilot.parser.scan", fake_scan)
    items, segments = scan_many(["Python", "AI"], [54, 113], max_pages=1, pause_seconds=0)

    assert len(items) == 1
    assert items[0]["query_sources"] == ["AI", "Python"]
    assert items[0]["area_sources"] == [54, 113]
    assert len(segments) == 4


def test_scan_many_limits_actual_requests_across_query_area_matrix(monkeypatch):
    calls = []

    def fake_scan(query, area, page, *args, **kwargs):
        calls.append((query, area, page))
        return ScanResult([{"id": f"{query}-{area}-{page}"}], "ok", "hh.ru", total=1)

    monkeypatch.setattr("applypilot.parser.scan", fake_scan)
    items, segments = scan_many(["AI", "LLM"], [1, 2], max_pages=1,
                                request_budget=2, pause_seconds=0)

    assert len(calls) == 2
    assert len(items) == 2
    assert segments[-1].status == "truncated"
    assert segments[-1].error == "search request budget reached"


def test_scan_rejects_external_redirect_without_requesting_target():
    class Response:
        status_code = 302

        def __init__(self):
            self.headers = {"Location": "https://example.com/redirect"}

    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return Response()

    client = Client()
    result = scan("Python", session=client)

    assert result.status == "failed"
    assert result.error == "external redirect rejected"
    assert client.urls == ["https://hh.ru/search/vacancy"]


def test_enrich_items_reads_full_description_without_accepting_external_url():
    class Response:
        status_code = 200
        text = "<div data-qa='vacancy-description'>Python and Go work</div>"

    class Client:
        def get(self, url, **kwargs):
            assert url == "https://hh.ru/vacancy/1"
            assert kwargs["allow_redirects"] is False
            return Response()

    items, errors = enrich_items([
        {"id": "1", "url": "https://hh.ru/vacancy/1"},
        {"id": "2", "url": "https://example.com/vacancy/2"},
    ], limit=2, session=Client())

    assert items[0]["description"] == "Python and Go work"
    assert items[0]["description_status"] == "ok"
    assert items[1]["description_status"] == "invalid_url"
    assert errors == ["2: invalid vacancy URL"]


def test_enrich_items_retries_a_temporary_response_once():
    class Response:
        text = "<div data-qa='vacancy-description'>Python work</div>"

        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}

    class Client:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return Response(503 if self.calls == 1 else 200)

    client = Client()
    items, errors = enrich_items([{"id": "1", "url": "https://hh.ru/vacancy/1"}],
                                 limit=1, session=client)

    assert client.calls == 2
    assert items[0]["description_status"] == "ok"
    assert errors == []


def test_snapshot_is_atomic_and_updates_success_pointer(tmp_path):
    from applypilot.parser import save_snapshot

    path = save_snapshot([{"id": "1"}], tmp_path, "Python", "ok")

    assert path.exists()
    assert (tmp_path / "last_successful.json").read_text(encoding="utf-8") == path.name
