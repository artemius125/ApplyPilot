import json

import pytest

from applypilot.cli import _prepare_cover_letter, _run_apply, main
from applypilot.config import AppConfig
from applypilot.cover_letters import load_letter_profile
from applypilot.llm import cache_key
from applypilot.session import SessionCheck
from applypilot.storage import Store


def test_template_preparation_never_calls_llm(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("applypilot.cli.generate", lambda *a, **kw: pytest.fail("LLM called"))
    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    profile = {"llm": {"enabled": True}, "cover_letter": {
        "mode": "template", "template": "Здравствуйте! Интересует {vacancy}."}}

    assert _prepare_cover_letter(config, {"name": "Go Developer"}, profile) == (
        "Здравствуйте! Интересует Go Developer.", "template")


def test_off_mode_does_not_load_resume_files_or_call_llm(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.cli.generate", lambda *a, **kw: pytest.fail("LLM called"))
    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    profile = {"cover_letter": {"mode": "off"}, "professional": {"resume_file": "missing.txt"}}

    assert _prepare_cover_letter(config, {}, profile) == ("", "disabled")


@pytest.mark.parametrize("fallback", [True, False])
def test_failed_llm_uses_template_only_when_explicitly_enabled(tmp_path, monkeypatch, fallback):
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("applypilot.cli.generate", fail)
    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    profile = {"cover_letter": {"mode": "llm", "fallback_to_template": fallback,
                                "template": "Отклик на {vacancy}"}}
    if fallback:
        assert _prepare_cover_letter(config, {"name": "Go"}, profile) == ("Отклик на Go", "template_fallback")
    else:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            _prepare_cover_letter(config, {"name": "Go"}, profile)


def test_llm_receives_resume_file_content_and_selected_resume(tmp_path, monkeypatch):
    resume = tmp_path / "resume.md"
    resume.write_text("Разработал очередь задач на Go", encoding="utf-8")
    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    profile = {"cover_letter": {"mode": "llm"}, "professional": {"resume_file": "resume.md"},
               "resumes": {"go": "Go Backend"}}
    received = {}

    def generate(item, current_profile, *args, **kwargs):
        received.update({"item": item, "profile": current_profile})
        return "Письмо", "generated"

    monkeypatch.setattr("applypilot.cli.generate", generate)

    _prepare_cover_letter(config, {"name": "Go Backend Developer"}, profile)

    assert received["profile"]["professional"]["resume_text"] == "Разработал очередь задач на Go"
    assert received["item"]["resume"] == "Go Backend"


def test_editing_resume_file_invalidates_letter_cache(tmp_path):
    resume = tmp_path / "resume.md"
    profile = {"professional": {"resume_file": "resume.md"}}
    resume.write_text("First project", encoding="utf-8")
    before = load_letter_profile(profile, tmp_path / "profile.toml")
    resume.write_text("Second project", encoding="utf-8")
    after = load_letter_profile(profile, tmp_path / "profile.toml")

    assert cache_key({"id": "1"}, before, "model") != cache_key({"id": "1"}, after, "model")


@pytest.mark.parametrize("command", ["letter", "llm"])
def test_preview_uses_template_without_browser_or_llm(tmp_path, monkeypatch, capsys, command):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("applypilot.cli.generate", lambda *a, **kw: pytest.fail("LLM called"))
    monkeypatch.setattr("applypilot.cli.check_session", lambda *a, **kw: pytest.fail("browser called"))
    profile = tmp_path / "profile.toml"
    profile.write_text('[cover_letter]\nmode="template"\ntemplate="Отклик на {vacancy}"\n', encoding="utf-8")
    snapshot = tmp_path / "vacancies.json"
    snapshot.write_text(json.dumps([{"id": "1", "name": "Go"}]), encoding="utf-8")

    assert main(["--profile", str(profile), command, "preview", "--input", str(snapshot), "--id", "1"]) == 0
    assert "source: template\nОтклик на Go" in capsys.readouterr().out
    assert not (tmp_path / "private/data/applypilot.sqlite3").exists()


def test_apply_inserts_required_template_letter_without_llm(tmp_path, monkeypatch):
    class Context:
        def new_page(self): return object()
        def storage_state(self): return {"cookies": []}
        def close(self): pass

    class Browser:
        def new_context(self, **kwargs): return Context()
        def close(self): pass

    class Playwright:
        def start(self): return self
        @property
        def chromium(self): return self
        def launch(self, **kwargs): return Browser()
        def stop(self): pass

    profile = {"reviewed": True, "account": "account", "llm": {"enabled": False},
               "cover_letter": {"mode": "template", "template": "Отклик на {vacancy}"}}
    config = AppConfig(tmp_path, tmp_path / "data", tmp_path / "profile.toml")
    store = Store(config.db_path)
    received = []
    monkeypatch.setattr("applypilot.cli._profile", lambda *args: profile)
    monkeypatch.setattr("applypilot.cli.check_session", lambda _: SessionCheck("confirmed", "ok"))
    monkeypatch.setattr("applypilot.cli.save_state", lambda *args: None)
    monkeypatch.setattr("applypilot.cli.generate", lambda *a, **kw: pytest.fail("LLM called"))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)

    def apply(_page, item, resume, cover_letter, **kwargs):
        received.append(cover_letter)
        return type("Result", (), {"status": "success", "note": "confirmed"})()

    monkeypatch.setattr("applypilot.autoapply.apply_one", apply)
    item = {"id": "1", "name": "Go", "url": "https://hh.ru/vacancy/1", "resume": "Go Backend",
            "cover_letter_required": True}

    assert _run_apply(config, store, [item], "run", "account", tmp_path / "snapshot.json", 1) == 0
    assert received == ["Отклик на Go"]
    assert store.statuses("account")["1"] == "success"


def test_dry_run_does_not_prepare_letters_or_read_missing_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.toml"
    profile.write_text('''[professional]
resume_file="missing.txt"
[cover_letter]
mode="llm"
''', encoding="utf-8")
    snapshot = tmp_path / "vacancies.json"
    snapshot.write_text(json.dumps([{"id": "1", "name": "Go", "score": 90}]), encoding="utf-8")
    monkeypatch.setattr("applypilot.cli._prepare_cover_letter", lambda *a, **kw: pytest.fail("letter prepared"))

    assert main(["--profile", str(profile), "apply", "--dry-run", "--input", str(snapshot)]) == 0
