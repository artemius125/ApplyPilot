#!/usr/bin/env bash
# Быстрый запуск ApplyPilot: поднимает локальную веб-админку и открывает её в браузере.
#
#   ./run.sh            # запуск на порту 8765
#   ./run.sh 9000       # другой порт
#
# Ключ aitunnel берётся: из переменной AITUNNEL_API_KEY, иначе из private/config/aitunnel.key,
# иначе — задаётся один раз во вкладке «Настройки» админки (хранится в private/data/admin-settings.json).
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT="${1:-8765}"

if [ ! -x .venv/bin/python ]; then
  echo "Нет .venv — сначала установка (см. README, раздел «Установка»)." >&2
  exit 1
fi

if [ -z "${AITUNNEL_API_KEY:-}" ] && [ -f private/config/aitunnel.key ]; then
  export AITUNNEL_API_KEY="$(tr -d ' \t\r\n' < private/config/aitunnel.key)"
fi

echo "ApplyPilot → http://127.0.0.1:${PORT}  (Ctrl-C для остановки)"
exec .venv/bin/python -m applypilot admin --port "${PORT}" --open
