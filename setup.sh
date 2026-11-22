#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
command -v python3 >/dev/null || { echo "Нужен Python 3.10+" >&2; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Требуется Python 3.10 или новее")
PY
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f config.json ]]; then
  echo
  echo "Первичная настройка CIRCLES CONTROL"
  .venv/bin/python combined_multi_account.py setup
fi
echo
echo "Готово. Запусти сервис командой: ./start.sh"