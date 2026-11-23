## 3.9.26 — Broadcast Delivery Confirmation & Recovery

- Count a broadcast as successful only after Telegram returns a created message with an ID.
- Log the destination chat ID, source message ID and Telegram-created message ID for each confirmed forward.
- Preserve ambiguous network outcomes separately from definite Telegram rejections; never report them as successful or replay them automatically.
- Stop and checkpoint a wave when an acknowledgement is lost, including across VPS restarts.
- Add an explicit owner review in the web panel: mark as delivered after checking the chat, or authorize a retry only after confirming the message is absent.
- Prevent review and campaign-resume races while the broadcast worker is still finalizing.
- Add regression coverage for checkpoint, owner decision, restart gate and panel feedback.

## 3.9.25 — Account Login and Profile Validation Hardening

- Validate the primary Telegram API ID / API Hash before creating an additional profile, so an unusable profile is not created from an unconfigured base setup.
- Give actionable Telegram login messages for invalid/banned numbers, invalid API credentials and FloodWait delays.
- Validate the profile and session file before opening a temporary login client; close clients and clear auth state safely when profile persistence fails.
- Preserve the selected authorization profile across page reloads and profile refreshes.
- Extend browser timeouts for Telegram phone-code and 2FA operations to 120 seconds, and refresh account state when Telegram reports an already-authorized session.
- Add end-to-end regression coverage for create → phone code → profile config persistence, verifying that the primary account configuration remains unchanged.

## 3.9.24 — Reliable Web Account Onboarding

- Allow human-readable Russian/Ukrainian account labels; create a safe ASCII profile directory name automatically.
- Add a per-profile “Authorize” shortcut and make the create-profile flow continue directly to phone authorization.
- Preserve the selected authorization profile across background profile-list refreshes.
- Return consistent, actionable JSON errors for profile creation, phone-code and 2FA steps.
- Keep each new Telegram session and data directory isolated; refuse to reuse orphan profile folders that already contain session data.
- Add regression coverage for profile creation, phone → code → 2FA API stages, UI selection persistence, and storage isolation.

## 3.9.23 — Safe Broadcast Checkpoint Recovery

- Made recipient-list reads side-effect free: periodic UI polling no longer rewrites or prunes the saved allowlist when Telegram returns an incomplete dialog snapshot.
- Separated the immutable recipient snapshot of an unfinished wave from the editable list used for future waves.
- Added an explicit, confirmed recovery action that resumes the saved post and remaining chat IDs only; already-completed chats are not replayed, and an old checkpoint cannot be silently replaced with a new campaign.
- Updated the panel to explain the difference between the saved wave and the current recipient selection.

## 3.9.22 — Independent Web Control Plane

- Added a standalone `web` host that runs the authenticated local web panel without opening a Telegram session or starting an account worker.
- Moved the primary `default` profile into the same managed start/stop lifecycle as secondary accounts; the panel stays online when every profile is stopped.
- Made profile startup from the web panel manual-only: saved campaigns do not auto-resume when a managed profile starts.
- Kept link capture and durable SQLite queueing available to connected profiles, while executing queued auto-subscribe actions only during an active broadcast campaign.
- Removed the legacy Telegram bot control runtime and bot-based dead-session notification.
- Updated `start.sh` and the systemd installer to start the web host; added lifecycle regression tests.

# CIRCLES CONTROL — Changelog

## 3.9.21 — Primary Account & Pending Queue Diagnostics

- Explicitly label profile `default` as the primary account in the Broadcast panel.
- Expose how many IDs in a pending checkpoint are absent from the current recipient allowlist, split between already completed and still pending IDs.
- Replace the generic pending-queue mismatch response with a count-based explanation while leaving the saved queue and recipient guard intact.
- Added regression tests for primary-account identity and pending recipient mismatch diagnostics.


## 3.9.20 — Campaign Button State

- Disable the launch button while the selected campaign is actively running; show its actual running state instead of allowing a duplicate start request.
- Show “Продолжить” when a persisted campaign is active but its worker is not running and needs recovery.
- Return HTTP 409 with an explicit message if a second start request reaches the server while a campaign is already running.
- Added regression tests for duplicate start requests and state-aware launch button behavior.


## 3.9.19 — Async UI Race Protection

- Queue the latest log refresh when filters change during an in-flight request; the final filter selection is loaded automatically instead of being lost.
- Bind long-running chat-scan progress polling to the account that started the scan so a profile switch cannot display another account's scan state.
- Added regression tests for both asynchronous UI race conditions.


## 3.9.18 — Broadcast Start Diagnostics

- Persist readable terminal reasons when a campaign task stops before its first wave or exits due to an account, source-post, destination, or Telegram wait condition.
- Surface the engine's last error and the active-but-not-running recovery state in the Broadcast page instead of showing only “выключена”.
- Added regression tests for the terminal-reason mapping and the panel's launch-state display.


## 3.9.17 — Preserve Multi-Page Recipient Selection

- Allow recipient pagination to continue after local checkbox changes; selected IDs remain in the profile-local set while pages change.
- Refreshing the Telegram dialog list no longer clears the user's unsaved selection; the interface explains that the local choice still needs saving.
- Refined stale-ID regression coverage and reuse of the selected-ID lookup set during recipient page rendering.


## 3.9.16 — Recipient List Integrity

- Validate saved recipient IDs against the selected profile's current dialog snapshot when loading the list, and prune stale IDs from the profile config.
- Saving a selection now retains valid current-profile chat IDs and removes stale or foreign IDs instead of rejecting the entire selection.
- Return the requested, retained, and removed counts so the panel can reconcile its checkboxes and report exactly how many stale IDs were discarded.
- Add regression tests for stale saved IDs, mixed valid/stale selections, all-stale selections, and cross-profile IDs.


## 3.9.15 — Stable Recipient Snapshot & One-Click Start

- Replaced the fragile typed `START <count>` challenge with one explicit launch dialog that displays the account, current recipient count, interval, and duration.
- The launch request now carries the exact recipient IDs shown to the user; the server intersects that snapshot with the current saved allowlist and live dialogs so additional groups cannot be introduced by a changing count.
- When Telegram refresh prunes stale recipients, the server returns the actual selected IDs and count and the panel updates its visible selection to match.
- Added regression tests for one-click confirmation and recipient-count changes during Telegram dialog refresh.


## 3.9.14 — Fast Broadcast Status & Safe Recipient Feedback

- Removed the Telegram Saved Messages preview lookup from the frequently polled broadcast-status endpoint; it now returns local campaign state and cached post details only.
- Cache a post preview after explicit post selection or campaign validation; after a restart, return the selected post ID without pretending its text/media preview was refreshed.
- Make recipient counter rendering null-safe and distinguish a server-side successful save from a subsequent UI rendering problem.
- Added regression tests proving status polling does not call Telegram and recipient saves do not falsely report failure after being accepted.


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