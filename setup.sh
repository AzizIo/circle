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

# Re-enter the wizard after an interrupted/partial setup. Previously every field was
# saved immediately, but setup.sh only checked whether config.json existed, so a
# Ctrl+C left a partial configuration that was incorrectly treated as complete.
if ! .venv/bin/python - <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path("config.json")
try:
    cfg = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)

valid = (
    bool(cfg.get("api_id"))
    and bool(cfg.get("api_hash"))
    and bool(re.fullmatch(r"-100\d+", str(cfg.get("target_chat_id") or "")))
    and bool(cfg.get("bot_token"))
)
raise SystemExit(0 if valid else 1)
PY
then
  echo
  echo "Первичная настройка не завершена или ID форума некорректен."
  echo "Открываю мастер настройки повторно; сохранённые API-ключи будут оставлены."
  .venv/bin/python combined_multi_account.py setup
fi

echo
echo "Готово. Запусти сервис командой: ./start.sh"
