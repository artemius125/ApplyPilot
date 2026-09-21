# ApplyPilot

Локальный инструмент для HH.ru: публичный поиск вакансий, объяснимый офлайн-отбор, чтение состояния авторизации, ручная проверка страниц и защищённый журнал откликов. Репозиторий не содержит личного профиля, резюме, cookies, журналов или ключей.

## 🚀 Быстрый старт из терминала

Окружение уже установлено (см. [«Установка»](#установка))? Запуск — одной командой из корня проекта:

```bash
./run.sh
```

Откроется веб-админка на <http://127.0.0.1:8765>. Дальше всё делается кнопками в браузере — сканирование, LLM-скрининг, просмотр вакансий, сопроводительные письма, отклики. Перебирать проект и запускать команды вручную не нужно.

- Другой порт: `./run.sh 9000`.
- Ключ LLM (aitunnel) задаётся один раз во вкладке **«Настройки»** (хранится локально в `private/data/admin-settings.json`, права `600`). Как альтернатива — положите его в `private/config/aitunnel.key` или экспортируйте `AITUNNEL_API_KEY`.
- Эквивалент без скрипта: `.venv/bin/python -m applypilot admin --open`.

Реальная отправка откликов остаётся под защитой: `reviewed = true` в профиле трека **плюс** подтверждение в интерфейсе. Всё остальное (скан, скрининг, письма, dry-run) — безопасно.

## Границы безопасности

`plan` читает входной JSON и существующий SQLite-журнал и сохраняет приватный JSON-план. `apply --dry-run` не открывает браузер, не меняет сессию и не вызывает LLM, но записывает локальный `run_id` и точный список кандидатов для аудита. `inspect` только читает DOM в отдельном Playwright-контексте, без `click`, `fill`, `submit` и изменяющего JavaScript. Реальная отправка возможна только через явно указанный `apply --run`.

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
  --area 113 --add-query "LLM Platform Engineer" --pages 1 --days 7 \
  --request-budget 100 --details-limit 100
```

Поиск проходит по всем явно заданным регионам, запросам и страницам, сохраняет источники
дубликатов и диагностику каждого сегмента. Полные описания загружаются ограниченно после
первичного сбора. Порог зарплаты, валюту, неизвестную зарплату и допустимый опыт можно задать
в TOML; значения CLI имеют приоритет. `benchmark` проверяет базовый уровень шума на
обезличенных AI/LLM, ML, Python и Go примерах.

`scan` использует HTML и `HH-Lux-InitialState`, а не OAuth API. Бюджет поисковых HTTP-запросов и число загружаемых описаний задаются через TOML или `--request-budget`/`--details-limit`; в коде нет скрытого потолка для этих значений. Статусы итогового
снимка — `ok`, `empty`, `truncated` и `partial`; ошибки отдельных сегментов (например,
403/429, CAPTCHA, сеть или отсутствующая структура HTML) сохраняются отдельно. Ошибочная
пустая выдача не заменяет последний успешный снимок.

`--request-budget` учитывает каждый поисковый HTTP-вызов, включая повторы после ошибок
и переходы по редиректам HH, суммарно для всех групп поиска. В диагностике сегмента
`pages` — обработанные страницы, `requests` — фактическое число HTTP-вызовов.
Загрузка описаний ограничивается отдельно через `--details-limit` и не входит в этот бюджет.
Неизвестная структура выдачи считается ошибкой, а не пустым успешным результатом.

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

Уровень опыта ограничивается только настройкой `experience.allowed`: без неё отдельного
запрета на вакансии с опытом более 6 лет нет. Для таких вакансий можно указать
`allowed = ["moreThan6"]` в секции `[experience]`. В примерном поисковом профиле
по-прежнему перечислены уровни до 6 лет — измените список под свой опыт.

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

## Веб-админка и LLM-скрининг

`applypilot admin` (или `./run.sh`) поднимает локальную панель на <http://127.0.0.1:8765> — только `127.0.0.1`, без внешнего доступа. Через неё удобно вести весь процесс, не запоминая команды.

- **Обзор** — баланс LLM, по трекам FIT/MAYBE/SKIP и количество свежих, топ-FIT со ссылками и кнопкой «Письмо», быстрые действия.
- **Вакансии** — разделы **Активные / Просмотренные / Плохие**. Открытие ссылки помечает вакансию «просмотрено» и убирает из активного пула; кнопка «👎 плохая» — из очереди откликов вовсе. Видны требуемый опыт (подсветка, если выше твоего), зарплата, свернутые дубли-реповторы HH. Кнопка «Выгрузить в текст» в разделе «Плохие» даёт `.md` для ручной донастройки правил.
- **Отклики** — очередь: видно, какие именно вакансии и в каком порядке уйдут, режимы «все / только FIT / только ★». Пробный запуск безопасен; реальная отправка — только при `reviewed = true` и подтверждении.
- **Статистика** — баланс, расход по дням, вердикты по трекам.
- **Резюме и треки** — читает активные резюме с HH и сверяет с треками; кнопка «Добавить трек» создаёт скелет `profile-<ключ>.toml` + `search-<ключ>.toml`. Панель **«Автопоиск (таймер)»** ставит/включает systemd-таймер регулярного скана+скрининга (подробности — [`packaging/README.md`](packaging/README.md)).
- **Настройки** — модель, свой API-ключ (multi-user), ограничения кандидата, зарплатный ориентир и правила скрининга по каждому треку.

**LLM-скрининг** (`applypilot screen`, опционально) — двухшаговый: сначала механический фильтр (`scan` → score), затем модель читает описание и ставит вердикт FIT/MAYBE/SKIP с причиной и `fit_score`. Модель по умолчанию `gpt-5-mini` через OpenAI-совместимый API (aitunnel); ключ — из `AITUNNEL_API_KEY`, `private/config/aitunnel.key` или из «Настроек». Треки задаются в `private/config/tracks.toml` (число не захардкожено: трек = резюме + запросы + рубрика). Сопроводительные письма (`letters.py`) генерируются под конкретную вакансию по фактам профиля.

## Сессия HH и контролируемый запуск

Cookies никогда не копируются в репозиторий, `.env`, отчёт или командную строку. Один раз
установите браузер в приватный каталог и войдите в открывшемся изолированном окне:

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/private/browsers"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m applypilot login
.venv/bin/python -m applypilot session check
```

После ручного входа Playwright сохраняет state только в `private/data/hh_session.json`.
Не подменяйте этот файл cookies из браузерных расширений и не добавляйте его в Git. Перед
любым запуском проверьте реальные доступные резюме и страницы кандидатов отдельным
read-only контекстом:

```bash
.venv/bin/python -m applypilot inspect --resumes
.venv/bin/python -m applypilot inspect --input private/data/snapshots/FILE.json \
  --selected --limit 15 --preset ai-agents-llmops
```

Полный безопасный цикл создаёт приватные артефакты и не отправляет отклики до последней
команды:

```bash
.venv/bin/python -m applypilot history import \
  --source private/archive/operation_exit/TOOLS/results/apply_log.csv
.venv/bin/python -m applypilot plan --input private/data/snapshots/FILE.json \
  --preset ai-agents-llmops --limit 15
.venv/bin/python -m applypilot apply --input private/data/snapshots/FILE.json \
  --preset ai-agents-llmops --dry-run --limit 10
# Review the generated private plan, run ID, resumes and inspection results first.
# Set reviewed = true in private/config/profile.toml only after that review.
.venv/bin/python -m applypilot apply --input private/data/snapshots/FILE.json \
  --preset ai-agents-llmops --run --limit 30 --target-success 10
.venv/bin/python -m applypilot sync  # all negotiation pages; use --pages N to set a ceiling
.venv/bin/python -m applypilot analytics
```

During `apply --run`, each candidate is logged before and after a potential submission. An
external ATS, CAPTCHA, screening question or ambiguous resume becomes `needs_manual`. After
the final action ApplyPilot waits for the configured confirmation deadline and, if the page is
still ambiguous, checks the read-only HH negotiations ledger. A matching vacancy reconciles to
`success`; only an unconfirmed `unknown` stops the run without a retry. The run record reports
its exact `run_id` and per-status counts.
`--limit` задаёт потолок кандидатов, а `--target-success` — число подтверждённых откликов:
`needs_manual` и `already_applied` не засчитываются, вместо них берутся следующие кандидаты.

Если popup HH предлагает ровно одно видимое резюме, используется этот уже выбранный вариант.
При нескольких вариантах требуется единственное совпадение с настроенным названием
(регистр и лишние пробелы не учитываются). Несколько вариантов без точного совпадения,
дубликаты совпадения или отсутствие названия переводят вакансию в `needs_manual` без submit.
Если первый клик HH сам отправляет отклик без формы, выбора резюме в этом сценарии нет;
заранее проверьте выбранное резюме в HH. CAPTCHA после потенциально отправляющего
действия считается `unknown` и блокирует повтор.

## История и LLM

```bash
.venv/bin/python -m applypilot history import --source private/archive/operation_exit/TOOLS/results
.venv/bin/python -m applypilot history reconcile --input private/reports/confirmed.json
.venv/bin/python -m applypilot llm preview --input private/data/snapshots/example.json --id 123
```

SQLite хранит запуски, точные списки кандидатов, попытки, события и атомарные резервы бюджета. Дедупликация идёт по приватному ключу аккаунта и ID вакансии: блокируются только `success`, `already_applied`, неподтверждённый `unknown` и незавершённый `submitting`; старые `skipped` не исключают свежую вакансию. `sync` автоматически переводит `unknown` в `success`, когда тот же vacancy ID присутствует в HH negotiations. Лимиты берутся из приватного профиля, без скрытого hard cap. Старый `timeout` импортируется как `unknown`; CSV остаётся форматом импорта/экспорта.

После прерванного запуска следующий `apply --run` или `sync` под блокировкой журнала
переводит оставшиеся `submitting` в `unknown`, сохраняя запрет повторной отправки.
Сбой генерации письма до отправки получает `failed_before_submit` и не блокирует
следующую попытку. Автоматическая сверка требует известного статуса HH и данных,
полученных не раньше текущего неопределённого результата.

При ограничении `sync --pages N` неполный обход явно помечается `truncated` в журнале,
консоли и JSON-отчёте. Полный обход заменяет текущие статусы только выбранного аккаунта;
неполный обновляет только найденные записи. Ошибка разметки или сети сохраняет прежние
данные. Удаление записи из текущей выдачи HH не удаляет локальную историю отправки.

### Сопроводительные письма

В `[cover_letter]` задайте `mode = "off"`, `"template"` или `"llm"`.
Явный `mode` имеет приоритет над `[llm].enabled`; старые профили без `mode`
сохраняют прежнее поведение через `llm.enabled`. В примере режим выключен.

Для письма без LLM уже есть готовый текст в
[`examples/profile.example.toml`](examples/profile.example.toml): выберите
`mode = "template"` и укажите своё имя. Шаблон берётся из `template` либо
из `template_file = "cover-letter.md"`, одновременно задавать оба нельзя.
Поддерживаются `{name}`, `{vacancy}`, `{company}`, `{resume}`, `{motivation}`,
`{summary}`, `{resume_text}`, `{skills}`, `{experience}`, `{projects}`.
Неизвестная переменная или пустое значение используемого поля останавливают
подготовку письма с ошибкой. Для буквальных фигурных скобок используйте `{{` и `}}`.

Чтобы LLM опиралась на резюме, добавьте в приватный профиль:

```toml
[professional]
resume_file = "resume.md"
summary = "" # ваше профессиональное описание
skills = []  # ваши навыки
```

Файл резюме должен содержать полный текст в UTF-8 (`.txt` или `.md`);
путь считается от директории профиля. Вместо файла можно задать `resume_text`.
PDF/DOCX автоматически не разбираются. Опыт и проекты добавляются таблицами
`[[professional.experience]]` и `[[professional.projects]]`; доступные поля есть
в примере профиля. Заполняйте их своими фактами: приложение не извлекает
профессиональную историю из HH автоматически.

Для генерации выберите `mode = "llm"`, укажите `[llm].model` и переменную окружения
`OPENROUTER_API_KEY`. В OpenRouter передаются имя, местоположение, английский,
мотивация, заполненные профессиональные поля и текст резюме, название выбранного
резюме и данные вакансии. Контакты, написанные внутри текста резюме, автоматически
не удаляются. Модель получает инструкцию использовать только подтверждённые факты,
но достоверность результата нужно проверять самостоятельно.

Модель проверяется по каталогу, автоматического перехода на другую модель нет.
Кэш учитывает вакансию, выбранное резюме, профиль с загруженным текстом, модель
и версию промпта. Пустой ответ считается ошибкой. При
`fallback_to_template = true` сбой LLM заменяется заранее проверенным шаблоном;
по умолчанию такой переход выключен. Некорректный профиль требует исправления.

Проверка письма без отправки отклика (в режиме LLM может вызвать OpenRouter):

```bash
.venv/bin/python -m applypilot letter preview \
  --input private/data/snapshots/example.json --id 123
```

`llm preview` сохранён как совместимый вариант команды. Предпросмотр и реальный
отклик используют одну подготовку письма; источник выводится как `template`,
`generated`, `cache` или `template_fallback`. `apply --dry-run` писем не генерирует.

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
