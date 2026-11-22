#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ARG="${1:-}"

if [[ -z "$SOURCE_ARG" ]]; then
  echo "Использование: $0 /path/to/current-circles-install" >&2
  echo "Пример: $0 /root/work" >&2
  exit 2
fi
if [[ ! -d "$SOURCE_ARG" ]]; then
  echo "Не найдена исходная папка: $SOURCE_ARG" >&2
  exit 2
fi
SOURCE="$(cd "$SOURCE_ARG" && pwd -P)"

if [[ "$SOURCE" == "$ROOT" ]]; then
  echo "Исходная папка совпадает с новым репозиторием." >&2
  exit 2
fi
if [[ ! -f "$SOURCE/config.json" ]]; then
  echo "В $SOURCE нет config.json; существующая установка не распознана." >&2
  exit 2
fi
if [[ ! -d "$SOURCE/data" ]]; then
  echo "В $SOURCE не найдена папка data/. Ничего не изменено." >&2
  exit 2
fi
if [[ -e "$ROOT/config.json" || -L "$ROOT/config.json" ]]; then
  echo "В новом репозитории уже есть config.json. Отказываюсь перезаписывать его." >&2
  exit 2
fi
if [[ -e "$ROOT/accounts" || -L "$ROOT/accounts" ]]; then
  echo "В новом репозитории уже существует accounts/. Отказываюсь менять её." >&2
  exit 2
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  if systemctl is-active --quiet circles-archiver; then
    echo "Сначала штатно останови старый сервис: sudo systemctl stop circles-archiver" >&2
    echo "Также останови активную рассылку из Telegram-панели, чтобы сохранить checkpoint." >&2
    exit 3
  fi
fi

python3 - "$SOURCE" "$ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
source_config = source / "config.json"
target_config = root / "config.json"

with source_config.open("r", encoding="utf-8") as handle:
    config = json.load(handle)

data_dir = Path(str(config.get("data_dir") or "data"))
if not data_dir.is_absolute():
    data_dir = source / data_dir
data_dir = data_dir.resolve()
if not data_dir.is_dir():
    raise SystemExit(f"Каталог данных не найден: {data_dir}")

# Keep all Telegram credentials and settings unchanged, but anchor the primary
# profile's data directory to the existing installation so the same session,
# SQLite databases and broadcast checkpoint are reused.
config["data_dir"] = str(data_dir)
config.pop("_config_path", None)

tmp = target_config.with_name("config.json.migration.tmp")
with tmp.open("w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, target_config)
PY

if [[ -d "$SOURCE/accounts" ]]; then
  ln -s "$SOURCE/accounts" "$ROOT/accounts"
fi

chmod 700 "$ROOT"
chmod 600 "$ROOT/config.json"

echo "Конфигурация подключена к существующей установке."
echo "Основной data_dir: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data_dir"])' "$ROOT/config.json")"
if [[ -L "$ROOT/accounts" ]]; then
  echo "Дополнительные профили подключены через: $ROOT/accounts -> $(readlink "$ROOT/accounts")"
else
  echo "Папка accounts/ в исходной установке не найдена; дополнительных профилей нет."
fi
echo "Файлы .session, SQLite и очереди не копировались и не перемещались."
echo "Далее выполни ./setup.sh, затем сначала протестируй ./start.sh в foreground."
