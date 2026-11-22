"""Local authenticated web manager for the live CIRCLES CONTROL process.

The web service never creates Telegram clients. It submits work to clients
registered by the existing run_service/AccountManager lifecycle.
Requires aiohttp; bind to loopback and use an SSH tunnel or a TLS reverse proxy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from smart_cleaner import SmartCleaner

log = logging.getLogger("circles.web_manager")
PROFILE_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")

PANEL_PATH = Path(__file__).with_name("telegram_manager_panel.html")
INDEX_HTML = PANEL_PATH.read_text(encoding="utf-8")



class TelegramManagerRuntime:
    def __init__(self, *, base_dir: str | Path, profiles_provider: Callable[[], list[tuple[str, dict, Path]]] | None = None):
        self.base_dir = Path(base_dir)
        self.profiles_provider = profiles_provider
        self.clients: dict[str, Any] = {}
        self.configs: dict[str, dict] = {}
        self.data_dirs: dict[str, Path] = {}
        self.cleaners: dict[str, SmartCleaner] = {}
        self.scan_tasks: dict[str, asyncio.Task] = {}
        self.scan_results: dict[str, dict] = {}
        self.job_tasks: dict[str, asyncio.Task] = {}
        self.profile_tasks: dict[str, set[asyncio.Task]] = {}
        self.account_manager: Any = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.token = ""
        self.host = "127.0.0.1"
        self.port = 8765

    def set_account_manager(self, manager: Any) -> None:
        self.account_manager = manager

    def register_profile(self, name: str, client: Any, cfg: dict, data_dir: Path) -> None:
        if not PROFILE_RE.fullmatch(name):
            raise ValueError("Invalid profile name")
        self.clients[name] = client
        self.configs[name] = cfg
        self.data_dirs[name] = Path(data_dir)
        cleaner = self.cleaners.get(name)
        if cleaner is None:
            whitelist_path = self.base_dir / "whitelist.txt"
            whitelist = []
            if whitelist_path.is_file():
                try:
                    whitelist = [x.strip() for x in whitelist_path.read_text(encoding="utf-8").splitlines() if x.strip()]
                except OSError:
                    log.warning("Cannot read cleaner whitelist at %s", whitelist_path)
            self.cleaners[name] = SmartCleaner(data_dir, whitelist=whitelist)

    def _track_profile_task(self, name: str, task: asyncio.Task) -> None:
        self.profile_tasks.setdefault(name, set()).add(task)
        def finished(done_task):
            tasks = self.profile_tasks.get(name)
            if tasks is not None:
                tasks.discard(done_task)
                if not tasks:
                    self.profile_tasks.pop(name, None)
        task.add_done_callback(finished)

    def unregister_profile(self, name: str) -> None:
        self.clients.pop(name, None)
        self.configs.pop(name, None)
        self.data_dirs.pop(name, None)
        # Stop profile-bound work before its live Telethon client is disconnected.
        pending = list(self.profile_tasks.get(name, set()))
        for task in pending:
            if not task.done():
                task.cancel()
        cleaner = self.cleaners.get(name)
        if cleaner and not self.runner and not pending:
            self.cleaners.pop(name, None)
            cleaner.close()
        elif pending:
            async def close_when_idle():
                await asyncio.gather(*pending, return_exceptions=True)
                if name not in self.clients and not self.profile_tasks.get(name):
                    current = self.cleaners.pop(name, None)
                    if current:
                        current.close()
            try:
                asyncio.get_running_loop().create_task(close_when_idle())
            except RuntimeError:
                pass

    def _get_profile(self, name: str) -> tuple[Any, SmartCleaner]:
        if not PROFILE_RE.fullmatch(name) or name not in self.clients:
            raise web.HTTPServiceUnavailable(text="Профиль не запущен в CIRCLES CONTROL")
        return self.clients[name], self.cleaners[name]

    def _token(self) -> str:
        env_token = os.getenv("CIRCLES_WEB_TOKEN", "").strip()
        if env_token:
            return env_token
        path = self.base_dir / ".telegram_manager_token"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(36)
        path.write_text(value + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path == "/" or request.path == "/health":
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + self.token
        if not secrets.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(text="Требуется Bearer token")
        return await handler(request)

    async def start(self) -> str:
        if self.runner:
            return self.token
        try:
            import aiohttp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Для Web Manager установите aiohttp: pip install 'aiohttp>=3.9,<4'") from exc
        self.host = os.getenv("CIRCLES_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("CIRCLES_WEB_ALLOW_REMOTE", "") == "1":
            raise RuntimeError("По умолчанию Web Manager доступен только на localhost. Для внешнего proxy настройте CIRCLES_WEB_ALLOW_REMOTE=1 и TLS/auth на proxy.")
        self.port = int(os.getenv("CIRCLES_WEB_PORT", "8765"))
        self.token = self._token()
        app = web.Application(middlewares=[self.auth_middleware], client_max_size=64 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/profiles", self.profiles)
        app.router.add_post("/api/profiles/{name}/start", self.start_profile)
        app.router.add_post("/api/profiles/{name}/stop", self.stop_profile)
        app.router.add_post("/api/profiles/{name}/scan", self.start_scan)
        app.router.add_get("/api/scans/{scan_id}", self.scan_status)
        app.router.add_get("/api/profiles/{name}/dialogs", self.dialogs)
        app.router.add_get("/api/profiles/{name}/summary", self.summary)
        app.router.add_post("/api/profiles/{name}/leave", self.leave_one)
        app.router.add_post("/api/profiles/{name}/preview", self.preview)
        app.router.add_post("/api/profiles/{name}/execute", self.execute)
        app.router.add_post("/api/profiles/{name}/resume", self.resume)
        app.router.add_get("/api/profiles/{name}/jobs/{job_id}", self.job_status)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        log.info("Telegram Manager Web UI: http://%s:%s/ (token file: %s)", self.host, self.port, self.base_dir / '.telegram_manager_token')
        return self.token

    async def stop(self) -> None:
        pending = list(self.scan_tasks.values()) + list(self.job_tasks.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        self.runner = None
        self.site = None
        for name in list(self.cleaners):
            cleaner = self.cleaners.pop(name)
            cleaner.close()
        self.clients.clear(); self.configs.clear(); self.data_dirs.clear(); self.profile_tasks.clear()
        self.account_manager = None

    async def index(self, request):
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8")

    async def health(self, request):
        return web.json_response({"ok": True})

    async def profiles(self, request):
        items = []
        if self.profiles_provider:
            try:
                profiles = self.profiles_provider()
            except Exception:
                profiles = []
            for name, cfg, ddir in profiles:
                connected = name in self.clients
                running = False
                status = "Подключён" if connected else "Сессия сохранена · не запущен"
                if self.account_manager is not None:
                    try:
                        status = self.account_manager.status(name)
                        running = self.account_manager.is_running(name)
                    except Exception:
                        pass
                items.append({"name": name, "label": cfg.get("account_label") or name,
                              "status": status, "active": connected, "running": running,
                              "can_start": bool(self.account_manager and name != "default" and not running
                                                and not connected and self.account_manager.has_session(name)),
                              "can_stop": bool(self.account_manager and name != "default" and running)})
        else:
            items = [{"name": n, "label": self.configs[n].get("account_label") or n,
                      "status": "Подключён", "active": True, "can_start": False, "can_stop": False}
                     for n in self.clients]
        return web.json_response({"profiles": items})

    async def start_profile(self, request):
        name = request.match_info["name"]
        if not PROFILE_RE.fullmatch(name) or self.account_manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен (нужна основная bot-панель CIRCLES CONTROL)")
        ok, message = await self.account_manager.start_profile(name)
        if not ok:
            raise web.HTTPConflict(text=message)
        return web.json_response({"ok": True, "message": message})

    async def stop_profile(self, request):
        name = request.match_info["name"]
        if not PROFILE_RE.fullmatch(name) or self.account_manager is None:
            raise web.HTTPServiceUnavailable(text="Менеджер аккаунтов недоступен")
        ok, message = await self.account_manager.stop_profile(name)
        if not ok:
            raise web.HTTPConflict(text=message)
        return web.json_response({"ok": True, "message": message})

    async def start_scan(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        scan_id = secrets.token_urlsafe(12)
        async def work():
            try:
                self.scan_results[scan_id] = {"status": "running"}
                result = await cleaner.scan(client)
                self.scan_results[scan_id] = {"status": "completed", "result": result}
            except asyncio.CancelledError:
                self.scan_results[scan_id] = {"status": "cancelled"}
                raise
            except Exception as exc:
                log.exception("Smart Cleaner scan failed for profile %s", name)
                self.scan_results[scan_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        task = asyncio.create_task(work(), name=f"smart-cleaner-scan:{name}")
        self.scan_tasks[scan_id] = task
        self._track_profile_task(name, task)
        return web.json_response({"scan_id": scan_id, "status": "running"}, status=202)

    async def scan_status(self, request):
        scan_id = request.match_info["scan_id"]
        return web.json_response(self.scan_results.get(scan_id, {"status": "unknown"}))

    async def dialogs(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        try:
            peer_type = request.query.get("peer_type") or None
            if peer_type not in {None, "group", "channel"}:
                raise ValueError("Неизвестный тип чата")
            result = cleaner.list_catalog(category=request.query.get("category") or None,
                                          query=request.query.get("query", ""),
                                          peer_type=peer_type,
                                          include_protected=True,
                                          limit=int(request.query.get("limit", 40)),
                                          offset=int(request.query.get("offset", 0)))
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def summary(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        return web.json_response(cleaner.catalog_stats())

    async def leave_one(self, request):
        """Leave exactly one explicitly selected chat after browser confirmation.

        This route still creates an immutable one-target preview and executes it
        through SmartCleaner, preserving its live ownership/admin/whitelist checks,
        one-operation lock, journaling, and Telegram flood-stop behavior.
        """
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Ожидается JSON-объект с chat_id")
            raw_id = payload.get("chat_id")
            if isinstance(raw_id, bool):
                raise ValueError("Некорректный ID чата")
            if isinstance(raw_id, int):
                chat_id = raw_id
            elif isinstance(raw_id, str) and __import__("re").fullmatch(r"-?\d+", raw_id.strip()):
                chat_id = int(raw_id.strip())
            else:
                raise ValueError("ID чата должен быть целым числом")
            if chat_id == 0:
                raise ValueError("Некорректный ID чата")
            preview = cleaner.preview(category="single", chat_ids=[chat_id], allow_scam=False)
            if preview.get("count") != 1 or len(preview.get("targets", [])) != 1:
                raise ValueError("Чат не найден или не разрешён для выхода")
            title = str(preview["targets"][0].get("title") or chat_id)
            preview_id = str(preview["preview_id"])
            task_id = secrets.token_urlsafe(12)

            async def work():
                self.scan_results[task_id] = {"status": "running", "chat_id": chat_id, "title": title}
                try:
                    result = await cleaner.execute(client, preview_id=preview_id,
                                                   confirmation="LEAVE 1", delay_seconds=2,
                                                   max_actions=1)
                    self.scan_results[task_id] = {"status": "completed", "result": result,
                                                  "chat_id": chat_id, "title": title}
                except asyncio.CancelledError:
                    self.scan_results[task_id] = {"status": "cancelled", "chat_id": chat_id, "title": title}
                    raise
                except Exception as exc:  # noqa: BLE001
                    saved = cleaner.job(preview_id)
                    if saved and saved["status"] == "paused":
                        processed = len(saved["done_ids"]) + len(saved["failed_items"])
                        remaining = max(0, len(saved["target_ids"]) - processed)
                        result = {"job_id": preview_id, "status": "paused",
                                  "completed": len(saved["done_ids"]), "failed": len(saved["failed_items"]),
                                  "remaining": remaining,
                                  "stop_reason": saved.get("last_error") or f"{type(exc).__name__}: {exc}"}
                        self.scan_results[task_id] = {"status": "completed", "result": result,
                                                      "chat_id": chat_id, "title": title}
                    else:
                        self.scan_results[task_id] = {"status": "failed",
                                                      "error": f"{type(exc).__name__}: {exc}",
                                                      "chat_id": chat_id, "title": title}

            task = asyncio.create_task(work(), name=f"smart-cleaner-leave-one:{name}:{chat_id}")
            self.job_tasks[task_id] = task
            self._track_profile_task(name, task)
            return web.json_response({"job_id": task_id, "status": "running", "chat_id": chat_id,
                                      "title": title}, status=202)
        except (ValueError, TypeError, KeyError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def preview(self, request):
        _, cleaner = self._get_profile(request.match_info["name"])
        try:
            payload = await request.json()
            result = cleaner.preview(category=str(payload.get("category", "all")),
                                     chat_ids=payload.get("chat_ids"),
                                     allow_scam=bool(payload.get("allow_scam", False)))
            return web.json_response(result)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def execute(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            preview_id = str(payload.get("preview_id", ""))
            confirmation = str(payload.get("confirmation", ""))
            pending = cleaner.job(preview_id)
            if not pending or pending["status"] != "preview":
                raise ValueError("Предпросмотр не найден, уже использован или истёк")
            expected = f"LEAVE {len(pending['target_ids'])}"
            if not secrets.compare_digest(confirmation.strip(), expected):
                raise ValueError(f"Для подтверждения введи точно: {expected}")
            # Consume the preview in a background task so the web UI stays responsive.
            task_id = secrets.token_urlsafe(12)
            async def work():
                try:
                    self.scan_results[task_id] = {"status": "running"}
                    result = await cleaner.execute(client, preview_id=preview_id, confirmation=confirmation)
                    self.scan_results[task_id] = {"status": "completed", "result": result}
                except Exception as exc:
                    saved = cleaner.job(preview_id)
                    if saved and saved["status"] == "paused":
                        processed = len(saved["done_ids"]) + len(saved["failed_items"])
                        remaining = max(0, len(saved["target_ids"]) - processed)
                        self.scan_results[task_id] = {"status": "completed", "result": {
                            "job_id": preview_id, "status": "paused", "completed": len(saved["done_ids"]),
                            "failed": len(saved["failed_items"]), "remaining": remaining,
                            "stop_reason": saved.get("last_error") or f"{type(exc).__name__}: {exc}"}}
                    else:
                        self.scan_results[task_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            task = asyncio.create_task(work(), name=f"smart-cleaner-job:{name}")
            self.job_tasks[task_id] = task
            self._track_profile_task(name, task)
            return web.json_response({"job_id": task_id, "status": "running"}, status=202)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def job_status(self, request):
        name = request.match_info["name"]
        _, cleaner = self._get_profile(name)
        job_id = request.match_info["job_id"]
        task_result = self.scan_results.get(job_id)
        if task_result:
            return web.json_response(task_result)
        if job_id in self.job_tasks and not self.job_tasks[job_id].done():
            return web.json_response({"status": "running"})
        job = cleaner.job(job_id)
        if not job:
            raise web.HTTPNotFound(text="Задача не найдена")
        return web.json_response(job)

    async def resume(self, request):
        name = request.match_info["name"]
        client, cleaner = self._get_profile(name)
        try:
            payload = await request.json()
            preview = cleaner.resume(str(payload.get("job_id", "")))
            return web.json_response(preview)
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))