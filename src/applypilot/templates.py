from __future__ import annotations

from pathlib import Path

TEMPLATES: dict[str, str] = {
    "ai-llm": '''# AI / LLM / Agents / RAG search template
preset = "ai-llm"
extends = "software-general"
queries = ["AI Agent", "LLM Engineer", "RAG Engineer", "GenAI Engineer"]
role_terms = ["ai", "llm", "agent", "rag", "genai", "nlp"]
include_terms = ["python", "llm", "rag", "langgraph", "openai", "agents"]
exclude_titles = ["продаж", "ассистент", "тренер", "маркетолог"]
''',
    "ai-agents-llmops": '''# Narrow AI Agents / LLMOps / RAG engineering search template
preset = "ai-agents-llmops"
queries = ["AI Agent Engineer", "LLM Engineer", "RAG Engineer", "LLMOps Engineer", "AI Platform Engineer"]
role_terms = ["ai", "llm", "agent", "rag", "genai", "llmops", "ai platform"]
include_terms = ["llm", "rag", "langgraph", "mcp", "qdrant", "vllm", "litellm", "python", "docker", "kubernetes"]
exclude_titles = ["qa", "тестиров", "security", "кибербез", "иб", "vibe", "вайб", "training", "обучению"]

# Add personal terminology without replacing the preset:
additional_queries = ["Разработчик ИИ агентов"]
''',
    "ml-engineering": '''# ML / NLP / CV / MLOps search template
preset = "ml-engineering"
extends = "software-general"
queries = ["ML Engineer", "Machine Learning Engineer", "NLP Engineer", "MLOps"]
role_terms = ["ml", "machine learning", "nlp", "computer vision", "mlops"]
include_terms = ["python", "pytorch", "tensorflow", "mlops", "nlp"]
exclude_titles = ["продаж", "ассистент", "тренер", "маркетолог"]
''',
    "python-backend": '''# Python Backend search template
preset = "python-backend"
extends = "software-general"
queries = ["Python Backend", "Python Developer", "FastAPI Developer", "Django Developer"]
role_terms = ["python", "backend", "fastapi", "django", "разработчик"]
include_terms = ["python", "fastapi", "django", "api", "postgresql"]
exclude_titles = ["продаж", "ассистент", "тренер", "маркетолог", "support", "qa", "тестиров", "quality assurance", "sdet"]
''',
    "go-backend": '''# Go Backend search template
preset = "go-backend"
extends = "software-general"
queries = ["Go Developer", "Golang Developer", "Go Backend", "Go Engineer"]
role_terms = ["go", "golang", "backend", "разработчик"]
include_terms = ["go", "golang", "grpc", "microservices", "kubernetes"]
exclude_titles = ["продаж", "ассистент", "тренер", "маркетолог"]
''',
    "software-general": '''# General Software Engineering search template
preset = "software-general"
queries = ["Software Engineer", "Backend Developer", "Python Developer", "Go Developer"]
role_terms = ["engineer", "developer", "разработчик", "backend", "software"]
include_terms = ["python", "go", "backend", "api", "software", "developer"]
exclude_titles = ["продаж", "ассистент", "тренер", "маркетолог"]
''',
}


def list_templates() -> tuple[str, ...]:
    return tuple(TEMPLATES)


def create_template(name: str, output: Path) -> Path:
    if name not in TEMPLATES:
        raise ValueError(f"unknown template: {name}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(TEMPLATES[name], encoding="utf-8")
    return output
