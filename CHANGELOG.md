# CIRCLES CONTROL — Changelog

## 3.9.3 — Workspace UX & Responsiveness

- Added client-side GET request coalescing and a 30-second request timeout with readable errors.
- Reduced background refresh frequency and prevented list refreshes while users are typing or have unsaved selections.
- Parallelized dashboard and archive summary requests.
- Added bounded waits for long-running chat scans and cleaner jobs.
- Added responsive navigation, clearer focus states, reduced-motion support, and subtle workspace transitions.
- Kept the campaign sending handler unchanged.



## 3.9.2 — One-click confirmations for routine actions

- Replaced typed phrases with clear Yes/Cancel-style confirmation buttons for single chat leave, bulk leave after preview, campaign stop, and media cleanup.
- Kept the typed `START N` confirmation for campaign launch as an explicit safeguard.
- Preserved server-side preview IDs and expected confirmation checks for cleanup and leave operations; the UI supplies the server-generated token only after the user clicks the confirmation button.
- Kept the destructive actions behind a separate explicit confirmation modal.

## 3.9.1 — Group-only Broadcast Recipients

- Restricted broadcast destinations to basic groups, supergroups and gigagroups; ordinary broadcast channels are excluded even when the account is an administrator.
- Kept channel membership tracking intact for the separate auto-subscribe workflow.
- Automatically removed old broadcast-channel IDs and stale destinations from saved recipient allowlists.
- Updated the recipient UI labels and added regression coverage for group/channel classification.

## 3.9.0 — Broadcast Workspace & Media Care

- Added one-click selection of all available groups in the active Telegram profile; blocked and explicitly excluded destinations are skipped.
- Removed successfully left chats from the Cleaner catalog and the saved broadcast recipient allowlist.
- Rebuilt the logs page as a live event feed with severity counters, search and level filters, plus automatic refresh.
- Added a two-step media-folder cleanup workflow: preview, then typed confirmation; files referenced by `circles.db` and files newer than 24 hours are protected.
- Added page-aware background refresh and disabled browser caching for the panel shell to reduce stale UI and manual refreshes.
- Updated the application version to 3.9.0.


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