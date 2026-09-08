import json

import pytest

from applypilot.negotiations import SyncError, sync_statuses
from applypilot.storage import Store


class _Response:
    def __init__(self, state, status_code=200, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self.text = _state_html(state) if state is not None else "<html>error</html>"


class _Session:
    def __init__(self, responses):
        self.cookies = _Cookies()
        self._responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self._responses)

    def close(self):
        return None


class _Cookies:
    def set(self, *_args, **_kwargs):
        return None


def _state_html(state):
    encoded = json.dumps(state, ensure_ascii=False)
    return f'<template id="HH-Lux-InitialState">{encoded}</template>'


def _state(topic_list):
    return {"applicantNegotiations": {"topicList": topic_list}}


def _state_path(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"cookies": []}', encoding="utf-8")
    return path


def test_sync_rejects_non_list_topic_list_and_preserves_previous_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "old", "status": "viewed"}], "account", complete=True
    )
    monkeypatch.setattr(
        "applypilot.negotiations.requests.Session",
        lambda: _Session([_Response(_state({"unexpected": "shape"}))]),
    )

    with pytest.raises(SyncError, match="topicList"):
        sync_statuses(_state_path(tmp_path), store, "account")

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT vacancy_id,status FROM negotiation_statuses WHERE account=?",
            ("account",),
        ).fetchall()
    assert [tuple(row) for row in rows] == [("old", "viewed")]
    assert store.latest_sync("account")["status"] == "error"


def test_sync_rejects_malformed_topic_entry(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr(
        "applypilot.negotiations.requests.Session",
        lambda: _Session([_Response(_state(["not an object"]))]),
    )

    with pytest.raises(SyncError, match="topic"):
        sync_statuses(_state_path(tmp_path), store, "account")


def test_sync_marks_nonempty_page_limit_as_truncated_and_keeps_partial_rows(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setattr(
        "applypilot.negotiations.requests.Session",
        lambda: _Session([
            _Response(_state([{"vacancyId": "one", "lastState": "RESPONSE"}])),
        ]),
    )

    rows = sync_statuses(_state_path(tmp_path), store, "account", max_pages=1)

    assert [row["vacancy_id"] for row in rows] == ["one"]
    assert store.latest_sync("account")["status"] == "truncated"
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM negotiation_statuses WHERE account=?", ("account",)
        ).fetchone()[0] == 1


def test_sync_marks_empty_terminal_page_as_complete_and_clears_old_rows(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "old", "status": "viewed"}], "account", complete=True
    )
    monkeypatch.setattr(
        "applypilot.negotiations.requests.Session",
        lambda: _Session([
            _Response(_state([])),
        ]),
    )

    assert sync_statuses(_state_path(tmp_path), store, "account") == []
    assert store.latest_sync("account")["status"] == "empty"
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM negotiation_statuses WHERE account=?", ("account",)
        ).fetchone()[0] == 0


def test_sync_failure_after_a_good_page_preserves_previous_rows(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses(
        [{"vacancy_id": "old", "status": "viewed"}], "account", complete=True
    )
    monkeypatch.setattr(
        "applypilot.negotiations.requests.Session",
        lambda: _Session([
            _Response(_state([{"vacancyId": "new", "lastState": "RESPONSE"}])),
            _Response(None, status_code=503, reason="Unavailable"),
        ]),
    )

    with pytest.raises(SyncError, match="HTTP 503"):
        sync_statuses(_state_path(tmp_path), store, "account")

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT vacancy_id,status FROM negotiation_statuses WHERE account=?",
            ("account",),
        ).fetchall()
    assert [tuple(row) for row in rows] == [("old", "viewed")]
    assert store.latest_sync("account")["status"] == "error"


@pytest.mark.parametrize("max_pages", [0, -1])
def test_sync_invalid_page_limit_does_not_clear_history(tmp_path, monkeypatch, max_pages):
    store = Store(tmp_path / "state.sqlite3")
    store.replace_negotiation_statuses([{"vacancy_id": "old", "status": "viewed"}], "account")
    monkeypatch.setattr("applypilot.negotiations.requests.Session", lambda: _Session([]))

    with pytest.raises(SyncError, match="max_pages"):
        sync_statuses(_state_path(tmp_path), store, "account", max_pages=max_pages)

    with store.connect() as conn:
        assert conn.execute(
            "SELECT vacancy_id FROM negotiation_statuses WHERE account=?", ("account",)
        ).fetchone()[0] == "old"
