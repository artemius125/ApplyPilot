import pytest

from applypilot.config import ConfigError
from applypilot.cover_letters import letter_mode, load_letter_profile, render_template


def test_template_mode_overrides_legacy_llm_enable():
    assert letter_mode({"llm": {"enabled": True}, "cover_letter": {"mode": "template"}}) == "template"
    assert letter_mode({"llm": {"enabled": True}}) == "llm"
    assert letter_mode({}) == "off"


@pytest.mark.parametrize("settings", [{"mode": "automatic"}, {"fallback_to_template": "yes"}, []])
def test_invalid_letter_settings_are_rejected(settings):
    with pytest.raises(ConfigError, match="cover_letter"):
        letter_mode({"cover_letter": settings})


def test_text_files_are_relative_to_profile_directory_and_do_not_mutate_input(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "resume.md").write_text("Опыт работы: разработка сервиса", encoding="utf-8")
    (config_dir / "letter.txt").write_text("Здравствуйте! Интересует {vacancy}.", encoding="utf-8")
    profile = {"professional": {"resume_file": "resume.md"},
               "cover_letter": {"template_file": "letter.txt"}}

    loaded = load_letter_profile(profile, config_dir / "profile.toml")

    assert loaded["professional"]["resume_text"] == "Опыт работы: разработка сервиса"
    assert loaded["cover_letter"]["template"] == "Здравствуйте! Интересует {vacancy}."
    assert "resume_text" not in profile["professional"]
    assert "template" not in profile["cover_letter"]


def test_inline_and_file_resume_are_not_silently_overwritten(tmp_path):
    profile = {"professional": {"resume_text": "inline", "resume_file": "resume.txt"}}

    with pytest.raises(ConfigError, match="resume_text.*resume_file"):
        load_letter_profile(profile, tmp_path / "profile.toml")


@pytest.mark.parametrize("filename", ["missing.txt", "resume.pdf"])
def test_missing_or_unsupported_resume_file_is_an_explicit_error(tmp_path, filename):
    with pytest.raises(ConfigError, match="professional.resume_file"):
        load_letter_profile({"professional": {"resume_file": filename}}, tmp_path / "profile.toml")


def test_template_uses_only_supplied_candidate_facts():
    profile = {"name": "Кандидат", "professional": {
        "summary": "Backend-разработчик", "skills": ["Go", "PostgreSQL"],
        "experience": [{"company": "Пример", "role": "Разработчик", "period": "2022–2024",
                        "achievements": ["Создал API"]}],
        "projects": [{"name": "Сервис", "description": "Автоматизация", "technologies": ["Go"]}],
    }, "cover_letter": {"template": "{name}: {vacancy} в {company}. {summary}. {skills}\n{experience}\n{projects}"}}

    text = render_template({"name": "Backend Engineer", "company": "Компания"}, profile)

    assert "Backend Engineer в Компания" in text
    assert "Go, PostgreSQL" in text
    assert "2022–2024" in text and "Создал API" in text and "Автоматизация" in text


@pytest.mark.parametrize("template", ["{missing}", "{name.__class__}", "{name[0]}", "{name!r}", "{name:>10}", "{"])
def test_template_rejects_unknown_fields_and_format_expressions(template):
    with pytest.raises(ConfigError, match="template"):
        render_template({"name": "Вакансия"}, {"name": "Кандидат", "cover_letter": {"template": template}})


def test_template_missing_required_value_does_not_produce_half_filled_letter():
    with pytest.raises(ConfigError, match="company"):
        render_template({"name": "Вакансия"}, {"cover_letter": {"template": "Ваша компания {company}"}})


def test_template_does_not_reinterpret_braces_in_data():
    assert render_template({"name": "{secret}"}, {"cover_letter": {"template": "{{Hello}} {vacancy}"}}) == "{Hello} {secret}"


def test_blank_template_is_rejected():
    with pytest.raises(ConfigError, match="template"):
        render_template({}, {"cover_letter": {"template": "  "}})
