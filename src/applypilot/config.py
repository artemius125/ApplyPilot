from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presets import ROLE_PRESETS, resolve_search

VALID_MISSING_SALARY = {"include", "exclude", "only"}
VALID_SALARY_POLICY = {"possible", "guaranteed"}
DEFAULT_SEARCH: dict[str, Any] = {
    "areas": [113], "max_pages": 20, "request_budget": 500, "details_limit": 500,
    "days": 7, "only_remote": False, "min_score": 0,
    "salary": {"currency": "RUR", "from": 0, "missing": "include", "policy": "possible"},
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    root: Path
    data_dir: Path
    profile_path: Path
    search_path: Path | None = None

    @classmethod
    def discover(cls, root: Path | None = None, data_dir: str | None = None,
                 profile: str | None = None, search: str | None = None) -> AppConfig:
        root = (root or Path.cwd()).resolve()
        data = Path(data_dir or os.getenv("APPLYPILOT_DATA_DIR", "private/data"))
        if not data.is_absolute():
            data = root / data
        profile_path = Path(profile or os.getenv("APPLYPILOT_PROFILE", "private/config/profile.toml"))
        if not profile_path.is_absolute():
            profile_path = root / profile_path
        search_path = Path(search) if search else root / "private/config/search.toml"
        if search_path and not search_path.is_absolute():
            search_path = root / search_path
        return cls(root, data.resolve(), profile_path.resolve(), search_path.resolve())

    def load_profile(self) -> dict[str, Any]:
        if not self.profile_path.exists():
            return {}
        with self.profile_path.open("rb") as fh:
            return tomllib.load(fh)

    def load_search(self) -> dict[str, Any]:
        if not self.search_path or not self.search_path.exists():
            return {}
        with self.search_path.open("rb") as fh:
            return tomllib.load(fh)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "applypilot.sqlite3"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"


def ensure_data_dirs(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)


def validate_search(search: dict[str, Any]) -> dict[str, Any]:
    values = dict(search)
    for key in ("max_pages", "max_queries", "request_budget", "details_limit", "days"):
        if values.get(key) is not None and int(values[key]) < 0:
            raise ConfigError(f"{key} must be non-negative")
    if "max_pages" in values and int(values["max_pages"]) == 0:
        raise ConfigError("max_pages must be positive")
    if "request_budget" in values and int(values["request_budget"]) == 0:
        raise ConfigError("request_budget must be positive")
    if "max_queries" in values and int(values["max_queries"]) == 0:
        raise ConfigError("max_queries must be positive")
    salary = values.get("salary", {}) or {}
    if str(salary.get("missing", "include")) not in VALID_MISSING_SALARY:
        raise ConfigError("salary.missing must be include, exclude or only")
    if str(salary.get("policy", "possible")) not in VALID_SALARY_POLICY:
        raise ConfigError("salary.policy must be possible or guaranteed")
    if int(salary.get("from", 0) or 0) < 0:
        raise ConfigError("salary.from must be non-negative")
    groups = values.get("groups")
    if groups is not None:
        if not isinstance(groups, list) or not groups:
            raise ConfigError("groups must be a non-empty array")
        if any(not isinstance(group, dict) for group in groups):
            raise ConfigError("each search group must be a TOML table")
    return values


def search_groups(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand optional per-region search groups without mutating the input."""
    groups = search.get("groups")
    if not groups:
        return [deepcopy(search)]
    base = deepcopy(DEFAULT_SEARCH)
    base.update({key: value for key, value in search.items() if key != "groups"})
    base["salary"] = {**DEFAULT_SEARCH["salary"], **(base.get("salary", {}) or {})}
    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        merged = deepcopy(base)
        for key, value in group.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**deepcopy(merged[key]), **deepcopy(value)}
            else:
                merged[key] = deepcopy(value)
        merged["group_name"] = str(group.get("name") or f"group-{index}")
        result.append(validate_search(merged))
    return result


def effective_search(raw: dict[str, Any], preset_name: str | None = None,
                     overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    requested = preset_name or raw.get("preset")
    if requested and requested not in ROLE_PRESETS:
        raise ConfigError(f"unknown preset: {requested}")
    resolved = resolve_search(raw, requested)
    if "request_budget" not in resolved and "max_queries" in resolved:
        resolved["request_budget"] = resolved["max_queries"]
    values = dict(DEFAULT_SEARCH)
    values.update(resolved)
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value
    values["areas"] = [int(area) for area in values.get("areas", [113])]
    # ``max_queries`` was the old name for the total HTTP request budget.
    # Keep it as a TOML compatibility alias, but never silently clamp either value.
    values["max_pages"] = int(values["max_pages"])
    values["request_budget"] = int(values["request_budget"])
    values["details_limit"] = int(values["details_limit"])
    values["salary"] = {**DEFAULT_SEARCH["salary"], **(values.get("salary", {}) or {})}
    return validate_search(values)


def search_origins(raw: dict[str, Any], preset_name: str | None = None) -> dict[str, str]:
    preset = preset_name or raw.get("preset")
    origins = {key: "default" for key in DEFAULT_SEARCH}
    origins.update({key: "preset" for key in ROLE_PRESETS.get(str(preset), {})})
    origins.update({key: "toml" for key in raw if key != "preset"})
    if preset_name:
        for key in ("queries", "include_terms", "exclude_titles", "role_terms", "required_role_terms",
                    "title_role_terms"):
            if key in ROLE_PRESETS.get(preset_name, {}):
                origins[key] = "cli preset"
    return origins
