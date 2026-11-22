# CIRCLES CONTROL 3.7.2

Multi-account Telegram media archiver, broadcast manager, Smart Cleaner and local Telegram Manager.

## Clean install (Ubuntu 22.04/24.04)

```bash
git clone https://github.com/AzizIo/circle.git
cd circle
chmod +x setup.sh start.sh scripts/install_systemd.sh
./setup.sh
./start.sh
```

`setup.sh` creates `.venv`, installs all runtime dependencies and opens the first-run configuration wizard if `config.json` does not exist. Configure the primary Telegram API credentials and bot in the wizard. Do not copy credentials into the repository.

For later interactive starts:

```bash
git pull --ff-only
./setup.sh       # safe to rerun; updates dependencies and preserves config/data
./start.sh
```

To install as a systemd service after first-run configuration:

```bash
sudo ./scripts/install_systemd.sh
sudo journalctl -u circles-archiver -f
```

The service runs as the invoking user (or `SUDO_USER`), from the repository directory. Keep `config.json`, `.session` files and runtime data on persistent storage.

## Runtime files

- `combined_multi_account.py` — supervisor, account manager, archiver and broadcast lifecycle.
- `broadcast_core.py` — durable per-chat state and action queue.
- `telegram_manager_web.py` — Web Manager API. It receives already-connected Telethon clients from CIRCLES CONTROL and does not open `.session` files or create Telegram clients.
- `smart_cleaner.py` — classification, safe preview and leave-operation workflow.
- `telegram_manager_panel.html` — frontend for `telegram_manager_web.py`.
- `config.json`, `accounts/`, `data/`, `manager_data/` — generated locally and excluded from Git.

## Safety and data ownership

The repository intentionally contains no API credentials, bot tokens, Telegram session files, user database, broadcast queue, or runtime catalog. `.gitignore` excludes these. Never force-add them.

Each account has its own configured `data_dir` and session. Smart Manager receives the already-connected client object from `run_service` / `AccountManager`; it does not create another client against the same SQLite session.

Starting a secondary account does not silently broaden broadcast recipients or enable auto-subscribe. User policy remains as saved in that profile's configuration.

A saved campaign is not permission to start sending automatically. On process startup it resumes only when `broadcast_autostart=true`. When the flag is false, the durable campaign checkpoint is retained but remains paused until an explicit manual start (or explicit `--broadcast` command).

Shutdown stops and awaits the broadcast worker before closing its SQLite stores. The same order applies to the profile workers: background work is joined before the Telegram client is disconnected.

The Smart Manager binds to `127.0.0.1:8765` by default. Access it through an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 USER@YOUR_VPS
```

Open `http://127.0.0.1:8765`. The access token is generated locally and stored under `manager_data/`; never publish it. Do not expose the manager directly to the public Internet.

## Commands

```bash
./setup.sh
./start.sh                    # normal supervisor mode
.venv/bin/python combined_multi_account.py setup
.venv/bin/python combined_multi_account.py accounts
.venv/bin/python combined_multi_account.py add-account work
.venv/bin/python combined_multi_account.py login --profile work
.venv/bin/python combined_multi_account.py run --broadcast --interval 60 --hours 4
```

The `--broadcast` flag is an explicit operator request to start a campaign. It does not alter `broadcast_autostart` in the stored configuration.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m compileall -q combined_multi_account.py broadcast_core.py smart_cleaner.py telegram_manager_web.py
.venv/bin/python -m pytest -q
```

CI runs compilation and the unit/regression suite on Python 3.12. Tests use mocks and temporary databases; they do not constitute a live Telegram acceptance test. Actual account login, Telegram API permissions, FloodWait behavior, live `systemd` startup and broadcast behavior must still be verified on the target VPS.