from __future__ import annotations

import pytest

from applypilot.balance import fetch_balance


class _FakeResponse:
    def __init__(self, payload, *, status_error: Exception | None = None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_fetch_balance_parses_real_shape():
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        # Реальная форма ответа GET /v1/aitunnel/balance.
        return _FakeResponse({"balance": 296.47})

    result = fetch_balance("sk-aitunnel-test", get=fake_get)
    assert result == pytest.approx(296.47)
    assert captured["url"] == "https://api.aitunnel.ru/v1/aitunnel/balance"
    assert captured["headers"]["Authorization"] == "Bearer sk-aitunnel-test"
    assert captured["timeout"] == 8.0


def test_fetch_balance_with_budget_field():
    def fake_get(url, headers, timeout):
        return _FakeResponse({"balance": 4999.55, "budget": 850.0})

    assert fetch_balance("k", get=fake_get) == pytest.approx(4999.55)


def test_fetch_balance_integer_and_string_values():
    def make(value):
        def fake_get(url, headers, timeout):
            return _FakeResponse({"balance": value})

        return fake_get

    assert fetch_balance("k", get=make(300)) == pytest.approx(300.0)
    assert fetch_balance("k", get=make("296,47")) == pytest.approx(296.47)


def test_fetch_balance_custom_base_url_strips_slash():
    captured = {}

    def fake_get(url, headers, timeout):
        captured["url"] = url
        return _FakeResponse({"balance": 1.0})

    fetch_balance("k", base_url="https://api.aitunnel.ru/", get=fake_get)
    assert captured["url"] == "https://api.aitunnel.ru/v1/aitunnel/balance"


def test_fetch_balance_empty_key_returns_none():
    called = {"n": 0}

    def fake_get(url, headers, timeout):
        called["n"] += 1
        return _FakeResponse({"balance": 1.0})

    assert fetch_balance("", get=fake_get) is None
    assert fetch_balance("   ", get=fake_get) is None
    assert called["n"] == 0


def test_fetch_balance_network_error_returns_none():
    def fake_get(url, headers, timeout):
        raise RuntimeError("network down")

    assert fetch_balance("k", get=fake_get) is None


def test_fetch_balance_http_status_error_returns_none():
    def fake_get(url, headers, timeout):
        return _FakeResponse({"balance": 1.0}, status_error=RuntimeError("401"))

    assert fetch_balance("k", get=fake_get) is None


def test_fetch_balance_bad_json_returns_none():
    def fake_get(url, headers, timeout):
        return _FakeResponse(ValueError("not json"))

    assert fetch_balance("k", get=fake_get) is None


def test_fetch_balance_unknown_shape_returns_none():
    for payload in ({}, {"balance": None}, {"balance": True}, {"balance": "abc"},
                    {"foo": 1}, [1, 2, 3], "text", 42):
        def fake_get(url, headers, timeout, _p=payload):
            return _FakeResponse(_p)

        assert fetch_balance("k", get=fake_get) is None
