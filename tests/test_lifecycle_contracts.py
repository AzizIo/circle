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
    init = _block(SOURCE, "        # Both link capture and queue execution", "    def _apply_delays")
    assert "self._ensure_autosub_worker()" not in init
    assert "client.add_event_handler" not in init
    assert "self.running = True" in _block(SOURCE, "    def _launch(self):", "    def start(self")
    launch = _block(SOURCE, "    def _launch(self):", "    def start(self")
    assert "self._ensure_autosub_worker()" in launch
    mention = _block(SOURCE, "    async def _on_mention(self, event):", "    async def close(self):")
    assert "self.autosub_queue.enqueue_many(accepted, chat_id, message_id)" in mention
    assert "if self.running and self.state.get(\"active\"):" in mention
    stop = _block(SOURCE, "    def stop(self):", "    def suspend(self):")
    assert "self._stop_autosub_worker()" in stop
    suspend = _block(SOURCE, "    def suspend(self):", "    def set_interval(self")
    assert "self._stop_autosub_worker()" in suspend
    stop_worker = _block(SOURCE, "    def _stop_autosub_worker(", "    def _ensure_autosub_worker")
    assert "self._detach_autosub_listener()" in stop_worker
    assert "worker.cancel()" in stop_worker
    finally_block = _block(SOURCE, "        finally:\n            await self.save_subs(force=True)", "    # ----- управление -----")
    assert "self._stop_autosub_worker()" in finally_block
    assert "await asyncio.gather(worker, return_exceptions=True)" in finally_block


def test_telegram_bot_is_not_started_by_web_managed_runtime():
    body = _block(SOURCE, "    # Web panel is the only operator control plane.", "    if target_err:")
    assert "await bot.start" not in body
    assert "TelegramClient(" not in body


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


def test_autosub_listener_attaches_only_for_active_campaign():
    attach = _block(SOURCE, "    def _attach_autosub_listener(", "    def _detach_autosub_listener")
    assert "self.client.add_event_handler(self._on_mention, self._autosub_new_message_event)" in attach
    detach = _block(SOURCE, "    def _detach_autosub_listener(", "    def _stop_autosub_worker")
    assert "self.client.remove_event_handler" in detach
    ensure = _block(SOURCE, "    def _ensure_autosub_worker(", "    async def _autosub_worker_loop")
    assert "not self.running or not self.state.get(\"active\")" in ensure
    assert "self.stop_event.is_set()" in ensure
    assert "self._attach_autosub_listener()" in ensure
    worker = _block(SOURCE, "    async def _autosub_worker_loop(self):", "    async def _on_mention(self, event):")
    assert "self.stop_event.is_set()" in worker
    mention = _block(SOURCE, "    async def _on_mention(self, event):", "    async def close(self):")
    assert "not self.running or not self.state.get(\"active\")" in mention
    assert "self.stop_event.is_set()" in mention
    web_source = (ROOT / "telegram_manager_web.py").read_text(encoding="utf-8")
    settings = _block(web_source, "    async def settings(self, request):", "    def _archive_connection")
    assert "bc._stop_autosub_worker()" in settings


def test_archive_target_is_optional_for_web_managed_accounts():
    helper = _block(SOURCE, "def account_cfg_problems(cfg: dict)", "def setup_logging(")
    assert 'problem.startswith("ID группы-форума")' in helper
    manager = _block(SOURCE, "class AccountManager:", "#  Бот-панель")
    start = _block(manager, "    async def start_profile(", "    async def _run_profile(")
    login = _block(manager, "    async def begin_login(", "    async def submit_code(")
    assert "account_cfg_problems(cfg)" in start
    assert "account_cfg_problems(cfg)" in login
    host = _block(SOURCE, "async def run_web_manager_host(", "async def run_all_accounts(")
    assert "account_cfg_problems(cfg)" not in host


def test_new_account_profile_gets_its_own_config_and_data_directory(tmp_path):
    import json
    from unittest.mock import patch
    import combined_multi_account as core

    default_config = tmp_path / "config.json"
    accounts_dir = tmp_path / "accounts"
    default_config.write_text(json.dumps({
        "api_id": 12345,
        "api_hash": "test-api-hash",
        "owner_id": 777,
        "account_label": "Main",
        "data_dir": "data",
        "target_chat_id": -1001234567890,
        "broadcast_allowlist_only": True,
        "broadcast_allowlist": [-100111],
        "session_name": "circles",
    }), encoding="utf-8")

    with (
        patch.object(core, "BASE", tmp_path),
        patch.object(core, "DEFAULT_CONFIG_PATH", default_config),
        patch.object(core, "ACCOUNTS_DIR", accounts_dir),
        patch.object(core, "CONFIG_PATH", default_config),
    ):
        manager = core.AccountManager(owner_id=777)
        created_path = manager.add_profile("backup", "Резервный аккаунт")
        cfg = json.loads(created_path.read_text(encoding="utf-8"))

    assert created_path == accounts_dir / "backup" / "config.json"
    assert cfg["account_label"] == "Резервный аккаунт"
    assert cfg["owner_id"] == 0
    assert cfg["api_id"] == 12345
    assert cfg["api_hash"] == "test-api-hash"
    assert cfg["session_name"] == "circles"
    assert cfg["data_dir"] == "accounts/backup/data"
    assert cfg["broadcast_allowlist_only"] is False
    assert cfg["broadcast_allowlist"] == []
    assert Path(tmp_path / cfg["data_dir"]) != tmp_path / "data"
    assert not (tmp_path / "data" / "circles.session").exists()


def test_new_account_profile_refuses_to_reuse_an_orphan_session_directory(tmp_path):
    from unittest.mock import patch
    import combined_multi_account as core

    default_config = tmp_path / "config.json"
    default_config.write_text('{"api_id": 1, "api_hash": "hash", "data_dir": "data"}',
                              encoding="utf-8")
    accounts_dir = tmp_path / "accounts"
    orphan = accounts_dir / "backup" / "data"
    orphan.mkdir(parents=True)
    (orphan / "circles.session").write_bytes(b"do-not-reuse")

    with (
        patch.object(core, "BASE", tmp_path),
        patch.object(core, "DEFAULT_CONFIG_PATH", default_config),
        patch.object(core, "ACCOUNTS_DIR", accounts_dir),
        patch.object(core, "CONFIG_PATH", default_config),
    ):
        manager = core.AccountManager(owner_id=777)
        try:
            manager.add_profile("backup", "Backup")
        except FileExistsError as exc:
            assert "уже существует" in str(exc) or "содержит данные" in str(exc)
        else:
            raise AssertionError("Orphan session directory must not be reused by a new account")
    assert (orphan / "circles.session").read_bytes() == b"do-not-reuse"


def test_additional_account_creation_requires_configured_primary_api_credentials(tmp_path):
    from unittest.mock import patch
    import combined_multi_account as core

    default_config = tmp_path / "config.json"
    default_config.write_text('{"api_id": 0, "api_hash": "", "data_dir": "data"}', encoding="utf-8")
    accounts_dir = tmp_path / "accounts"
    with (
        patch.object(core, "BASE", tmp_path),
        patch.object(core, "DEFAULT_CONFIG_PATH", default_config),
        patch.object(core, "ACCOUNTS_DIR", accounts_dir),
        patch.object(core, "CONFIG_PATH", default_config),
    ):
        try:
            core.add_account_profile("work", "Рабочий аккаунт")
        except ValueError as exc:
            assert "API ID" in str(exc) or "API HASH" in str(exc)
        else:
            raise AssertionError("An account without shared Telegram API credentials must not be created")
    assert not (accounts_dir / "work" / "config.json").exists()


def test_profile_login_disconnects_client_when_profile_config_cannot_be_saved():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch
    import combined_multi_account as core

    class FakeClient:
        connected = True
        disconnected = False
        async def get_me(self):
            return SimpleNamespace(id=321, first_name="New", last_name="Account",
                                   username="new_account", deleted=False)
        async def disconnect(self):
            self.connected = False
            self.disconnected = True

    async def run():
        manager = core.AccountManager(owner_id=111)
        fake = FakeClient()
        manager.auth_flows["work"] = {"client": fake}
        with (
            patch.object(core, "read_profile_config", return_value={"_config_path": "/tmp/work/config.json"}),
            patch.object(core, "save_cfg", side_effect=OSError("disk full")),
        ):
            stage, message = await manager._finish_login("work")
        assert stage == "error"
        assert "не удалось сохранить профиль" in message
        assert fake.disconnected
        assert "work" not in manager.auth_flows

    asyncio.run(run())



def test_broadcast_uncertain_delivery_is_not_completed_or_retried_blindly():
    body = _block(
        SOURCE,
        "    async def send_wave(self, msg, chats, pending: dict | None = None) -> dict:",
        "    def _record_wave(self, r, seconds, partial=False, partial_reason=\"\"):",
    )
    uncertain = body.index('if reason == "uncertain":')
    definite_rejection = body.index("# Definite Telegram rejections", uncertain)
    branch = body[uncertain:definite_rejection]
    save = branch.index("self._save_pending_progress(pending, r, completed)")
    abort = branch.index('self._fatal_reason = "uncertain_delivery"')
    assert save < abort
    assert "completed.add(chat_id)" not in branch
    assert 'pending["uncertain_deliveries"] = unresolved[-100:]' in branch


def test_broadcast_only_counts_confirmed_telegram_message_ids_as_success():
    forward = _block(
        SOURCE,
        "    async def forward(self, msg, chat):",
        "    @staticmethod\n    def _pending_result",
    )
    assert 'getattr(item, "id", None)' in forward
    assert 'return False, "uncertain"' in forward
    wave = _block(
        SOURCE,
        "    async def send_wave(self, msg, chats, pending: dict | None = None) -> dict:",
        "    def _record_wave(self, r, seconds, partial=False, partial_reason=\"\"):",
    )
    assert 'r["ok"] += 1' in wave
    assert 'r["errors"][reason] += 1' in wave



def test_uncertain_delivery_stops_as_a_distinct_recoverable_reason():
    body = _block(
        SOURCE,
        "                    if self._fatal:",
        '                    partial = has_remaining',
    )
    assert 'elif self._fatal_reason == "uncertain_delivery":' in body
    assert 'reason = "uncertain_delivery"' in body
    uncertain_branch = body.split('elif self._fatal_reason == "uncertain_delivery":', 1)[1].split("else:", 1)[0]
    assert 'st["last_error"] = "uncertain_delivery:' in uncertain_branch
    assert 'await self.alert("account-restricted"' not in uncertain_branch
    assert 'reason = "account_restricted"' not in uncertain_branch
    assert '"uncertain_delivery"' in _block(
        SOURCE,
        "    def terminal_reason_message(reason: str) -> str:",
        "    def summary(self) -> str:",
    )


def test_broadcaster_resume_is_blocked_until_uncertain_delivery_is_reviewed():
    body = _block(SOURCE, "    def resume(self) -> bool:", "    def stop(self):")
    assert 'pending.get("uncertain_deliveries", [])' in body
    assert "if isinstance(unresolved, list) and unresolved:" in body
    assert "self._launch()" in body


def test_resolve_uncertain_delivery_requires_owner_decision_and_updates_checkpoint():
    from types import MethodType
    import combined_multi_account as core

    bc = object.__new__(core.Broadcaster)
    pending = {
        "chat_ids": [101, 202],
        "completed_ids": [101],
        "ok": 1,
        "fail": 1,
        "errors": {"uncertain": 1},
        "failed": [("Test group", "uncertain")],
        "uncertain_deliveries": [{"chat_id": 202, "title": "Test group", "detected_at": 1}],
    }
    bc.state = {"active": True, "end": 9999999999, "pending_wave": pending, "last_error": "uncertain"}
    saved = []
    def save_progress(self, current, result, completed):
        current["completed_ids"] = sorted(completed)
        current["ok"] = result["ok"]
        current["fail"] = result["fail"]
        current["errors"] = dict(result["errors"])
        current["failed"] = result["failed"]
        self.state["pending_wave"] = current
        saved.append(True)
    bc._save_pending_progress = MethodType(save_progress, bc)

    result = bc.resolve_uncertain_delivery(202, "delivered")
    assert result["action"] == "delivered"
    assert result["remaining_uncertain"] == 0
    assert pending["completed_ids"] == [101, 202]
    assert pending["ok"] == 2
    assert pending["fail"] == 0
    assert pending["errors"] == {}
    assert pending["uncertain_deliveries"] == []
    assert bc.state["last_error"] == ""
    assert bc.state["next"] == 0
    assert saved

    retry_bc = object.__new__(core.Broadcaster)
    retry_pending = {
        "chat_ids": [303],
        "completed_ids": [],
        "ok": 0,
        "fail": 1,
        "errors": {"uncertain": 1},
        "failed": [("Retry group", "uncertain")],
        "uncertain_deliveries": [{"chat_id": 303, "title": "Retry group", "detected_at": 1}],
    }
    retry_bc.state = {"active": True, "end": 9999999999, "pending_wave": retry_pending,
                      "last_error": "uncertain_delivery"}
    def save_retry_progress(self, current, result, completed):
        current["completed_ids"] = sorted(completed)
        current["ok"] = result["ok"]
        current["fail"] = result["fail"]
        current["errors"] = dict(result["errors"])
        current["failed"] = result["failed"]
        self.state["pending_wave"] = current
    retry_bc._save_pending_progress = MethodType(save_retry_progress, retry_bc)
    retry_result = retry_bc.resolve_uncertain_delivery(303, "retry")
    assert retry_result["action"] == "retry"
    assert retry_result["remaining_uncertain"] == 0
    assert retry_pending["completed_ids"] == []
    assert retry_pending["ok"] == 0
    assert retry_pending["fail"] == 0
    assert retry_pending["errors"] == {}
    assert retry_bc.state["last_error"] == ""

    try:
        bc.resolve_uncertain_delivery(202, "retry")
    except ValueError as exc:
        assert "нет отправки" in str(exc).lower()
    else:
        raise AssertionError("A resolved destination must not be resolvable twice")


def test_web_broadcast_status_exposes_uncertain_destinations_and_gates_resume():
    source = (ROOT / "telegram_manager_web.py").read_text(encoding="utf-8")
    body = _block(source, "    def _broadcast_view(", "    async def dashboard(")
    assert '"uncertain_deliveries": uncertain_deliveries' in body
    assert "not uncertain_deliveries" in body
    start = _block(source, "    async def start_broadcast(", "    async def resolve_broadcast_uncertain(")
    assert 'pending.get("uncertain_deliveries", [])' in start


def test_broadcast_panel_has_explicit_uncertain_delivery_review_controls():
    html = (ROOT / "telegram_manager_panel.html").read_text(encoding="utf-8")
    assert 'id="bcUncertainPanel"' in html
    assert 'data-uncertain-action="delivered"' in html
    assert 'data-uncertain-action="retry"' in html
    assert 'url("/broadcast/uncertain")' in html


def test_web_broadcast_status_treats_finalizing_task_as_busy():
    source = (ROOT / "telegram_manager_web.py").read_text(encoding="utf-8")
    body = _block(source, "    def _broadcast_view(", "    async def dashboard(")
    assert 'task = getattr(bc, "task", None)' in body
    assert 'task is not None and not task.done()' in body
    assert '"running": worker_busy' in body
    start = _block(source, "    async def start_broadcast(", "    async def resolve_broadcast_uncertain(")
    assert 'task is not None and not task.done()' in start
