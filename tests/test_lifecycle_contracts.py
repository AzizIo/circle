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
    assert "для продолжения используй ручной запуск" in body
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