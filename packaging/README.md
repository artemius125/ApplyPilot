# Packaging ApplyPilot as a Debian `.deb`

`build-deb.sh` produces a self-contained Debian package. It bundles a Python
virtual environment (with ApplyPilot installed) under `/opt/applypilot` and
installs a launcher at `/usr/local/bin/applypilot`.

## Build

From the repository root:

```sh
bash packaging/build-deb.sh
```

The resulting file is written to the repository root as
`applypilot_<version>_amd64.deb`.

### Options

Both options are passed as environment variables:

- `VERSION` — override the package version. By default the version is read
  from `[project].version` in `pyproject.toml`.

  ```sh
  VERSION=1.2.3 bash packaging/build-deb.sh
  ```

- `WITH_BROWSER=1` — also install the optional `browser` extra (Playwright)
  into the bundled virtual environment.

  ```sh
  WITH_BROWSER=1 bash packaging/build-deb.sh
  ```

They can be combined:

```sh
VERSION=1.2.3 WITH_BROWSER=1 bash packaging/build-deb.sh
```

## Install

```sh
sudo dpkg -i applypilot_*.deb
```

If `dpkg` reports missing dependencies, resolve them with:

```sh
sudo apt-get -f install
```

## Run

```sh
applypilot --help
applypilot admin
```

The launcher simply runs `python -m applypilot` inside the bundled venv, so any
subcommand works the same as running the tool from source.

## Uninstall

```sh
sudo dpkg -r applypilot
```

## Limitations

- **Network is required at build time.** `build-deb.sh` runs `pip install`
  inside the bundled venv, which downloads dependencies from PyPI.
- **The venv is tied to a specific Python version.** The bundled environment is
  built against the `python3` used at build time (3.12+). The target machine
  must provide a compatible `python3` (declared as `Depends: python3 (>= 3.12)`).
  Building on a different minor version than the target can break the venv.
- **Playwright browsers are not bundled.** Even with `WITH_BROWSER=1`, only the
  Playwright Python package is installed. The user installs the browser binaries
  separately after installation (for example
  `/opt/applypilot/venv/bin/python -m playwright install`).
- **HH.ru sign-in is configured by the user.** Authentication and any HH.ru
  login are set up by the user after installation; nothing is baked into the
  package.
- **Private data is not included.** The repository's `private/` directory and
  any other personal data are never copied into the package.

## Автопоиск свежих вакансий (watch)

`applypilot-watch.sh` периодически ищет **свежие** вакансии на HH по обоим
направлениям пользователя и сразу их скринит, чтобы можно было ответить среди
первых. Для каждого направления скрипт запускает:

1. `scan --days 1` — короткое окно свежести, с конфигом поиска направления;
2. `screen` полученного снапшота — в отчёт и снапшот принятых вакансий.

**Отклик остаётся ручным.** Watcher только ищет, скринит и пишет лог — он
никогда не откликается. Отклик по-прежнему запускается отдельно и вручную
(`applypilot ... apply`).

Направления:

| Направление | profile / search | отчёт | снапшот принятых |
|-------------|------------------|-------|------------------|
| AI (`--track ai`) | `private/config/profile.toml` / `private/config/search.toml` | `private/reports/screen-ai.json` | `private/data/snapshots/accepted-ai.json` |
| Инфра/DevOps (`--track infra`) | `private/config/profile-infra.toml` / `private/config/search-infra.toml` | `private/reports/screen-infra.json` | `private/data/snapshots/accepted-infra.json` |

Ключ aitunnel берётся из переменной окружения `AITUNNEL_API_KEY`, а если она не
задана — из `api_key` в `private/data/admin-settings.json`. Корень репозитория
настраивается через `APPLYPILOT_HOME` (по умолчанию — родитель каталога
`packaging/`).

### Установка таймера systemd (user)

Юниты ставятся в пользовательский systemd, без `sudo`:

```sh
mkdir -p ~/.config/systemd/user
cp packaging/applypilot-watch.service packaging/applypilot-watch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now applypilot-watch.timer
```

По умолчанию таймер срабатывает через 5 минут после загрузки и далее раз в час
(`OnBootSec=5min`, `OnUnitActiveSec=1h`, `Persistent=true`). Интервал меняется в
`applypilot-watch.timer` (строка `OnUnitActiveSec=`), после чего:

```sh
systemctl --user daemon-reload
systemctl --user restart applypilot-watch.timer
```

Проверить расписание и разовый прогон:

```sh
systemctl --user list-timers applypilot-watch.timer
systemctl --user start applypilot-watch.service   # прогнать прямо сейчас
```

Перед установкой поправьте `APPLYPILOT_HOME` в `applypilot-watch.service` под
свой путь к репозиторию (по умолчанию `%h/Загрузки/ApplyPilot`). Ключ aitunnel
можно задать там же строкой `Environment=AITUNNEL_API_KEY=sk-aitunnel-...`.

### Альтернатива: cron

Если systemd-таймеры не используются, тот же скрипт можно повесить в `crontab -e`
(каждый час):

```cron
0 * * * * AITUNNEL_API_KEY=sk-aitunnel-... APPLYPILOT_HOME=/home/artem/Загрузки/ApplyPilot /home/artem/Загрузки/ApplyPilot/packaging/applypilot-watch.sh
```

### Лог

Каждый прогон дописывает строки с отметкой времени (старт, счётчики по
направлениям, финиш) в:

```
private/data/watch.log
```
