import asyncio
import tempfile
from pathlib import Path
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telegram_manager_web import TelegramManagerRuntime


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
                    def short(self):
                        return "работает" if self.running else "выключена"
                    async def preview(self):
                        return {"id": 77, "text": "Approved test post", "media": False, "pinned": True}
                    async def saved_posts(self, limit=10):
                        return []
                    async def chat_list(self, refresh=False):
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
                    self.assertEqual(bc.duration_set, 4)

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
                        json={"confirmation": "START 2"},
                    )
                    self.assertEqual(wrong.status, 400)
                    started = await client.post(
                        "/api/profiles/default/broadcast/start", headers=headers,
                        json={"confirmation": "START 1"},
                    )
                    self.assertEqual(started.status, 202)
                    self.assertEqual(bc.started, 1)

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

if __name__ == '__main__': unittest.main()
