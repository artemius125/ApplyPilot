#!/usr/bin/env bash
# applypilot-watch.sh — периодический скан свежих вакансий HH + LLM-скрининг.
#
# Что делает: для каждого направления (AI и инфра/DevOps) запускает `scan`
# с коротким окном свежести (--days 1), затем `screen` результата в отчёт и
# снапшот принятых вакансий. НИЧЕГО НЕ ОТКЛИКАЕТСЯ — отклик остаётся ручным
# и запускается отдельно (`apply`). Watcher только ищет, скринит и логирует,
# чтобы можно было ответить среди первых.
#
# Каталог репозитория настраивается через переменную окружения APPLYPILOT_HOME
# (по умолчанию — родитель родителя каталога самого скрипта).
# Ключ aitunnel берётся из AITUNNEL_API_KEY, иначе из private/data/admin-settings.json.
#
# Лог: private/data/watch.log

set -euo pipefail

# --- Определяем корень репозитория ------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
: "${APPLYPILOT_HOME:="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd -P)"}"

cd -- "$APPLYPILOT_HOME"

VENV="$APPLYPILOT_HOME/.venv"
PY="$VENV/bin/python"
LOG="$APPLYPILOT_HOME/private/data/watch.log"

mkdir -p "$APPLYPILOT_HOME/private/data" \
         "$APPLYPILOT_HOME/private/reports" \
         "$APPLYPILOT_HOME/private/data/snapshots"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG"
}

# --- Активируем виртуальное окружение ---------------------------------------
if [[ ! -x "$PY" ]]; then
    log "ОШИБКА: не найден интерпретатор venv: $PY"
    echo "applypilot-watch: не найден $PY" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# --- Ключ aitunnel ----------------------------------------------------------
# Приоритет у переменной окружения; иначе читаем api_key из настроек админки.
if [[ -z "${AITUNNEL_API_KEY:-}" ]]; then
    AITUNNEL_API_KEY="$("$PY" - "$APPLYPILOT_HOME/private/data/admin-settings.json" <<'PYEOF' || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(str(json.load(fh).get("api_key") or "").strip())
except Exception:
    print("")
PYEOF
)"
    export AITUNNEL_API_KEY
fi
if [[ -z "${AITUNNEL_API_KEY:-}" ]]; then
    log "ПРЕДУПРЕЖДЕНИЕ: AITUNNEL_API_KEY не задан — скрининг будет пропущен"
fi

# --- Один проход по направлению ---------------------------------------------
# Аргументы: <track> <profile> <search> <report> <accepted-snapshot>
run_track() {
    local track="$1" profile="$2" search="$3" report="$4" accepted="$5"
    local scan_out rc snap items screen_out kept

    log "track=$track: scan --days 1 старт"

    # set +e вокруг сетевых вызовов: надёжно ловим код возврата вне зависимости
    # от контекста вызова функции (иначе set -e внутри вызванной в '||' функции
    # ведёт себя неочевидно).
    set +e
    scan_out="$("$PY" -m applypilot --profile "$profile" --search "$search" \
        scan --days 1 2>&1)"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        log "track=$track: scan ПРОВАЛ (rc=$rc): $(printf '%s' "$scan_out" | tail -n1)"
        return 1
    fi

    snap="$(printf '%s\n' "$scan_out" | sed -n 's/.*snapshot: \(.*\)$/\1/p' | tail -n1)"
    items="$(printf '%s\n' "$scan_out" | sed -n 's/.*; items: \([0-9]*\);.*/\1/p' | head -n1)"
    if [[ -z "$snap" || ! -f "$snap" ]]; then
        log "track=$track: scan без снапшота — пропуск скрининга"
        return 1
    fi
    log "track=$track: scan ok, items=${items:-?}, snapshot=$snap"

    set +e
    screen_out="$("$PY" -m applypilot --profile "$profile" --search "$search" \
        screen --input "$snap" --track "$track" \
        --output "$report" --emit-snapshot "$accepted" 2>&1)"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        log "track=$track: screen ПРОВАЛ (rc=$rc): $(printf '%s' "$screen_out" | tail -n1)"
        return 1
    fi

    kept="$(printf '%s\n' "$screen_out" | sed -n 's/.*accepted ([^)]*): \([0-9]*\) ->.*/\1/p' | head -n1)"
    log "track=$track: screen ok, accepted=${kept:-?}, report=$report, accepted-snapshot=$accepted"
    return 0
}

# --- Запуск ------------------------------------------------------------------
log "watch старт (APPLYPILOT_HOME=$APPLYPILOT_HOME)"

overall=0

# Провал одного направления не должен прерывать другое.
run_track ai \
    "private/config/profile.toml" \
    "private/config/search.toml" \
    "private/reports/screen-ai.json" \
    "private/data/snapshots/accepted-ai.json" || overall=1

run_track infra \
    "private/config/profile-infra.toml" \
    "private/config/search-infra.toml" \
    "private/reports/screen-infra.json" \
    "private/data/snapshots/accepted-infra.json" || overall=1

log "watch финиш (overall_rc=$overall)"
exit "$overall"
