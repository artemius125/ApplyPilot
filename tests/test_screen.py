from __future__ import annotations

import json

import pytest

from applypilot.screen import ScreenError, parse_verdict, screen_vacancies
from applypilot.cli import main


def test_parse_verdict_plain():
    out = parse_verdict('{"verdict":"FIT","fit_score":85,"reason":"agents"}')
    assert out == {"verdict": "FIT", "fit_score": 85, "reason": "agents"}


def test_parse_verdict_code_fence_and_noise():
    out = parse_verdict('```json\n{"verdict":"skip","fit_score":10,"reason":"ml"}\n```')
    assert out["verdict"] == "SKIP" and out["fit_score"] == 10
    out = parse_verdict('Вот ответ: {"verdict":"MAYBE","fit_score":50,"reason":"mix"} — всё.')
    assert out["verdict"] == "MAYBE"


def test_parse_verdict_clamps_and_validates():
    assert parse_verdict('{"verdict":"FIT","fit_score":250,"reason":""}')["fit_score"] == 100
    assert parse_verdict('{"verdict":"FIT","fit_score":"x","reason":""}')["fit_score"] == 0
    with pytest.raises(ValueError):
        parse_verdict('{"verdict":"GREAT","fit_score":90}')
    with pytest.raises(ValueError):
        parse_verdict("   ")


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_screen_vacancies_merges_and_caches(tmp_path):
    calls = {"n": 0}

    def fake_post(url, json, headers, timeout):
        calls["n"] += 1
        assert headers["Authorization"] == "Bearer test-key"
        return _FakeResponse('{"verdict":"FIT","fit_score":80,"reason":"LLM-агенты"}')

    items = [{"id": "1", "name": "AI Agent Engineer", "company": "Acme",
              "url": "https://hh.ru/vacancy/1", "description": "RAG, LLM, FastAPI", "score": 70}]
    profile = {"name": "Test", "location": "Remote", "answers": {}}

    first = screen_vacancies(items, profile, tmp_path, track="ai",
                             api_key="test-key", post=fake_post)
    assert first[0]["verdict"] == "FIT" and first[0]["fit_score"] == 80
    assert first[0]["source"] == "generated" and first[0]["url"] == "https://hh.ru/vacancy/1"
    assert calls["n"] == 1

    # Second run must hit the cache and not call the model again.
    second = screen_vacancies(items, profile, tmp_path, track="ai",
                              api_key="test-key", post=fake_post)
    assert second[0]["source"] == "cache"
    assert calls["n"] == 1


def test_screen_requires_key(tmp_path):
    with pytest.raises(ScreenError):
        screen_vacancies([{"id": "1", "description": "x"}], {"answers": {}}, tmp_path,
                         api_key="", post=lambda *a, **k: None)


def test_cli_screen_skips_vacancies_in_any_existing_screen_report(tmp_path, monkeypatch):
    data = tmp_path / "private" / "data"
    reports = tmp_path / "private" / "reports"
    reports.mkdir(parents=True)
    (reports / "screen-infra.json").write_text(json.dumps({
        "results": [{"id": "1", "verdict": "SKIP"}],
    }), encoding="utf-8")
    source = tmp_path / "scan.json"
    source.write_text(json.dumps({"items": [{
        "id": "1", "name": "AI Agent Engineer", "description": "Python LLM RAG",
    }]}), encoding="utf-8")
    screened = []
    monkeypatch.setattr("applypilot.cli.screen_vacancies",
                        lambda items, *args, **kwargs: screened.extend(items) or [])

    result = main(["--data-dir", str(data), "screen", "--input", str(source)])

    assert result == 0
    assert screened == []
