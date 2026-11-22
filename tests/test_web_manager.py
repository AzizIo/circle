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


if __name__ == '__main__': unittest.main()