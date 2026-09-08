import json

import pytest

from applypilot.cli import _run_apply, main
from applypilot.config import AppConfig
from applypilot.session import SessionCheck
from applypilot.storage import Store


@pytest.fixture
def runner(tmp_path, monkeypatch):
    class Context:
        def new_page(self):
            return object()

        def storage_state(self):
            return {"cookies": []}

        def close(self):
            pass

    class Browser:
        def new_context(self, **kwargs):
            return Context()

        def close(self):
            pass

    class Playwright:
        @property
        def chromium(self):
            return self

        def start(self):
            return self

        def launch(self, **kwargs):
            return Browser()

        def stop(self):
            pass

    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    store = Store(config.db_path)
    profile = {"reviewed": True, "account": "account", "llm": {"enabled": False},
               "apply": {"delay_min_seconds": 0, "delay_max_seconds": 0},
               "limits": {"per_run": 10, "per_day": 10}}
    monkeypatch.setattr("applypilot.cli._profile", lambda *_args: profile)
    monkeypatch.setattr("applypilot.cli.effective_search", lambda *_args: {})
    monkeypatch.setattr("applypilot.cli.check_session", lambda _: SessionCheck("confirmed", "ok"))
    monkeypatch.setattr("applypilot.cli.save_state", lambda *_args: None)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    return config, store, profile


def test_failed_letter_generation_does_not_leave_unknown_submission(runner, monkeypatch):
    config, store, profile = runner
    profile["llm"]["enabled"] = True

    def fail_generation(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    def forbid_submission(*args, **kwargs):
        pytest.fail("submission must not start after generation failed")

    monkeypatch.setattr("applypilot.cli.generate", fail_generation)
    monkeypatch.setattr("applypilot.autoapply.apply_one", forbid_submission)
    item = {"id": "one", "url": "https://hh.ru/vacancy/1", "resume": "Python"}

    result = _run_apply(config, store, [item], "new", "account", config.root / "input.json", 1)

    assert result == 3
    assert store.statuses("account")["one"] == "failed_before_submit"
    assert "one" not in store.blocked_ids("account")
    assert store.run_summary("new")["counts"] == {"failed_before_submit": 1}


def test_new_run_recovers_abandoned_submission_without_retry(runner, monkeypatch):
    config, store, _ = runner
    item = {"id": "one", "url": "https://hh.ru/vacancy/1", "resume": "Python"}
    store.start_run("old", "account", "apply", config.root / "old.json", 1, [item])
    assert store.reserve(item, "old", 10, 10, "account")[0]

    def forbid_submission(*args, **kwargs):
        pytest.fail("abandoned submission must stay blocked")

    monkeypatch.setattr("applypilot.autoapply.apply_one", forbid_submission)

    _run_apply(config, store, [item], "new", "account", config.root / "new.json", 1)

    assert store.statuses("account")["one"] == "unknown"
    assert "one" in store.blocked_ids("account")
    assert store.run_summary("old")["run"]["status"] == "interrupted"


def test_keyboard_interrupt_marks_potential_submission_unknown(runner, monkeypatch):
    config, store, _ = runner
    item = {"id": "one", "url": "https://hh.ru/vacancy/1", "resume": "Python"}

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("applypilot.autoapply.apply_one", interrupt)

    result = _run_apply(config, store, [item], "run", "account", config.root / "input.json", 1)

    assert result == 130
    assert store.statuses("account")["one"] == "unknown"
    assert store.run_summary("run")["run"]["status"] == "interrupted"


def test_explicit_sync_recovers_abandoned_submission_before_reconciling(runner, monkeypatch, capsys):
    config, store, _ = runner
    item = {"id": "one"}
    store.start_run("old", "account", "apply", config.root / "old.json", 1, [item])
    assert store.reserve(item, "old", 10, 10, "account")[0]
    monkeypatch.setattr("applypilot.cli.validate_state", lambda _: (True, "ok"))

    def sync(_path, current_store, account, **kwargs):
        assert current_store.statuses(account)["one"] == "unknown"
        rows = [{"vacancy_id": "one", "status": "viewed"}]
        current_store.replace_negotiation_statuses(rows, account)
        current_store.save_sync_snapshot("hh.ru", "truncated", 1, account=account)
        return rows

    monkeypatch.setattr("applypilot.negotiations.sync_statuses", sync)
    output = config.root / "sync.json"

    result = main(["--data-dir", str(config.data_dir), "sync", "--output", str(output)])

    assert result == 0
    assert store.statuses("account")["one"] == "success"
    assert json.loads(output.read_text())["status"] == "truncated"
    assert "truncated" in capsys.readouterr().out
