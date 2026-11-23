# CIRCLES CONTROL — Changelog

## 3.9.13 — Restore Panel HTML Document Structure

- Fixed the missing closing style/head/body tags after the responsive top-bar CSS so the browser parses and renders the entire application again.
- Added a regression test that verifies the document has a closed style block and a body before the application root.


## 3.9.12 — Safer Session UI & Broadcast Validation

- Stop echoing the stored access token into the password field after login; show an access-ready state and expose the field only when the user chooses to change the key.
- Restructured the top bar for desktop, tablet, and mobile widths so account controls wrap without colliding with the page title.
- Avoid re-saving an unchanged recipient list during launch and reuse the fresh Telegram dialog snapshot for the server-side final validation instead of forcing a second full scan.
- Gave Telegram dialog validation requests a longer, explicit timeout and report the method/path; a timeout is no longer presented as proof that launch failed.
- Added regression tests for redundant dialog scans, token-field privacy, and contextual timeout feedback.


## 3.9.11 — Archive Loading & Selection Feedback

- Added persistent loading, success, and error feedback to archive catalogue and person-media navigation, with pagination controls disabled while requests are active.
- Guarded archive media responses against profile, selected-person, and page changes so late responses cannot display one person’s records under another person.
- Reset archive selection when switching Telegram profiles and show an explicit cancellation message before any media cleanup is performed.


## 3.9.10 — Account Action Feedback

- Added in-progress button states and explicit server-response feedback for account start/stop, profile refresh/creation, and Telegram login steps.
- Separated successful account/login actions from subsequent status-refresh failures so the UI does not report an accepted action as failed.
- Kept the recipient-panel collapse and action-feedback improvements from 3.9.9.


## 3.9.9 — Clearer Actions & Collapsible Recipient List

- Added a per-profile collapsible recipient panel with a chevron control; collapsed state persists across reloads and profile switches.
- Kept the account identity, selected-recipient count, and action status visible while the recipient list is collapsed.
- Added persistent inline progress/success/error feedback for recipient sync, select-all, save, campaign settings, and stop actions.
- Added clear cancellation feedback and visible running states for long Smart Cleaner actions.
- Corrected stop-button state refresh so it follows the actual server campaign status after a stop request.
- Added regression coverage for recipient panel collapse persistence and action feedback.


## 3.9.8 — Account-Scoped Broadcast Workspace & Feedback

- Persisted the selected Telegram profile across page reloads so the panel does not silently switch back to the first account.
- Added explicit active-profile and Telegram account identity labels to the recipient workspace and broadcast status.
- Kept recipient discovery and validation scoped to the selected profile; cross-profile chat IDs are rejected.
- Added visible pending/error/cancel states to the launch button, including clear feedback for an incorrect confirmation.
- Reused a profile-local dialog scan for up to 45 seconds after recipient validation to avoid scanning the same account twice during one launch flow.


## 3.9.7 — Scan, Archive Payload & Runtime Monitoring

- Added compact catalog and archive responses for the panel while preserving full legacy API payloads by default.
- Reused unchanged Telegram descriptions and recent-message samples for up to seven days during repeat scans to avoid duplicate enrichment requests.
- Exposed live scan counters and elapsed time so long scans show visible progress in the panel.
- Added host CPU/RAM, process RSS, SQLite database/WAL sizes, and rolling 60-second API latency (average/p95/error count) monitoring.


## 3.9.6 — Smart Cleaner Catalog Performance

- Added sort/filter indexes for the Smart Cleaner catalog and query-plan regression tests.
- Moved catalog list and summary reads to isolated read-only SQLite connections and worker threads so catalog queries do not block Telegram/web event processing.


## 3.9.5 — Non-blocking Archive Queries

- Moved archive summary, people catalog, and media-page SQLite reads into worker threads so large databases do not block the aiohttp event loop.
- Added a regression test confirming archive summary work runs outside the main event-loop thread.


## 3.9.4 — Server & Archive Performance

- Added a descending composite index for archive media pagination by user/date/message.
- Added compact logs API responses for the web panel, avoiding duplicate raw log payloads while preserving the legacy response by default.
- Synchronized the app version label and runtime version metadata.


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