# ApplyPilot

Локальный инструмент для HH.ru: публичный поиск вакансий, объяснимый офлайн-отбор, чтение состояния авторизации, ручная проверка страниц и защищённый журнал откликов. Репозиторий не содержит личного профиля, резюме, cookies, журналов или ключей.

## Границы безопасности

`plan` и `apply --dry-run` читают входной JSON и существующий SQLite-журнал, но не создают базу, не меняют сессию, не вызывают LLM и не открывают браузер. `inspect` только читает DOM в отдельном Playwright-контексте: максимум три вакансии, без `click`, `fill`, `submit` и изменяющего JavaScript. Реальная отправка возможна только через явно указанный `apply --run`; она не проверена в рамках этой миграции.

Неопределённый результат после потенциальной отправки получает `unknown`. Такой статус блокирует автоматический повтор и требует явного подтверждения через `history reconcile`.

## Установка

```bash
export PIP_CACHE_DIR="$PWD/private/pip-cache"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/private/browsers"
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.lock
# браузерные команды — отдельно:
.venv/bin/pip install -r requirements-browser.lock
.venv/bin/pip install -e ".[dev,browser]"
.venv/bin/python -m playwright install chromium
```

Для воспроизводимой установки используйте зафиксированные файлы `requirements.lock`, `requirements-dev.lock` и `requirements-browser.lock` как вход для `pip install -r`. На медленном внешнем диске создание окружения может быть заметно дольше; это не считается доказанной причиной зависания.

Скопируйте `examples/profile.example.toml` в `private/config/profile.toml` и `examples/search.example.toml` в `private/config/search.toml`. Личный профиль уже хранится локально, но игнорируется Git. Перед реальными отправками профиль должен быть проверен вручную: `reviewed = true`.

Глобальные параметры (`--data-dir`, `--profile`, `--search`) указываются до команды: `applypilot --data-dir private/data scan ...`.

Пути задаются флагами, затем переменными `APPLYPILOT_DATA_DIR` и `APPLYPILOT_PROFILE`, затем локальными `private/data` и `private/config/profile.toml`. Поисковая конфигурация берётся из `--search` или `private/config/search.toml`.

## Рабочий цикл без отправки

```bash
.venv/bin/python -m applypilot --help
.venv/bin/python -m applypilot doctor
.venv/bin/python -m applypilot scan --query "AI Agent"
.venv/bin/python -m applypilot plan --input private/data/snapshots/example.json --limit 5
.venv/bin/python -m applypilot apply --input private/data/snapshots/example.json --dry-run --limit 5
.venv/bin/python -m applypilot session check
.venv/bin/python -m applypilot inspect --input private/data/snapshots/example.json --limit 3
.venv/bin/python -m applypilot analytics
.venv/bin/python -m applypilot review --input private/data/snapshots/example.json \
  --preset python-backend --top 20
.venv/bin/python -m applypilot benchmark --suite tech-roles --control-only
.venv/bin/python -m applypilot templates list
.venv/bin/python -m applypilot config show --preset python-backend
```

Для универсального поиска используйте готовые пресеты `ai-llm`, узкий
`ai-agents-llmops`, `ml-engineering`, `python-backend`, `go-backend` и
`software-general`. `ai-agents-llmops` рассчитан на engineering-вакансии в
AI Agents, LLM, RAG и LLMOps: QA, security, продажи и обучение в него не входят.
Пример read-only запуска:

```bash
.venv/bin/python -m applypilot scan --preset python-backend --area 113 \
  --remote --days 14 --pages 1 --details-limit 50
.venv/bin/python -m applypilot plan --preset python-backend \
  --input private/data/snapshots/FILE.json --limit 10 --min-score 30 --rescore
```

Для профильного поиска AI-агентов используйте отдельный пресет. `--query`
намеренно заменяет весь набор его запросов; чтобы добавить формулировку и не
потерять базовые запросы, используйте повторяемый `--add-query`:

```bash
.venv/bin/python -m applypilot scan --preset ai-agents-llmops \
  --area 113 --add-query "LLM Platform Engineer" --pages 1 --days 7 --details-limit 40
```

Поиск проходит по всем явно заданным регионам, запросам и страницам, сохраняет источники
дубликатов и диагностику каждого сегмента. Полные описания загружаются ограниченно после
первичного сбора. Порог зарплаты, валюту, неизвестную зарплату и допустимый опыт можно задать
в TOML; значения CLI имеют приоритет. `benchmark` проверяет базовый уровень шума на
обезличенных AI/LLM, ML, Python и Go примерах.

`scan` использует HTML и `HH-Lux-InitialState`, а не OAuth API. Статусы итогового
снимка — `ok`, `empty`, `truncated` и `partial`; ошибки отдельных сегментов (например,
403/429, CAPTCHA, сеть или отсутствующая структура HTML) сохраняются отдельно. Ошибочная
пустая выдача не заменяет последний успешный снимок.

Для сценария «город + удалёнка по стране» используйте группы в TOML:

```toml
[[groups]]
name = "local"
queries = ["Go Backend"]
areas = [1, 54]
only_remote = false

[[groups]]
name = "remote-country"
queries = ["Go Backend remote"]
areas = [113]
only_remote = true
```

Доступны `salary.missing = "include" | "exclude" | "only"` и `salary.policy = "possible" | "guaranteed"`; валюта не конвертируется. `config show` показывает эффективные значения и источник (`default`, `preset`, `toml`, `cli preset`) без профиля.

Шаблоны можно посмотреть и создать без перезаписи существующего файла:

```bash
.venv/bin/python -m applypilot templates list
.venv/bin/python -m applypilot templates init --name ai-agents-llmops \
  --output private/config/ai-agents-search.toml
```

Сканер перебирает запросы, группы, регионы и страницы с общим бюджетом, дедуплицирует ID и сохраняет причины фильтрации. `truncated` означает, что лимит страниц достигнут; `partial` — что часть сбора завершилась ошибкой. Без полного описания вакансия остаётся `unknown`/`provisional` и не попадает в план подтверждённых откликов.

HTML-review разделяет top, остальные подтверждённые совпадения, provisional и
отклонённые вакансии. Пустая карточка не означает ошибку: если полный текст не
загружался из-за лимита enrichment, отчёт показывает это явным сообщением.

`session check` классифицирует формат файла, подтверждённый вход, истёкшую сессию, сетевую ошибку и неизвестную разметку. `login` запускает только собственный Playwright-браузер и сохраняет storage state атомарно с ограничением прав, если это поддерживает файловая система.

## История и LLM

```bash
.venv/bin/python -m applypilot history import --source private/archive/operation_exit/TOOLS/results
.venv/bin/python -m applypilot history reconcile --input private/reports/confirmed.json
.venv/bin/python -m applypilot llm preview --input private/data/snapshots/example.json --id 123
```

SQLite хранит попытки, события и атомарные резервы бюджета. Дедупликация идёт по аккаунту и ID вакансии; безопасный профильный предел — 5 попыток за запуск и 20 за UTC-сутки. Старый `timeout` импортируется как `unknown`; последовательность `timeout → success` сохраняет окончательный успех. CSV остаётся форматом импорта/экспорта.

LLM выключен по умолчанию. При включении нужно явно указать модель и ключ OpenRouter; модель проверяется по каталогу, автоматического перехода на платную модель нет. Максимум две попытки и 30 секунд, кэш зависит от ID, названия и описания вакансии, профиля, модели и версии промпта. В провайдер отправляется только минимальный набор сведений профиля.

Rerank — отдельный необязательный режим и не часть базового score:

```bash
.venv/bin/python -m applypilot llm rerank \
  --input private/data/snapshots/FILE.json --model provider/model --limit 20 --enable
```

Без `--enable` внешнего запроса нет. Модель обязательна, максимум — 20 кандидатов, общий deadline — 30 секунд.

## Приватные данные и публикация

Исходный проект не изменяется. Приватная копия — `private/archive/operation_exit`; профиль,
резюме, старые результаты, журнал и отчёты — в `private/`. Полный старый рабочий каталог в
репозиторий не копируется. Перед публикацией запускайте:

```bash
SOURCE_DIR=/absolute/path/to/OPERATION_EXIT
.venv/bin/python scripts/verify_archive.py \
  --source "$SOURCE_DIR" \
  --archive private/archive/operation_exit \
  --report private/reports/archive_manifest.txt
.venv/bin/python scripts/check_public.py
.venv/bin/python -m build
.venv/bin/python scripts/check_public.py
```

Проверяются текущие tracked-файлы, все доступные Git-коммиты и wheel/sdist. Значения секретов в отчёты не выводятся. GitHub и реальные отклики в этот этап не входят.

## Проверки

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Устройство модулей и границы данных описаны в
[архитектуре](docs/ARCHITECTURE.md). Локальные профили, отчёты и рабочие
журналы остаются в `private/` и не входят в публичный репозиторий.
