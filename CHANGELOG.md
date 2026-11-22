# CIRCLES CONTROL — Changelog

## 3.8.0 — Web Control Center

- Rebuilt the local web UI as a unified workspace: overview, chats/Smart Cleaner, broadcast studio, account manager, archiver, settings, and logs.
- Added authenticated APIs for campaign status/start/stop, Saved Messages post selection, explicit recipient selection, profile creation and login, configuration editing, disk status, and log tails.
- Web-initiated broadcasts require an explicit recipient allowlist and a typed confirmation. Existing checkpoint queues are checked against the current selection before resume.
- Added pagination-safe chat and recipient lists; recipient selections persist across pages.
- Kept the web service bound to loopback by default; use an SSH tunnel.

# Changelog

## 3.7.2
- Gate persisted broadcast resume behind `broadcast_autostart`.
- Preserve saved campaign checkpoints while autostart is disabled.
- Stop rewriting broadcast allowlists and auto-subscribe preferences at service/profile startup.
- Await the broadcast worker before closing SQLite stores.
- Add clean-install scripts, systemd installer, requirements, CI and regression tests.

## 3.7.1
- Smart Cleaner and authenticated local Telegram Manager integration.