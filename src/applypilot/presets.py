from __future__ import annotations

from copy import deepcopy
from typing import Any

ROLE_PRESETS: dict[str, dict[str, Any]] = {
    "ai-llm": {
        "queries": ["AI Agent", "LLM Engineer", "RAG Engineer", "GenAI Engineer",
                     "Prompt Engineer", "AI Automation"],
        "include_terms": ["llm", "rag", "agent", "genai", "python", "openai", "langchain"],
        "exclude_titles": ["продаж", "ассистент", "тренер", "маркетолог", "рекрутер", "support"],
        "role_terms": ["ai", "llm", "agent", "rag", "genai", "nlp", "искусственный интеллект"],
        "primary_role_terms": ["ai", "llm", "agent", "rag", "genai", "nlp"],
        "required_role_terms": ["engineer", "developer", "разработчик", "программист", "backend", "software"],
        "title_role_terms": ["ai", "llm", "agent", "rag", "genai", "prompt", "nlp"],
    },
    "ai-agents-llmops": {
        "queries": [
            "AI Agent Engineer", "LLM Engineer", "RAG Engineer", "LLMOps Engineer",
            "AI Platform Engineer", "GenAI Engineer", "AI Automation Engineer",
            "Разработчик ИИ агентов", "LLM разработчик", "RAG разработчик",
        ],
        "include_terms": [
            "llm", "rag", "agent", "langchain", "langgraph", "openai", "mcp", "qdrant",
            "embedding", "vector", "vllm", "litellm", "fastapi", "python", "docker", "kubernetes",
        ],
        "exclude_titles": [
            "продаж", "ассистент", "тренер", "маркетолог", "рекрутер", "support", "qa",
            "тестиров", "quality assurance", "sdet", "security", "кибербез", "иб", "vibe", "вайб",
            "training", "обучению",
        ],
        "role_terms": ["ai", "llm", "agent", "rag", "genai", "llmops", "ai platform"],
        "primary_role_terms": ["llm", "agent", "rag", "genai", "llmops", "ai platform", "ai automation"],
        "required_role_terms": ["engineer", "developer", "разработчик", "программист", "backend", "software"],
        "title_role_terms": ["ai", "llm", "agent", "rag", "genai", "llmops", "ai platform"],
    },
    "ml-engineering": {
        "queries": ["ML Engineer", "Machine Learning Engineer", "NLP Engineer", "MLOps", "Computer Vision"],
        "include_terms": ["python", "machine learning", "ml", "pytorch", "tensorflow", "nlp", "mlops"],
        "exclude_titles": ["продаж", "ассистент", "тренер", "маркетолог", "аналитик продаж"],
        "role_terms": ["ml", "machine learning", "машинн", "nlp", "computer vision", "mlops"],
        "primary_role_terms": ["ml", "machine learning", "deep learning", "nlp", "computer vision", "mlops",
                               "data scientist", "applied scientist"],
        "required_role_terms": ["engineer", "developer", "разработчик", "инженер", "scientist", "mlops"],
        "title_role_terms": ["ml", "machine learning", "deep learning", "nlp", "mlops", "data scientist",
                              "computer vision", "applied scientist"],
    },
    "python-backend": {
        "queries": ["Python Backend", "Python Developer", "FastAPI Developer", "Django Developer"],
        "include_terms": ["python", "fastapi", "django", "backend", "api", "postgresql"],
        "exclude_titles": ["продаж", "ассистент", "тренер", "маркетолог", "аналитик", "support",
                           "qa", "тестиров", "quality assurance", "sdet"],
        "role_terms": ["python", "backend", "fastapi", "django", "бекенд", "разработчик"],
        "primary_role_terms": ["python", "fastapi", "django"],
        "incompatible_title_terms": ["php", "symfony", "java", "c#", ".net", "1с", "frontend", "react", "angular"],
        "required_role_terms": ["developer", "разработчик", "engineer", "инженер", "backend", "программист"],
        "title_role_terms": ["python", "fastapi", "django", "backend"],
    },
    "go-backend": {
        "queries": ["Go Developer", "Golang Developer", "Go Backend", "Go Engineer"],
        "include_terms": ["go", "golang", "backend", "grpc", "microservices", "kubernetes"],
        "exclude_titles": ["продаж", "ассистент", "тренер", "маркетолог", "аналитик", "support"],
        "role_terms": ["go", "golang", "backend", "го-разработчик", "разработчик"],
        "primary_role_terms": ["go", "golang", "го-разработчик"],
        "incompatible_title_terms": ["php", "symfony", "java", "c#", ".net", "1с", "frontend", "react", "angular"],
        "required_role_terms": ["developer", "разработчик", "engineer", "инженер", "backend", "программист"],
        "title_role_terms": ["go", "golang"],
    },
    "software-general": {
        "queries": ["Software Engineer", "Backend Developer", "Python Developer", "Go Developer"],
        "include_terms": ["python", "go", "golang", "backend", "api", "software", "developer"],
        "exclude_titles": ["продаж", "ассистент", "тренер", "маркетолог", "рекрутер"],
        "role_terms": ["engineer", "developer", "разработчик", "backend", "software", "программист"],
        "required_role_terms": ["developer", "разработчик", "engineer", "инженер", "backend", "программист"],
        "title_role_terms": ["software", "developer", "разработчик", "engineer", "инженер", "backend", "python", "go", "golang"],
    },
}


def resolve_search(search: dict[str, Any], preset_name: str | None = None) -> dict[str, Any]:
    """Apply a public role preset, then let private search settings override it."""
    name = preset_name or search.get("preset")
    base = deepcopy(ROLE_PRESETS.get(str(name), {})) if name else {}
    preset_fields = {
        "queries", "include_terms", "exclude_titles", "role_terms", "primary_role_terms",
        "required_role_terms", "title_role_terms", "incompatible_title_terms",
    }
    for key, value in search.items():
        if key != "preset" and not (preset_name and key in preset_fields):
            base[key] = deepcopy(value)
    if preset_name:
        additional = search.get("additional_queries", []) or []
        base["queries"] = list(dict.fromkeys([*base.get("queries", []), *additional]))
    return base
