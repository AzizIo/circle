#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Не найдено виртуальное окружение. Сначала запусти: ./setup.sh" >&2
  exit 2
fi
if [[ ! -f "$ROOT/config.json" ]]; then
  echo "Не найден config.json. Сначала выполни: ./.venv/bin/python combined_multi_account.py setup" >&2
  exit 2
fi
cd "$ROOT"
exec "$PYTHON" "$ROOT/combined_multi_account.py" run "$@"