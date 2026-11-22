# Changelog

## 3.7.2
- Gate persisted broadcast resume behind `broadcast_autostart`.
- Preserve saved campaign checkpoints while autostart is disabled.
- Stop rewriting broadcast allowlists and auto-subscribe preferences at service/profile startup.
- Await the broadcast worker before closing SQLite stores.
- Add clean-install scripts, systemd installer, requirements, CI and regression tests.

## 3.7.1
- Smart Cleaner and authenticated local Telegram Manager integration.