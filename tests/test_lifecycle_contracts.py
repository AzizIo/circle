"""Regression guards for service lifecycle and shared-client invariants."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "combined_multi_account.py").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    a = source.index(start)
    return source[a:source.index(end, a)]


def test_saved_broadcast_resumes_only_when_autostart_is_enabled():
    body = _block(SOURCE, "    if allow_broadcast:\n        # A persisted active campaign", "    async def _health_loop():")
    assert "if autostart_enabled:\n            resumed = bc.resume()" in body
    assert "elif bc.state.get(\"active\")" in body
    assert "требуется явный запуск кампании из панели" in body
    assert "if broadcast is not None:\n                bc.start(*broadcast)" in body


def test_broadcast_sqlite_stores_close_after_worker_finishes():
    body = _block(SOURCE, "    async def close(self):\n        \"\"\"Finish/stop background work", "    # ----- отправка -----")
    wait_pos = body.index("await asyncio.wait_for(asyncio.shield(broadcast_task)")
    skip_pos = body.index("self.skip.close()")
    queue_pos = body.index("self.autosub_queue.close()")
    assert wait_pos < skip_pos < queue_pos


def test_starting_secondary_profile_preserves_broadcast_policy():
    body = _block(SOURCE, "    async def start_profile(self, name: str)", "    async def _run_profile(self, name: str")
    assert "save_cfg(cfg)" not in body
    assert "broadcast_allowlist_only\"] = False" not in body
    assert "broadcast_auto_subscribe\"] = True" not in body


def test_smart_cleaner_web_runtime_returns_registered_live_client():
    web_source = (ROOT / "telegram_manager_web.py").read_text(encoding="utf-8")
    body = _block(web_source, "    def _get_profile(self, name: str)", "    def _token(self)")
    assert "return self.clients[name], self.cleaners[name]" in body
    assert "TelegramClient(" not in web_source


def test_run_service_does_not_migrate_broadcast_policy_on_startup():
    body = _block(SOURCE, "async def run_service(", "    notifier = Notifier()")
    assert "broadcast_allowlist_only\"] = False" not in body
    assert "broadcast_auto_subscribe\"] = True" not in body
    assert "save_cfg(cfg)" not in body

def test_autosub_worker_is_campaign_scoped_and_queue_capture_is_durable():
    init = _block(SOURCE, "        # Link capture is session-level", "    def _apply_delays")
    assert "self._ensure_autosub_worker()" not in init
    assert "self.running = True" in _block(SOURCE, "    def _launch(self):", "    def start(self")
    launch = _block(SOURCE, "    def _launch(self):", "    def start(self")
    assert "self._ensure_autosub_worker()" in launch
    mention = _block(SOURCE, "    async def _on_mention(self, event):", "    async def close(self):")
    assert "self.autosub_queue.enqueue_many(accepted, chat_id, message_id)" in mention
    assert "if self.running and self.state.get(\"active\"):" in mention
    stop = _block(SOURCE, "    def stop(self):", "    def suspend(self):")
    assert "worker.cancel()" in stop
    suspend = _block(SOURCE, "    def suspend(self):", "    def set_interval(self")
    assert "worker.cancel()" in suspend
    finally_block = _block(SOURCE, "        finally:\n            await self.save_subs(force=True)", "    # ----- управление -----")
    assert "worker.cancel()" in finally_block
    assert "await asyncio.gather(worker, return_exceptions=True)" in finally_block


def test_telegram_bot_is_not_started_by_web_managed_runtime():
    body = _block(SOURCE, "    # Web panel is the only operator control plane.", "    if target_err:")
    assert "await bot.start" not in body
    assert "bot_token" not in body


def test_independent_web_host_does_not_hold_telegram_session():
    body = _block(SOURCE, "async def run_web_manager_host(", "async def run_all_accounts(")
    assert "WEB_MANAGER_RUNTIME.start()" in body
    assert "await stop_event.wait()" in body
    assert "await manager.stop_all()" in body
    assert "make_client(" not in body
    main = _block(SOURCE, "def main():", 'if __name__ == "__main__":')
    assert '"web"' in main
    assert 'if cmd == "web":' in main
    assert "runtime_lock_dir = BASE / \"manager_data\"" in main


def test_primary_account_is_web_managed_and_can_be_stopped():
    body = _block(SOURCE, "class AccountManager:", "#  Бот-панель")
    assert "def _profile_config_path(name: str)" in body
    assert "return DEFAULT_CONFIG_PATH" in body
    start = _block(body, "    async def start_profile(", "    async def _run_profile(")
    assert "self._profile_config_path(name)" in start
    assert 'if name == "default"' not in start
    stop = _block(body, "    async def stop_profile(", "    async def start_all(")
    assert 'if name == "default"' not in stop
    worker = _block(body, "    async def _run_profile(", "    async def stop_profile(")
    assert "allow_broadcast_autostart=False" in worker


def test_web_api_exposes_primary_profile_start_stop():
    web_source = (ROOT / "telegram_manager_web.py").read_text(encoding="utf-8")
    assert '"can_start": bool(self.account_manager and not running and not connected' in web_source
    assert '"can_stop": bool(self.account_manager and running)' in web_source


def test_systemd_and_start_script_launch_web_host_only():
    installer = (ROOT / "scripts/install_systemd.sh").read_text(encoding="utf-8")
    starter = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "combined_multi_account.py web" in installer
    assert "Restart=on-failure" in installer
    assert 'combined_multi_account.py" web "$@"' in starter


def test_web_managed_profile_does_not_auto_resume_broadcast():
    body = _block(SOURCE, "async def run_service(", "    async def _health_loop():")
    assert "allow_broadcast_autostart=True" in body
    assert "allow_broadcast_autostart=False" in _block(
        SOURCE, "    async def _run_profile(", "    async def stop_profile(")
