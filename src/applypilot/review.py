from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .scoring import evaluate_search_filter, filter_candidates, score_vacancy


def write_review(items: list[dict[str, Any]], profile: dict[str, Any], output: Path,
                 top: int = 20) -> Path:
    top = max(0, top)
    confirmed = filter_candidates(items, profile, max(len(items), top),
                                  int(profile.get("min_score", 0)))
    selected = confirmed[:top]
    additional = confirmed[top:]
    confirmed_ids = {item.id for item in confirmed}
    provisional: list[tuple[Any, Any]] = []
    rejected: list[tuple[Any, Any]] = []
    for item in items:
        if str(item.get("id", "")) in confirmed_ids:
            continue
        candidate = score_vacancy(item, profile)
        decision = evaluate_search_filter(candidate.to_dict(), profile.get("search", profile))
        target = rejected if candidate.decision == "reject" or decision.status == "reject" else provisional
        target.append((candidate, decision))
    provisional.sort(key=lambda row: (-row[0].score, row[0].id))
    rejected.sort(key=lambda row: (-row[0].score, row[0].id))

    rows: list[str] = []
    sections = (
        ("Подходящие: top (Selected)",
         [(candidate, evaluate_search_filter(candidate.to_dict(), profile.get("search", profile)))
          for candidate in selected]),
        ("Другие подтверждённые (Additional matches)",
         [(candidate, evaluate_search_filter(candidate.to_dict(), profile.get("search", profile)))
          for candidate in additional]),
        ("Недостаточно данных (Provisional)", provisional),
        ("Отклонённые (Rejected)", rejected),
    )
    summary = "".join(
        f"<li>{html.escape(title)}: {len(candidates)}</li>" for title, candidates in sections
    )
    rows.append(f"<h2>Сводка</h2><ul>{summary}</ul>")
    for title, candidates in sections:
        rows.append(f"<h2>{html.escape(title)} — показано {min(len(candidates), top)}"
                    f" из {len(candidates)}</h2><ol>")
        for candidate, decision in candidates[:top]:
            reasons = (list(candidate.reasons) + list(candidate.hard_killers)
                       + list(candidate.soft_killers) + list(decision.reasons))
            description = candidate.description.strip()
            if not description:
                description = ("Полное описание не загружено. Карточка сохранена для объяснения "
                               "предварительного отбора.")
            rows.append("<li><h3>" + html.escape(candidate.name) + "</h3>"
                        f"<p><a href='{html.escape(candidate.url, quote=True)}'>{html.escape(candidate.url)}</a> "
                        f"score={candidate.score} ranking={html.escape(candidate.decision)} "
                        f"search_filter={html.escape(decision.status)}</p>"
                        f"<p>{html.escape(description[:2000])}</p>"
                        f"<p>reasons: {html.escape(', '.join(reasons) or 'none')}</p>"
                        f"<p>fields: {html.escape(', '.join(decision.fields))}</p></li>")
        rows.append("</ol>")
    output.parent.mkdir(parents=True, exist_ok=True)
    document = "<!doctype html><meta charset='utf-8'><title>ApplyPilot review</title>" \
               f"<h1>ApplyPilot review {datetime.now(UTC).isoformat()}</h1>" + "".join(rows)
    output.write_text(document, encoding="utf-8")
    return output
