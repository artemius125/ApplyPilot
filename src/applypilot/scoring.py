from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DEFAULT_SCORING = {
    "hot": ("voice", "голос", "multi-agent", "мульти-агент", "нейросет"),
    "high": (
        "python", "llm", "rag", "langchain", "langgraph", "ai agent", "prompt engineer",
        "gpt", "claude", "gemini", "openai", "ai-агент", "искусственный интеллект",
        "chatbot", "чат-бот", "nats", "grpc", "docker", "fastapi", "typescript", "golang",
        "tts", "stt", "vector", "embedding", "chromadb", "pinecone", "qdrant", "crewai",
        "autogen", "n8n", "flowise", "llamaindex", "huggingface", "оркестр", "cursor",
        "windsurf", "claude code", "github copilot", "ai-first", "ai-native", "vibe cod",
        "copilot", "codex", "mcp", "model context protocol", "make.com", "zapier", "langflow",
        "genai", "vibe coding", "vibecoder",
    ),
    "medium": (
        "backend", "api", "rest", "microservic", "redis", "postgresql", "mongodb", "celery",
        "rabbitmq", "kubernetes", "k8s", "ci/cd", "git", "linux", "node.js", "react",
        "next.js", "vue", "pandas", "numpy",
    ),
    "hard_killers": (),
    "noise_titles": (),
}


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    company: str = ""
    url: str = ""
    description: str = ""
    score: int = 0
    reasons: tuple[str, ...] = ()
    resume: str = ""
    salary: dict[str, Any] | None = None
    experience: Any = ""
    area: str = ""
    schedule: str = ""
    is_remote: bool = False
    work_format: tuple[str, ...] = ()
    source: str = ""
    hard_killers: tuple[str, ...] = ()
    soft_killers: tuple[str, ...] = ()
    sb_risk: str = "LOW"
    score_source: str = "calculated"
    scorer_version: str = "2"
    components: dict[str, int] = field(default_factory=dict)
    component_evidence: dict[str, tuple[str, ...]] = field(default_factory=dict)
    decision: str = "pass"
    legacy_score: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class FilterDecision:
    """Explain one pure search-filter evaluation."""

    status: Literal["pass", "reject", "unknown"]
    reasons: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()


def normalize_vacancy(raw: dict[str, Any]) -> Candidate:
    vid = str(raw.get("id") or raw.get("vacancyId") or raw.get("vacancy_id") or "")
    name = str(raw.get("name") or raw.get("title") or raw.get("profession") or "")
    company = raw.get("company") or raw.get("employer") or ""
    if isinstance(company, dict):
        company = company.get("visibleName") or company.get("name", "")
    snippet = raw.get("snippet") or {}
    description = raw.get("description") or raw.get("requirement") or ""
    if isinstance(snippet, dict):
        description = " ".join(str(value) for value in (description, snippet.get("requirement", ""),
                                                         snippet.get("responsibility", "")) if value)
    salary = raw.get("salary") or raw.get("compensation")
    experience = raw.get("experience") or raw.get("workExperience") or ""
    if isinstance(experience, dict):
        experience = experience.get("id") or experience.get("name") or ""
    area = raw.get("area") or ""
    if isinstance(area, dict):
        area = area.get("name", "")
    schedule = raw.get("schedule") or raw.get("@workSchedule") or ""
    if isinstance(schedule, dict):
        schedule = schedule.get("id") or schedule.get("name") or ""
    formats: list[str] = []
    for value in raw.get("work_format") or raw.get("workFormats") or []:
        if isinstance(value, dict):
            elements = value.get("workFormatsElement") or [value.get("id", "")]
            formats.extend(str(element).lower() for element in elements if element)
        elif value:
            formats.append(str(value).lower())
    is_remote = bool(raw.get("is_remote")) or str(schedule).lower() == "remote" or "remote" in formats
    url = raw.get("url") or raw.get("alternate_url")
    if not url and vid:
        url = f"https://hh.ru/vacancy/{vid}"
    return Candidate(
        id=vid,
        name=name,
        company=str(company),
        url=str(url or ""),
        description=str(description),
        score=int(raw.get("score") or 0),
        reasons=tuple(str(x) for x in (raw.get("reasons") or raw.get("score_reasons") or []) if x),
        resume=str(raw.get("resume") or ""),
        salary=salary if isinstance(salary, dict) else None,
        experience=experience,
        area=str(area),
        schedule=str(schedule),
        is_remote=is_remote,
        work_format=tuple(dict.fromkeys(formats)),
        source=str(raw.get("source") or ""),
        hard_killers=tuple(str(x) for x in raw.get("hard_killers", []) if x),
        soft_killers=tuple(str(x) for x in raw.get("soft_killers", []) if x),
        sb_risk=str(raw.get("sb_risk") or "LOW"),
        score_source="legacy" if "score" in raw else "calculated",
    )


def choose_resume(title: str, profile: dict[str, Any]) -> str:
    resumes = profile.get("resumes", {})
    low = title.lower()
    if re.search(r"(?<![\w])(go|golang|го-разработчик)(?![\w])", low):
        return str(resumes.get("go", ""))
    if re.search(r"(?<![\w])(ml|machine learning|data scientist|nlp)(?![\w])", low):
        return str(resumes.get("ml", resumes.get("default", "")))
    if any(x in low for x in ("python", "backend", "fastapi", "django")):
        return str(resumes.get("python", resumes.get("default", "")))
    return str(resumes.get("ai_agent", resumes.get("default", "")))


def score_vacancy(raw: dict[str, Any], profile: dict[str, Any]) -> Candidate:
    candidate = normalize_vacancy(raw)
    historical_score = int(raw.get("score") or 0) if "score" in raw else None
    # Snapshots from the old parser already contain a score.  Treat it as an
    # authoritative historical value; adding new keyword points would change
    # old results on every read.
    if "score" in raw and not profile.get("rescore", False):
        resume = choose_resume(candidate.name, profile)
        return Candidate(**{**candidate.__dict__, "resume": resume, "score": historical_score,
                            "score_source": "legacy", "scorer_version": "legacy",
                            "legacy_score": historical_score, "decision": "pass"})

    text = f"{candidate.name} {candidate.description}".lower()
    rules = {**DEFAULT_SCORING, **profile.get("scoring", {})}
    search_rules = profile.get("search", {})
    role_terms = tuple(str(term).lower() for term in profile.get("role_terms", search_rules.get("role_terms", [])))
    required_role_terms = tuple(str(term).lower() for term in profile.get(
        "required_role_terms", search_rules.get("required_role_terms", [])))
    title_role_terms = tuple(str(term).lower() for term in profile.get(
        "title_role_terms", search_rules.get("title_role_terms", [])))
    primary_role_terms = tuple(str(term).lower() for term in profile.get(
        "primary_role_terms", search_rules.get("primary_role_terms", role_terms)))
    include_terms = tuple(str(term).lower() for term in profile.get(
        "include_terms", search_rules.get("include_terms", [])))
    title_excludes = tuple(str(term).lower() for term in profile.get(
        "exclude_titles", search_rules.get("exclude_titles", [])))
    incompatible_title_terms = tuple(str(term).lower() for term in profile.get(
        "incompatible_title_terms", search_rules.get("incompatible_title_terms", [])))
    title = candidate.name.lower()
    reasons = list(candidate.reasons)
    hard_killers = list(candidate.hard_killers)
    soft_killers = list(candidate.soft_killers)
    def matches(term: str, value: str) -> bool:
        escaped = re.escape(term.lower().strip())
        if not escaped:
            return False
        if re.fullmatch(r"[a-z0-9а-яё]+", term.lower().strip()) and len(term.strip()) <= 4:
            return bool(re.search(rf"(?<![\w]){escaped}(?![\w])", value))
        return term.lower() in value

    for term in rules["hard_killers"]:
        if matches(str(term), text):
            hard_killers.append(f"hard:{term}")
    for term in rules["noise_titles"]:
        if matches(str(term), title):
            hard_killers.append(f"noise:{term}")
    for term in title_excludes:
        if term and matches(term, title):
            hard_killers.append(f"preset_noise:{term}")
    if (any(matches(term, title) for term in incompatible_title_terms)
            and not any(matches(term, title) for term in primary_role_terms)):
        hard_killers.append("stack:title_incompatible")
    if required_role_terms and not any(matches(term, title) for term in required_role_terms):
        hard_killers.append("role:title_not_technical")
    if title_role_terms and not any(matches(term, title) for term in title_role_terms):
        hard_killers.append("role:title_not_target")
    if role_terms and not any(matches(term, text) for term in role_terms):
        hard_killers.append("role:no_match")
    hot = [str(term) for term in rules["hot"] if matches(str(term), text)]
    high = [str(term) for term in rules["high"] if matches(str(term), text)]
    medium = [str(term) for term in rules["medium"] if matches(str(term), text)]
    preset_matches = [term for term in include_terms if matches(term, text)]
    for term in hot + high + medium + preset_matches:
        reasons.append(f"keyword:{term}")
    if candidate.is_remote:
        reasons.append("format:remote")
    if candidate.sb_risk in {"HIGH", "DEFENSE", "FINREG"}:
        soft_killers.append(f"security:{candidate.sb_risk.lower()}")
    if hard_killers:
        score = 0
        components = {"role": 0, "stack": 0, "duties": 0, "experience": 0, "salary": 0, "format": 0}
        component_evidence: dict[str, tuple[str, ...]] = {key: () for key in components}
        decision = "reject"
    else:
        role_component = 35 if any(matches(term, title) for term in primary_role_terms) else 20 if role_terms else 0
        stack_component = min(25, len(set(hot)) * 8 + len(set(high)) * 4 +
                             len(set(preset_matches) - set(hot) - set(high) - set(medium)) * 5)
        duties_component = min(15, len(set(medium)) * 2)
        experience_component = 8 if str(candidate.experience).lower() in {
            "noexperience", "between1and3", "нет опыта", "от 1 года до 3 лет"} else 0
        salary_component = 10 if candidate.salary else 0
        format_component = 5 if candidate.is_remote else 0
        components = {"role": role_component, "stack": stack_component, "duties": duties_component,
                      "experience": experience_component, "salary": salary_component, "format": format_component}
        component_evidence = {
            "role": tuple(dict.fromkeys(term for term in primary_role_terms + role_terms + title_role_terms
                                          if matches(term, title))),
            "stack": tuple(dict.fromkeys(hot + high + preset_matches)),
            "duties": tuple(dict.fromkeys(medium)),
            "experience": (str(candidate.experience),) if experience_component else (),
            "salary": (str(candidate.salary),) if salary_component else (),
            "format": ("remote",) if format_component else (),
        }
        score = sum(components.values())
        if candidate.sb_risk in {"HIGH", "DEFENSE", "FINREG"}:
            score = max(0, score - (15 if candidate.sb_risk == "HIGH" else 20))
            components["stack"] = max(0, components["stack"] - (15 if candidate.sb_risk == "HIGH" else 20))
            score = sum(components.values())
        description_text = candidate.description.lower()
        description_evidence = {term for term in hot + high + medium + preset_matches
                                if matches(term, description_text)}
        if not candidate.description.strip() or not description_evidence:
            score = min(score, 29)
            decision = "unknown"
            reasons.append("unknown:insufficient_description_evidence")
        if any(matches(term, text) for term in ("cursor", "windsurf", "claude code", "vibe coding", "ai-native", "agentic")):
            reasons.append("signal:ai-assisted-workflow")
        if matches("ai", title):
            reasons.append("signal:title-ai")
        if matches("go", title):
            reasons.append("signal:title-go")
        decision = "pass" if score >= 30 else "unknown"
    return Candidate(**{**candidate.__dict__, "score": score,
                        "reasons": tuple(dict.fromkeys(reasons)), "resume": choose_resume(candidate.name, profile),
                        "hard_killers": tuple(dict.fromkeys(hard_killers)),
                        "soft_killers": tuple(dict.fromkeys(soft_killers)), "score_source": "calculated",
                        "components": components, "component_evidence": component_evidence,
                        "decision": decision, "scorer_version": "2", "legacy_score": historical_score})


def filter_candidates(raw_items: Iterable[dict[str, Any]], profile: dict[str, Any],
                      limit: int = 5, min_score: int = 0,
                      skip_high_security: bool = False,
                      blocked_ids: set[str] | None = None,
                      rescore: bool = False) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[str] = set()
    for raw in raw_items:
        scoring_profile = {**profile, "rescore": rescore}
        candidate = score_vacancy(raw, scoring_profile)
        if candidate.id in (blocked_ids or set()):
            continue
        if not candidate.id or candidate.id in seen:
            continue
        seen.add(candidate.id)
        text = f"{candidate.name} {candidate.description} {candidate.company}".lower()
        filter_decision = evaluate_search_filter(raw, profile.get("search", profile))
        if filter_decision.status != "pass":
            continue
        if candidate.decision != "pass" or candidate.score < min_score:
            continue
        if candidate.hard_killers:
            continue
        if skip_high_security and (candidate.sb_risk == "HIGH" or re.search(r"служба безопасности|security check", text)):
            continue
        result.append(candidate)
    result.sort(key=lambda x: (-x.score, x.id))
    return result[:max(0, limit)]


def prioritize_for_enrichment(raw_items: Iterable[dict[str, Any]], profile: dict[str, Any],
                              limit: int = 100) -> list[Candidate]:
    """Choose plausible and borderline rows for description loading.

    This intentionally keeps ``unknown`` rows that have no full description
    yet.  Final planning still uses :func:`filter_candidates` and therefore
    accepts only confirmed ``pass`` candidates.
    """
    result: list[Candidate] = []
    seen: set[str] = set()
    for raw in raw_items:
        candidate = score_vacancy(raw, profile)
        if not candidate.id or candidate.id in seen or candidate.hard_killers:
            continue
        seen.add(candidate.id)
        decision = evaluate_search_filter(raw, profile.get("search", profile))
        if decision.status == "reject":
            continue
        result.append(candidate)
    result.sort(key=lambda candidate: (-candidate.score, candidate.id))
    return result[:max(0, limit)]


def matches_search_filters(raw: dict[str, Any], search: dict[str, Any]) -> bool:
    """Backward-compatible boolean view of :func:`evaluate_search_filter`."""
    return evaluate_search_filter(raw, search).status == "pass"


def evaluate_search_filter(raw: dict[str, Any], search: dict[str, Any]) -> FilterDecision:
    """Apply non-ranking constraints without mutating a vacancy.

    ``unknown`` is returned when a strict constraint needs a field that the
    snapshot does not contain.  Callers may keep such rows for manual review,
    but must not present them as confirmed matches.
    """
    candidate = normalize_vacancy(raw)
    reasons: list[str] = []
    fields: list[str] = []
    if search.get("only_remote") and not candidate.is_remote:
        if not candidate.work_format and not candidate.schedule:
            return FilterDecision("unknown", ("remote:not_verified",), ("work_format", "schedule"))
        return FilterDecision("reject", ("remote:not_matching",), ("work_format", "schedule"))
    if search.get("only_remote"):
        reasons.append("remote:verified")
        fields.extend(("work_format", "schedule"))
    allowed_formats = {str(value).lower() for value in search.get("work_formats", [])}
    if allowed_formats:
        fields.append("work_format")
        if not candidate.work_format:
            return FilterDecision("unknown", ("work_format:not_provided",), tuple(fields))
        if not set(candidate.work_format) & allowed_formats:
            return FilterDecision("reject", ("work_format:not_matching",), tuple(fields))
        reasons.append("work_format:matching")
    allowed_experience = {str(value).lower() for value in search.get("experience", {}).get("allowed", [])}
    if allowed_experience:
        fields.append("experience")
        if not candidate.experience:
            return FilterDecision("unknown", ("experience:not_provided",), tuple(fields))
        if str(candidate.experience).lower() not in allowed_experience:
            return FilterDecision("reject", ("experience:not_matching",), tuple(fields))
        reasons.append("experience:matching")
    salary_cfg = search.get("salary", {})
    if salary_cfg:
        fields.append("salary")
        salary = candidate.salary or {}
        missing = str(salary_cfg.get("missing", "include"))
        if not salary:
            if missing == "exclude":
                return FilterDecision("reject", ("salary:missing_excluded",), tuple(fields))
            if missing == "only":
                return FilterDecision("pass", ("salary:missing_only",), tuple(fields))
            return FilterDecision("pass", ("salary:missing_allowed",), tuple(fields))
        if missing == "only":
            return FilterDecision("reject", ("salary:known_excluded_by_only",), tuple(fields))
        wanted_currency = str(salary_cfg.get("currency", "")).upper()
        currency = str(salary.get("currency", "")).upper()
        if wanted_currency and not currency:
            return FilterDecision("unknown", ("salary:currency_unknown",), tuple(fields))
        if wanted_currency and currency != wanted_currency:
            return FilterDecision("reject", ("salary:currency_mismatch",), tuple(fields))
        minimum = int(salary_cfg.get("from", 0) or 0)
        if minimum:
            lower = salary.get("from")
            upper = salary.get("to")
            policy = str(salary_cfg.get("policy", "possible"))
            if policy == "guaranteed":
                if lower is None:
                    return FilterDecision("unknown", ("salary:lower_bound_unknown",), tuple(fields))
                if int(lower) < minimum:
                    return FilterDecision("reject", ("salary:guaranteed_below_threshold",), tuple(fields))
            else:
                if lower is None and upper is None:
                    return FilterDecision("unknown", ("salary:range_unknown",), tuple(fields))
                if max(int(lower or 0), int(upper or 0)) < minimum:
                    return FilterDecision("reject", ("salary:possible_below_threshold",), tuple(fields))
        if salary.get("gross") is None and "gross" in salary:
            reasons.append("salary:gross_net_unknown")
        reasons.append("salary:matching")
    return FilterDecision("pass", tuple(reasons), tuple(dict.fromkeys(fields)))
