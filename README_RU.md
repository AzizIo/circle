# CIRCLES CONTROL 3.7.2

Многопрофильный Telegram-архиватор, менеджер рассылки, Smart Cleaner и локальный Telegram Manager.

## Чистая установка на Ubuntu 22.04/24.04

```bash
git clone https://github.com/AzizIo/circle.git
cd circle
chmod +x setup.sh start.sh scripts/install_systemd.sh
./setup.sh
./start.sh
```

`setup.sh` создаст `.venv`, установит зависимости и запустит мастер первоначальной настройки, если `config.json` ещё нет. В мастере укажи API ID/API Hash Telegram и настройки бота. Не помещай секреты в Git.

При последующих обновлениях:

```bash
git pull --ff-only
./setup.sh       # можно запускать повторно; конфиг и данные сохраняются
./start.sh
```

Чтобы установить systemd-службу после первоначальной настройки:

```bash
sudo ./scripts/install_systemd.sh
sudo journalctl -u circles-archiver -f
```

Служба запускается от пользователя, вызвавшего установщик (или `SUDO_USER`), из каталога репозитория. `config.json`, сессии и данные должны храниться на постоянном диске.

## Перенос существующей установки без повторной авторизации

Если текущая рабочая копия находится, например, в `/root/work`, а новый репозиторий клонирован в отдельную папку, не запускай мастер начальной настройки: он создаст новый каталог сессий. Сначала останови рассылку через Telegram-панель и штатно останови старую службу. Затем из новой папки репозитория выполни:

```bash
sudo systemctl stop circles-archiver
./scripts/link_existing_install.sh /root/work
./setup.sh
./start.sh
```

Скрипт копирует только `config.json` в новую папку, задаёт для основного профиля абсолютный путь к прежнему `data/` и подключает существующий `accounts/` символической ссылкой. Файлы `.session`, SQLite, очереди и медиаданные не копируются, не перемещаются и не переписываются. Скрипт откажется работать, если старая служба ещё активна или в новой папке уже есть `config.json` / `accounts/`.

Сначала проверь запуск вручную через `./start.sh`. Только после проверки устанавливай systemd из нового каталога командой `sudo ./scripts/install_systemd.sh` — она обновит unit `circles-archiver` на новый путь.

## Состав

- `combined_multi_account.py` — основной процесс, менеджер аккаунтов, архиватор и жизненный цикл рассылки.
- `broadcast_core.py` — постоянное состояние по чатам и очередь действий.
- `telegram_manager_web.py` — API веб-панели. Получает уже подключённые Telethon-клиенты CIRCLES CONTROL и не открывает `.session` самостоятельно.
- `smart_cleaner.py` — классификация чатов, безопасный предпросмотр и выход из выбранных чатов.
- `telegram_manager_panel.html` — интерфейс для `telegram_manager_web.py`.
- `config.json`, `accounts/`, `data/`, `manager_data/` создаются локально и не добавляются в Git.

## Сессии, секреты и данные

В репозитории намеренно нет API Hash, токенов ботов, Telegram `.session`, баз пользователя, очередей рассылки и локального каталога чатов. Всё это исключено через `.gitignore`. Не добавляй эти файлы в Git принудительно.

Каждый аккаунт использует собственный `data_dir` и собственную сессию. Smart Manager получает объект уже подключённого клиента из `run_service` / `AccountManager`; второй клиент для той же SQLite-сессии не создаётся.

Запуск дополнительного профиля не должен молча расширять список получателей рассылки или включать автоподписку. Сохранённая политика пользователя остаётся неизменной.

Сохранённая кампания сама по себе не является разрешением на отправку. После перезапуска она возобновляется автоматически только при `broadcast_autostart=true`. При выключенном флаге checkpoint сохраняется, но кампания ждёт ручного запуска или явно переданного `--broadcast`.

При остановке сначала завершается фоновая задача рассылки, и только потом закрываются её SQLite-хранилища. Клиент Telegram отключается после завершения фоновых операций.

Smart Manager по умолчанию слушает только `127.0.0.1:8765`. Подключайся через SSH-туннель:

```bash
ssh -L 8765:127.0.0.1:8765 USER@YOUR_VPS
```

Открой `http://127.0.0.1:8765`. Токен хранится локально в `manager_data/`; не публикуй его и не открывай панель напрямую в Интернет.

## Команды

```bash
./setup.sh
./start.sh                    # обычный запуск менеджера
.venv/bin/python combined_multi_account.py setup
.venv/bin/python combined_multi_account.py accounts
.venv/bin/python combined_multi_account.py add-account work
.venv/bin/python combined_multi_account.py login --profile work
.venv/bin/python combined_multi_account.py run --broadcast --interval 60 --hours 4
```

`--broadcast` — явная команда оператора на запуск кампании. Она не меняет сохранённый `broadcast_autostart`.

## Тесты

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m compileall -q combined_multi_account.py broadcast_core.py smart_cleaner.py telegram_manager_web.py
.venv/bin/python -m pytest -q
```

GitHub Actions запускает компиляцию и unit/regression suite на Python 3.12. Тесты используют mock-клиенты и временные базы; они не заменяют приёмочную проверку на живом Telegram. Реальный вход в аккаунт, Telegram API, FloodWait, запуск systemd и поведение рассылки нужно подтвердить на целевом VPS.