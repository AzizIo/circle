import asyncio
import tempfile
from pathlib import Path
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram_manager_web import TelegramManagerRuntime
from combined_multi_account import Broadcaster


class WebManagerTests(unittest.TestCase):
    def test_api_requires_bearer_token(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = 'unit-test-token'
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get('/health', runtime.health)
                app.router.add_get('/api/profiles', runtime.profiles)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    public = await client.get('/health')
                    self.assertEqual(public.status, 200)
                    denied = await client.get('/api/profiles')
                    self.assertEqual(denied.status, 401)
                    allowed = await client.get('/api/profiles', headers={'Authorization': 'Bearer unit-test-token'})
                    self.assertEqual(allowed.status, 200)
                finally:
                    await client.close()
        asyncio.run(run())

    def test_profiles_and_account_start_stop_routes(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                runtime = TelegramManagerRuntime(base_dir=td, profiles_provider=lambda: [
                    ('default', {'account_label': 'Main'}, Path(td)),
                    ('work', {'account_label': 'Work'}, Path(td)),
                ])
                runtime.token = 'unit-test-token'
                class Manager:
                    def __init__(self): self.running = False
                    def status(self, name): return 'Работает' if self.running else 'Сессия сохранена'
                    def is_running(self, name): return self.running
                    def has_session(self, name): return True
                    async def start_profile(self, name): self.running = True; return True, 'Запускается'
                    async def stop_profile(self, name): self.running = False; return True, 'Остановлен'
                runtime.set_account_manager(Manager())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get('/api/profiles', runtime.profiles)
                app.router.add_post('/api/profiles/{name}/start', runtime.start_profile)
                app.router.add_post('/api/profiles/{name}/stop', runtime.stop_profile)
                client = TestClient(TestServer(app)); await client.start_server()
                headers = {'Authorization': 'Bearer unit-test-token'}
                try:
                    response = await client.get('/api/profiles', headers=headers)
                    data = await response.json()
                    work = next(x for x in data['profiles'] if x['name'] == 'work')
                    self.assertTrue(work['can_start'])
                    started = await client.post('/api/profiles/work/start', headers=headers, json={})
                    self.assertEqual(started.status, 200)
                    stopped = await client.post('/api/profiles/work/stop', headers=headers, json={})
                    self.assertEqual(stopped.status, 200)
                finally:
                    await client.close()
        asyncio.run(run())


    def test_create_account_accepts_russian_label_and_returns_safe_profile_name(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class Manager:
                    def __init__(self):
                        self.created = None
                    def add_profile(self, name, label):
                        self.created = (name, label)
                        if name == "existing":
                            raise FileExistsError("Профиль уже существует")
                        return Path(td) / name / "config.json"

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                manager = Manager()
                runtime.set_account_manager(manager)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/accounts", runtime.create_account)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    response = await client.post("/api/accounts", headers=headers,
                                                 json={"name": "Рабочий аккаунт"})
                    self.assertEqual(response.status, 201)
                    data = await response.json()
                    self.assertEqual(data["name"], "rabochiy-akkaunt")
                    self.assertEqual(data["label"], "Рабочий аккаунт")
                    self.assertEqual(manager.created, ("rabochiy-akkaunt", "Рабочий аккаунт"))
                    self.assertIn("Теперь авторизуй Telegram", data["message"])

                    duplicate = await client.post("/api/accounts", headers=headers,
                                                   json={"name": "Existing"})
                    self.assertEqual(duplicate.status, 409)
                    self.assertIn("уже существует", await duplicate.text())

                    empty = await client.post("/api/accounts", headers=headers, json={"name": "   "})
                    self.assertEqual(empty.status, 400)
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_account_login_routes_cover_phone_code_and_two_factor(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class Manager:
                    async def begin_login(self, name, phone):
                        self.phone_request = (name, phone)
                        return "code", "Код отправлен"
                    async def submit_code(self, name, code):
                        self.code_request = (name, code)
                        return "password", "Нужен пароль 2FA"
                    async def submit_password(self, name, password):
                        self.password_request = (name, password)
                        return "done", "Аккаунт авторизован"

                manager = Manager()
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.set_account_manager(manager)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/login/start", runtime.login_start)
                app.router.add_post("/api/profiles/{name}/login/code", runtime.login_code)
                app.router.add_post("/api/profiles/{name}/login/password", runtime.login_password)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    phone = await client.post("/api/profiles/work/login/start", headers=headers,
                                              json={"phone": "+79991234567"})
                    self.assertEqual(phone.status, 200)
                    self.assertEqual((await phone.json())["stage"], "code")
                    self.assertEqual(manager.phone_request, ("work", "+79991234567"))

                    code = await client.post("/api/profiles/work/login/code", headers=headers,
                                             json={"code": "12345"})
                    self.assertEqual(code.status, 200)
                    self.assertEqual((await code.json())["stage"], "password")
                    self.assertEqual(manager.code_request, ("work", "12345"))

                    password = await client.post("/api/profiles/work/login/password", headers=headers,
                                                 json={"password": "test-2fa"})
                    self.assertEqual(password.status, 200)
                    self.assertEqual((await password.json())["stage"], "done")
                    self.assertEqual(manager.password_request, ("work", "test-2fa"))

                    empty_code = await client.post("/api/profiles/work/login/code", headers=headers,
                                                   json={"code": ""})
                    self.assertEqual(empty_code.status, 400)
                    self.assertIn("Введи код", await empty_code.text())
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_account_panel_exposes_authorize_action_and_preserves_selected_auth_profile(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn('data-authorize="', html)
        self.assertIn('data-authorize]', html)
        self.assertIn('oldAuth=$("authProfile").value', html)
        self.assertIn('auth.value=authProfileExists?oldAuth:', html)
        self.assertIn('$("authProfile").value=result.name', html)
        self.assertIn("Создать и перейти к авторизации", html)
        self.assertIn("Можно писать по-русски", html)
        self.assertIn("Авторизовать", html)

    def test_single_chat_leave_route_executes_one_chat_and_updates_catalog(self):
        async def run():
            import time
            with tempfile.TemporaryDirectory() as td:
                class Chat:
                    def __init__(self, chat_id):
                        self.id = chat_id
                        self.username = ""
                        self.admin_rights = None
                        self.creator = False
                class Dialog:
                    def __init__(self, chat_id):
                        self.id = chat_id
                        self.name = "Temporary test group"
                        self.entity = Chat(chat_id)
                class FakeClient:
                    def __init__(self): self.left = []
                    async def iter_dialogs(self):
                        yield Dialog(12345)
                    async def delete_dialog(self, entity):
                        self.left.append(entity.id)
                client = FakeClient()
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", client, {}, Path(td))
                cleaner = runtime.cleaners["default"]
                now = time.time()
                cleaner.db.execute(
                    "INSERT INTO catalog(chat_id,peer_type,title,category,confidence,scanned_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)", (12345, "group", "Temporary test group", "uncertain", 0.0, now, now))
                cleaner.db.commit()
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post('/api/profiles/{name}/leave', runtime.leave_one)
                app.router.add_get('/api/profiles/{name}/jobs/{job_id}', runtime.job_status)
                app.router.add_get('/api/profiles/{name}/summary', runtime.summary)
                test_client = TestClient(TestServer(app)); await test_client.start_server()
                headers = {'Authorization': 'Bearer unit-test-token'}
                try:
                    denied = await test_client.post('/api/profiles/default/leave', json={'chat_id': 12345})
                    self.assertEqual(denied.status, 401)
                    response = await test_client.post('/api/profiles/default/leave', headers=headers,
                                                      json={'chat_id': 12345})
                    self.assertEqual(response.status, 202)
                    job_id = (await response.json())['job_id']
                    result = None
                    for _ in range(100):
                        status_response = await test_client.get(
                            f'/api/profiles/default/jobs/{job_id}', headers=headers)
                        result = await status_response.json()
                        if result.get('status') != 'running':
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['result']['completed'], 1)
                    self.assertEqual(client.left, [12345])
                    stats_response = await test_client.get('/api/profiles/default/summary', headers=headers)
                    stats = await stats_response.json()
                    self.assertEqual(stats['total'], 0)
                finally:
                    await test_client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_leave_route_rejects_invalid_chat_id(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = 'unit-test-token'
                class FakeClient:
                    async def iter_dialogs(self):
                        if False: yield None
                runtime.register_profile('default', FakeClient(), {}, Path(td))
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post('/api/profiles/{name}/leave', runtime.leave_one)
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.post('/api/profiles/default/leave',
                        headers={'Authorization': 'Bearer unit-test-token'}, json={'chat_id': True})
                    self.assertEqual(response.status, 400)
                    float_response = await client.post('/api/profiles/default/leave',
                        headers={'Authorization': 'Bearer unit-test-token'}, json={'chat_id': 123.4})
                    self.assertEqual(float_response.status, 400)
                finally:
                    await client.close(); await runtime.stop()
        asyncio.run(run())


    def test_web_control_settings_recipients_and_explicit_broadcast(self):
        async def run():
            import json
            import time
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                cfg = {
                    "_config_path": str(config_path), "data_dir": td, "account_label": "Main",
                    "bot_token": "must-not-leak", "api_hash": "must-not-leak",
                    "broadcast_interval_min": 60, "broadcast_hours": 12,
                    "broadcast_delay_min": 0.6, "broadcast_delay_max": 1.2,
                    "broadcast_allowlist_only": False, "broadcast_allowlist": [],
                    "archive_media_types": ["round_video"], "paused": False,
                    "save_outgoing": True, "info_message": True, "keep_local": True,
                }
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                class FakeBroadcaster:
                    def __init__(self):
                        self.running = False
                        self.progress = (0, 0)
                        self.state = {
                            "active": False, "end": 0, "next": 0, "interval": 60,
                            "wave": 0, "run_ok": 0, "run_fail": 0, "life_ok": 0,
                            "life_fail": 0, "life_waves": 0, "last_error": "",
                            "pending_wave": None, "history": [],
                        }
                        self.started = 0
                        self.stopped = 0
                        self.interval_set = None
                        self.duration_set = None
                        self._dialogs_scanned_at = time.time()
                        self.chat_list_calls = []
                    def short(self):
                        return "работает" if self.running else "выключена"
                    async def preview(self):
                        return {"id": 77, "text": "Approved test post", "media": False, "pinned": True}
                    async def saved_posts(self, limit=10):
                        return []
                    async def chat_list(self, refresh=False):
                        self.chat_list_calls.append(bool(refresh))
                        if refresh:
                            self._dialogs_scanned_at = time.time()
                        return [(101, "Approved Group"), (202, "Other Group")]
                    def chat_status(self, chat_id):
                        return "ok"
                    def _apply_delays(self):
                        return None
                    def set_interval(self, value):
                        self.interval_set = value
                    def set_duration(self, value):
                        self.duration_set = value
                    def start(self, interval, hours):
                        self.started += 1
                        self.running = True
                        return True
                    def stop(self):
                        self.stopped += 1
                        self.running = False

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                bc = FakeBroadcaster()
                runtime.register_broadcaster("default", bc)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/settings", runtime.settings)
                app.router.add_patch("/api/profiles/{name}/settings", runtime.settings)
                app.router.add_get("/api/profiles/{name}/broadcast/recipients", runtime.recipients)
                app.router.add_post("/api/profiles/{name}/broadcast/recipients", runtime.set_recipients)
                app.router.add_post("/api/profiles/{name}/broadcast/start", runtime.start_broadcast)
                app.router.add_post("/api/profiles/{name}/broadcast/stop", runtime.stop_broadcast)
                app.router.add_get("/api/profiles/{name}/broadcast", runtime.broadcast_status)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    settings_response = await client.get("/api/profiles/default/settings", headers=headers)
                    self.assertEqual(settings_response.status, 200)
                    settings = await settings_response.json()
                    self.assertNotIn("bot_token", settings)
                    self.assertNotIn("api_hash", settings)

                    saved = await client.patch(
                        "/api/profiles/default/settings", headers=headers,
                        json={"broadcast_interval_min": 90, "broadcast_hours": 4,
                              "broadcast_delay_min": 1.0, "broadcast_delay_max": 2.0,
                              "save_outgoing": False},
                    )
                    self.assertEqual(saved.status, 200)
                    persisted = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["broadcast_interval_min"], 90)
                    self.assertEqual(persisted["broadcast_hours"], 4)
                    self.assertFalse(persisted["save_outgoing"])
                    self.assertEqual(bc.interval_set, 90)
                    # Duration is applied live only to an active campaign; the saved config takes effect on next start.
                    self.assertIsNone(bc.duration_set)

                    selected = await client.post(
                        "/api/profiles/default/broadcast/recipients", headers=headers,
                        json={"chat_ids": [101]},
                    )
                    self.assertEqual(selected.status, 200)
                    listed = await client.get(
                        "/api/profiles/default/broadcast/recipients", headers=headers,
                    )
                    listed_data = await listed.json()
                    self.assertEqual(listed_data["selected_ids"], [101])
                    self.assertTrue(listed_data["allowlist_only"])

                    wrong = await client.post(
                        "/api/profiles/default/broadcast/start", headers=headers,
                        json={"confirmed": False, "recipient_ids": [101]},
                    )
                    self.assertEqual(wrong.status, 400)
                    started = await client.post(
                        "/api/profiles/default/broadcast/start", headers=headers,
                        json={"confirmed": True, "recipient_ids": [101]},
                    )
                    self.assertEqual(started.status, 202)
                    started_data = await started.json()
                    self.assertEqual(started_data["selected_count"], 1)
                    self.assertEqual(started_data["selected_ids"], [101])
                    self.assertEqual(bc.started, 1)
                    self.assertEqual(bc.chat_list_calls, [False, False, False])

                    stopped = await client.post(
                        "/api/profiles/default/broadcast/stop", headers=headers, json={},
                    )
                    self.assertEqual(stopped.status, 202)
                    self.assertEqual(bc.stopped, 1)

                    status = await client.get("/api/profiles/default/broadcast", headers=headers)
                    self.assertEqual(status.status, 200)
                    status_data = await status.json()
                    self.assertEqual(status_data["selected_recipients"], 1)
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())


    def test_select_all_recipients_only_includes_available_groups(self):
        async def run():
            import json
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                cfg = {"_config_path": str(config_path), "data_dir": td,
                       "broadcast_allowlist": [], "broadcast_allowlist_only": False}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    def __init__(self):
                        self.statuses = {101: "ok", 202: "ok", 303: "ban", 404: "ex"}
                    async def chat_list(self, refresh=False):
                        return [(101, "Group"), (202, "Supergroup"),
                                (303, "Blocked group"), (404, "Excluded group")]
                    def chat_status(self, chat_id):
                        return self.statuses[int(chat_id)]
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/recipients", runtime.set_recipients)
                app.router.add_get("/api/profiles/{name}/broadcast/recipients", runtime.recipients)
                client = TestClient(TestServer(app)); await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    response = await client.post("/api/profiles/default/broadcast/recipients",
                                                 headers=headers, json={"select_all": True})
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["selected_ids"], [101, 202])
                    self.assertEqual(data["selected_count"], 2)
                    self.assertEqual(data["available_count"], 2)
                    self.assertEqual(data["excluded_count"], 2)
                    persisted = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["broadcast_allowlist"], [101, 202])
                    self.assertTrue(persisted["broadcast_allowlist_only"])
                    listed = await client.get("/api/profiles/default/broadcast/recipients",
                                              headers=headers)
                    listed_data = await listed.json()
                    self.assertEqual(listed_data["selected_ids"], [101, 202])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_media_cleanup_only_deletes_old_unreferenced_files(self):
        async def run():
            import os
            import sqlite3
            import time
            with tempfile.TemporaryDirectory() as td:
                data_dir = Path(td)
                videos = data_dir / "videos" / "42"
                videos.mkdir(parents=True)
                referenced = videos / "archived_photo.jpg"
                orphan = videos / "orphan_video.mp4"
                temporary = videos / "download.mp4.part"
                recent = videos / "recent_video.mp4"
                for path in (referenced, orphan, temporary, recent):
                    path.write_bytes(b"test-media")
                old_time = time.time() - 25 * 60 * 60
                for path in (referenced, orphan, temporary):
                    os.utime(path, (old_time, old_time))
                db = sqlite3.connect(data_dir / "circles.db")
                db.execute("CREATE TABLE circles(file_path TEXT)")
                db.execute("INSERT INTO circles(file_path) VALUES(?)", (str(referenced),))
                db.commit()
                db.close()

                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, data_dir)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/media/cleanup/scan", runtime.media_cleanup_scan)
                app.router.add_post("/api/profiles/{name}/media/cleanup/execute", runtime.media_cleanup_execute)
                client = TestClient(TestServer(app)); await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    scanned = await client.get("/api/profiles/default/media/cleanup/scan",
                                               headers=headers)
                    self.assertEqual(scanned.status, 200)
                    preview = await scanned.json()
                    self.assertEqual(preview["referenced_files"], 1)
                    self.assertEqual(preview["candidate_count"], 2)
                    self.assertEqual(preview["candidate_bytes"], orphan.stat().st_size + temporary.stat().st_size)
                    wrong = await client.post("/api/profiles/default/media/cleanup/execute",
                                              headers=headers,
                                              json={"preview_id": preview["preview_id"],
                                                    "confirmation": "DELETE_ORPHANS 3"})
                    self.assertEqual(wrong.status, 400)
                    deleted = await client.post("/api/profiles/default/media/cleanup/execute",
                                                headers=headers,
                                                json={"preview_id": preview["preview_id"],
                                                      "confirmation": "DELETE_ORPHANS 2"})
                    self.assertEqual(deleted.status, 200)
                    result = await deleted.json()
                    self.assertEqual(result["deleted"], 2)
                    self.assertTrue(referenced.exists())
                    self.assertTrue(recent.exists())
                    self.assertFalse(orphan.exists())
                    self.assertFalse(temporary.exists())
                finally:
                    await client.close()
                    await runtime.stop()

        asyncio.run(run())

    def test_archive_summary_query_runs_in_worker_thread(self):
        async def run():
            import threading
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, Path(td))
                runtime._archive_summary_sync = lambda _name: {
                    "thread_name": threading.current_thread().name,
                }
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get(
                    "/api/profiles/{name}/archive/summary", runtime.archive_summary,
                )
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    response = await client.get(
                        "/api/profiles/default/archive/summary",
                        headers={"Authorization": "Bearer unit-test-token"},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertNotEqual(data["thread_name"], "MainThread")
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_smart_cleaner_catalog_reads_run_in_worker_threads(self):
        async def run():
            import threading
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, Path(td))
                runtime.cleaners["default"].catalog_stats = lambda: {
                    "thread_name": threading.current_thread().name,
                }
                runtime.cleaners["default"].list_catalog = lambda **_kwargs: {
                    "thread_name": threading.current_thread().name, "total": 0, "items": [],
                }
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/summary", runtime.summary)
                app.router.add_get("/api/profiles/{name}/dialogs", runtime.dialogs)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    summary_response = await client.get(
                        "/api/profiles/default/summary", headers=headers,
                    )
                    dialogs_response = await client.get(
                        "/api/profiles/default/dialogs", headers=headers,
                    )
                    self.assertEqual(summary_response.status, 200)
                    self.assertEqual(dialogs_response.status, 200)
                    summary = await summary_response.json()
                    dialogs = await dialogs_response.json()
                    self.assertNotEqual(summary["thread_name"], "MainThread")
                    self.assertNotEqual(dialogs["thread_name"], "MainThread")
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_panel_persists_selected_profile_between_page_reloads(self):
        import telegram_manager_web as web_module

        self.assertIn('localStorage.getItem("circles_active_profile")', web_module.INDEX_HTML)
        self.assertIn('localStorage.setItem("circles_active_profile",S.profile)', web_module.INDEX_HTML)
        self.assertIn('id="bcAccountSource"', web_module.INDEX_HTML)
        self.assertIn('id="bcStartFeedback"', web_module.INDEX_HTML)

    def test_panel_closes_head_style_before_body(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertEqual(html.count("<style>"), 1)
        self.assertEqual(html.count("</style>"), 1)
        self.assertLess(html.index("</style>"), html.index("<body>"))
        self.assertLess(html.index("<body>"), html.index('<div class="app">'))
        self.assertIn("</body></html>", html)

    def test_broadcast_status_does_not_call_telegram_preview(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                class FakeBroadcaster:
                    def __init__(self):
                        self.state = {
                            "active": False, "end": 0, "next": 0, "interval": 60,
                            "wave": 0, "run_ok": 0, "run_fail": 0, "life_ok": 12,
                            "life_fail": 2, "life_waves": 3, "last_error": "",
                            "pending_wave": None, "history": [], "post_id": 456,
                        }
                        self.running = False
                        self.preview_calls = 0
                        self.progress = (0, 0)
                    def short(self):
                        return "выключена"
                    async def preview(self):
                        self.preview_calls += 1
                        raise AssertionError("status must not call Telegram preview")

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, Path(td))
                bc = FakeBroadcaster()
                runtime.register_broadcaster("default", bc)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/broadcast", runtime.broadcast_status)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    response = await client.get(
                        "/api/profiles/default/broadcast",
                        headers={"Authorization": "Bearer unit-test-token"},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["life_ok"], 12)
                    self.assertEqual(data["post"]["id"], 456)
                    self.assertFalse(data["post"]["preview_available"])
                    self.assertEqual(bc.preview_calls, 0)
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_selected_broadcast_post_is_cached_for_status(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                class FakeBroadcaster:
                    state = {"post_id": 456, "active": False, "pending_wave": None,
                             "history": [], "life_ok": 0, "life_fail": 0, "life_waves": 0}
                    running = False
                    progress = (0, 0)
                    def short(self):
                        return "выключена"
                    async def select_post(self, post_id):
                        return {"id": post_id, "text": "Cached text", "media": True, "pinned": True}
                    async def preview(self):
                        raise AssertionError("status must use cached preview")

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/post", runtime.select_broadcast_post)
                app.router.add_get("/api/profiles/{name}/broadcast", runtime.broadcast_status)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    selected = await client.post(
                        "/api/profiles/default/broadcast/post", headers=headers,
                        json={"post_id": 456},
                    )
                    self.assertEqual(selected.status, 200)
                    status = await client.get("/api/profiles/default/broadcast", headers=headers)
                    data = await status.json()
                    self.assertEqual(data["post"]["text"], "Cached text")
                    self.assertTrue(data["post"]["preview_available"])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_recipient_counter_updates_are_null_safe_and_save_result_is_truthful(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn("function safeText(id,value)", html)
        self.assertIn('safeText("recipientSelectionHint",summary)', html)
        self.assertIn("let savedOnServer=false", html)
        self.assertIn("Список сохранён на сервере, но интерфейс не обновился:", html)
        self.assertIn('S.recipientAccountLabel=r.account_label||profile', html)

    def test_logs_keep_latest_filter_request_when_previous_load_is_in_flight(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn("logLoading:false,logReloadPending:false", html)
        load = html[html.index("async function loadLogs()"):html.index('$("loadLogs").onclick=')]
        self.assertIn("if(S.logLoading){S.logReloadPending=true;return}", load)
        self.assertIn("const rerun=S.logReloadPending;S.logReloadPending=false", load)
        self.assertIn("Promise.resolve().then(()=>loadLogs())", load)
        self.assertIn('document.querySelector(".page.active")?.id!=="page-logs"', load)

    def test_chat_scan_progress_is_bound_to_the_profile_that_started_it(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        scan = html[html.index("async function scanChats()"):html.index('$("scanChats").onclick=')]
        self.assertIn("const profile=S.profile", scan)
        self.assertIn('if(S.profile!==profile)return;r=await api("/api/scans/"+a.scan_id)', scan)
        self.assertIn('if(S.profile===profile)toast(e.message,"error")', scan)

    def test_broadcast_view_identifies_primary_profile_and_pending_recipient_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = TelegramManagerRuntime(base_dir=td)
            cfg = {"account_label": "Main", "owner_id": 12345, "broadcast_allowlist": [11, 22]}
            class FakeBroadcaster:
                running = False
                progress = (0, 0)
                state = {
                    "active": True, "end": 9999999999, "next": 0, "interval": 60,
                    "wave": 1, "run_ok": 67, "run_fail": 0, "life_ok": 67,
                    "life_fail": 0, "life_waves": 0, "last_error": "",
                    "pending_wave": {"chat_ids": [11, 22, 33, 44], "completed_ids": [11, 33]},
                    "history": [],
                }
                def short(self):
                    return "выключена"
            runtime.configs["default"] = cfg
            view = runtime._broadcast_view("default", FakeBroadcaster())
            self.assertTrue(view["is_primary"])
            self.assertEqual(view["account_label"], "Main")
            self.assertEqual(view["pending_total"], 4)
            self.assertEqual(view["pending_completed"], 2)
            self.assertEqual(view["pending_remaining"], 2)
            self.assertEqual(view["pending_not_selected"], 2)
            self.assertEqual(view["pending_completed_not_selected"], 1)
            self.assertEqual(view["pending_remaining_not_selected"], 1)
            self.assertTrue(view["has_pending_wave"])
            self.assertTrue(view["resume_available"])

    def test_broadcast_ui_explains_frozen_pending_wave_without_blocking_on_allowlist(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        load = html[html.index("async function loadBroadcast()"):html.index('$("loadPosts").onclick=')]
        self.assertIn('d.is_primary?"Основной аккаунт"', load)
        self.assertIn("d.pending_not_selected", load)
        self.assertIn("d.pending_remaining", load)
        self.assertIn("не меняет эту волну", load)
        self.assertNotIn("Продолжение заблокировано, потому что список получателей изменился", load)
        start = html[html.index('$("startBc").onclick=async()=>{'):html.index('$("stopBc").onclick=')]
        self.assertIn("resume_saved:true", start)
        self.assertIn("Продолжить сохранённую очередь?", start)

    def test_broadcast_start_returns_conflict_if_campaign_is_already_running(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                cfg = {"broadcast_allowlist_only": True, "broadcast_allowlist": [101]}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    running = True
                    state = {"active": True}
                    async def preview(self):
                        raise AssertionError("Already-running campaign must be rejected before Telegram calls")

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/start", runtime.start_broadcast)
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.post(
                        "/api/profiles/default/broadcast/start",
                        headers={"Authorization": "Bearer unit-test-token"},
                        json={"confirmed": True, "recipient_ids": [101]},
                    )
                    self.assertEqual(response.status, 409)
                    self.assertIn("уже выполняется", await response.text())
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_broadcast_start_button_tracks_running_and_recoverable_states(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        load = html[html.index("async function loadBroadcast()"):html.index('$("loadPosts").onclick=')]
        self.assertIn("S.broadcastRunning=!!d.running;S.broadcastActive=!!d.active", load)
        self.assertIn('$("startBc").disabled=S.broadcastRunning||', load)
        self.assertIn('S.broadcastRunning?"Кампания работает":', load)
        self.assertIn('"▶ Продолжить"', load)
        self.assertIn("savedQueueBlocked", load)
        start = html[html.index('$("startBc").onclick=async()=>{'):html.index('$("stopBc").onclick=')]
        self.assertIn('b.disabled=!!S.broadcastRunning', start)

    def test_broadcaster_exposes_clear_terminal_start_reasons(self):
        expected = {
            "nopost": "Избранном",
            "nochats": "доступных групп",
            "spam": "ограничении аккаунта",
            "account_restricted": "ограничил действия",
            "source_restricted": "исходное сообщение",
            "flood_expired": "Ограничение Telegram",
        }
        for reason, phrase in expected.items():
            with self.subTest(reason=reason):
                self.assertIn(phrase, Broadcaster.terminal_reason_message(reason))
        self.assertEqual(Broadcaster.terminal_reason_message("done"), "")

    def test_broadcast_ui_displays_engine_failure_and_resume_state(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        load = html[html.index("async function loadBroadcast()"):html.index('$("loadPosts").onclick=')]
        self.assertIn("d.last_error", load)
        self.assertIn('Причина остановки/незапуска: ', load)
        self.assertIn("pendingResume=!!d.active&&!d.running", load)
        self.assertIn("⚠ ожидает продолжения", load)

    def test_panel_hides_saved_access_token_and_explains_timeout_context(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn('id="changeToken"', html)
        self.assertIn('function setTokenUi(hasAccess)', html)
        self.assertIn('setTokenUi(true)', html)
        self.assertIn('S.authToken', html)
        self.assertNotIn('$("token").value=t', html)
        self.assertIn('const timeoutMs=slowTelegramRequest?120000:30000', html)
        self.assertIn('Запрос "+method+" "+path+" не завершился за "', html)
        self.assertIn('Нет подтверждения от сервера. Проверь текущий статус кампании', html)
        start = html[html.index('$("startBc").onclick=async()=>{'):html.index('$("stopBc").onclick=async()=>{')]
        self.assertIn('await askConfirm(', start)
        self.assertIn('"Запустить кампанию?"', start)
        self.assertNotIn('START ', start)

    def test_account_actions_expose_pending_and_accepted_states(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn('withButton(b,"Запускаю…"', html)
        self.assertIn('withButton(b,"Останавливаю…"', html)
        self.assertIn('withButton($("createProfile"),"Создаю…"', html)
        self.assertIn('withButton($("sendLoginCode"),"Отправляю…"', html)
        self.assertIn('withButton($("submitLoginCode"),"Проверяю код…"', html)
        self.assertIn('withButton($("submitLoginPassword"),"Проверяю 2FA…"', html)
        self.assertIn("Авторизация завершена, но список профилей не обновлён", html)
        self.assertIn("Запрос запуска профиля", html)
        self.assertIn("Запрос остановки профиля", html)

    def test_broadcast_recipient_panel_collapse_and_action_feedback(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn('id="recipientsCard"', html)
        self.assertIn('id="recipientsPanelBody"', html)
        self.assertIn('id="toggleRecipientsPanel"', html)
        self.assertIn('aria-controls="recipientsPanelBody"', html)
        self.assertIn('function recipientsCollapsedKey()', html)
        self.assertIn('localStorage.setItem(recipientsCollapsedKey()', html)
        self.assertIn('function restoreRecipientsCollapsed()', html)
        self.assertIn("restoreRecipientsCollapsed();", html)
        self.assertIn(".recipient-card.is-collapsed #recipientsPanelBody{display:none}", html)
        self.assertIn('id="recipientActionFeedback"', html)
        self.assertIn('id="recipientCollapsedSummary"', html)
        self.assertIn('hint.dataset.state="busy"', html)
        self.assertIn('hint.dataset.state="success"', html)
        self.assertIn('hint.dataset.state="error"', html)
        self.assertIn("Остановка отменена. Кампания продолжает работу.", html)

    def test_archive_ui_shows_load_state_and_ignores_stale_media_responses(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        self.assertIn('id="archiveCatalogFeedback"', html)
        self.assertIn('id="archiveMediaFeedback"', html)
        self.assertIn('aria-busy="false"', html)
        self.assertIn('S.archiveTotal=Number(data.total)||0', html)
        self.assertIn('S.archiveMediaTotal=Number(d.total)||0', html)
        self.assertIn('if(S.profile!==profile||S.latest.archive!==version)return;', html)
        self.assertIn('if(S.profile===profile&&S.latest.archive===version){list.setAttribute("aria-busy","false");updateControls(false)}', html)
        self.assertIn('const current=()=>S.profile===profile&&Number(S.archiveUser)===user&&S.archiveMediaPage===page&&S.latest.archiveMedia===version', html)
        self.assertIn('Загружаю записи медиа…', html)
        self.assertIn('Ожидаю подтверждение…', html)
        self.assertIn('Очистка отменена. Файлы не удалены.', html)
        self.assertIn('S.archiveUser=null', html)
        self.assertIn('S.archiveUser=Number(b.dataset.user);S.archiveMediaPage=0;S.archiveMediaTotal=0;', html)

    def test_broadcast_recipient_lists_are_isolated_by_profile(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                class FakeBroadcaster:
                    def __init__(self, chats):
                        self.chats = chats
                    async def chat_list(self, refresh=False):
                        return list(self.chats)
                    def chat_status(self, chat_id):
                        return "ok"

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                alpha_cfg = {"_config_path": str(Path(td) / "alpha.json"),
                             "account_label": "Alpha phone", "owner_id": 1001,
                             "broadcast_allowlist": [], "broadcast_allowlist_only": False}
                beta_cfg = {"_config_path": str(Path(td) / "beta.json"),
                            "account_label": "Beta phone", "owner_id": 2002,
                            "broadcast_allowlist": [], "broadcast_allowlist_only": False}
                runtime.register_profile("alpha", FakeClient(), alpha_cfg, Path(td) / "alpha")
                runtime.register_profile("beta", FakeClient(), beta_cfg, Path(td) / "beta")
                runtime.register_broadcaster("alpha", FakeBroadcaster([(101, "Alpha group")]))
                runtime.register_broadcaster("beta", FakeBroadcaster([(202, "Beta group")]))
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/broadcast/recipients", runtime.recipients)
                app.router.add_post("/api/profiles/{name}/broadcast/recipients", runtime.set_recipients)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    alpha = await client.get(
                        "/api/profiles/alpha/broadcast/recipients", headers=headers)
                    beta = await client.get(
                        "/api/profiles/beta/broadcast/recipients", headers=headers)
                    self.assertEqual(alpha.status, 200)
                    self.assertEqual(beta.status, 200)
                    a, b = await alpha.json(), await beta.json()
                    self.assertEqual([item["id"] for item in a["items"]], [101])
                    self.assertEqual([item["id"] for item in b["items"]], [202])
                    self.assertEqual((a["profile"], a["account_id"]), ("alpha", 1001))
                    self.assertEqual((b["profile"], b["account_id"]), ("beta", 2002))

                    wrong_account = await client.post(
                        "/api/profiles/alpha/broadcast/recipients", headers=headers,
                        json={"chat_ids": [202]},
                    )
                    self.assertEqual(wrong_account.status, 200)
                    wrong_data = await wrong_account.json()
                    self.assertEqual(wrong_data["selected_ids"], [])
                    self.assertEqual(wrong_data["selected_count"], 0)
                    self.assertEqual(wrong_data["removed_stale_count"], 1)
                    self.assertEqual(alpha_cfg["broadcast_allowlist"], [])

                    saved = await client.post(
                        "/api/profiles/beta/broadcast/recipients", headers=headers,
                        json={"chat_ids": [202]},
                    )
                    self.assertEqual(saved.status, 200)
                    saved_data = await saved.json()
                    self.assertEqual(saved_data["profile"], "beta")
                    self.assertEqual(saved_data["account_id"], 2002)
                    self.assertEqual(beta_cfg["broadcast_allowlist"], [202])
                    self.assertEqual(alpha_cfg["broadcast_allowlist"], [])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_recipient_get_is_read_only_and_never_prunes_saved_ids(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                original_allowlist = [101, 202, 303, 101]
                cfg = {"_config_path": str(config_path), "account_label": "Main",
                       "broadcast_allowlist_only": True, "broadcast_allowlist": list(original_allowlist)}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    async def chat_list(self, refresh=False):
                        return [(101, "Current group"), (404, "Another current group")]
                    def chat_status(self, chat_id):
                        return "ok"

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/broadcast/recipients", runtime.recipients)
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.get(
                        "/api/profiles/default/broadcast/recipients",
                        headers={"Authorization": "Bearer unit-test-token"},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["selected_ids"], [101])
                    self.assertEqual(data["selected_count"], 1)
                    self.assertEqual(data["removed_stale_count"], 2)
                    self.assertEqual(cfg["broadcast_allowlist"], original_allowlist)
                    self.assertFalse(config_path.exists(), "GET must not persist a pruned allowlist")
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_recipient_save_keeps_valid_ids_and_drops_stale_ids(self):
        async def run():
            import json
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                cfg = {"_config_path": str(config_path), "broadcast_allowlist_only": False,
                       "broadcast_allowlist": []}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    async def chat_list(self, refresh=False):
                        return [(101, "Current group"), (202, "Another current group")]
                    def chat_status(self, chat_id):
                        return "ok"

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/recipients", runtime.set_recipients)
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.post(
                        "/api/profiles/default/broadcast/recipients",
                        headers={"Authorization": "Bearer unit-test-token"},
                        json={"chat_ids": [101, 202, 999, 998, 997, 996, 101]},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["requested_count"], 6)
                    self.assertEqual(data["selected_count"], 2)
                    self.assertEqual(data["selected_ids"], [101, 202])
                    self.assertEqual(data["removed_stale_count"], 4)
                    self.assertEqual(cfg["broadcast_allowlist"], [101, 202])
                    persisted = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["broadcast_allowlist"], [101, 202])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_recipient_save_with_only_stale_ids_clears_selection_without_error(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                cfg = {"_config_path": str(Path(td) / "config.json"),
                       "broadcast_allowlist_only": True, "broadcast_allowlist": [901]}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    async def chat_list(self, refresh=False):
                        return [(101, "Current group")]
                    def chat_status(self, chat_id):
                        return "ok"

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                runtime.register_broadcaster("default", FakeBroadcaster())
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/recipients", runtime.set_recipients)
                client = TestClient(TestServer(app)); await client.start_server()
                try:
                    response = await client.post(
                        "/api/profiles/default/broadcast/recipients",
                        headers={"Authorization": "Bearer unit-test-token"},
                        json={"chat_ids": [901]},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["selected_count"], 0)
                    self.assertEqual(data["removed_stale_count"], 1)
                    self.assertEqual(cfg["broadcast_allowlist"], [])
                    self.assertTrue(cfg["broadcast_allowlist_only"])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_recipient_pagination_keeps_local_selection_and_refresh_does_not_clear_it(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        loader = html[html.index("async function loadRecipients(refresh)"):html.index("function drawRecipients()")]
        refresh = html[html.index('$("refreshRecipients").onclick='):html.index('$("selectAllRecipients").onclick=')]
        self.assertNotIn("if(S.recipientDirty)return false", loader)
        self.assertNotIn("||S.recipientDirty||document.querySelector", loader)
        self.assertIn("if(!S.recipientInitialized&&!S.recipientDirty){S.recipientSelected=new Set", loader)
        self.assertIn('hadUnsaved=S.recipientDirty', refresh)
        self.assertNotIn("S.recipientSelected.clear()", refresh)
        self.assertIn("if(!hadUnsaved)S.recipientInitialized=false", refresh)
        self.assertIn("Несохранённый выбор сохранён на экране", refresh)

    def test_recipient_panel_reconciles_server_filtered_ids_after_save(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        save = html[html.index('$("saveRecipients").onclick=async()=>{'):html.index('$("saveBcSettings").onclick')]
        self.assertIn("S.recipientSelected=new Set((Array.isArray(r.selected_ids)?r.selected_ids:submittedIds).map(Number))", save)
        self.assertIn("const removed=Number(r.removed_stale_count)||0", save)
        self.assertIn("устаревших или чужих ID", save)
        self.assertIn("S.recipientDirty=false;S.recipientInitialized=true", save)

    def test_broadcast_start_uses_current_allowlist_after_dialog_refresh(self):
        async def run():
            import time
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                cfg = {
                    "_config_path": str(config_path), "broadcast_allowlist_only": True,
                    "broadcast_allowlist": [101, 202],
                }
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                class FakeBroadcaster:
                    def __init__(self):
                        self._dialogs_scanned_at = time.time() - 120
                        self.state = {"active": False, "pending_wave": None, "post_id": 0}
                        self.running = False
                        self.progress = (0, 0)
                        self.started = 0
                    def short(self):
                        return "выключена"
                    async def preview(self):
                        return {"id": 9, "text": "Preview", "media": False, "pinned": True}
                    async def chat_list(self, refresh=False):
                        if refresh:
                            # Simulate Broadcaster pruning a group that is no longer present.
                            cfg["broadcast_allowlist"] = [101]
                            self._dialogs_scanned_at = time.time()
                        return [(101, "Current group")]
                    def start(self, interval, hours):
                        self.started += 1
                        self.running = True
                        return True

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                bc = FakeBroadcaster()
                runtime.register_broadcaster("default", bc)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/start", runtime.start_broadcast)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    response = await client.post(
                        "/api/profiles/default/broadcast/start",
                        headers={"Authorization": "Bearer unit-test-token"},
                        json={"confirmed": True, "recipient_ids": [101, 202]},
                    )
                    self.assertEqual(response.status, 202)
                    data = await response.json()
                    self.assertEqual(data["selected_count"], 1)
                    self.assertEqual(data["selected_ids"], [101])
                    self.assertEqual(cfg["broadcast_allowlist"], [101])
                    self.assertEqual(bc.started, 1)
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_saved_broadcast_wave_resumes_from_its_checkpoint_not_current_allowlist(self):
        async def run():
            import time
            with tempfile.TemporaryDirectory() as td:
                config_path = Path(td) / "config.json"
                cfg = {"_config_path": str(config_path), "broadcast_allowlist_only": True,
                       "broadcast_allowlist": [9001, 9002]}
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                class FakeBroadcaster:
                    def __init__(self):
                        self.running = False
                        self.progress = (0, 0)
                        self.state = {
                            "active": True, "end": time.time() + 3600, "next": 0, "interval": 60,
                            "pending_wave": {"post_id": 77,
                                             "chat_ids": list(range(1, 310)),
                                             "completed_ids": list(range(1, 68))},
                        }
                        self.resume_calls = 0
                        self.start_calls = 0
                    def short(self):
                        return "выключена"
                    def resume(self):
                        self.resume_calls += 1
                        self.running = True
                        return True
                    def start(self, interval, hours):
                        self.start_calls += 1
                        raise AssertionError("Pending checkpoint must never be replaced with a new campaign")

                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), cfg, Path(td))
                bc = FakeBroadcaster()
                runtime.register_broadcaster("default", bc)
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_post("/api/profiles/{name}/broadcast/start", runtime.start_broadcast)
                client = TestClient(TestServer(app))
                await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    blocked = await client.post(
                        "/api/profiles/default/broadcast/start", headers=headers,
                        json={"confirmed": True, "recipient_ids": [9001, 9002]},
                    )
                    self.assertEqual(blocked.status, 400)
                    self.assertEqual(bc.resume_calls, 0)
                    self.assertEqual(bc.start_calls, 0)

                    resumed = await client.post(
                        "/api/profiles/default/broadcast/start", headers=headers,
                        json={"confirmed": True, "resume_saved": True},
                    )
                    self.assertEqual(resumed.status, 202)
                    data = await resumed.json()
                    self.assertTrue(data["resumed"])
                    self.assertEqual(data["selected_count"], 309)
                    self.assertEqual(data["already_completed"], 67)
                    self.assertEqual(data["remaining_count"], 242)
                    self.assertEqual(bc.resume_calls, 1)
                    self.assertEqual(bc.start_calls, 0)
                    self.assertEqual(cfg["broadcast_allowlist"], [9001, 9002])
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_panel_start_uses_one_click_confirmation_without_dynamic_count_code(self):
        import telegram_manager_web as web_module

        html = web_module.INDEX_HTML
        start = html[html.index('$("startBc").onclick=async()=>{'):html.index('$("stopBc").onclick=async()=>{')]
        self.assertIn('await askConfirm(', start)
        self.assertIn('"Запустить кампанию?"', start)
        self.assertIn('confirmed:true,recipient_ids:launchIds', start)
        self.assertNotIn('askModal(', start)
        self.assertNotIn('START "+saved.selected_count', start)
        self.assertIn('selected_ids', start)
        self.assertIn('Список получателей', start)

    def test_broadcast_start_reuses_recent_profile_dialog_validation(self):
        async def run():
            import time
            with tempfile.TemporaryDirectory() as td:
                class FakeBroadcaster:
                    def __init__(self):
                        self._dialogs_scanned_at = time.time()
                        self.calls = []
                        self.chats = [(301, "Current group")]
                    async def chat_list(self, refresh=False):
                        self.calls.append(bool(refresh))
                        if refresh:
                            self._dialogs_scanned_at = time.time()
                        return list(self.chats)

                runtime = TelegramManagerRuntime(base_dir=td)
                bc = FakeBroadcaster()
                result = await runtime._validated_broadcast_chats(bc)
                self.assertEqual(result, [(301, "Current group")])
                self.assertEqual(bc.calls, [False])

                bc._dialogs_scanned_at = time.time() - 120
                await runtime._validated_broadcast_chats(bc)
                self.assertEqual(bc.calls, [False, True])
        asyncio.run(run())

    def test_archive_compact_payload_skips_unrendered_fields(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            class FakeClient:
                async def iter_dialogs(self):
                    if False:
                        yield None

            runtime = TelegramManagerRuntime(base_dir=td)
            runtime.register_profile("default", FakeClient(), {}, data_dir)
            db = sqlite3.connect(data_dir / "circles.db")
            try:
                db.executescript("""
                    CREATE TABLE topics(topic_id INTEGER PRIMARY KEY,user_id INTEGER,title TEXT);
                    CREATE TABLE circles(
                        topic_id INTEGER,user_id INTEGER,msg_id INTEGER,date_utc TEXT,duration INTEGER,
                        outgoing INTEGER,sent INTEGER,attempts INTEGER,media_type TEXT,mime_type TEXT,
                        file_size INTEGER,file_path TEXT
                    );
                """)
                db.execute("INSERT INTO topics VALUES(1,123,'Archive user')")
                db.execute(
                    "INSERT INTO circles VALUES(1,123,77,'2026-11-23T10:00:00Z',12,0,1,2,"
                    "'video','video/mp4',4096,'/missing/video.mp4')"
                )
                db.commit()
            finally:
                db.close()

            try:
                full_people = runtime._archive_people_sync("default", "", 50, 0)
                compact_people = runtime._archive_people_sync("default", "", 50, 0, compact=True)
                self.assertIn("topic_id", full_people["items"][0])
                self.assertIn("first_media", full_people["items"][0])
                self.assertNotIn("topic_id", compact_people["items"][0])
                self.assertNotIn("first_media", compact_people["items"][0])
                self.assertEqual(compact_people["items"][0]["media_count"], 1)

                full_media = runtime._archive_person_media_sync("default", 123, 30, 0)
                compact_media = runtime._archive_person_media_sync("default", 123, 30, 0, compact=True)
                self.assertIn("duration", full_media["items"][0])
                self.assertIn("attempts", full_media["items"][0])
                self.assertIn("mime_type", full_media["items"][0])
                self.assertNotIn("duration", compact_media["items"][0])
                self.assertNotIn("attempts", compact_media["items"][0])
                self.assertNotIn("mime_type", compact_media["items"][0])
                self.assertEqual(compact_media["items"][0]["msg_id"], 77)
            finally:
                runtime.cleaners["default"].close()

    def test_system_metrics_report_sqlite_ram_and_recent_api_latency(self):
        async def run():
            import time
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None

                data_dir = Path(td)
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, data_dir)
                (data_dir / "circles.db").write_bytes(b"x" * 100)
                (data_dir / "circles.db-wal").write_bytes(b"y" * 23)
                now = time.time()
                runtime.api_latency_samples = [
                    (now - 2, 12.5, 200),
                    (now - 1, 55.0, 503),
                ]
                app = web.Application(middlewares=[runtime.metrics_middleware, runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/metrics", runtime.metrics)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    response = await client.get(
                        "/api/profiles/default/metrics",
                        headers={"Authorization": "Bearer unit-test-token"},
                    )
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["sqlite"]["archive_bytes"], 123)
                    self.assertGreater(data["sqlite"]["total_bytes"], 123)
                    self.assertIn("total_bytes", data["memory"])
                    self.assertIn("process_rss_bytes", data["memory"])
                    self.assertEqual(data["api"]["requests_60s"], 2)
                    self.assertEqual(data["api"]["errors_60s"], 1)
                    self.assertEqual(data["api"]["p95_ms"], 55.0)
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())

    def test_archive_media_sort_index_is_created_and_used(self):
        import combined_multi_account as app_module

        with tempfile.TemporaryDirectory() as td:
            db = app_module.DB(Path(td) / "circles.db")
            try:
                indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(circles)")}
                self.assertIn("idx_circles_user_date_msg", indexes)
                plan = db.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT msg_id,date_utc FROM circles "
                    "WHERE user_id=? ORDER BY date_utc DESC,msg_id DESC LIMIT ?",
                    (123, 30),
                ).fetchall()
                self.assertTrue(any("idx_circles_user_date_msg" in str(row) for row in plan), plan)
            finally:
                db.close()

    def test_broadcast_log_api_parses_levels_and_filters(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                class FakeClient:
                    async def iter_dialogs(self):
                        if False:
                            yield None
                runtime = TelegramManagerRuntime(base_dir=td)
                runtime.token = "unit-test-token"
                runtime.register_profile("default", FakeClient(), {}, Path(td))
                log_path = Path(td) / "broadcast.log"
                log_path.write_text(
                    "2026-11-22 10:00:00 INFO sent to group\n"
                    "2026-11-22 10:00:01 WARNING retry after flood wait\n"
                    "2026-11-22 10:00:02 ERROR failed to send\n",
                    encoding="utf-8",
                )
                app = web.Application(middlewares=[runtime.auth_middleware])
                app.router.add_get("/api/profiles/{name}/logs", runtime.logs)
                client = TestClient(TestServer(app)); await client.start_server()
                headers = {"Authorization": "Bearer unit-test-token"}
                try:
                    response = await client.get(
                        "/api/profiles/default/logs?kind=broadcast&level=ERROR", headers=headers)
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                    self.assertEqual(data["count"], 1)
                    self.assertEqual(data["entries"][0]["level"], "ERROR")
                    self.assertEqual(data["counts"]["INFO"], 1)
                    self.assertEqual(data["counts"]["WARNING"], 1)
                    self.assertEqual(data["counts"]["ERROR"], 1)

                    compact_response = await client.get(
                        "/api/profiles/default/logs?kind=broadcast&level=ERROR&compact=1",
                        headers=headers,
                    )
                    self.assertEqual(compact_response.status, 200)
                    compact_data = await compact_response.json()
                    self.assertNotIn("lines", compact_data)
                    self.assertNotIn("raw", compact_data["entries"][0])
                    self.assertEqual(compact_data["entries"][0]["message"], "failed to send")
                finally:
                    await client.close()
                    await runtime.stop()
        asyncio.run(run())


    def test_broadcast_dialog_scan_only_returns_groups_and_supergroups(self):
        async def run():
            from types import SimpleNamespace
            from unittest.mock import patch
            import combined_multi_account as app_module

            class FakeChat:
                def __init__(self, chat_id, title):
                    self.id = chat_id
                    self.title = title
                    self.username = None
                    self.deactivated = False

            class FakeChannel:
                def __init__(self, chat_id, title, *, broadcast=False, megagroup=False, gigagroup=False):
                    self.id = chat_id
                    self.title = title
                    self.username = None
                    self.deactivated = False
                    self.broadcast = broadcast
                    self.megagroup = megagroup
                    self.gigagroup = gigagroup

            entities = [
                FakeChat(101, "Basic group"),
                FakeChannel(202, "Supergroup", megagroup=True),
                FakeChannel(303, "Broadcast channel", broadcast=True),
                FakeChannel(404, "Broadcast channel with conflicting flags",
                            broadcast=True, megagroup=True),
                FakeChannel(505, "Gigagroup", gigagroup=True),
            ]

            class FakeClient:
                async def iter_dialogs(self, ignore_migrated=True):
                    for entity in entities:
                        yield SimpleNamespace(entity=entity)

            with tempfile.TemporaryDirectory() as td:
                cfg = {
                    "_config_path": str(Path(td) / "config.json"),
                    "broadcast_allowlist_only": True,
                    "broadcast_allowlist": [101, 202, 303, 404, 505, 999],
                }
                broadcaster = app_module.Broadcaster(cfg, FakeClient(), Path(td), enabled=False)
                fake_types = SimpleNamespace(Chat=FakeChat, Channel=FakeChannel)
                with patch.object(app_module, "types", fake_types):
                    groups = await broadcaster._scan_dialogs_impl()

                self.assertEqual([entity.id for entity in groups], [101, 202, 505])
                self.assertEqual({101, 202, 303, 404, 505}, broadcaster.current_memberships)
                self.assertEqual([101, 202, 505], cfg["broadcast_allowlist"])
                self.assertEqual([101, 202, 505], [chat_id for chat_id, _ in broadcaster.chat_cache])

        asyncio.run(run())

if __name__ == '__main__': unittest.main()
