from __future__ import annotations

from typing import Any

from .presets import ROLE_PRESETS
from .scoring import filter_candidates

_POSITIVE = {
    "ai-llm": [("AI Agent Engineer", "Python LLM RAG LangGraph"), ("LLM Engineer", "Python OpenAI agents"),
                ("RAG Developer", "Python vector search"), ("GenAI Software Engineer", "Python GenAI API"),
                ("Prompt Engineer", "LLM evaluation Python"), ("AI Automation Developer", "agents n8n Python"),
                ("NLP Engineer", "Python NLP transformers"), ("AI Backend Developer", "Python FastAPI LLM"),
                ("AI Platform Engineer", "LLM services Docker"), ("AI Product Engineer", "agents Python API")],
    "ai-agents-llmops": [
        ("AI Agent Engineer", "Python LLM RAG LangGraph MCP"),
        ("LLM Engineer", "Python LLM vLLM Docker"),
        ("RAG Developer", "Python Qdrant embeddings FastAPI"),
        ("LLMOps Engineer", "Python LiteLLM Kubernetes"),
        ("AI Platform Engineer", "LLM platform Python Docker"),
        ("GenAI Engineer", "Python OpenAI agents"),
        ("AI Automation Engineer", "LangGraph MCP FastAPI"),
        ("AI Backend Developer", "Python LLM RAG services"),
        ("LLM Software Engineer", "Python model serving Docker"),
        ("RAG Backend Engineer", "Python vector search API"),
    ],
    "ml-engineering": [("ML Engineer", "Python PyTorch MLOps"), ("Machine Learning Engineer", "Python models"),
                       ("NLP Engineer", "Python NLP transformers"), ("MLOps Engineer", "ML pipelines Kubernetes"),
                       ("Computer Vision Engineer", "Python OpenCV"), ("Data Scientist", "Python machine learning PyTorch"),
                       ("ML Developer", "Python training inference"), ("Deep Learning Engineer", "PyTorch CV machine learning"),
                       ("Applied Scientist", "machine learning Python"), ("ML Platform Engineer", "MLOps Python")],
    "python-backend": [("Python Backend Developer", "Python FastAPI PostgreSQL"), ("Python Developer", "Django REST API"),
                       ("FastAPI Engineer", "Python async API"), ("Django Backend Developer", "Python Django"),
                       ("Senior Python Engineer", "Python microservices"), ("Backend Engineer Python", "Python Redis"),
                       ("Python API Developer", "Python REST"), ("Python Software Engineer", "Python Docker"),
                       ("Python Integration Developer", "Python integrations"), ("Junior Python Developer", "Python backend")],
    "go-backend": [("Go Backend Developer", "Go gRPC microservices"), ("Golang Engineer", "Golang Kubernetes"),
                   ("Go Developer", "Go PostgreSQL API"), ("Senior Go Engineer", "Golang distributed systems"),
                   ("Go Software Developer", "Go services"), ("Backend Engineer Go", "Go Kafka"),
                   ("Golang API Developer", "Golang REST"), ("Go Platform Engineer", "Go Docker"),
                   ("Junior Go Developer", "Go backend"), ("Go Microservices Engineer", "Golang gRPC")],
    "software-general": [("Software Engineer", "Python APIs services"), ("Backend Developer", "REST PostgreSQL"),
                         ("Fullstack Software Developer", "TypeScript API"), ("Platform Engineer", "Linux Docker"),
                         ("Software Developer", "Python Go"), ("Backend Engineer", "microservices"),
                         ("API Engineer", "REST services"), ("Systems Developer", "Linux Python"),
                         ("Application Engineer", "software API"), ("Development Engineer", "backend services")],
}

_BORDERLINE = {
    preset: [(name, "requirements unclear") for name in names]
    for preset, names in {
        "ai-llm": ["AI Engineer", "AI Developer", "Prompt Engineer", "AI Software Engineer"],
        "ai-agents-llmops": ["AI Engineer", "LLM Specialist", "RAG Architect", "GenAI Developer"],
        "ml-engineering": ["ML Engineer", "Data Scientist", "NLP Engineer", "MLOps Engineer"],
        "python-backend": ["Python Developer", "Backend Engineer", "FastAPI Developer", "Python Engineer"],
        "go-backend": ["Go Developer", "Golang Engineer", "Backend Go Engineer", "Go Developer"],
        "software-general": ["Software Engineer", "Backend Developer", "Platform Engineer", "API Developer"],
    }.items()
}

_NOISE = [("Удалённый ассистент руководителя", "организация встреч и документы"),
          ("Менеджер по продажам AI-сервисов", "холодные продажи и клиенты"),
          ("Маркетолог ML-продукта", "контент реклама и продвижение"),
          ("Тренер по нейросетям", "обучение пользователей"),
          ("Digital Product Designer AI", "дизайн интерфейсов"),
          ("AI Generative Artist", "визуальный контент")]

_NOISE_BY_PRESET = {
    "ai-agents-llmops": [
        ("QA Engineer (LLM)", "LLM RAG testing"),
        ("AI Security Engineer", "LLM RAG security"),
        ("Тренер по нейросетям", "обучение пользователей"),
        ("AI Sales Manager", "продажи AI-платформы"),
        ("Vibe Coding Mentor", "обучению вайб-кодингу"),
        ("AI Recruiter", "подбор разработчиков"),
    ],
}


def benchmark_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    index = 0
    for preset, rows in _POSITIVE.items():
        for name, description in rows:
            cases.append({"id": f"positive-{index}", "name": name, "description": description,
                          "preset": preset, "relevant": True, "kind": "positive",
                          "split": "control" if index % 2 else "calibration"})
            index += 1
        for name, description in _BORDERLINE[preset]:
            cases.append({"id": f"borderline-{index}", "name": name, "description": description,
                          "preset": preset, "relevant": False, "kind": "borderline", "split": "control"})
            index += 1
        for name, description in _NOISE_BY_PRESET.get(preset, _NOISE):
            cases.append({"id": f"noise-{index}", "name": name, "description": description,
                          "preset": preset, "relevant": False, "kind": "noise", "split": "control"})
            index += 1
    return cases


def _metrics(rows: list[dict[str, Any]], selected: list[Any], expected: int) -> dict[str, float | int]:
    selected_ids = {str(row.id) for row in selected}
    true_positive = sum(row["relevant"] and row["id"] in selected_ids for row in rows)
    return {"selected": len(selected), "precision": true_positive / max(1, len(selected)),
            "recall": true_positive / max(1, expected)}


def run_benchmark(suite: str = "tech-roles", control_only: bool = False) -> dict[str, Any]:
    if suite != "tech-roles":
        raise ValueError(f"unknown benchmark suite: {suite}")
    cases = benchmark_cases()
    if control_only:
        cases = [case for case in cases if case["split"] == "control"]
    rows: list[dict[str, Any]] = []
    by_preset: dict[str, dict[str, Any]] = {}
    for preset in ROLE_PRESETS:
        preset_rows = [case for case in cases if case["preset"] == preset]
        rules = ROLE_PRESETS[preset]
        profile = {"search": rules, **rules}
        selected_all = filter_candidates(preset_rows, profile, limit=100, min_score=30)
        selected_10 = filter_candidates(preset_rows, profile, limit=10, min_score=30)
        selected_20 = filter_candidates(preset_rows, profile, limit=20, min_score=30)
        expected = sum(case["relevant"] for case in preset_rows)
        by_preset[preset] = {"cases": len(preset_rows), "precision_at_10": _metrics(preset_rows, selected_10, expected),
                             "precision_at_20": _metrics(preset_rows, selected_20, expected),
                             "recall": _metrics(preset_rows, selected_all, expected)}
        selected_ids = {item.id for item in selected_all}
        rows.extend({"id": case["id"], "preset": preset, "kind": case["kind"],
                     "expected": case["relevant"], "relevant": case["relevant"],
                     "accepted": case["id"] in selected_ids,
                     "selected": case["id"] in selected_ids}
                    for case in preset_rows)
    selected = [row for row in rows if row["selected"]]
    true_positive = sum(row["expected"] for row in selected)
    errors = [row for row in rows if row["selected"] != row["expected"]]
    accuracy = 1 - (len(errors) / max(1, len(rows)))
    return {"suite": suite, "cases": len(cases), "control_only": control_only,
            "accuracy": accuracy,
            "precision": true_positive / max(1, len(selected)),
            "recall": true_positive / max(1, sum(row["expected"] for row in rows)),
            "by_preset": by_preset, "rows": rows, "errors": errors}
