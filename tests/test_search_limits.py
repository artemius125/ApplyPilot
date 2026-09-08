import json

import pytest
import requests

from applypilot.cli import main
from applypilot.parser import save_snapshot, scan, scan_many
from applypilot.scoring import filter_candidates


class Response:
    def __init__(self, status=200, state=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = '<template id="HH-Lux-InitialState">' + json.dumps(
            state if state is not None else {"vacancySearchResult": {
                "vacancies": [{"vacancyId": "1", "name": "Go Developer"}], "totalResults": 1,
            }}
        ) + '</template>'


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_one_request_budget_does_not_retry_429(monkeypatch):
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(429), Response()])

    _, segments = scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert len(client.calls) == 1
    assert segments[0].requests == 1


def test_redirect_consumes_request_budget():
    client = Client([Response(302, headers={"Location": "https://hh.ru/search/redirected"}), Response()])

    _, segments = scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert client.calls == ["https://hh.ru/search/vacancy"]
    assert segments[0].status == "truncated"


def test_network_failure_consumes_request_budget(monkeypatch):
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([requests.ConnectionError("offline"), Response()])

    scan_many(["Go"], [113], session=client, request_budget=1, pause_seconds=0)

    assert len(client.calls) == 1


@pytest.mark.parametrize("state", [{}, {"vacancySearchResult": {}}, {"vacancySearchResult": {"vacancies": None}}])
def test_unrecognized_search_structure_is_not_empty_success(state):
    result = scan("Go", session=Client([Response(state=state)]))

    assert result.status == "failed"


def test_cli_shares_actual_request_budget_across_groups(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.parser.time.sleep", lambda _: None)
    client = Client([Response(503), Response(), Response()])
    monkeypatch.setattr("applypilot.parser.requests.Session", lambda: client)
    search = tmp_path / "search.toml"
    search.write_text('''request_budget = 2
details_limit = 0
[[groups]]
name = "first"
queries = ["Go"]
[[groups]]
name = "second"
queries = ["Python"]
''', encoding="utf-8")
    data = tmp_path / "data"

    result = main(["--search", str(search), "--data-dir", str(data), "scan"])

    assert result == 0
    assert len(client.calls) == 2
    snapshot = json.loads(next((data / "snapshots").glob("hh_vacancies_*.json")).read_text())
    assert snapshot["status"] == "truncated"
    assert sum(segment["requests"] for segment in snapshot["segments"]) == 2


@pytest.mark.parametrize("allowed", [None, ["moreThan6"]])
def test_senior_experience_can_be_selected(allowed):
    search = {"role_terms": ["go"], "primary_role_terms": ["go"]}
    if allowed is not None:
        search["experience"] = {"allowed": allowed}
    item = {"id": "1", "name": "Go Backend Engineer", "description": "Golang gRPC PostgreSQL",
            "experience": "moreThan6"}

    selected = filter_candidates([item], {"search": search}, min_score=30)

    assert [candidate.id for candidate in selected] == ["1"]


def test_experience_allowlist_still_excludes_senior_vacancies():
    item = {"id": "1", "name": "Go Backend Engineer", "description": "Golang gRPC PostgreSQL",
            "experience": "moreThan6"}
    search = {"role_terms": ["go"], "experience": {"allowed": ["between1And3"]}}

    assert filter_candidates([item], {"search": search}, min_score=30) == []


def test_empty_truncated_scan_does_not_replace_last_successful_snapshot(tmp_path):
    previous = save_snapshot([{"id": "1"}], tmp_path, "Go", "ok")

    save_snapshot([], tmp_path, "Go", "truncated", "search request budget reached")

    assert (tmp_path / "last_successful.json").read_text() == previous.name
