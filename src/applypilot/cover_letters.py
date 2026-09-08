from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from string import Formatter
from typing import Any

from .config import ConfigError, professional_context


def letter_mode(profile: dict[str, Any]) -> str:
    """Resolve the letter source, preserving profiles that use only llm.enabled."""
    settings = profile.get("cover_letter", {})
    if not isinstance(settings, dict):
        raise ConfigError("cover_letter must be a table")
    if not isinstance(settings.get("fallback_to_template", False), bool):
        raise ConfigError("cover_letter.fallback_to_template must be a boolean")
    mode = settings.get("mode")
    if mode is None:
        llm = profile.get("llm", {})
        if not isinstance(llm, dict) or not isinstance(llm.get("enabled", False), bool):
            raise ConfigError("llm.enabled must be a boolean")
        mode = "llm" if llm.get("enabled", False) else "off"
    if mode not in ("off", "template", "llm"):
        raise ConfigError("cover_letter.mode must be off, template or llm")
    return mode


def load_letter_profile(profile: dict[str, Any], profile_path: Path) -> dict[str, Any]:
    """Load explicitly configured UTF-8 resume and template files beside the profile."""
    result = deepcopy(profile)
    letter_mode(result)
    for section, text_key, file_key in (
        ("professional", "resume_text", "resume_file"),
        ("cover_letter", "template", "template_file"),
    ):
        values = result.setdefault(section, {})
        if not isinstance(values, dict):
            raise ConfigError(f"{section} must be a table")
        text = values.get(text_key, "")
        filename = values.get(file_key, "")
        if not isinstance(text, str) or not isinstance(filename, str):
            raise ConfigError(f"{section}.{text_key} and {file_key} must be strings")
        if text.strip() and filename.strip():
            raise ConfigError(f"configure only one of {section}.{text_key} and {file_key}")
        if not filename.strip():
            continue
        path = Path(filename)
        if not path.is_absolute():
            path = profile_path.parent / path
        if path.suffix.lower() not in {".txt", ".md"}:
            raise ConfigError(f"{section}.{file_key} must be a txt or md file")
        try:
            values[text_key] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"cannot read {section}.{file_key} as utf-8 text") from exc
        if not values[text_key].strip():
            raise ConfigError(f"{section}.{file_key} must not be empty")
    professional_context(result)
    return result


def _records_text(records: list[dict[str, Any]]) -> str:
    lines = []
    for record in records:
        parts = []
        for value in record.values():
            if isinstance(value, list):
                parts.append(", ".join(value))
            elif value:
                parts.append(value)
        if parts:
            lines.append(" — ".join(parts))
    return "\n".join(lines)


def render_template(item: dict[str, Any], profile: dict[str, Any]) -> str:
    """Render an offline letter with allowlisted scalar fields and no expressions."""
    letter_mode(profile)
    template = profile.get("cover_letter", {}).get("template", "")
    if not isinstance(template, str) or not template.strip():
        raise ConfigError("cover_letter.template must contain non-empty text")
    professional = professional_context(profile)
    company = item.get("company") or item.get("employer") or ""
    if isinstance(company, dict):
        company = company.get("name") or company.get("visibleName") or ""
    answers = profile.get("answers", {})
    if not isinstance(answers, dict):
        raise ConfigError("answers must be a table")
    values = {
        "name": profile.get("name", ""),
        "vacancy": item.get("name") or item.get("title") or "",
        "company": company,
        "resume": item.get("resume", ""),
        "motivation": answers.get("motivation", ""),
        "summary": professional.get("summary", ""),
        "resume_text": professional.get("resume_text", ""),
        "skills": ", ".join(professional.get("skills", [])),
        "experience": _records_text(professional.get("experience", [])),
        "projects": _records_text(professional.get("projects", [])),
    }
    try:
        fields = list(Formatter().parse(template))
    except ValueError as exc:
        raise ConfigError("invalid cover_letter.template braces") from exc
    output = []
    for literal, field, format_spec, conversion in fields:
        output.append(literal)
        if field is None:
            continue
        if field not in values or format_spec or conversion:
            raise ConfigError("unsupported field or expression in cover_letter.template")
        value = values[field]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"cover_letter.template requires a non-empty {field}")
        output.append(value)
    text = "".join(output).strip()
    if not text:
        raise ConfigError("cover_letter.template produced empty text")
    return text
