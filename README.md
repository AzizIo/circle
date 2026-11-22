# CIRCLES CONTROL 3.7.2

Clean-install repository for CIRCLES CONTROL: Telegram multi-account archiver, broadcast manager, Smart Cleaner and local Telegram Manager.

## Quick start

```bash
git clone https://github.com/AzizIo/circle.git
cd circle
./setup.sh
./start.sh
```

For installation and safe migration of an existing VPS with already-authorized sessions, read [README_RU.md](README_RU.md).

## Repository layout

The application source files, web panel, requirements, setup/start scripts, systemd installer, regression tests and GitHub Actions workflow are committed directly to this repository. The setup script installs dependencies into a local `.venv`; it does not download or unpack another private source bundle.

## Local-only data

Do not commit `config.json`, Telegram `.session` files, account directories, SQLite databases, broadcast checkpoints, queue files, logs, or web manager access tokens. The `.gitignore` excludes these local runtime files.
