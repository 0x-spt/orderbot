#!/usr/bin/env bash
# Starts the order check robot. Does only preflight checks; caffeinate, the lock and the
# Telegram start/stop messages are done by `python -m orderbot` itself.
#   scripts/run.sh          run the robot (stop with Ctrl+C)
#   scripts/run.sh --check  only check that everything is in place, start nothing
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

if [ ! -f .env ]; then
  echo "Нет файла .env в $ROOT. Создайте его: cp .env.example .env, заполните и выполните chmod 600 .env" >&2
  exit 1
fi

MODE="$(stat -f '%Lp' .env 2>/dev/null || stat -c '%a' .env 2>/dev/null || echo "")"
if [ "$MODE" != "600" ]; then
  echo "ВНИМАНИЕ: у файла .env права $MODE, а должны быть 600 (в нём ключи). Выполните: chmod 600 .env" >&2
fi

# Read LAYOUT_FILE from .env as text (the file is not executed).
LAYOUT="$(grep -E '^[[:space:]]*LAYOUT_FILE=' .env | tail -n 1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//")"
LAYOUT="${LAYOUT:-${LAYOUT_FILE:-}}"
if [ -z "$LAYOUT" ]; then
  echo "В .env не задан LAYOUT_FILE (например layouts/production.yaml)." >&2
  exit 1
fi
case "$LAYOUT" in /*) ;; *) LAYOUT="$ROOT/$LAYOUT" ;; esac
if [ ! -f "$LAYOUT" ]; then
  echo "Файл разметки экрана не найден: $LAYOUT. Сделайте его по README, раздел «Калибровка экрана»." >&2
  exit 1
fi

if [ ! -f .venv/bin/activate ]; then
  echo "Нет окружения .venv. Выполните установку из README." >&2
  exit 1
fi

if [ "$CHECK" = 1 ]; then
  echo "Проверка пройдена."
  exit 0
fi

# shellcheck disable=SC1091
. .venv/bin/activate
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec python -m orderbot
